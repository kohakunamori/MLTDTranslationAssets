#!/usr/bin/env python3
"""Extract official song lyrics into ``lyrics/`` for every new/changed song.

Why this exists
---------------
``lyrics/`` was produced once, offline, from a frozen asset snapshot (asset
1077100, run 2026-09-24) and then never touched again: no workflow read it, no
workflow wrote it, and the bundle memo could not see ``scrobj_*`` at all (0 of
its 11,816 entries were lyric bundles).  The consequence at asset 1077720 was 60
songs whose lyrics simply did not exist in the repository -- including
``scrobj_ittana`` (the song "一旦愛して"), the new event song.  New songs could never enter
the translation queue because nothing ever looked for them.

This command is the lyric half of "look outward".  It selects every ``scrobj_*``
bundle of the current official index that the repository does not carry yet,
downloads it, extracts its lyric lines and writes
``lyrics/songs/<bundle>.jsonl`` with ``status=untranslated`` rows, then rebuilds
``lyrics/all_lyrics.jsonl`` and ``lyrics/lyrics_manifest.json`` from the data.

Two properties matter for safety:

* **Nothing is ever silently overwritten.**  A song whose upstream bundle is
  unchanged is skipped without downloading.  A song whose bundle *did* change is
  re-extracted through ``merge_slots``: a line keeps its translation when its
  Japanese text is unchanged (re-keyed if only its position moved) and becomes
  untranslated when the source text itself changed.  Losing an accepted
  translation is counted and reported, never hidden.
* **The batch is bounded.**  ``--max-new-bundles`` / ``--max-new-bytes`` fail
  closed with exit code 2, exactly like ``discover_official_bundles.py``, so an
  upstream restructuring cannot silently pull in a gigabyte of bundles.

Only reading happens here: these rows are the repository's translation source.
Writing a localized lyric bundle back into a mountable in-game overlay is a
separate, deliberately gated step (see the bilingual candidate run) and is not
performed by this command.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "pipelines" / "text")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from build_generated_release import load_official_index, load_version_manifest  # noqa: E402
from mltd_localize_scrobj import (  # noqa: E402
    LyricBundleError,
    merge_slots,
    read_slots,
    read_song,
    rebuild_aggregate,
    slot_rows,
    song_names,
    write_song,
)
from official_bundle_families import (  # noqa: E402
    DEFAULT_REGISTRY,
    classify,
    families_for_pipeline,
    load_registry,
    logical_bundle_name,
)

PIPELINE = "song_lyrics"

DEFAULT_MAX_NEW_BUNDLES = 200
DEFAULT_MAX_NEW_BYTES = 512 * 1024 * 1024

#: Memo of the content-addressed remote each song was extracted from.  It exists
#: so a daily run downloads only songs whose upstream object actually changed.
DEFAULT_SOURCE_MEMO = ROOT / "lyrics" / "source-bundle-index.json"


def load_source_memo(path: Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    bundles = document.get("bundles") if isinstance(document, dict) else None
    if not isinstance(bundles, dict):
        return {}
    return {str(key): str(value) for key, value in bundles.items()}


def write_source_memo(path: Path, bundles: dict[str, str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "kind": "mltd-lyrics-source-memo",
        "note": "每首歌是从哪个内容寻址的官方对象抽出来的；remote 变了才需要重新抽取与合并。",
        "bundles": dict(sorted(bundles.items())),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def select_songs(index: dict, registry: dict, present: set[str]) -> tuple[list[str], list[str]]:
    """(songs missing from the repository, songs already present)."""
    allowed = families_for_pipeline(registry, PIPELINE)
    missing: list[str] = []
    known: list[str] = []
    for logical in index:
        name = logical_bundle_name(logical)
        if classify(registry, name) not in allowed:
            continue
        (known if name in present else missing).append(name)
    return sorted(missing), sorted(known)


def changed_songs(index: dict, memo: dict[str, str]) -> list[str]:
    """Songs already extracted whose upstream object is no longer the same."""
    changed: list[str] = []
    for logical, row in index.items():
        name = logical_bundle_name(logical)
        recorded = memo.get(name)
        if recorded is None:
            continue
        if recorded != str(row.get("remote", "")):
            changed.append(name)
    return sorted(changed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=ROOT / ".llm-official-work")
    parser.add_argument("--index", type=Path, default=None,
                        help="official msgpack index (defaults to <work-root>/<index_name>)")
    parser.add_argument("--version-manifest", type=Path, default=ROOT / "manifests" / "asset-version.json")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--lyrics-root", type=Path, default=ROOT / "lyrics")
    parser.add_argument("--source-memo", type=Path, default=DEFAULT_SOURCE_MEMO)
    parser.add_argument("--max-new-bundles", type=int, default=DEFAULT_MAX_NEW_BUNDLES)
    parser.add_argument("--max-new-bytes", type=int, default=DEFAULT_MAX_NEW_BYTES)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be extracted without downloading or writing")
    parser.add_argument("--no-aggregate", action="store_true",
                        help="skip rewriting all_lyrics.jsonl / lyrics_manifest.json")
    args = parser.parse_args()

    version = load_version_manifest(args.version_manifest)
    if args.index:
        index_path = Path(args.index)
    else:
        index_path = Path(args.work_root) / version["index_name"]
    if not index_path.is_file():
        raise SystemExit(f"official index not found: {index_path} (run the refresh first)")
    index = load_official_index(index_path)
    registry = load_registry(args.registry)

    present = set(song_names(args.lyrics_root))
    missing, known = select_songs(index, registry, present)
    memo = load_source_memo(args.source_memo)
    # A missing memo must not force a full re-download of every song: record the
    # current remotes and treat what is already extracted as current.  From then
    # on, only genuinely changed objects are re-extracted.
    first_run = not memo
    if first_run:
        for name in known:
            logical = name
            row = index.get(logical)
            if row is not None:
                memo[logical] = str(row.get("remote", ""))
    changed = [] if first_run else [name for name in changed_songs(index, memo)]

    total_bytes = sum(int(index[name].get("declared_size", 0) or 0) for name in missing if name in index)
    refused = None
    if args.max_new_bundles and len(missing) > args.max_new_bundles:
        refused = f"{len(missing)} new lyric bundles exceed the cap of {args.max_new_bundles}"
    elif args.max_new_bytes and total_bytes > args.max_new_bytes:
        refused = f"{total_bytes} new lyric bytes exceed the cap of {args.max_new_bytes}"

    report = {
        "asset_version": version["asset_version"],
        "official_lyric_bundles": len(missing) + len(known),
        "present_songs": len(present),
        "new_songs": missing,
        "new_songs_bytes": total_bytes,
        "changed_songs": changed,
        "refused": refused,
        "extracted": [],
        "empty": [],
        "merged": [],
        "failures": [],
        "dry_run": bool(args.dry_run),
    }
    if refused:
        print(json.dumps(report, ensure_ascii=False))
        print(f"refused: {refused}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False))
        return 0

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    archive = Path(args.work_root) / "archive" / "jp-android"
    to_process = [(name, False) for name in missing] + [(name, True) for name in changed]
    for name, is_update in to_process:
        row = index.get(name)
        if row is None:
            report["failures"].append({"bundle": name, "error": "not in the official index"})
            continue
        destination = archive / str(row.get("remote", ""))
        try:
            from build_generated_release import download  # imported late: tests patch the index only

            if not destination.is_file():
                download(f"{version['asset_root']}/{row['remote']}", destination, row.get("declared_size"))
            slots = read_slots(destination)
        except (LyricBundleError, OSError, ValueError) as exc:
            report["failures"].append({"bundle": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if is_update:
            rows, stats = merge_slots(name, read_song(args.lyrics_root, name), slots, now)
            report["merged"].append({"bundle": name, "rows": len(rows), **stats})
        else:
            rows = slot_rows(name, slots, now)
            if not rows:
                # A developer/test bundle can carry text but no lyric line.  An
                # empty song file would still add a 0-slot entry to the manifest
                # and a permanent file to review, so it is reported instead.
                report["empty"].append(name)
                memo[name] = str(row.get("remote", ""))
                continue
            report["extracted"].append({"bundle": name, "rows": len(rows)})
        write_song(args.lyrics_root, name, rows)
        memo[name] = str(row.get("remote", ""))

    write_source_memo(args.source_memo, memo)
    if not args.no_aggregate:
        manifest = rebuild_aggregate(args.lyrics_root)
        report["counts"] = manifest["counts"]

    print(json.dumps(report, ensure_ascii=False))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
