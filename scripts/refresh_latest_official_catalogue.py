#!/usr/bin/env python3
"""Extract the latest official text bundles and append new source rows.

Only logical bundles already represented by this repository are downloaded.
This keeps the scheduled job bounded while still detecting new/changed GTX
records in the current text surface.  The official archive remains temporary;
only source text with ``untranslated`` status is committed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_generated_release import download, load_official_index, load_version_manifest


def locale_rows():
    for path in sorted((ROOT / "locales").rglob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield path, json.loads(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=ROOT / ".llm-official-work")
    parser.add_argument("--max-bundles", type=int, default=0)
    args = parser.parse_args()

    version = load_version_manifest(ROOT / "manifests" / "asset-version.json")
    known_bundles = set()
    existing = set()
    for _path, row in locale_rows():
        bundle = str(row.get("bundle", "")).strip()
        if bundle:
            known_bundles.add(bundle.casefold() if bundle.endswith(".unity3d") else (bundle + ".unity3d").casefold())
        existing.add((str(row.get("asset_version", "")), bundle, str(row.get("item_key", "")), str(row.get("source_sha256", ""))))

    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=True)
    index_path = work / version["index_name"]
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    index = load_official_index(index_path)
    selected = {
        logical: row for logical, row in index.items()
        if logical.casefold() in known_bundles
    }
    if args.max_bundles:
        selected = dict(sorted(selected.items())[:args.max_bundles])
    if not selected:
        raise SystemExit("no known text bundles matched the official index")

    archive = work / "archive"
    for row in selected.values():
        destination = archive / "jp-android" / row["remote"]
        download(f"{version['asset_root']}/{row['remote']}", destination, row["declared_size"])
    snapshot = work / "snapshot.json"
    snapshot.write_text(json.dumps({
        "complete": True, "scope": "jp-android", "asset_index": str(index_path),
        "upstream_root": version["asset_root"],
        "objects": [{"logical": logical, **row} for logical, row in sorted(selected.items())],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    catalogue = work / "catalogue.jsonl"
    subprocess.run([
        sys.executable, str(ROOT / "pipelines/text/mltd_localization_pipeline.py"),
        "extract-snapshot", "--snapshot", str(snapshot), "--archive-root", str(archive),
        "--output", str(catalogue), "--workers", "8",
    ], cwd=ROOT, check=True)

    output = ROOT / "locales" / "master" / f"official-{version['asset_version']}-untranslated.jsonl"
    additions = []
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for line in catalogue.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        bundle = str(row.get("bundle", ""))
        key = str(row.get("key", ""))
        source = str(row.get("source", ""))
        sid = str(row.get("source_sha256", ""))
        identity = (version["asset_version"], bundle, key, sid)
        if not source or not key or identity in existing:
            continue
        additions.append({
            "asset_version": version["asset_version"],
            "client_version": None,
            "source_client_version": version["client_version"],
            "bundle": bundle, "item_key": key, "source_sha256": sid,
            "ja": source, "zh": "", "status": "untranslated",
            "translation_stage": "untranslated", "updated_at": now,
        })
        existing.add(identity)
    if additions:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8", newline="\n") as stream:
            for row in additions:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"asset_version": version["asset_version"],
                      "matched_bundles": len(selected), "new_rows": len(additions),
                      "output": str(output) if additions else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
