#!/usr/bin/env python3
"""Incrementally scan archived Unity bundles into current-asset-relationships.sqlite.

The scanner is resumable and evidence-preserving.  It records:
- object type counts;
- AssetBundle.m_Container source paths;
- AssetBundle.m_Dependencies;
- exact/heuristic resolution of dependency/container names back to manifest logicals.

It does not deserialize arbitrary MonoBehaviour business payloads.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import multiprocessing as mp
import os
import sqlite3
from pathlib import Path

import UnityPy


KNOWN_SOURCE_SUFFIXES = (
    ".json",
    ".unity",
    ".prefab",
    ".asset",
    ".mat",
    ".controller",
    ".anim",
    ".png",
    ".jpg",
    ".jpeg",
    ".tga",
    ".psd",
    ".txt",
    ".bytes",
    ".gtx",
    ".imo",
    ".acb",
    ".awb",
    ".mp4",
)


def candidate_logicals(value: str) -> list[tuple[str, str]]:
    """Return candidate manifest logical names from a dependency/container path."""
    value = str(value or "").replace("\\", "/")
    base = value.rsplit("/", 1)[-1]
    out: list[tuple[str, str]] = []
    if not base:
        return out

    def add(name: str, rule: str):
        if name and (name, rule) not in out:
            out.append((name, rule))

    add(base, "basename-exact")
    if not base.endswith(".unity3d"):
        add(base + ".unity3d", "basename+unity3d")

    low = base.lower()
    for suffix in KNOWN_SOURCE_SUFFIXES:
        if low.endswith(suffix):
            stem = base[: -len(suffix)]
            add(stem + ".unity3d", f"strip{suffix}+unity3d")
            # Some logicals preserve the source extension, especially json/gtx/png.
            add(base + ".unity3d", f"preserve{suffix}+unity3d")
            break
    return out


def _serialized_file_record(assets_file) -> dict:
    externals = []
    for idx, ext in enumerate(getattr(assets_file, "externals", None) or [], 1):
        guid = getattr(ext, "guid", None)
        try:
            guid_hex = bytes(guid).hex() if guid is not None else ""
        except Exception:
            guid_hex = ""
        externals.append(
            {
                "file_id": idx,
                "path": str(getattr(ext, "path", "") or ""),
                "guid": guid_hex,
                "type": int(getattr(ext, "type", 0) or 0),
            }
        )
    return {
        "name": str(getattr(assets_file, "name", "") or ""),
        "unity_version": str(getattr(assets_file, "unity_version", "") or ""),
        "externals": externals,
    }


def _container_items(raw):
    if raw is None:
        return "none", []
    if isinstance(raw, dict):
        return "dict", list(raw.items())
    if isinstance(raw, (list, tuple)):
        return type(raw).__name__, list(raw)
    return type(raw).__name__, []


def _dependency_name(dep) -> str:
    if isinstance(dep, str):
        return dep
    if isinstance(dep, dict):
        for key in ("m_Name", "name", "first", "path", "m_PathName"):
            if dep.get(key):
                return str(dep[key])
    if isinstance(dep, (list, tuple)) and len(dep) == 2:
        for value in dep:
            if isinstance(value, str) and value:
                return value
    return ""


def _empty_result(logical: str, status: str, error: str, now: str) -> dict:
    return {
        "logical": logical,
        "status": status,
        "error": error,
        "object_count": 0,
        "types": {},
        "bundle_name": "",
        "streamed_scene": None,
        "assetbundle_object_count": 0,
        "serialized_file_count": 0,
        "container_raw_count": 0,
        "container_skipped_count": 0,
        "dependency_raw_count": 0,
        "dependency_skipped_count": 0,
        "serialized_files": [],
        "objects": [],
        "assetbundles": [],
        "containers": [],
        "dependencies": [],
        "scanned_at": now,
    }


def scan_one(job):
    logical, sha256, archive_root = job
    p = Path(archive_root) / "objects" / sha256[:2] / sha256
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not p.is_file():
        return _empty_result(logical, "missing-object", str(p), now)
    try:
        env = UnityPy.load(str(p))
        type_counts = collections.Counter()
        serialized_files: dict[str, dict] = {}
        objects = []
        assetbundles = []
        containers = []
        dependencies = []
        first_bundle_name = ""
        first_streamed_scene = None
        container_raw_total = 0
        container_skipped_total = 0
        dependency_raw_total = 0
        dependency_skipped_total = 0

        for obj in env.objects:
            type_name = obj.type.name
            type_counts[type_name] += 1
            assets_file = obj.assets_file
            serialized_file_name = str(getattr(assets_file, "name", "") or "")
            if serialized_file_name not in serialized_files:
                serialized_files[serialized_file_name] = _serialized_file_record(assets_file)
            objects.append(
                {
                    "serialized_file_name": serialized_file_name,
                    "path_id": int(obj.path_id),
                    "type_id": int(getattr(obj, "type_id", 0) or 0),
                    "class_id": int(getattr(obj, "class_id", 0) or 0),
                    "type_name": type_name,
                    "byte_size": int(getattr(obj, "byte_size", 0) or 0),
                }
            )
            if type_name != "AssetBundle":
                continue

            tree = obj.read_typetree()
            bundle_name = str(tree.get("m_AssetBundleName") or tree.get("m_Name") or "")
            streamed_scene = (
                int(bool(tree.get("m_IsStreamedSceneAssetBundle")))
                if "m_IsStreamedSceneAssetBundle" in tree
                else None
            )
            if not first_bundle_name:
                first_bundle_name = bundle_name
            if first_streamed_scene is None and streamed_scene is not None:
                first_streamed_scene = streamed_scene

            container_shape, container_items = _container_items(tree.get("m_Container"))
            raw_container_count = len(container_items)
            skipped_container_count = 0
            for ordinal, item in enumerate(container_items):
                try:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        path, info = item
                    elif isinstance(item, dict):
                        path = item.get("first") or item.get("key") or ""
                        info = item.get("second") or item.get("value") or {}
                    else:
                        skipped_container_count += 1
                        continue
                    if not isinstance(info, dict):
                        skipped_container_count += 1
                        continue
                    asset = info.get("asset") or {}
                    if not isinstance(asset, dict):
                        asset = {}
                    containers.append(
                        {
                            "serialized_file_name": serialized_file_name,
                            "assetbundle_path_id": int(obj.path_id),
                            "ordinal": ordinal,
                            "path": str(path or ""),
                            "preload_index": info.get("preloadIndex"),
                            "preload_size": info.get("preloadSize"),
                            "asset_file_id": asset.get("m_FileID"),
                            "asset_path_id": asset.get("m_PathID"),
                        }
                    )
                except Exception:
                    skipped_container_count += 1

            raw_dependencies = tree.get("m_Dependencies") or []
            if isinstance(raw_dependencies, dict):
                dependency_items = list(raw_dependencies.values())
            elif isinstance(raw_dependencies, (list, tuple)):
                dependency_items = list(raw_dependencies)
            else:
                dependency_items = []
            raw_dependency_count = len(dependency_items)
            skipped_dependency_count = 0
            for ordinal, dep in enumerate(dependency_items):
                dep_name = _dependency_name(dep)
                if not dep_name:
                    skipped_dependency_count += 1
                    continue
                dependencies.append(
                    {
                        "serialized_file_name": serialized_file_name,
                        "assetbundle_path_id": int(obj.path_id),
                        "ordinal": ordinal,
                        "name": dep_name,
                    }
                )

            container_raw_total += raw_container_count
            container_skipped_total += skipped_container_count
            dependency_raw_total += raw_dependency_count
            dependency_skipped_total += skipped_dependency_count
            assetbundles.append(
                {
                    "serialized_file_name": serialized_file_name,
                    "assetbundle_path_id": int(obj.path_id),
                    "bundle_name": bundle_name,
                    "streamed_scene": streamed_scene,
                    "container_shape": container_shape,
                    "container_raw_count": raw_container_count,
                    "container_skipped_count": skipped_container_count,
                    "dependency_raw_count": raw_dependency_count,
                    "dependency_skipped_count": skipped_dependency_count,
                }
            )

        result = _empty_result(logical, "ok", "", now)
        result.update(
            {
                "object_count": len(objects),
                "types": dict(type_counts),
                "bundle_name": first_bundle_name,
                "streamed_scene": first_streamed_scene,
                "assetbundle_object_count": len(assetbundles),
                "serialized_file_count": len(serialized_files),
                "container_raw_count": container_raw_total,
                "container_skipped_count": container_skipped_total,
                "dependency_raw_count": dependency_raw_total,
                "dependency_skipped_count": dependency_skipped_total,
                "serialized_files": list(serialized_files.values()),
                "objects": objects,
                "assetbundles": assetbundles,
                "containers": containers,
                "dependencies": dependencies,
            }
        )
        return result
    except Exception as exc:
        return _empty_result(logical, "error", f"{type(exc).__name__}: {exc}"[:1000], now)


def ensure_schema(con: sqlite3.Connection):
    meta = dict(con.execute("SELECT key,value FROM metadata"))
    if meta.get("schema") != "mltd-current-asset-relationships-v2":
        raise RuntimeError(
            "deep scanner requires mltd-current-asset-relationships-v2; "
            f"found {meta.get('schema')!r}"
        )
    if meta.get("build_complete") != "1":
        raise RuntimeError("relationship DB build_complete!=1; refusing to scan partial DB")
    required = {
        "bundle_scan",
        "bundle_serialized_file",
        "bundle_external_ref",
        "bundle_object",
        "bundle_assetbundle",
        "bundle_container",
        "bundle_dependency",
        "bundle_name_relation",
        "deep_scan_metadata",
    }
    present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - present)
    if missing:
        raise RuntimeError("relationship DB missing v2 deep-scan tables: " + ", ".join(missing))


def resolve_value(value: str, logical_set: set[str]) -> tuple[str | None, str]:
    for candidate, rule in candidate_logicals(value):
        if candidate in logical_set:
            return candidate, rule
    return None, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("build/current-asset-relationships.sqlite"))
    ap.add_argument(
        "--archive-root",
        type=Path,
        default=Path("work/full-asset-archive-1077100"),
    )
    ap.add_argument("--workers", type=int, default=max(1, min(12, (os.cpu_count() or 4) - 2)))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tags", default="", help="comma-separated asset_tag filter")
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--chunk-size", type=int, default=32)
    args = ap.parse_args()

    if not args.db.is_file():
        raise FileNotFoundError(args.db)
    if not args.archive_root.is_dir():
        raise FileNotFoundError(args.archive_root)

    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(con)
    logical_set = {r[0] for r in con.execute("SELECT logical_name FROM asset")}

    tags = sorted({x.strip() for x in args.tags.split(",") if x.strip()})
    params = []
    where = ["a.archived=1"]
    if not args.retry_errors:
        where.append("(b.logical_name IS NULL)")
    else:
        where.append("(b.logical_name IS NULL OR b.scan_status!='ok')")
    joins = "LEFT JOIN bundle_scan b ON b.logical_name=a.logical_name"
    if tags:
        joins += " JOIN asset_tag t ON t.logical_name=a.logical_name"
        where.append("t.tag IN (" + ",".join("?" for _ in tags) + ")")
        params.extend(tags)
    sql = (
        "SELECT DISTINCT a.logical_name,a.archive_sha256 "
        "FROM asset a " + joins + " WHERE " + " AND ".join(where) +
        " ORDER BY a.logical_name"
    )
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    jobs = [(r[0], r[1], str(args.archive_root)) for r in con.execute(sql, params)]
    print(
        json.dumps(
            {
                "jobs": len(jobs),
                "workers": args.workers,
                "tags": tags,
                "retry_errors": args.retry_errors,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not jobs:
        con.close()
        return 0

    counters = collections.Counter()
    start = dt.datetime.now(dt.timezone.utc)
    with mp.Pool(processes=args.workers) as pool:
        for i, result in enumerate(
            pool.imap_unordered(scan_one, jobs, chunksize=max(1, args.chunk_size)),
            1,
        ):
            logical = result["logical"]
            con.execute(
                """INSERT INTO bundle_scan(
                       logical_name,scan_status,error,object_count,object_types_json,
                       assetbundle_name,streamed_scene,assetbundle_object_count,
                       serialized_file_count,container_raw_count,container_skipped_count,
                       dependency_raw_count,dependency_skipped_count,scanned_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(logical_name) DO UPDATE SET
                       scan_status=excluded.scan_status,
                       error=excluded.error,
                       object_count=excluded.object_count,
                       object_types_json=excluded.object_types_json,
                       assetbundle_name=excluded.assetbundle_name,
                       streamed_scene=excluded.streamed_scene,
                       assetbundle_object_count=excluded.assetbundle_object_count,
                       serialized_file_count=excluded.serialized_file_count,
                       container_raw_count=excluded.container_raw_count,
                       container_skipped_count=excluded.container_skipped_count,
                       dependency_raw_count=excluded.dependency_raw_count,
                       dependency_skipped_count=excluded.dependency_skipped_count,
                       scanned_at=excluded.scanned_at""",
                (
                    logical,
                    result["status"],
                    result["error"],
                    result["object_count"],
                    json.dumps(result["types"], sort_keys=True, separators=(",", ":")),
                    result["bundle_name"],
                    result["streamed_scene"],
                    result["assetbundle_object_count"],
                    result["serialized_file_count"],
                    result["container_raw_count"],
                    result["container_skipped_count"],
                    result["dependency_raw_count"],
                    result["dependency_skipped_count"],
                    result["scanned_at"],
                ),
            )
            con.execute("DELETE FROM bundle_serialized_file WHERE logical_name=?", (logical,))
            con.execute("DELETE FROM bundle_external_ref WHERE logical_name=?", (logical,))
            con.execute("DELETE FROM bundle_object WHERE logical_name=?", (logical,))
            con.execute("DELETE FROM bundle_assetbundle WHERE logical_name=?", (logical,))
            con.execute("DELETE FROM bundle_container WHERE logical_name=?", (logical,))
            con.execute("DELETE FROM bundle_dependency WHERE logical_name=?", (logical,))
            con.execute("DELETE FROM bundle_name_relation WHERE logical_name=?", (logical,))

            con.executemany(
                """INSERT INTO bundle_serialized_file(
                       logical_name,serialized_file_name,unity_version,external_count,externals_json
                   ) VALUES (?,?,?,?,?)""",
                [
                    (
                        logical,
                        row["name"],
                        row["unity_version"],
                        len(row["externals"]),
                        json.dumps(row["externals"], ensure_ascii=False, separators=(",", ":")),
                    )
                    for row in result["serialized_files"]
                ],
            )
            serialized_file_names = {row["name"] for row in result["serialized_files"]}
            external_rows = []
            for row in result["serialized_files"]:
                for ext in row["externals"]:
                    external_path = str(ext["path"] or "")
                    target_name = external_path.replace("\\", "/").rsplit("/", 1)[-1]
                    if target_name not in serialized_file_names:
                        target_name = None
                    external_rows.append(
                        (
                            logical,
                            row["name"],
                            ext["file_id"],
                            external_path,
                            ext["guid"],
                            ext["type"],
                            target_name,
                        )
                    )
            con.executemany(
                """INSERT INTO bundle_external_ref(
                       logical_name,serialized_file_name,file_id,external_path,guid,
                       external_type,target_serialized_file_name
                   ) VALUES (?,?,?,?,?,?,?)""",
                external_rows,
            )
            con.executemany(
                """INSERT INTO bundle_object(
                       logical_name,serialized_file_name,path_id,type_id,class_id,type_name,byte_size
                   ) VALUES (?,?,?,?,?,?,?)""",
                [
                    (
                        logical,
                        row["serialized_file_name"],
                        row["path_id"],
                        row["type_id"],
                        row["class_id"],
                        row["type_name"],
                        row["byte_size"],
                    )
                    for row in result["objects"]
                ],
            )
            for row in result["assetbundles"]:
                con.execute(
                    """INSERT INTO bundle_assetbundle(
                           logical_name,serialized_file_name,assetbundle_path_id,
                           observed_bundle_name,streamed_scene,container_shape,
                           container_raw_count,container_skipped_count,
                           dependency_raw_count,dependency_skipped_count
                       ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        logical,
                        row["serialized_file_name"],
                        row["assetbundle_path_id"],
                        row["bundle_name"],
                        row["streamed_scene"],
                        row["container_shape"],
                        row["container_raw_count"],
                        row["container_skipped_count"],
                        row["dependency_raw_count"],
                        row["dependency_skipped_count"],
                    ),
                )
                if row["bundle_name"]:
                    target, rule = resolve_value(row["bundle_name"], logical_set)
                    con.execute(
                        "INSERT OR REPLACE INTO bundle_name_relation VALUES (?,?,?,?)",
                        (logical, row["bundle_name"], target, rule),
                    )

            for row in result["containers"]:
                target, rule = resolve_value(row["path"], logical_set)
                con.execute(
                    """INSERT OR REPLACE INTO bundle_container(
                           logical_name,serialized_file_name,assetbundle_path_id,
                           container_ordinal,container_path,preload_index,preload_size,
                           asset_file_id,asset_path_id,resolved_logical_name,resolution_rule
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        logical,
                        row["serialized_file_name"],
                        row["assetbundle_path_id"],
                        row["ordinal"],
                        row["path"],
                        row["preload_index"],
                        row["preload_size"],
                        row["asset_file_id"],
                        row["asset_path_id"],
                        target,
                        rule,
                    ),
                )
            for row in result["dependencies"]:
                target, rule = resolve_value(row["name"], logical_set)
                con.execute(
                    """INSERT OR REPLACE INTO bundle_dependency(
                           logical_name,serialized_file_name,assetbundle_path_id,
                           dependency_ordinal,dependency_name,resolved_logical_name,resolution_rule
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        logical,
                        row["serialized_file_name"],
                        row["assetbundle_path_id"],
                        row["ordinal"],
                        row["name"],
                        target,
                        rule,
                    ),
                )
            counters[result["status"]] += 1
            if i % 500 == 0 or i == len(jobs):
                con.commit()
                elapsed = (dt.datetime.now(dt.timezone.utc) - start).total_seconds()
                rate = i / elapsed if elapsed > 0 else 0
                print(
                    json.dumps(
                        {
                            "done": i,
                            "total": len(jobs),
                            "rate_per_sec": round(rate, 2),
                            "status": dict(counters),
                        }
                    ),
                    flush=True,
                )

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    stats = {
        "last_scan_at": now,
        "last_job_count": len(jobs),
        "last_workers": args.workers,
        "last_tags": tags,
        "last_status": dict(counters),
        "total_scanned": con.execute("SELECT COUNT(*) FROM bundle_scan").fetchone()[0],
        "total_ok": con.execute("SELECT COUNT(*) FROM bundle_scan WHERE scan_status='ok'").fetchone()[0],
        "total_errors": con.execute("SELECT COUNT(*) FROM bundle_scan WHERE scan_status='error'").fetchone()[0],
        "total_serialized_files": con.execute("SELECT COUNT(*) FROM bundle_serialized_file").fetchone()[0],
        "total_external_refs": con.execute("SELECT COUNT(*) FROM bundle_external_ref").fetchone()[0],
        "resolved_internal_external_refs": con.execute(
            "SELECT COUNT(*) FROM bundle_external_ref WHERE target_serialized_file_name IS NOT NULL"
        ).fetchone()[0],
        "total_objects": con.execute("SELECT COUNT(*) FROM bundle_object").fetchone()[0],
        "total_assetbundle_objects": con.execute("SELECT COUNT(*) FROM bundle_assetbundle").fetchone()[0],
        "total_containers": con.execute("SELECT COUNT(*) FROM bundle_container").fetchone()[0],
        "resolved_containers": con.execute(
            "SELECT COUNT(*) FROM bundle_container WHERE resolved_logical_name IS NOT NULL"
        ).fetchone()[0],
        "total_dependencies": con.execute("SELECT COUNT(*) FROM bundle_dependency").fetchone()[0],
        "resolved_dependencies": con.execute(
            "SELECT COUNT(*) FROM bundle_dependency WHERE resolved_logical_name IS NOT NULL"
        ).fetchone()[0],
        "container_skipped_total": con.execute(
            "SELECT COALESCE(SUM(container_skipped_count),0) FROM bundle_scan"
        ).fetchone()[0],
        "dependency_skipped_total": con.execute(
            "SELECT COALESCE(SUM(dependency_skipped_count),0) FROM bundle_scan"
        ).fetchone()[0],
    }
    for key, value in stats.items():
        con.execute(
            "INSERT OR REPLACE INTO deep_scan_metadata VALUES (?,?)",
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
        )
    con.commit()
    con.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
