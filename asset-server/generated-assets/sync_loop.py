#!/usr/bin/env python3
"""Continuously mirror successful generated Assets releases without a current pointer."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/scripts")

from assets_mirror import (  # noqa: E402
    AssetVersionMirror,
    AssetsMirrorError,
    GitHubAssetsSource,
    ManifestValidationError,
    ObjectPool,
)


def sync_once(root: Path, repository: str, branch: str) -> dict:
    source = GitHubAssetsSource(repo=repository, branch=branch)
    mirror = AssetVersionMirror(source, ObjectPool(root), root)
    head = source.head_commit()
    versions_payload = source._get_json(
        f"{source.api_base}/repos/{repository}/contents/generated?ref={head}"
    )
    if not isinstance(versions_payload, list):
        raise RuntimeError("GitHub generated directory listing was not an array")
    versions = sorted(
        [
            str(item["name"])
            for item in versions_payload
            if isinstance(item, dict)
            and item.get("type") == "dir"
            and str(item.get("name", "")).isdigit()
            and int(str(item.get("name", ""))) >= int(os.environ.get("MLTD_MIN_ASSET_VERSION", "100"))
        ],
        key=lambda value: (int(value), value),
        reverse=True,
    )
    reports = []
    for asset_version in versions:
        try:
            manifest = source.fetch_manifest(asset_version, head)
            if manifest.get("build_status") != "success":
                continue
            reports.append(mirror.sync(asset_version, commit=head, dry_run=False))
        except (AssetsMirrorError, ManifestValidationError) as exc:
            reports.append({"asset_version": asset_version, "sync_status": "refused", "error": str(exc)})
    return {
        "repository": repository,
        "branch": branch,
        "head": head,
        "versions_seen": len(versions),
        "reports": reports,
        "current_pointer_used": False,
        "current_pointer_written": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("MLTD_MIRROR_ROOT", "/data")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("MLTD_SYNC_INTERVAL", "21600")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    repository = os.environ.get("MLTD_ASSETS_REPOSITORY", "kohakunamori/MLTDTranslationAssets")
    branch = os.environ.get("MLTD_ASSETS_BRANCH", "main")
    args.root.mkdir(parents=True, exist_ok=True)
    lock_file = (args.root / ".generated-assets-sync.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise SystemExit("another generated-assets sync is already running")
    while True:
        try:
            print(json.dumps(sync_once(args.root, repository, branch), ensure_ascii=False), flush=True)
        except Exception as exc:  # keep the distributor alive; next poll retries
            print(json.dumps({"sync_status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
