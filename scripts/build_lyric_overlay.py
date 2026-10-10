#!/usr/bin/env python3
"""Package accepted song lyrics into the release, not just into the library.

The lyric library (``lyrics/songs/*.jsonl``) is *source material*: the game
client never reads it, so a translated song stayed Japanese on the phone even
though the repository had the Chinese.  This step closes that gap for every
release, automatically, the same way the text surface already works:

1. every song with at least one ``accepted`` Chinese line is matched against the
   official catalogue (``scrobj_*.unity3d``);
2. its official bundle is fetched (through the same content-addressed official
   cache the text path uses) and rewritten in place with the Chinese lines;
3. the patched bundle is written into the **same overlay directory** the text pass
   produces, keyed by the official remote name, which is the name the client
   requests;
4. rows are appended to the overlay's ``localization-manifest.json``, so
   ``build_generated_release.py`` publishes lyric bundles exactly like text
   bundles: same ``runtime_path``, same content-addressed store, same manifest.

Nothing here is optional at release time: the release builder calls it on every
build, so a new official version picks up new lyrics without a human step.
Guards: a hard cap on how many songs and bytes one run may package (fail closed
before anything is downloaded), and every patched bundle is read back and
compared slot by slot before it is allowed into the overlay.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "pipelines" / "text") not in sys.path:
    sys.path.insert(0, str(ROOT / "pipelines" / "text"))

from mltd_localize_scrobj import (  # noqa: E402
    SlotTranslations,
    read_song,
    save_localized_bundle,
    slot_translations,
)

DEFAULT_MAX_BUNDLES = 600
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
SCOPE = "jp-android"


def song_bundles(lyrics_root: Path) -> list[str]:
    """Every lyric bundle name the library carries, sorted."""
    songs = Path(lyrics_root) / "songs"
    if not songs.is_dir():
        return []
    return sorted(path.name[: -len(".jsonl")] for path in songs.glob("*.jsonl"))


def collect_translations(lyrics_root: Path, bundle: str) -> SlotTranslations:
    return slot_translations(read_song(Path(lyrics_root), bundle))


def _resolve_catalogue_entry(index: dict[str, dict], bundle: str) -> dict | None:
    if bundle in index:
        return index[bundle]
    folded = bundle.casefold()
    for name, row in index.items():
        if name.casefold() == folded:
            return row
    return None


def pending_songs(*, index: dict[str, dict], lyrics_root: Path,
                  max_bundles: int = DEFAULT_MAX_BUNDLES,
                  max_bytes: int = DEFAULT_MAX_BYTES
                  ) -> tuple[list[tuple[str, dict, SlotTranslations]], dict]:
    """Every song the library can patch, with the catalogue row for each.

    Reading the library is cheap (a few hundred small files) and downloading the
    official bundles is not, so this is the step the caller can afford to run
    before deciding what actually needs rewriting -- see ``run``'s ``targets``.
    """
    lyrics_root = Path(lyrics_root)
    summary = {
        "songs_with_translation": 0,
        "songs_without_translation": 0,
        "songs_absent_from_catalogue": [],
        "declared_bytes": 0,
    }
    pending: list[tuple[str, dict, SlotTranslations]] = []
    for bundle in song_bundles(lyrics_root):
        translations = collect_translations(lyrics_root, bundle)
        if not translations:
            summary["songs_without_translation"] += 1
            continue
        summary["songs_with_translation"] += 1
        row = _resolve_catalogue_entry(index, bundle)
        if row is None:
            summary["songs_absent_from_catalogue"].append(bundle)
            continue
        summary["declared_bytes"] += int(row.get("declared_size") or 0)
        pending.append((bundle, row, translations))

    if max_bundles and len(pending) > max_bundles:
        raise ValueError(
            f"refusing to package {len(pending)} lyric bundles (cap {max_bundles}); "
            "raise --max-bundles deliberately if the library really grew that much"
        )
    if max_bytes and summary["declared_bytes"] > max_bytes:
        raise ValueError(
            f"refusing to package {summary['declared_bytes']} bytes of lyrics "
            f"(cap {max_bytes}); raise --max-bytes deliberately"
        )
    return pending, summary


def run(
    *,
    index: dict[str, dict],
    archive_root: Path,
    overlay_root: Path,
    lyrics_root: Path,
    asset_version: str,
    upstream_root: str,
    downloader,
    cache_root: Path | None = None,
    scope: str = SCOPE,
    max_bundles: int = DEFAULT_MAX_BUNDLES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    progress_every: int = 0,
    targets: Iterable[str] | None = None,
) -> dict:
    """Patch every translatable song into ``overlay_root``; append its manifest rows.

    ``downloader`` is the release builder's own ``download`` function, so lyric
    objects are fetched and cached by exactly the same rules (declared size is
    verified, the content fingerprint drives cross-version reuse).

    ``targets`` narrows that to the songs whose own lyrics changed; anything left
    out is neither downloaded nor rewritten, and the caller is the one that knows
    its bytes are still the ones the previous release published.  ``None`` means
    every song, which is what a full build passes.
    """
    lyrics_root = Path(lyrics_root)
    overlay_root = Path(overlay_root)
    archive_root = Path(archive_root)
    pending, summary = pending_songs(index=index, lyrics_root=lyrics_root,
                                     max_bundles=max_bundles, max_bytes=max_bytes)
    summary.update({
        "bundles_patched": 0,
        "slots_patched": 0,
        "songs_without_matching_lines": [],
        "accepted_rows_not_applied": 0,
        "written_bytes": 0,
    })
    if targets is not None:
        wanted = {str(name) for name in targets}
        selected = [item for item in pending if item[0] in wanted]
        summary["songs_reused"] = len(pending) - len(selected)
        pending = selected
    prepared: list[dict] = []

    # Fetch in parallel: these objects are small (~100 KB), so the cost is one
    # connection per file and the release builder already fetches its text
    # objects the same way.  ``download`` tolerates concurrent callers (it writes
    # a ``.part`` file and replaces it atomically, and the shared cache copy
    # tolerates a racing writer), and every size/fingerprint check still runs on
    # every object.
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            futures = [
                pool.submit(
                    downloader,
                    f"{upstream_root}/{row['remote']}",
                    archive_root / scope / str(row["remote"]),
                    int(row.get("declared_size") or 0) or None,
                    row.get("catalog_hash"),
                    cache_root,
                )
                for _bundle, row, _translations in pending
            ]
            for future in futures:
                future.result()

    for position, (bundle, row, translations) in enumerate(pending, start=1):
        remote = str(row["remote"])
        source = archive_root / scope / remote
        output = overlay_root / scope / remote
        manifest = save_localized_bundle(source, output, translations)
        if not manifest["changed"]:
            # Upstream rewrote this song's lines; nothing may be overwritten, and
            # the skipped rows must still be counted or the report would claim
            # every accepted line made it into the release.
            summary["songs_without_matching_lines"].append(bundle)
            summary["accepted_rows_not_applied"] += len(translations)
            continue
        summary["bundles_patched"] += 1
        summary["slots_patched"] += int(manifest["changed"])
        summary["written_bytes"] += int(manifest["output_bytes"])
        summary["accepted_rows_not_applied"] += len(manifest["unmatched_texts"])
        prepared.append(
            {
                "logical": bundle,
                "remote": remote,
                "changed": int(manifest["changed"]),
                "exact": int(manifest["changed"]),
                "memory": 0,
                "slots_total": int(manifest["slots_total"]),
                "scope": "lyrics",
                **manifest,
            }
        )
        if progress_every and position % progress_every == 0:
            print(
                f"lyric overlay {position}/{len(pending)} patched={summary['bundles_patched']} "
                f"slots={summary['slots_patched']}",
                file=sys.stderr,
                flush=True,
            )

    append_manifest(overlay_root / "localization-manifest.json", prepared, summary, asset_version)
    summary["patched_bundles"] = [row["logical"] for row in prepared]
    return summary


def append_manifest(
    manifest_path: Path, rows: list[dict], summary: dict, asset_version: str
) -> None:
    """Add the lyric rows to the overlay manifest the release builder reads.

    The text pass owns the file; this is an append so both surfaces ship from one
    manifest and therefore one catalogue and one store write.
    """
    manifest_path = Path(manifest_path)
    if manifest_path.is_file():
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        document = {"schema_version": 1, "bundles": []}
    existing = {str(row.get("logical")) for row in document.get("bundles", [])}
    for row in rows:
        if str(row.get("logical")) in existing:
            raise ValueError(f"lyric overlay would duplicate manifest row: {row.get('logical')}")
        document.setdefault("bundles", []).append(row)
    document["lyrics"] = {
        "asset_version": str(asset_version),
        "bundles": len(rows),
        "slots_patched": int(summary["slots_patched"]),
        "songs_with_translation": int(summary["songs_with_translation"]),
        "songs_without_translation": int(summary["songs_without_translation"]),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-manifest", type=Path, default=ROOT / "manifests/asset-version.json")
    parser.add_argument("--work-root", type=Path, default=ROOT / ".generated-work")
    parser.add_argument("--overlay-root", type=Path, default=None,
                        help="overlay produced by the text pass (default <work-root>/overlay)")
    parser.add_argument("--lyrics-root", type=Path, default=ROOT / "lyrics")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--official-cache", type=Path, default=None)
    parser.add_argument("--max-bundles", type=int, default=DEFAULT_MAX_BUNDLES,
                        help="fail closed above this many songs in one run")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="fail closed above this many declared bytes in one run")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be packaged without downloading or writing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import build_generated_release as release  # lazy: it imports this module

    version = release.load_version_manifest(args.version_manifest)
    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=True)
    index_path = work / version["index_name"]
    release.download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    index = release.load_official_index(index_path)
    overlay = (args.overlay_root or work / "overlay").resolve()
    cache = args.official_cache.resolve() if args.official_cache else None
    if args.dry_run:
        translations = {
            bundle: collect_translations(args.lyrics_root, bundle)
            for bundle in song_bundles(args.lyrics_root)
        }
        with_translation = [name for name, rows in translations.items() if rows]
        declared = 0
        missing = []
        for name in with_translation:
            row = _resolve_catalogue_entry(index, name)
            if row is None:
                missing.append(name)
                continue
            declared += int(row.get("declared_size") or 0)
        report = {
            "dry_run": True,
            "asset_version": str(version["asset_version"]),
            "songs_with_translation": len(with_translation),
            "songs_absent_from_catalogue": missing,
            "declared_bytes": declared,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    summary = run(
        index=index,
        archive_root=work / "archive",
        overlay_root=overlay,
        lyrics_root=args.lyrics_root,
        asset_version=str(version["asset_version"]),
        upstream_root=str(version["asset_root"]),
        downloader=release.download,
        cache_root=cache,
        progress_every=100,
    )
    printable = {key: value for key, value in summary.items() if key != "patched_bundles"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
