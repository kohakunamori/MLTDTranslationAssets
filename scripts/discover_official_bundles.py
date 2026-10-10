#!/usr/bin/env python3
"""Report which official bundles are new, localizable, or an unknown surface.

Why this exists
---------------
Until 2026-10-09 the daily job could only ever re-verify the bundles the
repository already carried: ``refresh_latest_official_catalogue.py`` built its
download set from ``locales/**`` and intersected it with the official index, so
the tracked set could only describe itself (11,816 tracked against 168,391
official bundles).  A new event's story bundles and a new song's lyric bundles
were therefore never downloaded, never extracted and never translated -- 77 text
bundles and 60 lyric bundles were already missing at asset 1077720, including
``event_0448_story_*`` and ``scrobj_ittana`` (the song "一旦愛して").

This command is the missing "look outward" step.  It reads the official index
that the refresh already downloads and answers three questions:

1. which bundles match a declared *localizable family* but are not in the
   repository yet (the work the pipeline was silently dropping);
2. how large that work is, so a runaway append is refused instead of committed
   (``--max-new-bundles`` / ``--max-new-bytes``, fail closed with exit 2);
3. which **family signatures** are new since the last recorded inventory -- an
   entirely new resource type.  Those are reported, never downloaded by default:
   154,919 of the 168,391 official bundles are textures, audio and other
   non-text resources, and blindly fetching them would be neither translation
   nor review.

The inventory (``manifests/official-asset-inventory.json``) stores one count per
family signature instead of 168k names, so a version bump produces a small,
reviewable diff while still remembering which resource types have been seen.

Exit codes: 0 = ok, 2 = refused because a cap was exceeded, other = failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from build_generated_release import load_official_index, load_version_manifest  # noqa: E402
from official_bundle_families import (  # noqa: E402
    DEFAULT_REGISTRY,
    classify,
    family_signature,
    is_reviewed,
    load_registry,
    logical_bundle_name,
    registry_families,
    reviewed_unclassified,
)

SCHEMA_VERSION = 1
INVENTORY_SCHEMA_VERSION = 1

#: Refuse an automatic expansion larger than this.  A normal month adds a
#: handful of text bundles and a handful of songs; the 2026-10-01 failure mode
#: (identity regression re-appending the whole catalogue) is ~394k rows and
#: thousands of bundles.  400 bundles / 1 GiB sits far above real content and far
#: below that failure.
DEFAULT_MAX_NEW_BUNDLES = 400
DEFAULT_MAX_NEW_BYTES = 1024 * 1024 * 1024

DEFAULT_INVENTORY = ROOT / "manifests" / "official-asset-inventory.json"


def locale_bundle_names(root: Path) -> set[str]:
    """Every bundle the repository already carries a translation row for."""
    names: set[str] = set()
    locales = root / "locales"
    if not locales.is_dir():
        return names
    for path in sorted(locales.rglob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bundle = str(row.get("bundle", "")).strip()
                if bundle:
                    names.add(logical_bundle_name(bundle))
    return names


def lyrics_bundle_names(root: Path) -> set[str]:
    """Every lyric bundle the repository already carries slots for."""
    songs = root / "lyrics" / "songs"
    if not songs.is_dir():
        return set()
    return {
        logical_bundle_name(path.name[: -len(".jsonl")])
        for path in sorted(songs.glob("*.jsonl"))
    }


def memo_bundle_names(root: Path) -> set[str]:
    """Logical bundles the incremental downloader has already verified.

    This is the third source of "tracked", and it is not redundant: the official
    index really does contain a typo'd bundle (``pecial_108_fc_01_jp.gtx``) whose
    rows are byte-identical to its correctly named sibling, so every row it
    produces is de-duplicated by source identity and it ends up with no
    ``locales/`` file at all.  Without the memo that bundle would be reported as
    "new" on every single run forever, and a report that always cries wolf is
    worse than no report.
    """
    path = root / "manifests" / "official-bundle-index.json"
    if not path.is_file():
        return set()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entries = document.get("bundles", document) if isinstance(document, dict) else document
    if not isinstance(entries, dict):
        return set()
    return {logical_bundle_name(str(name)) for name in entries}


def tracked_bundle_names(root: Path) -> set[str]:
    """Union of every source of "we already have this bundle"."""
    return locale_bundle_names(root) | lyrics_bundle_names(root) | memo_bundle_names(root)


def load_inventory(path: Path) -> dict[str, int]:
    """Read the family-signature inventory; an unusable file degrades to empty.

    A missing or stale inventory costs a one-off "everything looks new" report
    about unclassified families, never correctness: the localizable selection
    never depends on it.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict) or document.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        return {}
    families = document.get("families")
    if not isinstance(families, dict):
        return {}
    return {
        str(signature).casefold(): int(count)
        for signature, count in families.items()
        if isinstance(count, int) and count >= 0
    }


def write_inventory(path: Path, asset_version: str, families: dict[str, int], observed_at: str) -> None:
    """Write the inventory deterministically so an unchanged index is no diff."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "kind": "mltd-official-asset-inventory",
        "asset_version": str(asset_version),
        "observed_at": observed_at,
        "note": "家族签名 = 逻辑包名把数字折叠成 #；只记录出现过的资源类型与数量，不记录 16.8 万个名字。unclassified 家族即「新资源类型」，需要人工判断是否可汉化。",
        "families": dict(sorted(families.items())),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def inventory_of(index: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for logical in index:
        signature = family_signature(logical)
        counts[signature] = counts.get(signature, 0) + 1
    return counts


def discover(
    index: dict[str, dict],
    registry: dict,
    tracked: set[str],
    *,
    previous_inventory: dict[str, int] | None = None,
    max_new_bundles: int = DEFAULT_MAX_NEW_BUNDLES,
    max_new_bytes: int = DEFAULT_MAX_NEW_BYTES,
    unclassified_samples: int = 12,
) -> dict:
    """Classify the official index against the localizable families.

    Returns the report dictionary.  ``refused`` is non-None when a cap tripped;
    the caller turns that into exit code 2 and nothing is downloaded.
    """
    families = registry_families(registry)
    reviewed = reviewed_unclassified(registry)
    tracked_folded = {name.casefold() for name in tracked}

    per_family: dict[str, dict] = {
        family_id: {"total": 0, "tracked": 0, "new": 0, "new_bytes": 0}
        for family_id in families
    }
    new_bundles: list[dict] = []
    unclassified_counts: dict[str, int] = {}
    unclassified_bytes: dict[str, int] = {}
    unclassified_samples_by_family: dict[str, list[str]] = {}
    unclassified_bundles = 0

    for logical, row in index.items():
        name = logical_bundle_name(logical)
        family_id = classify(registry, name)
        if family_id is None:
            signature = family_signature(name)
            if is_reviewed(signature, reviewed):
                continue
            unclassified_bundles += 1
            unclassified_counts[signature] = unclassified_counts.get(signature, 0) + 1
            unclassified_bytes[signature] = unclassified_bytes.get(signature, 0) + int(
                row.get("declared_size", 0) or 0
            )
            samples = unclassified_samples_by_family.setdefault(signature, [])
            if len(samples) < unclassified_samples:
                samples.append(name)
            continue
        entry = per_family[family_id]
        entry["total"] += 1
        if name.casefold() in tracked_folded:
            entry["tracked"] += 1
            continue
        size = int(row.get("declared_size", 0) or 0)
        entry["new"] += 1
        entry["new_bytes"] += size
        new_bundles.append(
            {
                "logical": name,
                "family": family_id,
                "pipeline": str(families[family_id].get("pipeline", "")),
                "remote": str(row.get("remote", "")),
                "declared_size": size,
            }
        )

    new_bundles.sort(key=lambda item: (item["family"], item["logical"]))
    total_new = len(new_bundles)
    total_new_bytes = sum(item["declared_size"] for item in new_bundles)

    refused = None
    if max_new_bundles and total_new > max_new_bundles:
        refused = (
            f"{total_new} new localizable bundles exceed the cap of {max_new_bundles}; "
            "refusing to auto-discover this batch"
        )
    elif max_new_bytes and total_new_bytes > max_new_bytes:
        refused = (
            f"{total_new_bytes} new bytes exceed the cap of {max_new_bytes}; "
            "refusing to auto-discover this batch"
        )

    previous = {key.casefold(): value for key, value in (previous_inventory or {}).items()}
    new_signatures = sorted(
        signature
        for signature in unclassified_counts
        if signature not in previous
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "official_bundles": len(index),
        "tracked_bundles": len(tracked_folded),
        "localizable": {
            "families": per_family,
            "new_bundles": total_new,
            "new_bytes": total_new_bytes,
            "bundles": new_bundles,
        },
        "unclassified": {
            "bundles": unclassified_bundles,
            "families": len(unclassified_counts),
            "new_families": [
                {
                    "signature": signature,
                    "bundles": unclassified_counts[signature],
                    "bytes": unclassified_bytes[signature],
                    "samples": unclassified_samples_by_family.get(signature, []),
                }
                for signature in new_signatures[:40]
            ],
            "new_family_count": len(new_signatures),
        },
        "refused": refused,
    }


def resolve_index_path(args: argparse.Namespace) -> Path:
    if args.index:
        return Path(args.index)
    version = load_version_manifest(args.version_manifest)
    return Path(args.work_root) / version["index_name"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=None,
                        help="official msgpack index (defaults to <work-root>/<index_name>)")
    parser.add_argument("--version-manifest", type=Path,
                        default=ROOT / "manifests" / "asset-version.json")
    parser.add_argument("--work-root", type=Path, default=ROOT / ".llm-official-work")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--report", type=Path, default=None,
                        help="write the full report JSON here")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="repository root whose locales/ and lyrics/ define 'tracked'")
    parser.add_argument("--max-new-bundles", type=int, default=DEFAULT_MAX_NEW_BUNDLES)
    parser.add_argument("--max-new-bytes", type=int, default=DEFAULT_MAX_NEW_BYTES)
    parser.add_argument("--no-inventory-write", action="store_true",
                        help="report without updating manifests/official-asset-inventory.json")
    args = parser.parse_args()

    index_path = resolve_index_path(args)
    if not index_path.is_file():
        raise SystemExit(f"official index not found: {index_path} (run the refresh first)")
    index = load_official_index(index_path)
    registry = load_registry(args.registry)
    tracked = tracked_bundle_names(args.root)
    report = discover(
        index,
        registry,
        tracked,
        previous_inventory=load_inventory(args.inventory),
        max_new_bundles=args.max_new_bundles,
        max_new_bytes=args.max_new_bytes,
    )

    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    version = load_version_manifest(args.version_manifest)["asset_version"] if args.version_manifest.is_file() else ""
    report["asset_version"] = str(version)
    report["observed_at"] = observed_at
    report["index"] = str(index_path)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if not args.no_inventory_write:
        write_inventory(args.inventory, str(version), inventory_of(index), observed_at)

    summary = {
        "asset_version": report["asset_version"],
        "official_bundles": report["official_bundles"],
        "tracked_bundles": report["tracked_bundles"],
        "new_localizable_bundles": report["localizable"]["new_bundles"],
        "new_localizable_bytes": report["localizable"]["new_bytes"],
        "per_family": report["localizable"]["families"],
        "unclassified_bundles": report["unclassified"]["bundles"],
        "unclassified_families": report["unclassified"]["families"],
        "new_unclassified_families": report["unclassified"]["new_family_count"],
        "refused": report["refused"],
        "report": str(args.report) if args.report else None,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if report["refused"]:
        print(f"refused: {report['refused']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
