#!/usr/bin/env python3
"""Manifest-driven MLTD version discovery, archival, materialization, and activation."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from server.versioned_asset_store import VersionedAssetStore  # noqa: E402

DEFAULT_VERSION_API = "https://api.matsurihi.me/mltd/v1/version/latest"
DEFAULT_ASSET_ROOT = "https://td-assets.bn765.com/{version}/production/2018/Android"
CONTROL_NAME = "manifest.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def fetch_json(url: str, timeout: float) -> dict:
    req = Request(url, headers={"User-Agent": "mltd-asset-archive-controller/1"})
    with urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"version API HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def empty_control(api: str) -> dict:
    return {
        "schema_version": 1,
        "game": "mltd",
        "version_source": api,
        "updated_at": now(),
        "active_version": None,
        "releases": {},
    }


def load_control(root: Path, api: str) -> dict:
    path = root / CONTROL_NAME
    control = read_json(path) or empty_control(api)
    if control.get("game") != "mltd":
        raise ValueError(f"unexpected archive manifest game: {control.get('game')!r}")
    control.setdefault("schema_version", 1)
    control.setdefault("version_source", api)
    control.setdefault("active_version", None)
    control.setdefault("releases", {})
    return control


def merge_release(control: dict, release: dict) -> dict:
    version = str(release["version"])
    releases = control.setdefault("releases", {})
    old = releases.get(version)
    immutable = ("version", "scope", "asset_root", "index_name")
    if old is not None:
        for key in immutable:
            if str(old.get(key)) != str(release.get(key)):
                raise ValueError(
                    f"immutable release conflict for {version}: {key}: "
                    f"{old.get(key)!r} != {release.get(key)!r}"
                )
        merged = dict(old)
        for key, value in release.items():
            if value is not None:
                merged[key] = value
        release = merged
    else:
        release.setdefault("discovered_at", now())
    release.setdefault("retained", True)
    releases[version] = release
    control["updated_at"] = now()
    return release


def discover_latest(root: Path, api: str, asset_root_template: str, scope: str, timeout: float) -> tuple[dict, dict]:
    upstream = fetch_json(api, timeout)
    res = upstream.get("res") or upstream.get("resource")
    if not isinstance(res, dict):
        raise ValueError("version API response has no res/resource object")
    version = str(res["version"])
    index_name = str(res.get("indexName") or res.get("index_name") or "")
    if not index_name:
        raise ValueError("version API response has no resource index name")
    release = {
        "version": version,
        "scope": scope,
        "asset_root": asset_root_template.format(version=version).rstrip("/"),
        "index_name": index_name,
        "resource_update_time": res.get("updateTime") or res.get("update_time"),
        "app_version": (upstream.get("app") or {}).get("version"),
        "source": "version-api",
    }
    control = load_control(root, api)
    release = merge_release(control, release)
    atomic_json(root / CONTROL_NAME, control)
    return control, release


def import_store(root: Path, control: dict) -> None:
    db = root / "index.sqlite3"
    if not db.is_file():
        return
    store = VersionedAssetStore(root, read_only=True)
    with store.db() as conn:
        rows = conn.execute(
            "SELECT version,scope,asset_root,manifest_name,manifest_sha256,"
            "object_count,complete,created_at,updated_at FROM versions"
        ).fetchall()
    for row in rows:
        version, scope, asset_root, index_name, digest, count, complete, created, updated = row
        existing_source = control.get("releases", {}).get(
            str(version), {}
        ).get("source", "store-import")
        merge_release(control, {
            "version": str(version),
            "scope": str(scope),
            "asset_root": str(asset_root),
            "index_name": str(index_name),
            "index_sha256": digest,
            "object_count": int(count or 0),
            "complete": bool(complete),
            "store_created_at": created,
            "store_updated_at": updated,
            "source": existing_source,
        })


def refresh_status(root: Path, control: dict, *, deep: bool = False) -> None:
    db = root / "index.sqlite3"
    if db.is_file():
        store = VersionedAssetStore(root, read_only=True)
        for version, release in control.get("releases", {}).items():
            scope = str(release.get("scope") or "jp-android")
            identity = store.version(version, scope)
            if identity is None:
                release["registered"] = False
                continue
            release["registered"] = True
            release["index_sha256"] = identity.get("manifest_sha256")
            release["object_count"] = int(identity.get("object_count") or 0)
            release["complete"] = bool(identity.get("complete"))
            if deep:
                release["store"] = store.stats(version, scope)
            release["materialized"] = (root / "views" / version / scope).is_dir()
    current = root / "current"
    if current.is_symlink():
        try:
            control["active_version"] = current.resolve(strict=True).name
        except OSError:
            control["active_version"] = None
    control["updated_at"] = now()


def run_checked(argv: list[str]) -> None:
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, check=True)


def archive_release(root: Path, release: dict, *, workers: int, timeout: float, proxy: str | None, durable: bool) -> None:
    cmd = [
        sys.executable, str(REPO / "tools" / "versioned_assets.py"), "sync",
        "--root", str(root),
        "--version", str(release["version"]),
        "--scope", str(release["scope"]),
        "--asset-root", str(release["asset_root"]),
        "--manifest", str(release["index_name"]),
        "--workers", str(workers),
        "--timeout", str(timeout),
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    if durable:
        cmd += ["--durable"]
    run_checked(cmd)


def verify_release(root: Path, release: dict) -> None:
    run_checked([
        sys.executable, str(REPO / "tools" / "versioned_assets.py"),
        "verify",
        "--root", str(root),
        "--version", str(release["version"]),
        "--scope", str(release.get("scope") or "jp-android"),
        "--hash",
    ])


def materialize_release(root: Path, release: dict) -> None:
    run_checked([
        sys.executable, str(REPO / "tools" / "materialize_versioned_assets.py"),
        "materialize", "--root", str(root),
        "--version", str(release["version"]),
        "--scope", str(release["scope"]),
        "--skip-hash-verify",
    ])


def activate_release(root: Path, release: dict) -> None:
    run_checked([
        sys.executable, str(REPO / "tools" / "materialize_versioned_assets.py"),
        "switch-current", "--root", str(root),
        "--version", str(release["version"]),
    ])


def require_free_space(root: Path, minimum_free_bytes: int) -> None:
    stat = os.statvfs(root)
    free = stat.f_bavail * stat.f_frsize
    if free < minimum_free_bytes:
        raise RuntimeError(
            f"archive filesystem free space too low: {free} < {minimum_free_bytes}"
        )


def release_sort_key(release: dict) -> tuple[int, int | str]:
    version = str(release.get("version") or "")
    try:
        return (0, int(version))
    except ValueError:
        return (1, version)


def reconcile_release(
    root: Path,
    control: dict,
    release: dict,
    *,
    workers: int,
    timeout: float,
    proxy: str | None,
    durable: bool,
    minimum_free_bytes: int,
) -> dict:
    if release.get("retained") is False:
        return release

    refresh_status(root, control)
    release = control["releases"][str(release["version"])]
    if not release.get("complete"):
        require_free_space(root, minimum_free_bytes)
        archive_release(
            root,
            release,
            workers=workers,
            timeout=timeout,
            proxy=proxy,
            durable=durable,
        )
        refresh_status(root, control)
        release = control["releases"][str(release["version"])]

    if release.get("complete") and not release.get("materialized"):
        verify_release(root, release)
        materialize_release(root, release)
        refresh_status(root, control)
        release = control["releases"][str(release["version"])]

    return release


def reconcile_manifest(
    root: Path,
    control: dict,
    *,
    workers: int,
    timeout: float,
    proxy: str | None,
    durable: bool,
    minimum_free_bytes: int,
    activate_latest: bool,
) -> dict:
    import_store(root, control)
    refresh_status(root, control)
    atomic_json(root / CONTROL_NAME, control)
    retained = sorted(
        (
            release
            for release in control.get("releases", {}).values()
            if release.get("retained") is not False
        ),
        key=release_sort_key,
        reverse=True,
    )

    for release in retained:
        reconcile_release(
            root,
            control,
            release,
            workers=workers,
            timeout=timeout,
            proxy=proxy,
            durable=durable,
            minimum_free_bytes=minimum_free_bytes,
        )
        atomic_json(root / CONTROL_NAME, control)

    if activate_latest and retained:
        latest = control["releases"][str(retained[0]["version"])]
        if not latest.get("complete") or not latest.get("materialized"):
            raise RuntimeError(
                f"refusing to activate incomplete/unmaterialized version "
                f"{latest['version']}"
            )
        if control.get("active_version") != str(latest["version"]):
            verify_release(root, latest)
            activate_release(root, latest)
            refresh_status(root, control)

    control["updated_at"] = now()
    atomic_json(root / CONTROL_NAME, control)
    return control


def watch_cycle(
    root: Path,
    *,
    version_api: str,
    asset_root_template: str,
    scope: str,
    discovery_timeout: float,
    workers: int,
    archive_timeout: float,
    proxy: str | None,
    durable: bool,
    minimum_free_bytes: int,
    auto_activate: bool,
) -> tuple[dict, str | None]:
    discovery_error: str | None = None
    try:
        control, _release = discover_latest(
            root,
            version_api,
            asset_root_template,
            scope,
            discovery_timeout,
        )
    except Exception as exc:
        discovery_error = f"{type(exc).__name__}: {exc}"
        control = load_control(root, version_api)

    control = reconcile_manifest(
        root,
        control,
        workers=workers,
        timeout=archive_timeout,
        proxy=proxy,
        durable=durable,
        minimum_free_bytes=minimum_free_bytes,
        activate_latest=auto_activate,
    )
    return control, discovery_error


def main() -> int:
    ap = argparse.ArgumentParser(description="MLTD manifest-driven archive controller")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--version-api", default=DEFAULT_VERSION_API)
    ap.add_argument("--asset-root-template", default=DEFAULT_ASSET_ROOT)
    ap.add_argument("--scope", default="jp-android")
    ap.add_argument("--timeout", type=float, default=60.0)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("discover")

    status = sub.add_parser("status")
    status.add_argument("--import-store", action="store_true")

    archive = sub.add_parser("archive")
    archive.add_argument("--version")
    archive.add_argument("--index-name")
    archive.add_argument("--asset-root")
    archive.add_argument("--workers", type=int, default=64)
    archive.add_argument("--proxy")
    archive.add_argument("--durable", action="store_true")
    archive.add_argument("--min-free-gib", type=float, default=80.0)
    archive.add_argument("--materialize", action="store_true")
    archive.add_argument("--activate", action="store_true")

    activate = sub.add_parser("activate")
    activate.add_argument("--version", required=True)

    rec = sub.add_parser("reconcile-latest")
    rec.add_argument("--workers", type=int, default=64)
    rec.add_argument("--proxy")
    rec.add_argument("--durable", action="store_true")
    rec.add_argument("--min-free-gib", type=float, default=80.0)
    rec.add_argument("--no-activate", action="store_true")

    rec_manifest = sub.add_parser(
        "reconcile-manifest",
        help="archive/materialize every retained release declared in manifest.json",
    )
    rec_manifest.add_argument("--workers", type=int, default=64)
    rec_manifest.add_argument("--proxy")
    rec_manifest.add_argument("--durable", action="store_true")
    rec_manifest.add_argument("--min-free-gib", type=float, default=80.0)
    rec_manifest.add_argument("--activate-latest", action="store_true")

    watch = sub.add_parser(
        "watch",
        help="continuously discover upstream versions and reconcile manifest.json",
    )
    watch.add_argument("--poll-seconds", type=int, default=21600)
    watch.add_argument("--workers", type=int, default=64)
    watch.add_argument("--proxy")
    watch.add_argument("--durable", action="store_true")
    watch.add_argument("--min-free-gib", type=float, default=80.0)
    watch.add_argument("--auto-activate", choices=("0", "1"), default="0")

    args = ap.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    if args.command == "discover":
        control, release = discover_latest(
            root, args.version_api, args.asset_root_template, args.scope, args.timeout
        )
        import_store(root, control)
        refresh_status(root, control)
        atomic_json(root / CONTROL_NAME, control)
        print(json.dumps(release, ensure_ascii=False, indent=2))
        return 0

    control = load_control(root, args.version_api)
    import_store(root, control)

    if args.command == "status":
        refresh_status(root, control, deep=True)
        atomic_json(root / CONTROL_NAME, control)
        print(json.dumps(control, ensure_ascii=False, indent=2))
        return 0

    if args.command == "activate":
        refresh_status(root, control)
        release = control.get("releases", {}).get(str(args.version))
        if release is None:
            raise ValueError(f"unknown MLTD archive version: {args.version}")
        if not release.get("complete") or not release.get("materialized"):
            raise RuntimeError(
                f"refusing to activate incomplete/unmaterialized version {args.version}"
            )
        verify_release(root, release)
        activate_release(root, release)
        refresh_status(root, control)
        atomic_json(root / CONTROL_NAME, control)
        print(json.dumps({
            "active_version": control.get("active_version"),
            "version": str(args.version),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "archive":
        if args.version:
            version = str(args.version)
            release = control.get("releases", {}).get(version)
            if release is None:
                if not args.index_name:
                    raise ValueError("new explicit MLTD version requires --index-name")
                release = merge_release(control, {
                    "version": version,
                    "scope": args.scope,
                    "asset_root": (
                        args.asset_root
                        or args.asset_root_template.format(version=version)
                    ).rstrip("/"),
                    "index_name": args.index_name,
                    "source": "explicit",
                })
        else:
            control, release = discover_latest(
                root, args.version_api, args.asset_root_template, args.scope, args.timeout
            )
        require_free_space(root, int(args.min_free_gib * 1024**3))
        archive_release(
            root, release, workers=args.workers, timeout=args.timeout,
            proxy=args.proxy, durable=args.durable,
        )
        verify_release(root, release)
        if args.materialize or args.activate:
            materialize_release(root, release)
        if args.activate:
            activate_release(root, release)
        refresh_status(root, control)
        atomic_json(root / CONTROL_NAME, control)
        return 0

    if args.command == "reconcile-latest":
        control, discovered = discover_latest(
            root, args.version_api, args.asset_root_template, args.scope, args.timeout
        )
        import_store(root, control)
        release = reconcile_release(
            root,
            control,
            control["releases"][str(discovered["version"])],
            workers=args.workers,
            timeout=args.timeout,
            proxy=args.proxy,
            durable=args.durable,
            minimum_free_bytes=int(args.min_free_gib * 1024**3),
        )

        if not args.no_activate:
            if not release.get("complete") or not release.get("materialized"):
                raise RuntimeError(
                    f"refusing to activate incomplete/unmaterialized version "
                    f"{release['version']}"
                )
            if control.get("active_version") != str(release["version"]):
                verify_release(root, release)
                activate_release(root, release)
                refresh_status(root, control)

        atomic_json(root / CONTROL_NAME, control)
        print(json.dumps({
            "version": str(release["version"]),
            "complete": bool(release.get("complete")),
            "materialized": bool(release.get("materialized")),
            "active_version": control.get("active_version"),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "reconcile-manifest":
        control = reconcile_manifest(
            root,
            control,
            workers=args.workers,
            timeout=args.timeout,
            proxy=args.proxy,
            durable=args.durable,
            minimum_free_bytes=int(args.min_free_gib * 1024**3),
            activate_latest=args.activate_latest,
        )
        print(json.dumps({
            "active_version": control.get("active_version"),
            "releases": {
                version: {
                    "retained": release.get("retained") is not False,
                    "complete": bool(release.get("complete")),
                    "materialized": bool(release.get("materialized")),
                }
                for version, release in sorted(control.get("releases", {}).items())
            },
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "watch":
        poll_seconds = max(60, int(args.poll_seconds))
        discovery_timeout = min(float(args.timeout), 15.0)
        while True:
            try:
                control, discovery_error = watch_cycle(
                    root,
                    version_api=args.version_api,
                    asset_root_template=args.asset_root_template,
                    scope=args.scope,
                    discovery_timeout=discovery_timeout,
                    workers=args.workers,
                    archive_timeout=args.timeout,
                    proxy=args.proxy,
                    durable=args.durable,
                    minimum_free_bytes=int(args.min_free_gib * 1024**3),
                    auto_activate=args.auto_activate == "1",
                )
                print(json.dumps({
                    "watch": "ok",
                    "discovery_error": discovery_error,
                    "active_version": control.get("active_version"),
                    "retained_versions": [
                        version
                        for version, release in sorted(control.get("releases", {}).items())
                        if release.get("retained") is not False
                    ],
                    "next_poll_seconds": poll_seconds,
                }, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(
                    json.dumps({
                        "watch": "error",
                        "stage": "reconcile",
                        "error": f"{type(exc).__name__}: {exc}",
                        "next_poll_seconds": poll_seconds,
                    }, ensure_ascii=False),
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(poll_seconds)

    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
