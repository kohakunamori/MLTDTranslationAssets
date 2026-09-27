#!/usr/bin/env python3
"""Track and monitor Japanese MLTD asset versions from official and community sources.

Usage:
  python scripts/track_jp_assets.py --check-only
  python scripts/track_jp_assets.py --update
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "manifests" / "asset-version.json"
MATSURIHI_API = "https://api.matsurihi.me/api/mltd/v2/version/assets"
ASSET_ROOT_TEMPLATE = "https://td-assets.bn765.com/{version}/production/2018/Android"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_version_manifest() -> dict:
    if VERSION_FILE.is_file():
        try:
            return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARNING: Could not parse {VERSION_FILE}: {e}", file=sys.stderr)
    return {
        "client_version": "9.0.200",
        "asset_version": 1077500,
        "last_synced_at": now_iso(),
        "matsurihi_api": MATSURIHI_API,
        "asset_root": ASSET_ROOT_TEMPLATE
    }


def fetch_matsurihi_versions(timeout: float = 15.0) -> list[dict]:
    req = Request(MATSURIHI_API, headers={"User-Agent": "mltd-localization-tracker/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {MATSURIHI_API}")
        data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Expected list from Matsurihi API, got {type(data)}")
        return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Track MLTD Japanese Asset Versions")
    parser.add_argument("--check-only", action="store_true", help="Exit code 10 if new version available, 0 if up to date")
    parser.add_argument("--update", action="store_true", help="Update manifests/asset-version.json with latest metadata")
    parser.add_argument("--output-json", type=str, default=None, help="Path to write status JSON")
    args = parser.parse_args()

    manifest = load_version_manifest()
    current_ver = int(manifest.get("asset_version", 1077500))

    print(f"Current pinned asset version: {current_ver}")
    print(f"Querying Matsurihi API: {MATSURIHI_API} ...")

    try:
        versions = fetch_matsurihi_versions()
    except Exception as e:
        print(f"ERROR: Failed to fetch Matsurihi versions: {e}", file=sys.stderr)
        return 1

    if not versions:
        print("ERROR: Empty version list returned from API", file=sys.stderr)
        return 1

    latest_item = versions[-1]
    latest_ver = int(latest_item.get("version", current_ver))
    latest_updated = latest_item.get("updatedAt", "")
    latest_index = latest_item.get("indexName", "")

    print(f"Latest remote asset version: {latest_ver} (Updated: {latest_updated})")
    print(f"Remote index data file: {latest_index}")

    has_update = latest_ver > current_ver
    report = {
        "current_version": current_ver,
        "latest_version": latest_ver,
        "has_update": has_update,
        "index_name": latest_index,
        "updated_at": latest_updated,
        "checked_at": now_iso(),
        "index_url": f"{ASSET_ROOT_TEMPLATE.format(version=latest_ver)}/{latest_index}" if latest_index else None
    }

    if args.output_json:
        out_p = Path(args.output_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote report to {out_p}")

    if has_update:
        print(f"🚀 NEW ASSET VERSION AVAILABLE: {current_ver} -> {latest_ver}")
        if args.update:
            manifest["asset_version"] = latest_ver
            manifest["index_name"] = latest_index
            manifest["last_synced_at"] = now_iso()
            manifest["remote_updated_at"] = latest_updated
            VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            VERSION_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Updated {VERSION_FILE} to version {latest_ver}")
        if args.check_only:
            return 10
    else:
        print(f"✅ Repository is up to date with latest assets (version {current_ver}).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
