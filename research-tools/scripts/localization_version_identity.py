#!/usr/bin/env python3
"""Fail-closed (client version, assets version) identity for one JP localization.

The asset version is read from the archived snapshot's upstream URL, never
inferred from the newest remote version or the workspace directory name.
The client version must be supplied by the caller / client profile.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLIENT_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ASSET_RE = re.compile(r"^[0-9]+$")
UPSTREAM_RE = re.compile(r"/([0-9]+)/production(?:/|$)")


def _repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (REPO / path).resolve()


def version_identity(
    snapshot_path: Path,
    *,
    client_version: str,
    asset_version: str,
    asset_index: Path | None = None,
) -> dict:
    if not CLIENT_RE.fullmatch(client_version):
        raise ValueError(f"invalid client version: {client_version!r}")
    if not ASSET_RE.fullmatch(asset_version):
        raise ValueError(f"invalid assets version: {asset_version!r}")
    snapshot_path = _repo_path(snapshot_path)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if snapshot.get("complete") is not True:
        raise ValueError(f"GTX snapshot not complete: {snapshot_path}")
    if snapshot.get("scope") != "jp-android":
        raise ValueError(f"unsupported GTX snapshot scope: {snapshot.get('scope')!r}")
    upstream = str(snapshot.get("upstream_root") or "")
    match = UPSTREAM_RE.search(upstream)
    if not match:
        raise ValueError(f"cannot establish assets version from upstream_root: {upstream!r}")
    observed_version = match.group(1)
    if observed_version != asset_version:
        raise ValueError(
            f"assets version mismatch: requested {asset_version}, "
            f"snapshot upstream_root is {observed_version}"
        )
    index_value = snapshot.get("asset_index")
    if not isinstance(index_value, str) or not index_value:
        raise ValueError("snapshot missing asset_index")
    snapshot_index = _repo_path(index_value)
    if not snapshot_index.is_file():
        raise ValueError(f"snapshot asset index missing: {snapshot_index}")
    if asset_index is not None and snapshot_index != _repo_path(asset_index):
        raise ValueError(
            f"asset index mismatch: snapshot={snapshot_index}, requested={_repo_path(asset_index)}"
        )
    if not isinstance(snapshot.get("objects"), list) or not snapshot["objects"]:
        raise ValueError("GTX snapshot missing objects")
    return {
        "schema_version": 1,
        "region": "jp",
        "client_version": client_version,
        "assets_version": asset_version,
        "version_key": f"jp-client-{client_version}-assets-{asset_version}",
        "scope": snapshot["scope"],
        "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "snapshot_objects": len(snapshot["objects"]),
        "asset_index_name": snapshot_index.name,
        "asset_index_sha256": hashlib.sha256(snapshot_index.read_bytes()).hexdigest(),
        "upstream_root": upstream,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--client-version", required=True)
    ap.add_argument("--asset-version", required=True)
    ap.add_argument("--asset-index", type=Path)
    args = ap.parse_args()
    print(json.dumps(
        version_identity(
            args.snapshot,
            client_version=args.client_version,
            asset_version=args.asset_version,
            asset_index=args.asset_index,
        ),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
