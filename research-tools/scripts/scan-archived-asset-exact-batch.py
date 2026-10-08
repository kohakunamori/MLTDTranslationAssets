#!/usr/bin/env python3
"""Scan archived MLTD asset bundles for exact update-batch provenance.

The frozen manifest defines logical existence, while the local archive can be
incomplete. Raw bytes are used only as a cheap prefix prefilter; exact batch
membership is accepted only after UnityPy reads AssetBundle.m_Container.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
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
    p.add_argument("--batch", required=True)
    p.add_argument("--terms", nargs="*", default=[])
    p.add_argument("--version", default="1077100")
    p.add_argument("--scope", default="jp-android")
    p.add_argument("--output", type=Path, required=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    raw = msgpack.unpackb(args.asset_index.read_bytes(), raw=False, strict_map_key=False)
    index: dict[str, Any] = raw[0]

    terms = tuple(t.lower() for t in args.terms if t)
    logicals = sorted(
        k for k in index
        if isinstance(k, str)
        and (not terms or any(t in k.lower() for t in terms))
    )

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

    # Unity bundles commonly preserve only an initial batch fragment contiguously
    # in raw bytes. YYYYMM_<2 digits> is long enough to be selective here.
    raw_prefix = args.batch[:9].encode("utf-8")

    stats = Counter()
    hits: list[dict[str, Any]] = []

    for logical in logicals:
        rec = index.get(logical)
        if not isinstance(rec, (list, tuple)) or len(rec) < 2:
            stats["bad_manifest_record"] += 1
            continue
        physical = str(rec[1])
        sha = archived.get(physical)
        if not sha:
            stats["not_archived"] += 1
            continue
        path = args.archive_root / "objects" / sha[:2] / sha
        if not path.is_file():
            stats["missing_object"] += 1
            continue

        stats["archived_scanned"] += 1
        data = path.read_bytes()
        if raw_prefix not in data:
            continue
        stats["raw_prefix_hits"] += 1

        try:
            env = UnityPy.load(str(path))
        except Exception:
            stats["unity_load_error"] += 1
            continue

        containers: set[str] = set()
        object_types = Counter()
        object_names: set[str] = set()
        structured: list[dict[str, Any]] = []

        exact = False
        for obj in env.objects:
            object_types[obj.type.name] += 1
            if obj.type.name != "AssetBundle":
                continue
            try:
                bundle = obj.read()
                for cp in (getattr(bundle, "m_Container", {}) or {}).keys():
                    text = str(cp)
                    m = BATCH_RE.search(text)
                    if m and m.group(1) == args.batch:
                        exact = True
                        containers.add(text)
            except Exception:
                continue

        if not exact:
            continue

        stats["exact_hits"] += 1
        for obj in env.objects:
            typ = obj.type.name
            if typ not in {
                "TextAsset", "MonoBehaviour", "GameObject", "Sprite", "Texture2D",
                "Material", "Mesh", "AnimationClip", "AudioClip", "AssetBundle",
            }:
                continue
            row: dict[str, Any] = {"type": typ, "path_id": int(obj.path_id)}
            try:
                parsed = obj.read()
                name = getattr(parsed, "m_Name", None) or getattr(parsed, "name", None)
                if name:
                    row["name"] = str(name)
                    object_names.add(str(name))
                if typ == "TextAsset":
                    script = getattr(parsed, "m_Script", b"")
                    if isinstance(script, str):
                        script = script.encode("utf-8", errors="replace")
                    row["bytes"] = len(script)
                    try:
                        row["text_preview"] = script.decode("utf-8")[:8192]
                    except Exception:
                        pass
                elif typ == "MonoBehaviour":
                    try:
                        tree = obj.read_typetree()
                    except Exception:
                        tree = None
                    if isinstance(tree, dict):
                        shallow: dict[str, Any] = {}
                        for key, value in tree.items():
                            if isinstance(value, (str, int, float, bool)) or value is None:
                                shallow[key] = value
                            elif (
                                isinstance(value, list)
                                and len(value) <= 128
                                and all(
                                    isinstance(x, (str, int, float, bool, type(None)))
                                    for x in value
                                )
                            ):
                                shallow[key] = value
                        if shallow:
                            row["shallow_typetree"] = shallow
            except Exception as exc:
                row["read_error"] = type(exc).__name__

            if typ in {"TextAsset", "MonoBehaviour"}:
                structured.append(row)

        hits.append({
            "logical": logical,
            "physical": physical,
            "archive_sha256": sha,
            "containers": sorted(containers),
            "object_types": dict(sorted(object_types.items())),
            "object_names": sorted(object_names),
            "structured_objects": structured,
        })

    hits.sort(key=lambda x: x["logical"])
    result = {
        "schema": "mltd-current-archived-asset-exact-batch-scan-v1",
        "version": args.version,
        "scope": args.scope,
        "batch": args.batch,
        "terms": list(terms),
        "raw_prefilter": raw_prefix.decode("utf-8", errors="replace"),
        "inputs": {
            "asset_index": str(args.asset_index),
            "asset_index_sha256": sha256_path(args.asset_index),
            "archive_index": str(args.archive_root / "index.sqlite3"),
            "archive_index_sha256": sha256_path(args.archive_root / "index.sqlite3"),
        },
        "counts": {
            "candidate_logicals": len(logicals),
            **dict(sorted(stats.items())),
            "structured_hits": sum(1 for x in hits if x["structured_objects"]),
        },
        "hits": hits,
        "limitations": [
            "The local archive is incomplete; no-hit claims apply only to archived objects scanned.",
            "Raw prefix matching is only a prefilter; exact membership requires AssetBundle.m_Container.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "output": str(args.output),
        "batch": args.batch,
        "candidate_logicals": len(logicals),
        "archived_scanned": stats["archived_scanned"],
        "raw_prefix_hits": stats["raw_prefix_hits"],
        "exact_hits": stats["exact_hits"],
        "structured_hits": result["counts"]["structured_hits"],
    }))
    for row in hits:
        print(
            row["logical"],
            "| structured=", len(row["structured_objects"]),
            "| types=", row["object_types"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
