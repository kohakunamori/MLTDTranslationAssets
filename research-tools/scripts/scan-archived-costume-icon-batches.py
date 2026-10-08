#!/usr/bin/env python3
"""Scan archived MLTD costume-icon bundles for an update-batch prefix.

The frozen asset manifest is authoritative for logical existence. The local
archive may be incomplete, so this tool only makes positive claims for archived
objects whose raw AssetBundle bytes expose /update/<batch>/ paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import msgpack
import UnityPy


BATCH_RE = re.compile(r"/update/([^/]+)/")


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--asset-index", type=Path, required=True)
    p.add_argument("--archive-root", type=Path, required=True)
    p.add_argument("--batch-prefix", required=True)
    p.add_argument("--version", default="1077100")
    p.add_argument("--scope", default="jp-android")
    p.add_argument("--output", type=Path, required=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    raw = msgpack.unpackb(args.asset_index.read_bytes(), raw=False, strict_map_key=False)
    index: dict[str, Any] = raw[0]

    con = sqlite3.connect(args.archive_root / "index.sqlite3")
    archived = {
        str(name): str(sha)
        for name, sha in con.execute(
            "select name,sha256 from entries "
            "where version=? and scope=? and status=200 and sha256 is not null",
            (args.version, args.scope),
        )
    }
    con.close()

    logicals = sorted(
        k for k in index
        if isinstance(k, str)
        and k.startswith("costume_icon_")
        and k.endswith(".unity3d")
    )

    scanned = 0
    raw_prefix_hits = 0
    unity_load_errors = 0
    missing_archive = 0
    missing_object = 0
    hits: list[dict[str, Any]] = []
    prefix_bytes = args.batch_prefix.encode("utf-8")

    for logical in logicals:
        rec = index[logical]
        if not isinstance(rec, (list, tuple)) or len(rec) < 2:
            continue
        physical = str(rec[1])
        sha = archived.get(physical)
        if not sha:
            missing_archive += 1
            continue
        path = args.archive_root / "objects" / sha[:2] / sha
        if not path.is_file():
            missing_object += 1
            continue

        scanned += 1
        data = path.read_bytes()
        if prefix_bytes not in data:
            continue
        raw_prefix_hits += 1

        try:
            env = UnityPy.load(str(path))
        except Exception:
            unity_load_errors += 1
            continue

        batches: set[str] = set()
        containers: set[str] = set()
        for obj in env.objects:
            if obj.type.name != "AssetBundle":
                continue
            try:
                bundle = obj.read()
                for cp in (getattr(bundle, "m_Container", {}) or {}).keys():
                    text = str(cp)
                    for match in BATCH_RE.finditer(text):
                        batch = match.group(1)
                        if batch.startswith(args.batch_prefix):
                            batches.add(batch)
                            containers.add(text)
            except Exception:
                continue

        if batches:
            hits.append({
                "logical": logical,
                "physical": physical,
                "archive_sha256": sha,
                "batches": sorted(batches),
                "containers": sorted(containers),
            })

    result = {
        "schema": "mltd-current-archived-costume-icon-batch-scan-v1",
        "version": args.version,
        "scope": args.scope,
        "batch_prefix": args.batch_prefix,
        "inputs": {
            "asset_index": str(args.asset_index),
            "asset_index_sha256": sha256_path(args.asset_index),
            "archive_index": str(args.archive_root / "index.sqlite3"),
            "archive_index_sha256": sha256_path(args.archive_root / "index.sqlite3"),
        },
        "counts": {
            "manifest_costume_icon_logicals": len(logicals),
            "archived_objects_scanned": scanned,
            "raw_prefix_hits": raw_prefix_hits,
            "unity_load_errors_after_prefix": unity_load_errors,
            "manifest_logicals_without_archived_object": missing_archive,
            "archive_index_rows_without_object_file": missing_object,
            "positive_hits": len(hits),
        },
        "hits": hits,
        "limitations": [
            "The local archive is incomplete; absence from hits does not prove a manifest logical never belonged to the requested batch.",
            "Positive hits are source-backed by raw AssetBundle /update/<batch>/ container paths.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "manifest_icons": len(logicals),
        "scanned": scanned,
        "hits": len(hits),
        "batch_prefix": args.batch_prefix,
    }))
    for row in hits:
        print(row["logical"], "|", ",".join(row["batches"]), "|", row["archive_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
