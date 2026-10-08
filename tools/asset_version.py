#!/usr/bin/env python3
"""Convenience CLI for MLTD local/remote asset-version management."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
CONTROLLER = REPO / "tools" / "archive_controller.py"
MATERIALIZER = REPO / "tools" / "materialize_versioned_assets.py"
REMOTE_CATALOG = "https://api.matsurihi.me/api/mltd/v2/version/assets"
ASSET_ROOT = "https://td-assets.bn765.com/{version}/production/2018/Android"


def fetch_json(url: str, timeout: float):
    req = Request(url, headers={"User-Agent": "mltd-asset-version-cli/1"})
    with urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        return json.loads(response.read().decode("utf-8"))


def load_control(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.is_file():
        return {"active_version": None, "releases": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("game") not in (None, "mltd"):
        raise ValueError(f"unexpected manifest game: {data.get('game')!r}")
    data.setdefault("active_version", None)
    data.setdefault("releases", {})
    return data


def version_key(value: str):
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def state_of(release: dict | None, active: str | None) -> str:
    if release is None:
        return "remote-only"
    bits = []
    if release.get("complete"):
        bits.append("complete")
    else:
        bits.append("incomplete")
    if release.get("materialized"):
        bits.append("materialized")
    if active == str(release.get("version")):
        bits.append("current")
    return ",".join(bits)


def human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TiB"


def progress_of(root: Path, version: str, scope: str = "jp-android") -> dict:
    progress_path = root / "versions" / str(version) / "archive-progress.json"
    if progress_path.is_file():
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        registered = int(data.get("total_objects") or 0)
        mapped = int(data.get("successful_objects") or 0)
        logical_bytes = int(data.get("processed_bytes") or 0)
        return {
            "version": str(version),
            "scope": str(data.get("scope") or scope),
            "registered": registered,
            "mapped": mapped,
            "missing": int(data.get("missing_objects") or max(0, registered - mapped)),
            "logical_bytes": logical_bytes,
            "percent": float(
                data.get("percent")
                if data.get("percent") is not None
                else ((mapped * 100.0 / registered) if registered else 0.0)
            ),
            "running": bool(data.get("running")),
            "updated_at": data.get("updated_at"),
            "available": True,
            "source": "live-progress",
        }

    control = load_control(root)
    release = control.get("releases", {}).get(str(version), {})
    observed = release.get("store") or {}
    registered = int(observed.get("registered") or 0)
    mapped = int(observed.get("mapped") or 0)
    logical_bytes = int(observed.get("logical_bytes") or 0)
    return {
        "version": str(version),
        "scope": scope,
        "registered": registered,
        "mapped": mapped,
        "missing": max(0, registered - mapped),
        "logical_bytes": logical_bytes,
        "percent": (mapped * 100.0 / registered) if registered else 0.0,
        "running": False,
        "updated_at": control.get("updated_at"),
        "available": bool(registered),
        "source": "manifest-observed" if registered else "unavailable",
    }


def progress_label(progress: dict | None, complete: bool = False) -> str:
    if complete:
        return "100.0%"
    if not progress or not progress.get("available"):
        return "-"
    return (
        f"{progress['percent']:.1f}% "
        f"({progress['mapped']}/{progress['registered']})"
    )


def print_rows(rows: list[dict], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("(none)")
        return
    widths = {
        key: max(len(key.upper()), *(len(str(r.get(key, ""))) for r in rows))
        for key in ("version", "updated", "state", "progress", "index")
    }
    print(
        f"{'VERSION':<{widths['version']}}  "
        f"{'UPDATED':<{widths['updated']}}  "
        f"{'STATE':<{widths['state']}}  "
        f"{'PROGRESS':<{widths['progress']}}  "
        f"{'INDEX':<{widths['index']}}"
    )
    for r in rows:
        print(
            f"{str(r.get('version','')):<{widths['version']}}  "
            f"{str(r.get('updated','')):<{widths['updated']}}  "
            f"{str(r.get('state','')):<{widths['state']}}  "
            f"{str(r.get('progress','')):<{widths['progress']}}  "
            f"{str(r.get('index','')):<{widths['index']}}"
        )


def remote_catalog(timeout: float) -> list[dict]:
    data = fetch_json(REMOTE_CATALOG, timeout)
    if not isinstance(data, list):
        raise ValueError("MLTD remote asset catalog is not an array")
    out = []
    for item in data:
        if not isinstance(item, dict) or "version" not in item:
            continue
        index_name = str(item.get("indexName") or item.get("index_name") or "")
        if not index_name:
            continue
        out.append({
            "version": str(item["version"]),
            "updated": str(item.get("updatedAt") or item.get("updateTime") or ""),
            "index": index_name,
        })
    return sorted(out, key=lambda x: version_key(x["version"]), reverse=True)


def controller_cmd(root: Path, args: list[str], dry_run: bool = False) -> None:
    cmd = [sys.executable, str(CONTROLLER), "--root", str(root), *args]
    print("+", " ".join(cmd), flush=True)
    if not dry_run:
        subprocess.run(cmd, check=True)


def cmd_list(args) -> int:
    control = load_control(args.root)
    active = control.get("active_version")
    rows = []
    for version, release in sorted(
        control.get("releases", {}).items(), key=lambda kv: version_key(kv[0]), reverse=True
    ):
        progress = progress_of(args.root, version)
        rows.append({
            "version": version,
            "updated": release.get("resource_update_time") or "",
            "state": state_of(release, active),
            "progress": progress_label(progress, bool(release.get("complete"))),
            "index": release.get("index_name") or "",
        })
    print_rows(rows, args.json)
    return 0


def cmd_current(args) -> int:
    control = load_control(args.root)
    active = control.get("active_version")
    current = args.root / "current"
    target = None
    if current.is_symlink():
        try:
            target = str(current.resolve(strict=True))
        except OSError:
            target = str(current.readlink())
    payload = {"active_version": active, "current_target": target}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(active or "(none)")
        if target:
            print(target)
    return 0


def cmd_progress(args) -> int:
    control = load_control(args.root)
    versions = args.versions or sorted(
        control.get("releases", {}),
        key=version_key,
        reverse=True,
    )
    reports = []
    for version in versions:
        report = progress_of(args.root, version)
        release = control.get("releases", {}).get(str(version), {})
        report["complete"] = bool(release.get("complete"))
        report["materialized"] = bool(release.get("materialized"))
        reports.append(report)
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for report in reports:
        print(
            f"{report['version']} {report['percent']:.2f}% "
            f"objects={report['mapped']}/{report['registered']} "
            f"missing={report['missing']} "
            f"mapped_bytes={human_bytes(report['logical_bytes'])} "
            f"complete={str(report['complete']).lower()} "
            f"materialized={str(report['materialized']).lower()}"
        )
    return 0


def cmd_switch(args) -> int:
    controller_cmd(args.root, ["activate", "--version", args.version])
    return 0


def cmd_remote_list(args) -> int:
    control = load_control(args.root)
    active = control.get("active_version")
    local = control.get("releases", {})
    rows = []
    for item in remote_catalog(args.timeout):
        release = local.get(item["version"])
        if args.missing_only and release and release.get("complete") and release.get("materialized"):
            continue
        rows.append({
            **item,
            "state": state_of(release, active),
            "progress": (
                progress_label(progress_of(args.root, item["version"]), bool(release and release.get("complete")))
                if release else "-"
            ),
        })
        if args.limit and len(rows) >= args.limit:
            break
    print_rows(rows, args.json)
    return 0


def select_remote(args) -> list[dict]:
    catalog = remote_catalog(args.timeout)
    by_version = {item["version"]: item for item in catalog}
    control = load_control(args.root)
    local = control.get("releases", {})
    if args.all_missing:
        selected = [
            item for item in catalog
            if not (
                local.get(item["version"], {}).get("complete")
                and local.get(item["version"], {}).get("materialized")
            )
        ]
        if args.limit:
            selected = selected[: args.limit]
        return selected
    if not args.versions:
        raise ValueError("remote pull requires VERSION ... or --all-missing")
    selected = []
    for version in args.versions:
        item = by_version.get(str(version))
        if item is None:
            raise ValueError(f"MLTD asset version not present in remote catalog: {version}")
        selected.append(item)
    return selected


def cmd_remote_pull(args) -> int:
    selected = select_remote(args)
    control = load_control(args.root)
    for item in selected:
        version = item["version"]
        release = control.get("releases", {}).get(version)
        if release and release.get("complete") and release.get("materialized"):
            print(f"{version}: already complete/materialized; skip")
            if args.activate:
                controller_cmd(args.root, ["activate", "--version", version], args.dry_run)
            continue
        command = [
            "--timeout", str(args.timeout),
            "archive",
            "--version", version,
            "--index-name", item["index"],
            "--asset-root", ASSET_ROOT.format(version=version),
            "--workers", str(args.workers),
            "--min-free-gib", str(args.min_free_gib),
            "--materialize",
        ]
        if args.proxy:
            command += ["--proxy", args.proxy]
        if args.activate:
            command += ["--activate"]
        controller_cmd(args.root, command, args.dry_run)
        if not args.dry_run:
            control = load_control(args.root)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MLTD asset version manager")
    ap.add_argument("--root", type=Path, default=Path("/data"))
    ap.add_argument("--timeout", type=float, default=30.0)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list locally registered versions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("current", help="show active/current version")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_current)

    p = sub.add_parser("progress", help="show download progress for local versions")
    p.add_argument("versions", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("switch", help="switch current to a complete materialized version")
    p.add_argument("version")
    p.set_defaults(func=cmd_switch)

    remote = sub.add_parser("remote", help="inspect or acquire remote versions")
    rsub = remote.add_subparsers(dest="remote_command", required=True)

    p = rsub.add_parser("list", help="list versions available from Matsurihi")
    p.add_argument("--missing-only", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_remote_list)

    p = rsub.add_parser("pull", help="fully archive/materialize selected remote versions")
    p.add_argument("versions", nargs="*")
    p.add_argument("--all-missing", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=256)
    p.add_argument("--min-free-gib", type=float, default=80.0)
    p.add_argument("--proxy")
    p.add_argument("--activate", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_remote_pull)

    args = ap.parse_args()
    args.root = args.root.resolve()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
