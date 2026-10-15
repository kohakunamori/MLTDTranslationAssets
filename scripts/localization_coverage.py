"""Report source synchronization, translation backlog and image inventory.

Image bundle inventory is coverage detection only. It does not assert that
every texture contains Japanese, perform OCR, or call an image provider.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from build_generated_release import load_official_index, load_version_manifest
from pipelines.core.hashing import sha256_file
from scripts.japanese_residue import repair_reason, KANA
from scripts.event_unit_localization import is_bundle, read_rows, source_digest
import bundle_incremental as incremental

from image_resources import FAMILIES, THUMBNAILS, classify
IMAGE_FAMILIES = {**FAMILIES, **THUMBNAILS}


def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def image_coverage(index, reviewed, generated):
    result = {}
    groups = {family: set() for family in IMAGE_FAMILIES}
    for name in index:
        family = classify(name)
        if family:
            groups[family].add(name)
    for family, names in groups.items():
        result[family] = {"official_bundles": len(names), "reviewed_bundles": len(names & reviewed),
                          "generated_bundles": len(names & generated),
                          "missing_reviewed_bundles": sorted(names - reviewed),
                          "missing_generated_bundles": sorted(names - generated)}
    return result


def source_coverage(index, root):
    gtx = load(root / "manifests/official-bundle-index.json").get("bundles", {})
    lyrics = load(root / "lyrics/source-bundle-index.json").get("bundles", {})
    events = load(root / "manifests/event-unit-source-index.json").get("bundles", {})
    surfaces = {"gtx_text": [], "song_lyrics": [], "event_units": []}
    totals = Counter()
    for name, row in index.items():
        if name.endswith("_jp.gtx.unity3d"):
            surface, synced = "gtx_text", gtx.get(name) == row["remote"]
        elif name.startswith("scrobj_") and name != "scrobj_00test.unity3d":
            surface, synced = "song_lyrics", lyrics.get(name) == row["remote"]
        elif is_bundle(name):
            surface = "event_units"
            source_path = root / "locales/event-unit" / f"{name}.jsonl"
            recorded = events.get(name, {})
            synced = (source_path.is_file() and recorded.get("source_identity") == incremental.source_identity(row)
                      and recorded.get("sources") == source_digest(read_rows(source_path)))
        else:
            continue
        totals[surface] += 1
        if not synced:
            surfaces[surface].append(name)
    return {key: {"official_bundles": totals[key], "unsynchronized_bundles": sorted(value)} for key, value in surfaces.items()}


def build_report(index, root, version, index_sha):
    generated_doc = load(root / "generated" / version / "manifest.json")
    generated = {row["logical_key"] for row in generated_doc.get("entries", [])}
    reviewed = {row["bundle"] for row in load(root / "manifests/images.manifest.json").get("images", [])}
    features = load(root / "configs/localization-pipeline.json")
    sources = source_coverage(index, root)
    backlog, repairs = Counter(), []
    for path in sorted((root / "locales").rglob("*.jsonl")):
        with path.open(encoding='utf-8') as source:
            for line_no, line in enumerate(source, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                backlog[row["status"]] += 1
                if row["status"] == "accepted" and KANA.search(row.get("zh", "")):
                    backlog["accepted_with_kana_candidates"] += 1
                reason = repair_reason(row)
                if reason:
                    repairs.append({"file": path.relative_to(root).as_posix(), "line": line_no,
                                    "item_key": row["item_key"], "source_sha256": row["source_sha256"], "reason": reason})
    return {"schema_version": 1, "asset_version": version, "official_index_sha256": index_sha,
            "source_sync_complete": not any(row["unsynchronized_bundles"] for row in sources.values()),
            "sources": sources, "locales": dict(backlog), "repair_candidates": repairs,
            "images": {"mode": "coverage_detection_only", "generation_enabled": features.get("images", {}).get("enabled", False),
                       "measurement": "bundle inventory including thumbnails; Japanese presence requires visual/OCR review",
                       "generated_source_commit": generated_doc.get("source_commit"),
                       "families": image_coverage(index, reviewed, generated)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=ROOT / "manifests/localization-coverage.json")
    parser.add_argument("--require-source-sync", action="store_true")
    args = parser.parse_args(argv)
    version = load_version_manifest(ROOT / "manifests/asset-version.json")["asset_version"]
    report = build_report(load_official_index(args.index), ROOT, version, sha256_file(args.index))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"asset_version": version, "source_sync_complete": report["source_sync_complete"],
                      "untranslated": report["locales"].get("untranslated", 0),
                      "repair_candidates": len(report["repair_candidates"]),
                      "images": {key: {field: value for field, value in row.items() if field.endswith("bundles") and isinstance(value, int)} for key, row in report["images"]["families"].items()}}))
    if args.require_source_sync and not report["source_sync_complete"]:
        raise SystemExit("source synchronization incomplete; inspect localization-coverage.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
