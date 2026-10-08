#!/usr/bin/env python3
"""Resume an existing versioned MLTD archive from a byte mirror.

The authoritative archive identity (version/scope/asset_root/manifest hash)
already stored in VersionedAssetStore is left untouched.  --mirror-root only
selects where missing payload bytes are fetched for this run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from server.versioned_asset_store import VersionedAssetStore  # noqa: E402
from tools.versioned_assets import Client  # noqa: E402


def write_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--scope", default="jp-android")
    ap.add_argument("--mirror-root", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--durable", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    store = VersionedAssetStore(args.root)
    identity = store.version(args.version, args.scope)
    if identity is None:
        raise SystemExit(
            f"archive identity is not registered: {args.version}/{args.scope}"
        )

    with store.db() as conn:
        missing = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT name
                FROM entries
                WHERE version=? AND scope=? AND sha256 IS NULL
                ORDER BY rowid
                """,
                (str(args.version), str(args.scope)),
            )
        ]
    if args.limit is not None:
        missing = missing[: max(0, int(args.limit))]

    client = Client(
        store,
        version=args.version,
        scope=args.scope,
        asset_root=args.mirror_root,
        proxy=None,
        timeout=args.timeout,
        durable=args.durable,
    )

    progress_path = (
        args.root
        / "versions"
        / str(args.version)
        / "mirror-resume-progress.json"
    )
    started = time.time()
    downloaded = failed = bytes_done = 0
    failures: list[dict] = []
    last_publish = 0.0

    def publish(*, running: bool, force: bool = False) -> None:
        nonlocal last_publish
        now = time.monotonic()
        if not force and now - last_publish < 1.0:
            return
        stats = store.stats(args.version, args.scope)
        write_progress(
            progress_path,
            {
                "schema_version": 1,
                "version": str(args.version),
                "scope": str(args.scope),
                "authoritative_asset_root": identity["asset_root"],
                "mirror_root": args.mirror_root.rstrip("/"),
                "selected_missing": len(missing),
                "downloaded": downloaded,
                "failed": failed,
                "downloaded_bytes": bytes_done,
                "registered": stats["registered"],
                "mapped": stats["mapped"],
                "missing": stats["missing"],
                "running": running,
                "elapsed_seconds": round(time.time() - started, 3),
                "failures": failures[:100],
            },
        )
        last_publish = now

    publish(running=True, force=True)
    if missing:
        with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as pool:
            futures = {
                pool.submit(client.fetch, name, force=True): name for name in missing
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                    downloaded += 1
                    bytes_done += int(result.get("size") or 0)
                    if args.verbose:
                        print(
                            f"{downloaded:6d}/{len(missing)} "
                            f"{int(result.get('size') or 0):12d} {name}",
                            flush=True,
                        )
                except Exception as exc:
                    failed += 1
                    failures.append({"name": name, "error": str(exc)})
                    print(f"FAILED {name}: {exc}", file=sys.stderr, flush=True)
                publish(running=True)

    stats = store.stats(args.version, args.scope)
    complete = failed == 0 and stats["missing"] == 0
    store.mark_complete(args.version, args.scope, complete)
    publish(running=False, force=True)

    report = {
        "status": "complete" if complete else ("partial" if downloaded else "no-progress"),
        "version": str(args.version),
        "scope": str(args.scope),
        "authoritative_asset_root": identity["asset_root"],
        "mirror_root": args.mirror_root.rstrip("/"),
        "selected_missing": len(missing),
        "downloaded": downloaded,
        "failed": failed,
        "downloaded_bytes": bytes_done,
        "elapsed_seconds": round(time.time() - started, 3),
        "stats": stats,
        "progress": str(progress_path),
        "failures": failures[:100],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
