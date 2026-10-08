#!/usr/bin/env python3
"""Validate all version-pinned GTX overlay objects, then smoke the real HTTP route.

This does not modify the source archive or start a persistent asset service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import urllib.request
from pathlib import Path

from scripts.localization_version_identity import version_identity
from server.local_asset_server import AssetServer


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(
    *,
    snapshot_path: Path,
    archive_root: Path,
    overlay_root: Path,
    client_version: str,
    asset_version: str,
) -> dict:
    expected = version_identity(
        snapshot_path,
        client_version=client_version,
        asset_version=asset_version,
    )
    bound = json.loads((overlay_root / "version-identity.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (overlay_root / "localization-manifest.json").read_text(encoding="utf-8")
    )
    if bound != expected or manifest.get("version_identity") != expected:
        raise ValueError("overlay version identity mismatch")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    expected_remotes = {row["remote"] for row in snapshot["objects"]}
    indexed = {row["remote"]: row for row in manifest.get("bundles", [])}
    if (
        len(indexed) != len(manifest.get("bundles", []))
        or set(indexed) != expected_remotes
        or manifest.get("bundles_scanned") != len(expected_remotes)
        or manifest.get("bundles_written") != len(expected_remotes)
        or manifest.get("resolved") != manifest.get("source_candidates")
        or manifest.get("stale_exact") != 0
    ):
        raise ValueError("GTX manifest coverage / remote keys mismatch")
    scope = expected["scope"]
    stored = {x.name for x in (overlay_root / scope).glob("*.unity3d")}
    if stored != expected_remotes:
        raise ValueError("overlay disk and manifest remote keys mismatch")
    changed = 0
    for remote, row in indexed.items():
        src = archive_root / scope / remote
        dest = overlay_root / scope / remote
        if (
            src != Path(row["source_path"]).resolve()
            and src.resolve() != Path(row["source_path"]).resolve()
        ):
            raise ValueError(f"manifest source path mismatch: {remote}")
        if dest.resolve() != Path(row["output_path"]).resolve():
            raise ValueError(f"manifest overlay path mismatch: {remote}")
        if not src.is_file() or not dest.is_file():
            raise ValueError(f"GTX source or output missing: {remote}")
        if sha256(src) != row["source_bundle_sha256"]:
            raise ValueError(f"source bundle SHA-256 mismatch: {remote}")
        if sha256(dest) != row["output_bundle_sha256"]:
            raise ValueError(f"output bundle SHA-256 mismatch: {remote}")
        if int(row["changed"]) <= 0 or row["source_bundle_sha256"] == row["output_bundle_sha256"]:
            raise ValueError(f"manifest contains unchanged GTX: {remote}")
        changed += int(row["changed"])
    if changed != manifest.get("records_changed"):
        raise ValueError("changed record count mismatch")
    index = archive_root / scope / expected["asset_index_name"]
    if sha256(index) != expected["asset_index_sha256"]:
        raise ValueError("unmodified upstream index hash mismatch")

    # The actual local asset HTTP server must prefer overlay remote keys, but
    # still serve the exact pinned official asset-index .data file.
    samples = [manifest["bundles"][i] for i in sorted({
        0, len(manifest["bundles"]) // 2, len(manifest["bundles"]) - 1
    })]
    server = AssetServer(
        ("127.0.0.1", 0),
        archive_root=archive_root,
        overlay_root=overlay_root,
        url_prefix="assets",
        events=None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for row in samples:
            url = (
                f"http://127.0.0.1:{server.server_port}/assets/"
                f"{scope}/{row['remote']}"
            )
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"Range": "bytes=0-31"}),
                timeout=8,
            ) as reply:
                if reply.status != 206 or reply.read() != (
                    overlay_root / scope / row["remote"]
                ).read_bytes()[:32]:
                    raise ValueError(f"HTTP overlay route failed: {row['remote']}")
        url = (
            f"http://127.0.0.1:{server.server_port}/assets/"
            f"{scope}/{expected['asset_index_name']}"
        )
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"Range": "bytes=0-31"}),
            timeout=8,
        ) as reply:
            if reply.status != 206 or reply.read() != index.read_bytes()[:32]:
                raise ValueError("HTTP index route mismatch")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {
        "version_identity": expected,
        "verified_bundles": len(indexed),
        "records_changed": changed,
        "http_overlay_samples": len(samples),
        "http_index_checked": True,
        "source_archive_modified": False,
        "status": "GTX-overlay-verified",
        "other_surfaces_verified": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--archive-root", type=Path, required=True)
    ap.add_argument("--overlay-root", type=Path, required=True)
    ap.add_argument("--client-version", required=True)
    ap.add_argument("--asset-version", required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    report = verify(
        snapshot_path=args.snapshot,
        archive_root=args.archive_root,
        overlay_root=args.overlay_root,
        client_version=args.client_version,
        asset_version=args.asset_version,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
