#!/usr/bin/env python3
"""Cache every JP GTX referenced by an MLTD asset index.

This is intentionally narrower than tools/cache_assets.py: the asset index maps
logical GTX names to opaque remote object names, while the generic manifest
cache works on remote object names directly.  The snapshot produced here keeps
both identities so a later version can be diffed without guessing filenames.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import msgpack

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from server.asset_archive import AssetArchive
from tools.cache_assets import Client


def load_rows(index_path: Path) -> list[dict]:
    raw = msgpack.unpackb(index_path.read_bytes(), raw=False, strict_map_key=False)
    index = raw[0]
    rows: list[dict] = []
    for logical, value in index.items():
        if not str(logical).lower().endswith("_jp.gtx.unity3d"):
            continue
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            raise ValueError(f"unexpected index row for {logical!r}: {value!r}")
        rows.append(
            {
                "logical": str(logical),
                "catalog_hash": str(value[0]),
                "remote": str(value[1]),
                "declared_size": int(value[2]),
            }
        )
    rows.sort(key=lambda row: row["logical"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset-index", type=Path, required=True)
    ap.add_argument("--archive-root", type=Path, required=True)
    ap.add_argument("--scope", default="jp-android")
    ap.add_argument("--upstream-root", required=True)
    ap.add_argument("--proxy")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--snapshot", type=Path, required=True)
    args = ap.parse_args()

    rows = load_rows(args.asset_index)
    archive = AssetArchive(args.archive_root)
    client = Client(
        archive,
        scope=args.scope,
        root_url=args.upstream_root,
        proxy=args.proxy,
        timeout=args.timeout,
        durable=False,
        verify_existing=False,
    )
    started = time.time()
    failures: list[dict] = []
    downloaded = cached = 0

    def fetch(row: dict) -> dict:
        result = client.fetch(row["remote"])
        path = archive.object_path(args.scope, row["remote"])
        actual = path.stat().st_size
        if actual != row["declared_size"]:
            raise ValueError(
                f"size mismatch {row['logical']}: expected={row['declared_size']} actual={actual}"
            )
        with path.open("rb") as handle:
            if handle.read(7) != b"UnityFS":
                raise ValueError(f"not UnityFS: {row['logical']}")
        return {**row, "status": result["status"], "path": str(path)}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
                if result["status"] == "downloaded":
                    downloaded += 1
                else:
                    cached += 1
            except Exception as exc:
                failures.append({"logical": row["logical"], "remote": row["remote"], "error": str(exc)})

    snapshot = {
        "schema_version": 1,
        "asset_index": str(args.asset_index),
        "scope": args.scope,
        "upstream_root": args.upstream_root.rstrip("/"),
        "selected": len(rows),
        "declared_bytes": sum(row["declared_size"] for row in rows),
        "downloaded": downloaded,
        "cached": cached,
        "failed": len(failures),
        "complete": not failures and downloaded + cached == len(rows),
        "duration_seconds": round(time.time() - started, 3),
        "failures": failures,
        "objects": rows,
    }
    args.snapshot.parent.mkdir(parents=True, exist_ok=True)
    args.snapshot.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in snapshot.items() if key not in {"objects", "failures"}},
            ensure_ascii=False,
            indent=2,
        )
    )
    if failures:
        print(json.dumps({"failures_first": failures[:10]}, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
