#!/usr/bin/env python3
"""Summarize the materialized MLTD asset relationship graph.

This is an evidence/reporting tool only.  It does not mutate the relationship
DB and deliberately distinguishes exact serialized edges from lexical/domain
classification.  Domain matrices can double-count multi-tagged assets and
must not be treated as business master relationships.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    return p.parse_args()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    return list(con.execute(sql, params))


def scalar(con: sqlite3.Connection, sql: str, params: tuple = ()):
    r = con.execute(sql, params).fetchone()
    return None if r is None else r[0]


def table_count(con: sqlite3.Connection, table: str) -> int:
    return int(scalar(con, f"SELECT COUNT(*) FROM {table}") or 0)


def tag_map(con: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for logical, tag in con.execute("SELECT logical_name,tag FROM asset_tag"):
        out[str(logical)].add(str(tag))
    for logical, tags in out.items():
        if "other" in tags and len(tags) > 1:
            tags.discard("other")
    return out


def domain_matrix(
    con: sqlite3.Connection,
    sql: str,
    tags: dict[str, set[str]],
) -> dict:
    matrix: Counter[tuple[str, str]] = Counter()
    total = 0
    self_edges = 0
    for source, target in con.execute(sql):
        if not target:
            continue
        source = str(source)
        target = str(target)
        total += 1
        if source == target:
            self_edges += 1
        source_tags = tags.get(source) or {"untagged"}
        target_tags = tags.get(target) or {"untagged"}
        for a in source_tags:
            for b in target_tags:
                matrix[(a, b)] += 1
    return {
        "resolved_edges": total,
        "self_edges": self_edges,
        "cross_logical_edges": total - self_edges,
        "note": "domain counts can exceed edge count because assets may have multiple lexical tags",
        "domain_edges": [
            {"source_tag": a, "target_tag": b, "count": n}
            for (a, b), n in sorted(
                matrix.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
            )
        ],
    }


def main() -> int:
    args = parse_args()
    db = args.db.resolve()
    if not db.is_file():
        raise SystemExit(f"missing DB: {db}")

    uri = db.as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
        metadata = dict(con.execute("SELECT key,value FROM metadata"))
        tags = tag_map(con)

        base_tables = [
            "asset",
            "asset_tag",
            "asset_token",
            "entity",
            "asset_entity",
            "family",
            "catalog_group",
            "content_group",
        ]
        deep_tables = [
            "bundle_scan",
            "bundle_serialized_file",
            "bundle_external_ref",
            "bundle_object",
            "bundle_assetbundle",
            "bundle_container",
            "bundle_dependency",
        ]
        counts = {t: table_count(con, t) for t in base_tables + deep_tables}

        archive = con.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN archived=0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN size_matches=0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN size_matches IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN archived=0 THEN declared_size ELSE 0 END)
            FROM asset
            """
        ).fetchone()

        scan_status = {str(k): int(v) for k, v in rows(
            con, "SELECT scan_status,COUNT(*) FROM bundle_scan GROUP BY scan_status"
        )}
        scan_sums = con.execute(
            """
            SELECT COALESCE(SUM(container_skipped_count),0),
                   COALESCE(SUM(dependency_skipped_count),0),
                   COALESCE(SUM(container_raw_count),0),
                   COALESCE(SUM(dependency_raw_count),0)
            FROM bundle_scan
            """
        ).fetchone()

        object_types = [
            {"type": str(t), "count": int(n)}
            for t, n in rows(
                con,
                "SELECT type_name,COUNT(*) FROM bundle_object "
                "GROUP BY type_name ORDER BY COUNT(*) DESC,type_name",
            )
        ]
        container_shapes = [
            {"shape": str(t), "count": int(n)}
            for t, n in rows(
                con,
                "SELECT container_shape,COUNT(*) FROM bundle_assetbundle "
                "GROUP BY container_shape ORDER BY COUNT(*) DESC,container_shape",
            )
        ]
        container_resolution = [
            {"rule": (str(rule) if rule else "unresolved"), "count": int(n)}
            for rule, n in rows(
                con,
                "SELECT resolution_rule,COUNT(*) FROM bundle_container "
                "GROUP BY resolution_rule ORDER BY COUNT(*) DESC",
            )
        ]
        dependency_resolution = [
            {"rule": (str(rule) if rule else "unresolved"), "count": int(n)}
            for rule, n in rows(
                con,
                "SELECT resolution_rule,COUNT(*) FROM bundle_dependency "
                "GROUP BY resolution_rule ORDER BY COUNT(*) DESC",
            )
        ]
        edge_targets = [
            {"type": (str(t) if t else "unresolved"), "count": int(n)}
            for t, n in rows(
                con,
                "SELECT target_type_name,COUNT(*) FROM bundle_container_object_edge "
                "GROUP BY target_type_name ORDER BY COUNT(*) DESC",
            )
        ]

        container_matrix = domain_matrix(
            con,
            "SELECT logical_name,resolved_logical_name FROM bundle_container "
            "WHERE resolved_logical_name IS NOT NULL",
            tags,
        )
        dependency_matrix = domain_matrix(
            con,
            "SELECT logical_name,resolved_logical_name FROM bundle_dependency "
            "WHERE resolved_logical_name IS NOT NULL",
            tags,
        )

        live_checks = {
            "song_container_paths_containing_stage": int(
                scalar(
                    con,
                    """
                    SELECT COUNT(*)
                    FROM bundle_container bc
                    JOIN asset_tag t ON t.logical_name=bc.logical_name
                    WHERE t.tag='song' AND LOWER(bc.container_path) LIKE '%stage%'
                    """,
                )
                or 0
            ),
            "song_to_stage_dependencies": int(
                scalar(
                    con,
                    """
                    SELECT COUNT(*)
                    FROM bundle_dependency d
                    JOIN asset_tag s ON s.logical_name=d.logical_name AND s.tag='song'
                    JOIN asset_tag t ON t.logical_name=d.resolved_logical_name AND t.tag='stage'
                    """,
                )
                or 0
            ),
            "stage_to_song_dependencies": int(
                scalar(
                    con,
                    """
                    SELECT COUNT(*)
                    FROM bundle_dependency d
                    JOIN asset_tag s ON s.logical_name=d.logical_name AND s.tag='stage'
                    JOIN asset_tag t ON t.logical_name=d.resolved_logical_name AND t.tag='song'
                    """,
                )
                or 0
            ),
        }

        tag_coverage = []
        for tag, total in rows(
            con,
            "SELECT tag,COUNT(DISTINCT logical_name) FROM asset_tag GROUP BY tag ORDER BY tag",
        ):
            archived_count = int(
                scalar(
                    con,
                    """
                    SELECT COUNT(DISTINCT a.logical_name)
                    FROM asset a JOIN asset_tag t USING(logical_name)
                    WHERE t.tag=? AND a.archived=1
                    """,
                    (tag,),
                )
                or 0
            )
            scanned_count = int(
                scalar(
                    con,
                    """
                    SELECT COUNT(DISTINCT a.logical_name)
                    FROM asset a
                    JOIN asset_tag t USING(logical_name)
                    JOIN bundle_scan b USING(logical_name)
                    WHERE t.tag=? AND b.scan_status='ok'
                    """,
                    (tag,),
                )
                or 0
            )
            tag_coverage.append(
                {
                    "tag": str(tag),
                    "manifest_assets": int(total),
                    "archived_assets": archived_count,
                    "deep_scan_ok": scanned_count,
                }
            )

        report = {
            "schema_version": 1,
            "status": "evidence-only",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "input": {
                "db": str(db),
                "db_sha256": sha256_file(db),
                "relationship_schema": metadata.get("schema"),
                "build_complete": metadata.get("build_complete"),
                "manifest_sha256": metadata.get("asset_index_sha256"),
                "archive_index_sha256": metadata.get("archive_index_sha256"),
                "archive_version": metadata.get("archive_version"),
                "archive_scope": metadata.get("archive_scope"),
            },
            "integrity_check": integrity,
            "counts": counts,
            "archive_coverage": {
                "manifest_assets": int(archive[0] or 0),
                "archived_assets": int(archive[1] or 0),
                "unarchived_assets": int(archive[2] or 0),
                "size_mismatches": int(archive[3] or 0),
                "size_unknown": int(archive[4] or 0),
                "unarchived_declared_bytes": int(archive[5] or 0),
            },
            "deep_scan": {
                "scan_status": scan_status,
                "container_skipped": int(scan_sums[0] or 0),
                "dependency_skipped": int(scan_sums[1] or 0),
                "container_raw": int(scan_sums[2] or 0),
                "dependency_raw": int(scan_sums[3] or 0),
                "container_shapes": container_shapes,
                "object_types": object_types,
                "container_resolution": container_resolution,
                "dependency_resolution": dependency_resolution,
                "container_object_target_types": edge_targets,
            },
            "tag_coverage": tag_coverage,
            "container_domain_matrix": container_matrix,
            "dependency_domain_matrix": dependency_matrix,
            "live_exact_edge_checks": live_checks,
            "interpretation_limits": [
                "asset_tag and filename-token relations are lexical evidence, not business master truth",
                "container resolution by basename/extension is a candidate textual relation unless the raw serialized value itself is exact",
                "domain matrices can double-count an edge when either endpoint has multiple tags",
                "absence of an edge in an incomplete archive does not prove absence in the full manifest",
                "LIVE song/cast/stage promotion remains blocked until source-bound SongStatus/master evidence is recovered",
            ],
        }
    finally:
        con.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "status": report["status"],
        "integrity": report["integrity_check"],
        "counts": report["counts"],
        "archive_coverage": report["archive_coverage"],
        "live_exact_edge_checks": report["live_exact_edge_checks"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
