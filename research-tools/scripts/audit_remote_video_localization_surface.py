#!/usr/bin/env python3
"""Inventory remote MLTD MP4-in-UnityFS assets as a separate baked-video localization surface.

This audit is metadata/structure only. It does not claim a video needs translation
until representative frames are visually reviewed. The exact asset manifest is
used as source authority; current relationship DB rows provide bundle shape when
available.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

import msgpack

ROOT = Path(__file__).resolve().parents[1]


def load_manifest(path: Path) -> dict[str, list]:
    root = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(root, (list, tuple)) or len(root) != 1 or not isinstance(root[0], dict):
        raise ValueError("expected MLTD asset index shape [map]")
    return root[0]


def classify(name: str) -> tuple[str, str]:
    n = name.lower()
    if n.startswith("igp_demo_movie_"):
        return "igp_demo", "high"
    if n.startswith("fk_memorial"):
        return "fk_memorial", "review"
    if "anniversary" in n:
        return "anniversary", "review"
    if n.startswith("vc_event_op_"):
        return "event_opening", "review"
    if n.startswith("vc_event_"):
        return "event_vc", "review"
    if n.startswith("vc_season"):
        return "season_promo", "review"
    if n.startswith("special_"):
        return "special", "review"
    if n.startswith("vc_botop_"):
        return "botop", "review"
    return "other", "review"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--asset-index",
        type=Path,
        default=ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data",
    )
    ap.add_argument(
        "--relationship-db",
        type=Path,
        default=ROOT / "build/current-asset-relationships.sqlite",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build/localization-90200/remote-video-localization-surface-audit.json",
    )
    args = ap.parse_args()

    manifest = load_manifest(args.asset_index)
    names = sorted(
        str(name)
        for name in manifest
        if str(name).lower().endswith(".mp4.unity3d")
    )

    con = None
    if args.relationship_db.is_file():
        con = sqlite3.connect(
            "file:" + args.relationship_db.resolve().as_posix() + "?mode=ro&immutable=1",
            uri=True,
            timeout=1,
        )
        con.row_factory = sqlite3.Row

    rows = []
    class_counts = Counter()
    priority_counts = Counter()
    shape_counts = Counter()
    missing_relationship = []
    for name in names:
        record = manifest[name]
        remote = str(record[1])
        size = int(record[2])
        category, priority = classify(name)
        class_counts[category] += 1
        priority_counts[priority] += 1

        relationship = None
        if con is not None:
            scan = con.execute(
                "SELECT scan_status,object_count,object_types_json FROM bundle_scan WHERE logical_name=?",
                (name,),
            ).fetchone()
            if scan is not None:
                relationship = {
                    "scan_status": str(scan["scan_status"]),
                    "object_count": int(scan["object_count"]),
                    "object_types": json.loads(str(scan["object_types_json"])),
                }
                shape_counts[json.dumps(relationship["object_types"], sort_keys=True)] += 1
            else:
                missing_relationship.append(name)

        rows.append(
            {
                "logical_name": name,
                "remote_name": remote,
                "declared_size": size,
                "category": category,
                "review_priority": priority,
                "relationship": relationship,
            }
        )

    if con is not None:
        metadata = {
            str(k): str(v)
            for k, v in con.execute(
                "SELECT key,value FROM metadata WHERE key IN "
                "('archive_asset_root','archive_complete','archive_version')"
            )
        }
        con.close()
    else:
        metadata = {}

    sample_root = ROOT / "build/localization-90200/video-audit-samples"
    sample_bundle = sample_root / "igp_demo_movie_1.mp4.unity3d"
    sample_mp4 = sample_root / "igp_demo_movie_1.mp4.extracted.mp4"
    sample_contact = sample_root / "igp_demo_movie_1.contact.jpg"

    report = {
        "schema_version": 1,
        "kind": "mltd-remote-baked-video-localization-surface",
        "asset_index": str(args.asset_index),
        "relationship_db": str(args.relationship_db),
        "video_assets": len(rows),
        "category_counts": dict(sorted(class_counts.items())),
        "priority_counts": dict(sorted(priority_counts.items())),
        "relationship_missing": len(missing_relationship),
        "relationship_missing_names": missing_relationship,
        "bundle_object_shape_counts": dict(sorted(shape_counts.items())),
        "archive_metadata": metadata,
        "representative_sample": {
            "logical_name": "igp_demo_movie_1.mp4.unity3d",
            "bundle_present": sample_bundle.is_file(),
            "extracted_mp4_present": sample_mp4.is_file(),
            "contact_sheet_present": sample_contact.is_file(),
            "verified_bundle_sha256": "740B1FEAF779C5F1A9654913261A24DF08E26C69E794B348C5D39CCAC492250E"
            if sample_bundle.is_file()
            else None,
            "extracted_payload": {
                "container": "TextAsset",
                "name": "igp_demo_movie_1.mp4",
                "format": "H.264 MP4",
                "width": 1280,
                "height": 720,
                "duration_seconds": 17.0837,
                "payload_bytes": 8040806,
            }
            if sample_mp4.is_file()
            else None,
        },
        "status": "frame_review_required" if rows else "closed_no_video_assets",
        "policy": (
            "Do not count MP4 assets in textual coverage. Review representative frames by "
            "category for baked Japanese copy/subtitles/logos. Promote only videos with "
            "confirmed visible text to a video-localization/redraw/re-encode worklist."
        ),
        "rows": rows,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "video_assets": report["video_assets"],
                "category_counts": report["category_counts"],
                "priority_counts": report["priority_counts"],
                "relationship_missing": report["relationship_missing"],
                "bundle_object_shape_counts": report["bundle_object_shape_counts"],
                "status": report["status"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
