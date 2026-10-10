#!/usr/bin/env python3
"""Extract the latest official text bundles and append new source rows.

Selection is name-pattern based, not "what do we already have".  Until
2026-10-09 this job could only re-verify the bundles already represented in
``locales/``: a bundle the repository had never seen could never be selected, so
a new event's story text was invisible to every automated step (77 text bundles
were already missing at asset 1077720).  A logical bundle is now downloaded when
it is either (a) already represented here, or (b) a new name inside a
``gtx_text`` family declared in ``manifests/localizable-bundle-families.json``.

The scheduled job stays bounded by caps, not by ignorance: the new-row cap below
fails closed, ``discover_official_bundles.py`` refuses an oversized discovery
batch, and families bound to another pipeline (song lyrics) are excluded so a
bundle never reaches an extractor that cannot read it.  The official archive
remains temporary; only source text with ``untranslated`` status is committed.

Downloads are incremental.  An official ``remote`` object name is
content-addressed, so ``manifests/official-bundle-index.json`` memoises the
remotes a *committed* run already verified: a daily run re-downloads only the
bundles whose upstream object actually changed instead of all ~11.8k of them.
The memo is rewritten in the same run that appends the rows it verified, and the
workflow commits both together — a run that dies before publishing leaves the
memo untouched and the next run re-verifies what it lost.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_generated_release import download, load_official_index, load_version_manifest
from official_bundle_families import (
    DEFAULT_REGISTRY,
    classify,
    families_for_pipeline,
    load_registry,
    logical_bundle_name,
)

#: Refuse an automatic append larger than this. A version bump that genuinely
#: adds thousands of lines needs an explicit --max-new-rows decision.
DEFAULT_MAX_NEW_ROWS = 5000

#: The extractor this command runs.  Only families bound to it may be selected:
#: a lyric bundle handed to the GTX reader fails with "expected exactly one
#: TextAsset", so the split is enforced by the registry rather than by hope.
EXTRACTOR_PIPELINE = "gtx_text"

#: Verified-remote memo. ``--full-rescan`` ignores it for one run.
BUNDLE_INDEX_SCHEMA_VERSION = 1
DEFAULT_BUNDLE_INDEX = ROOT / "manifests" / "official-bundle-index.json"


def locale_rows():
    for path in sorted((ROOT / "locales").rglob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield path, json.loads(line)


def source_identity(bundle: str, item_key: str, source_sha256: str) -> tuple[str, str, str]:
    """Identity of a source line, independent of the asset version.

    An official source that the repository already represents in *any* asset
    version is not new: the version bump alone must not make the whole
    catalogue "new". Including ``asset_version`` here (the previous behaviour)
    turned every upstream version step into an append of the full catalogue —
    ~393k rows and an unbounded translation queue on 2026-10-01.
    """
    return (bundle, item_key, source_sha256)


def select_bundles(
    index: dict, known_bundles: set[str], registry: dict
) -> tuple[dict, list[str]]:
    """Split the official index into (bundles to download, newly discovered).

    ``known_bundles`` are the names this repository already carries rows for.
    Everything else must match a family bound to this command's extractor;
    names that match nothing stay untouched so a new resource type is reported
    (by ``discover_official_bundles.py``) instead of being fetched blindly.
    """
    allowed = families_for_pipeline(registry, EXTRACTOR_PIPELINE)
    selected: dict = {}
    discovered: list[str] = []
    for logical, row in index.items():
        if logical.casefold() in known_bundles:
            selected[logical] = row
            continue
        if classify(registry, logical) in allowed:
            selected[logical] = row
            discovered.append(logical)
    return selected, sorted(discovered)


def collect_new_rows(catalogue_rows, existing, asset_version: str, client_version: str, now: str):
    """Rows for source lines this repository does not already carry."""
    seen = set(existing)
    additions = []
    for row in catalogue_rows:
        bundle = str(row.get("bundle", ""))
        key = str(row.get("key", ""))
        source = str(row.get("source", ""))
        sid = str(row.get("source_sha256", ""))
        identity = source_identity(bundle, key, sid)
        if not source or not key or not bundle or identity in seen:
            continue
        seen.add(identity)
        additions.append({
            "asset_version": asset_version,
            "client_version": None,
            "source_client_version": client_version,
            "bundle": bundle, "item_key": key, "source_sha256": sid,
            "ja": source, "zh": "", "status": "untranslated",
            "translation_stage": "untranslated", "updated_at": now,
        })
    return additions


def enforce_new_row_cap(additions, cap: int) -> None:
    """Fail closed when a refresh would append more than ``cap`` rows.

    A large append is either an upstream restructuring or the dedupe identity
    regressing; both need a human decision, not an automatic commit.
    """
    if cap > 0 and len(additions) > cap:
        raise SystemExit(
            f"refusing to append {len(additions)} new rows (cap {cap}); "
            "re-run with an explicit --max-new-rows to accept a large append"
        )


def load_bundle_index(path: Path) -> dict[str, str]:
    """Read the verified ``logical -> remote`` memo.

    A missing, unreadable or unknown-schema memo only costs downloads, never
    correctness: it degrades to a full verification instead of failing the run.
    """
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: ignoring unreadable bundle index {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(document, dict) or document.get("schema_version") != BUNDLE_INDEX_SCHEMA_VERSION:
        print(f"warning: ignoring bundle index {path} with unknown schema", file=sys.stderr)
        return {}
    bundles = document.get("bundles")
    if not isinstance(bundles, dict):
        return {}
    return {
        logical: remote
        for logical, remote in bundles.items()
        if isinstance(logical, str) and isinstance(remote, str) and remote.endswith(".unity3d")
    }


def plan_bundle_downloads(index: dict, verified: dict[str, str], full_rescan: bool = False):
    """Split the tracked official bundles into ``(download, reused)``.

    A bundle is reused when the memo records the same content-addressed remote:
    an identical object cannot carry source text the repository has not seen.
    """
    to_download: dict[str, dict] = {}
    reused: dict[str, dict] = {}
    for logical, row in index.items():
        if not full_rescan and verified.get(logical) == row["remote"]:
            reused[logical] = row
        else:
            to_download[logical] = row
    return to_download, reused


def next_bundle_index(index: dict, verified: dict[str, str], downloaded: dict) -> dict[str, str]:
    """Memo for the next run, containing only remotes that are still valid.

    ``downloaded`` are the bundles this run verified itself.  Everything else
    keeps its previous entry only while the official remote hash is unchanged —
    a bundle skipped by ``--max-bundles`` therefore stays memoised, while a
    changed or vanished remote is dropped and re-downloaded next run.
    """
    bundles: dict[str, str] = {}
    for logical, row in sorted(index.items()):
        remote = str(row["remote"])
        if logical in downloaded or verified.get(logical) == remote:
            bundles[logical] = remote
    return bundles


def write_bundle_index(path: Path, bundles: dict[str, str]) -> None:
    """Write the memo deterministically so an unchanged run produces no diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": BUNDLE_INDEX_SCHEMA_VERSION,
                "bundles": dict(sorted(bundles.items()))}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=ROOT / ".llm-official-work")
    parser.add_argument("--max-bundles", type=int, default=0)
    parser.add_argument("--max-new-rows", type=int, default=DEFAULT_MAX_NEW_ROWS)
    parser.add_argument("--bundle-index", type=Path, default=DEFAULT_BUNDLE_INDEX,
                        help="verified remote memo (logical -> content-addressed remote)")
    parser.add_argument("--families", type=Path, default=DEFAULT_REGISTRY,
                        help="localizable bundle family registry")
    parser.add_argument("--full-rescan", action="store_true",
                        help="ignore the memo and re-download every tracked bundle")
    args = parser.parse_args()

    version = load_version_manifest(ROOT / "manifests" / "asset-version.json")
    registry = load_registry(args.families)
    known_bundles = set()
    existing = set()
    for _path, row in locale_rows():
        bundle = str(row.get("bundle", "")).strip()
        if bundle:
            known_bundles.add(logical_bundle_name(bundle).casefold())
        existing.add(source_identity(bundle, str(row.get("item_key", "")), str(row.get("source_sha256", ""))))

    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=True)
    index_path = work / version["index_name"]
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    index = load_official_index(index_path)
    selected, discovered = select_bundles(index, known_bundles, registry)
    if args.max_bundles:
        selected = dict(sorted(selected.items())[:args.max_bundles])
    if not selected:
        raise SystemExit("no known text bundles matched the official index")

    verified = load_bundle_index(args.bundle_index)
    to_download, reused = plan_bundle_downloads(selected, verified, args.full_rescan)

    output = ROOT / "locales" / "master" / f"official-{version['asset_version']}-untranslated.jsonl"
    additions: list[dict] = []
    if to_download:
        archive = work / "archive"
        for row in to_download.values():
            destination = archive / "jp-android" / row["remote"]
            download(f"{version['asset_root']}/{row['remote']}", destination, row["declared_size"])
        snapshot = work / "snapshot.json"
        snapshot.write_text(json.dumps({
            "complete": True, "scope": "jp-android", "asset_index": str(index_path),
            "upstream_root": version["asset_root"],
            "objects": [{"logical": logical, **row} for logical, row in sorted(to_download.items())],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        catalogue = work / "catalogue.jsonl"
        subprocess.run([
            sys.executable, str(ROOT / "pipelines/text/mltd_localization_pipeline.py"),
            "extract-snapshot", "--snapshot", str(snapshot), "--archive-root", str(archive),
            "--output", str(catalogue), "--workers", "8",
        ], cwd=ROOT, check=True)

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        catalogue_rows = [
            json.loads(line)
            for line in catalogue.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        additions = collect_new_rows(catalogue_rows, existing,
                                     version["asset_version"], version["client_version"], now)
        enforce_new_row_cap(additions, args.max_new_rows)
        if additions:
            output.parent.mkdir(parents=True, exist_ok=True)
            # Append is idempotent once the identity ignores the asset version: a
            # re-run finds every previously appended row in ``existing`` and adds
            # nothing. Do not switch this to overwrite — the in-place LLM apply
            # step stores pending translations in this same file, and truncating
            # it would discard them.
            with output.open("a", encoding="utf-8", newline="\n") as stream:
                for row in additions:
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    bundles = next_bundle_index(index, verified, to_download)
    write_bundle_index(args.bundle_index, bundles)

    print(json.dumps({"asset_version": version["asset_version"],
                      "matched_bundles": len(selected),
                      "discovered_bundles": len(discovered),
                      "discovered_logical_names": discovered,
                      "downloaded_bundles": len(to_download),
                      "reused_bundles": len(reused),
                      "downloaded_bytes": sum(int(row["declared_size"]) for row in to_download.values()),
                      "new_rows": len(additions),
                      "output": str(output) if additions else None,
                      "bundle_index": str(args.bundle_index),
                      "bundle_index_entries": len(bundles)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
