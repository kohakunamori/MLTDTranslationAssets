#!/usr/bin/env python3
"""Materialize immutable MLTD static version views from the shared SHA-256 CAS."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from server.asset_archive import safe_relative_name  # noqa: E402
from server.versioned_asset_store import VersionedAssetStore  # noqa: E402


def _link_exact(source: Path, destination: Path, expected_size: int) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.stat().st_size != expected_size:
        raise IOError(f"CAS size mismatch for {source.name}")
    source.chmod(0o644)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError as exc:
        raise OSError(
            f"hardlink materialization failed ({source} -> {destination}); "
            "CAS and view must be on the same filesystem"
        ) from exc


def materialize(args) -> int:
    store = VersionedAssetStore(args.root)
    version, scope = store.normalize_identity(args.version, args.scope)
    identity = store.version(version, scope)
    if identity is None:
        raise ValueError(f"version not registered: {version}/{scope}")

    with store.db() as conn:
        rows = conn.execute(
            """
            SELECT name,sha256,size FROM entries
            WHERE version=? AND scope=? ORDER BY name
            """,
            (version, scope),
        ).fetchall()

    missing = [name for name, digest, _size in rows if not digest]
    if missing:
        raise RuntimeError(
            f"version is not fully archived: {len(missing)} logical objects are unmapped"
        )

    if not getattr(args, "skip_hash_verify", False):
        missing_files = mismatched = 0
        for _name, digest, size in rows:
            source = store.object_path(str(digest))
            if not source.is_file():
                missing_files += 1
                continue
            if source.stat().st_size != int(size or -1):
                mismatched += 1
                continue
            if store.sha256_file(source) != str(digest):
                mismatched += 1
        if missing_files or mismatched:
            raise RuntimeError(
                "refusing materialization after SHA-256 verification failure: "
                f"missing={missing_files} mismatched={mismatched}"
            )

    views_root = store.root / "views"
    views_root.mkdir(parents=True, exist_ok=True)
    final_root = views_root / version
    final_scope = final_root / scope
    if final_scope.exists():
        if not args.replace:
            print(json.dumps({
                "version": version,
                "scope": scope,
                "view": str(final_scope),
                "status": "exists",
            }, indent=2))
            return 0
        shutil.rmtree(final_scope)

    staging_root = Path(tempfile.mkdtemp(prefix=f".{version}-{scope}.", dir=views_root))
    staging_scope = staging_root / scope
    linked = 0
    logical_bytes = 0
    try:
        for name, digest, size in rows:
            name = safe_relative_name(name)
            size = int(size or 0)
            source = store.object_path(str(digest))
            destination = staging_scope.joinpath(*name.split("/"))
            _link_exact(source, destination, size)
            linked += 1
            logical_bytes += size

        metadata = {
            "schema_version": 1,
            "version": version,
            "scope": scope,
            "files": linked,
            "logical_bytes": logical_bytes,
            "storage": "hardlink-view-over-sha256-cas",
        }
        (staging_root / "view.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for dirpath, _dirnames, _filenames in os.walk(staging_root):
            os.chmod(dirpath, 0o755)
        if final_root.exists():
            final_root.mkdir(parents=True, exist_ok=True)
            os.replace(staging_scope, final_scope)
            shutil.rmtree(staging_root)
        else:
            os.replace(staging_root, final_root)
        store.mark_complete(version, scope, True)
        print(json.dumps(metadata | {"view": str(final_scope)}, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def switch_current(args) -> int:
    root = Path(args.root).resolve()
    version = str(args.version).strip()
    if not version or "/" in version or "\\" in version or version in {".", ".."}:
        raise ValueError("unsafe version")
    target = root / "views" / version
    if not target.is_dir():
        raise FileNotFoundError(f"static view not found: {target}")

    link = root / "current"
    temp = root / f".current.{os.getpid()}.tmp"
    temp.unlink(missing_ok=True)
    relative_target = Path("views") / version
    os.symlink(relative_target, temp, target_is_directory=True)
    try:
        os.replace(temp, link)
    except OSError as exc:
        if exc.errno != 18:
            raise
        temp.unlink(missing_ok=True)
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                raise IsADirectoryError(link)
            link.unlink()
        os.symlink(relative_target, link, target_is_directory=True)
    print(json.dumps({"current": version, "link": str(link)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize/switch MLTD static asset versions")
    sub = parser.add_subparsers(dest="command", required=True)

    mat = sub.add_parser("materialize")
    mat.add_argument("--root", type=Path, required=True)
    mat.add_argument("--version", required=True)
    mat.add_argument("--scope", default="jp-android")
    mat.add_argument("--replace", action="store_true")
    mat.add_argument(
        "--skip-hash-verify",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    mat.set_defaults(func=materialize)

    sw = sub.add_parser("switch-current")
    sw.add_argument("--root", type=Path, required=True)
    sw.add_argument("--version", required=True)
    sw.set_defaults(func=switch_current)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
