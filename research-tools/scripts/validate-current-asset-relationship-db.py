#!/usr/bin/env python3
"""Fail-closed validator for a completed MLTD current asset relationship DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import msgpack


def ro_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro", uri=True)


def manifest_rows(path: Path) -> dict:
    root = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(root, (list, tuple)) or len(root) != 1 or not isinstance(root[0], dict):
        raise ValueError("expected MLTD asset index shape [map]")
    return root[0]


def one(con: sqlite3.Connection, sql: str, params=()):
    return con.execute(sql, params).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument(
        "--asset-index",
        type=Path,
        default=Path("work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"),
    )
    ap.add_argument(
        "--archive-index",
        type=Path,
        default=Path("work/full-asset-archive-1077100/index.sqlite3"),
    )
    ap.add_argument("--archive-version", default="1077100")
    ap.add_argument("--archive-scope", default="jp-android")
    ap.add_argument("--expect-deep-empty", action="store_true")
    args = ap.parse_args()

    for p in (args.db, args.summary, args.asset_index, args.archive_index):
        if not p.is_file():
            raise FileNotFoundError(p)

    manifest = manifest_rows(args.asset_index)
    remote_names = {str(v[1]) for v in manifest.values()}
    summary = json.loads(args.summary.read_text(encoding="utf-8"))

    arc = ro_connect(args.archive_index)
    arc.row_factory = sqlite3.Row
    version = arc.execute(
        """SELECT object_count,complete FROM versions
           WHERE version=? AND scope=?""",
        (args.archive_version, args.archive_scope),
    ).fetchone()
    if version is None:
        raise RuntimeError("archive version/scope missing")
    archive_rows = {
        r["name"]: r
        for r in arc.execute(
            """SELECT name,sha256,size,status FROM entries
               WHERE version=? AND scope=?""",
            (args.archive_version, args.archive_scope),
        )
    }
    arc.close()

    expected_archived = 0
    expected_unarchived = 0
    expected_size_mismatch = 0
    for logical, rec in manifest.items():
        remote = str(rec[1])
        declared = int(rec[2])
        row = archive_rows.get(remote)
        # Keep archive semantics identical to the builder: full content-addressed
        # objects may retain 206 provenance after ranged/resumed acquisition.
        if row is not None and row["status"] in (200, 206) and row["sha256"]:
            expected_archived += 1
        else:
            expected_unarchived += 1
        if row is not None and row["size"] is not None and int(row["size"]) != declared:
            expected_size_mismatch += 1

    con = ro_connect(args.db)
    blockers: list[str] = []

    integrity_rows = [str(r[0]) for r in con.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        blockers.append("integrity_check failed: " + " | ".join(integrity_rows[:20]))

    fk_rows = list(con.execute("PRAGMA foreign_key_check"))
    if fk_rows:
        blockers.append(f"foreign_key_check returned {len(fk_rows)} rows")

    metadata = dict(con.execute("SELECT key,value FROM metadata"))
    if metadata.get("schema") != "mltd-current-asset-relationships-v2":
        blockers.append(f"unexpected schema {metadata.get('schema')!r}")
    if metadata.get("build_complete") != "1":
        blockers.append(f"build_complete={metadata.get('build_complete')!r}")
    if metadata.get("manifest_entry_count") != str(len(manifest)):
        blockers.append("manifest_entry_count metadata mismatch")
    if metadata.get("archive_requested_version") != args.archive_version:
        blockers.append("archive requested version metadata mismatch")
    if metadata.get("archive_requested_scope") != args.archive_scope:
        blockers.append("archive requested scope metadata mismatch")

    counts = {
        t: one(con, f"SELECT COUNT(*) FROM {t}")
        for t in (
            "asset",
            "asset_tag",
            "asset_token",
            "entity",
            "asset_entity",
            "family",
            "catalog_group",
            "content_group",
            "bundle_scan",
            "bundle_serialized_file",
            "bundle_external_ref",
            "bundle_object",
            "bundle_assetbundle",
            "bundle_container",
            "bundle_dependency",
        )
    }
    view_present = bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='bundle_container_object_edge'"
        ).fetchone()
    )
    if not view_present:
        blockers.append("bundle_container_object_edge view missing")

    if args.expect_deep_empty:
        deep_nonzero = {
            k: v
            for k, v in counts.items()
            if k.startswith("bundle_") and k != "bundle_scan" and v
        }
        if counts["bundle_scan"]:
            deep_nonzero["bundle_scan"] = counts["bundle_scan"]
        if deep_nonzero:
            blockers.append("deep tables not empty before scan: " + json.dumps(deep_nonzero, sort_keys=True))

    asset_rows = counts["asset"]
    logicals = one(con, "SELECT COUNT(DISTINCT logical_name) FROM asset")
    remotes = one(con, "SELECT COUNT(DISTINCT remote_name) FROM asset")
    archived = one(con, "SELECT COUNT(*) FROM asset WHERE archived=1")
    unarchived = one(con, "SELECT COUNT(*) FROM asset WHERE archived=0")
    size_mismatch = one(con, "SELECT COUNT(*) FROM asset WHERE size_matches=0")
    size_unknown = one(con, "SELECT COUNT(*) FROM asset WHERE size_matches IS NULL")

    if (asset_rows, logicals, remotes) != (len(manifest), len(manifest), len(remote_names)):
        blockers.append(
            f"asset cardinality mismatch rows/logicals/remotes={asset_rows}/{logicals}/{remotes} "
            f"expected={len(manifest)}/{len(manifest)}/{len(remote_names)}"
        )
    if archived != expected_archived or unarchived != expected_unarchived:
        blockers.append(
            f"archive coverage mismatch db={archived}/{unarchived} "
            f"expected={expected_archived}/{expected_unarchived}"
        )
    if size_mismatch != expected_size_mismatch:
        blockers.append(f"size mismatch count {size_mismatch} expected {expected_size_mismatch}")

    orphan_queries = {
        "asset_tag": """SELECT COUNT(*) FROM asset_tag x
                        LEFT JOIN asset a USING(logical_name) WHERE a.logical_name IS NULL""",
        "asset_token": """SELECT COUNT(*) FROM asset_token x
                          LEFT JOIN asset a USING(logical_name) WHERE a.logical_name IS NULL""",
        "asset_entity": """SELECT COUNT(*) FROM asset_entity x
                           LEFT JOIN asset a USING(logical_name) WHERE a.logical_name IS NULL""",
    }
    orphans = {k: one(con, q) for k, q in orphan_queries.items()}
    if any(orphans.values()):
        blockers.append("orphan child rows: " + json.dumps(orphans, sort_keys=True))

    family_mismatches = one(
        con,
        """WITH g AS (
             SELECT family_signature,COUNT(*) member_count,SUM(archived) archived_count
             FROM asset GROUP BY family_signature
           )
           SELECT COUNT(*) FROM (
             SELECT g.family_signature FROM g LEFT JOIN family f USING(family_signature)
             WHERE f.family_signature IS NULL
                OR g.member_count!=f.member_count
                OR g.archived_count!=f.archived_count
             UNION ALL
             SELECT f.family_signature FROM family f LEFT JOIN g USING(family_signature)
             WHERE g.family_signature IS NULL
           )""",
    )
    catalog_mismatches = one(
        con,
        """WITH g AS (
             SELECT catalog_hash,COUNT(*) member_count,SUM(archived) fetched_count,
                    COUNT(DISTINCT archive_sha256) distinct_archive_sha256,
                    COUNT(DISTINCT declared_size) distinct_sizes,
                    COUNT(DISTINCT family_signature) family_count
             FROM asset GROUP BY catalog_hash
           )
           SELECT COUNT(*) FROM (
             SELECT g.catalog_hash FROM g LEFT JOIN catalog_group c USING(catalog_hash)
             WHERE c.catalog_hash IS NULL
                OR g.member_count!=c.member_count
                OR g.fetched_count!=c.fetched_count
                OR g.distinct_archive_sha256!=c.distinct_archive_sha256
                OR g.distinct_sizes!=c.distinct_sizes
                OR g.family_count!=c.family_count
             UNION ALL
             SELECT c.catalog_hash FROM catalog_group c LEFT JOIN g USING(catalog_hash)
             WHERE g.catalog_hash IS NULL
           )""",
    )
    content_mismatches = one(
        con,
        """WITH g AS (
             SELECT archive_sha256,COUNT(*) member_count
             FROM asset WHERE archive_sha256 IS NOT NULL GROUP BY archive_sha256
           )
           SELECT COUNT(*) FROM (
             SELECT g.archive_sha256 FROM g LEFT JOIN content_group c USING(archive_sha256)
             WHERE c.archive_sha256 IS NULL OR g.member_count!=c.member_count
             UNION ALL
             SELECT c.archive_sha256 FROM content_group c LEFT JOIN g USING(archive_sha256)
             WHERE g.archive_sha256 IS NULL OR c.member_count!=g.member_count
           )""",
    )
    aggregate_mismatches = {
        "family": family_mismatches,
        "catalog_group": catalog_mismatches,
        "content_group": content_mismatches,
    }
    if any(aggregate_mismatches.values()):
        blockers.append("aggregate mismatches: " + json.dumps(aggregate_mismatches, sort_keys=True))

    summary_counts = summary.get("counts") or {}
    summary_expected = {
        "manifest_assets": len(manifest),
        "unique_remote_names": len(remote_names),
        "archived_assets": expected_archived,
        "unarchived_assets": expected_unarchived,
        "size_mismatches": expected_size_mismatch,
        "families": counts["family"],
        "catalog_groups": counts["catalog_group"],
        "exact_physical_sha_groups": counts["content_group"],
        "asset_entity_links": counts["asset_entity"],
        "asset_tokens": counts["asset_token"],
    }
    summary_mismatches = {
        k: {"actual": summary_counts.get(k), "expected": v}
        for k, v in summary_expected.items()
        if summary_counts.get(k) != v
    }
    if summary_mismatches:
        blockers.append("summary mismatch: " + json.dumps(summary_mismatches, sort_keys=True))

    con.close()

    result = {
        "status": "pass" if not blockers else "fail",
        "blockers": blockers,
        "manifest_assets": len(manifest),
        "archive_version": args.archive_version,
        "archive_scope": args.archive_scope,
        "archive_object_count": int(version["object_count"]),
        "archive_complete": int(version["complete"]),
        "expected_archived_assets": expected_archived,
        "expected_unarchived_assets": expected_unarchived,
        "db_counts": counts,
        "bundle_container_object_edge_view": view_present,
        "asset_cardinality": {
            "rows": asset_rows,
            "unique_logicals": logicals,
            "unique_remotes": remotes,
            "archived": archived,
            "unarchived": unarchived,
            "size_mismatch": size_mismatch,
            "size_unknown": size_unknown,
        },
        "orphans": orphans,
        "aggregate_mismatches": aggregate_mismatches,
        "summary_mismatches": summary_mismatches,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
