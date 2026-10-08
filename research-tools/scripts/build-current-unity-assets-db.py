#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import sqlite3
import zlib
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any

import msgpack
import UnityPy

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 5
DB_SCHEMA = "mltd-current-unity-assets-v5"
REFERENCE_GRAPH_VERSION = 3
FINAL_QUERY_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_unity_object_type ON unity_object(type_name)",
    "CREATE INDEX IF NOT EXISTS idx_unity_object_name ON unity_object(object_name)",
    "CREATE INDEX IF NOT EXISTS idx_unity_object_script ON unity_object(script_class)",
    "CREATE INDEX IF NOT EXISTS idx_unity_object_key_nocase ON unity_object(logical_name,serialized_file_name COLLATE NOCASE,path_id)",
    "CREATE INDEX IF NOT EXISTS idx_ref_target ON unity_object_reference(target_serialized_file_name,path_id)",
    "CREATE INDEX IF NOT EXISTS idx_ref_target_logical ON unity_object_reference(target_logical_name,target_serialized_file_name,path_id)",
    "CREATE INDEX IF NOT EXISTS idx_monoscript_class ON unity_monoscript(namespace,class_name)",
    "CREATE INDEX IF NOT EXISTS idx_textasset_classification ON unity_textasset(classification)",
    "CREATE INDEX IF NOT EXISTS idx_component_target ON unity_component(path_id)",
    "CREATE INDEX IF NOT EXISTS idx_transform_parent ON unity_transform(parent_object_id,parent_path_id)",
)
FINAL_QUERY_INDEX_NAMES = tuple(ddl.split()[5] for ddl in FINAL_QUERY_INDEX_DDL)
BINARY_HEAVY_TYPES = {"Texture2D", "Cubemap", "Mesh", "AudioClip", "MovieTexture", "VideoClip"}
RICH_CONTENT_TYPES = {"MonoBehaviour", "MonoScript", "TextAsset"}
MAX_INDEXED_FIELDS_RICH = 256
RICH_ARRAY_FIELD_BUDGET = 64
STRUCTURED_JSON_MAX_RAW = 1 << 20
SEMANTIC_TERMS = ("song", "stage", "idol", "costume", "camera", "dance", "live", "timeline")


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def safe_text(value: Any) -> str:
    """Return SQLite/UTF-8-safe text while preserving lone surrogates visibly.

    Some Unity strings contain unpaired UTF-16 surrogate code points.  Python's
    sqlite3 adapter correctly refuses to encode those as UTF-8.  Represent them
    as literal ``\\udxxx`` escape text instead; raw object/payload SHA-256 remains
    the byte-authoritative evidence, so this normalized representation is both
    loss-auditable and safe to query/export.
    """
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def json_dumps(v: Any) -> str:
    return safe_text(json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def compressed_json(v: Any) -> bytes:
    return zlib.compress(json_dumps(v).encode("utf-8"), 6)


def cap_index_fields(type_name: str, fields: list[tuple]) -> list[tuple]:
    """Bound rich-object EAV without losing authoritative structured content.

    Rich objects keep their complete normalized_json (and TextAsset parsed_json).
    EAV is only a search accelerator.  Keep shallow scalar metadata first, then
    deeper non-array paths, and reserve only a small deterministic budget for
    array paths.  This prevents dialogue/timeline MonoBehaviours from producing
    tens of thousands of redundant SQLite rows per object.
    """
    if type_name not in RICH_CONTENT_TYPES or len(fields) <= MAX_INDEXED_FIELDS_RICH:
        return fields
    shallow = [r for r in fields if "[" not in r[0] and r[0].count(".") <= 3]
    deep = [r for r in fields if "[" not in r[0] and r[0].count(".") > 3]
    arrays = [r for r in fields if "[" in r[0]][:RICH_ARRAY_FIELD_BUDGET]
    out = (shallow + deep)[:MAX_INDEXED_FIELDS_RICH]
    if len(out) < MAX_INDEXED_FIELDS_RICH:
        out.extend(arrays[:MAX_INDEXED_FIELDS_RICH-len(out)])
    return out


def filter_fields(type_name: str, fields: list[tuple]) -> list[tuple]:
    """Keep high-value searchable scalar paths while avoiding runaway EAV growth."""
    if type_name in RICH_CONTENT_TYPES:
        return cap_index_fields(type_name, fields)
    if type_name in ("GameObject", "Transform", "RectTransform"):
        keep_leaf={"m_Name","m_Layer","m_TagString","m_IsActive","m_RootOrder"}
        return [r for r in fields if r[0].rsplit(".",1)[-1] in keep_leaf]
    # Standard engine objects can expose enormous shallow typetrees (notably ParticleSystem
    # and renderers). Keep only direct/root scalar metadata in EAV; the complete PPtr graph
    # remains separate and a compressed structured fallback is retained for bounded objects.
    return [r for r in fields if "[" not in r[0] and r[0].count(".") <= 1]


def drop_final_query_indexes(con: sqlite3.Connection) -> None:
    """Drop read/query-only secondary indexes while bulk ingest is active."""
    for name in FINAL_QUERY_INDEX_NAMES + ("idx_field_path", "idx_field_text", "idx_field_int"):
        con.execute(f"DROP INDEX IF EXISTS {name}")
    # Historical redundant index; the UNIQUE(logical_name,serialized_file_name,path_id)
    # auto-index remains present and is enough for resume/delete lookups.
    con.execute("DROP INDEX IF EXISTS idx_unity_object_key")
    con.commit()


def create_final_query_indexes(con: sqlite3.Connection) -> None:
    """Materialize all user-facing/query resolver indexes after bulk ingest."""
    for ddl in FINAL_QUERY_INDEX_DDL:
        con.execute(ddl)
    con.commit()


def init_db(con: sqlite3.Connection, relationship_db: Path) -> None:
    con.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA temp_store=MEMORY;
        PRAGMA cache_size=-524288;
        PRAGMA wal_autocheckpoint=32768;
        CREATE TABLE IF NOT EXISTS metadata(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_asset(
            logical_name TEXT PRIMARY KEY,
            remote_name TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL,
            declared_size INTEGER NOT NULL,
            archive_size INTEGER,
            content_type TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_source_asset_sha ON source_asset(archive_sha256);

        CREATE TABLE IF NOT EXISTS source_serialized_file(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            unity_version TEXT NOT NULL,
            external_count INTEGER NOT NULL,
            externals_json TEXT NOT NULL,
            PRIMARY KEY(logical_name,serialized_file_name)
        );
        CREATE INDEX IF NOT EXISTS idx_source_serialized_file_name_nocase
            ON source_serialized_file(serialized_file_name COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS source_external_ref(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            external_path TEXT NOT NULL,
            guid TEXT NOT NULL,
            external_type INTEGER NOT NULL,
            target_serialized_file_name TEXT,
            PRIMARY KEY(logical_name,serialized_file_name,file_id)
        );
        CREATE TABLE IF NOT EXISTS bundle_state(
            logical_name TEXT PRIMARY KEY,
            archive_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            object_count INTEGER NOT NULL DEFAULT 0,
            parsed_count INTEGER NOT NULL DEFAULT 0,
            partial_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            reference_count INTEGER NOT NULL DEFAULT 0,
            scalar_count INTEGER NOT NULL DEFAULT 0,
            textasset_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS unity_object(
            object_id INTEGER PRIMARY KEY,
            logical_name TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            path_id INTEGER NOT NULL,
            class_id INTEGER,
            type_name TEXT NOT NULL,
            byte_size INTEGER,
            object_name TEXT,
            parse_status TEXT NOT NULL,
            parser_error TEXT NOT NULL DEFAULT '',
            raw_sha256 TEXT,
            normalized_json BLOB,
            script_file_id INTEGER,
            script_path_id INTEGER,
            script_class TEXT,
            semantic_tags TEXT NOT NULL DEFAULT '',
            UNIQUE(logical_name, serialized_file_name, path_id)
        );

        CREATE TABLE IF NOT EXISTS unity_object_field(
            object_id INTEGER NOT NULL,
            field_path TEXT NOT NULL,
            value_type TEXT NOT NULL,
            text_value TEXT,
            int_value INTEGER,
            real_value REAL,
            bool_value INTEGER,
            json_value TEXT,
            truncated INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(object_id, field_path),
            FOREIGN KEY(object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS unity_object_reference(
            object_id INTEGER NOT NULL,
            field_path TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            path_id INTEGER NOT NULL,
            target_serialized_file_name TEXT,
            target_logical_name TEXT,
            target_resolution_rule TEXT,
            target_object_id INTEGER,
            PRIMARY KEY(object_id, field_path, file_id, path_id),
            FOREIGN KEY(object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS unity_monobehaviour(
            object_id INTEGER PRIMARY KEY,
            script_file_id INTEGER,
            script_path_id INTEGER,
            script_class TEXT,
            FOREIGN KEY(object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS unity_monoscript(
            object_id INTEGER PRIMARY KEY,
            assembly_name TEXT,
            namespace TEXT,
            class_name TEXT,
            FOREIGN KEY(object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS unity_textasset(
            object_id INTEGER PRIMARY KEY,
            payload_size INTEGER NOT NULL,
            payload_sha256 TEXT,
            classification TEXT NOT NULL,
            encoding TEXT,
            compression TEXT,
            text_preview TEXT,
            parsed_json BLOB,
            FOREIGN KEY(object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS unity_gameobject(
            object_id INTEGER PRIMARY KEY,
            component_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS unity_component(
            gameobject_object_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            path_id INTEGER NOT NULL,
            component_object_id INTEGER,
            PRIMARY KEY(gameobject_object_id, ordinal),
            FOREIGN KEY(gameobject_object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS unity_transform(
            object_id INTEGER PRIMARY KEY,
            gameobject_file_id INTEGER,
            gameobject_path_id INTEGER,
            gameobject_object_id INTEGER,
            parent_file_id INTEGER,
            parent_path_id INTEGER,
            parent_object_id INTEGER,
            local_position_json TEXT,
            local_rotation_json TEXT,
            local_scale_json TEXT,
            anchor_min_json TEXT,
            anchor_max_json TEXT,
            anchored_position_json TEXT,
            size_delta_json TEXT,
            pivot_json TEXT,
            FOREIGN KEY(object_id) REFERENCES unity_object(object_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS parser_error(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            path_id INTEGER NOT NULL,
            type_name TEXT NOT NULL,
            error_class TEXT NOT NULL,
            error_text TEXT NOT NULL,
            raw_size INTEGER,
            raw_sha256 TEXT,
            PRIMARY KEY(logical_name, serialized_file_name, path_id)
        );
        """
    )
    ref_columns = {r[1] for r in con.execute("PRAGMA table_info(unity_object_reference)")}
    if "target_logical_name" not in ref_columns:
        con.execute("ALTER TABLE unity_object_reference ADD COLUMN target_logical_name TEXT")
    if "target_resolution_rule" not in ref_columns:
        con.execute("ALTER TABLE unity_object_reference ADD COLUMN target_resolution_rule TEXT")

    existing_rel_path = con.execute("SELECT value FROM metadata WHERE key='relationship_db'").fetchone()
    existing_rel_hash = con.execute("SELECT value FROM metadata WHERE key='relationship_db_sha256'").fetchone()
    if (
        existing_rel_path and existing_rel_hash
        and Path(existing_rel_path[0]).resolve() == relationship_db.resolve()
    ):
        relationship_hash = existing_rel_hash[0]
    else:
        relationship_hash = sha256_path(relationship_db)
    values = {
        "schema": DB_SCHEMA,
        "schema_version": str(SCHEMA_VERSION),
        "relationship_db": str(relationship_db),
        "relationship_db_sha256": relationship_hash,
        "created_or_opened_at": utcnow(),
        "builder": str(Path(__file__).name),
        "normalized_json_codec": "zlib+utf8+json",
        "unicode_text_policy": "lone-surrogate-backslash-escaped; raw_sha256 remains authoritative",
        "field_index_policy": f"rich EAV capped at {MAX_INDEXED_FIELDS_RICH} fields/object with <= {RICH_ARRAY_FIELD_BUDGET} array paths; complete normalized_json/parsed_json remains authoritative",
        "query_index_policy": "no global unity_object_field secondary indexes during/final; field/value scans trade speed for bounded disk footprint",
    }
    con.executemany("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", values.items())
    con.commit()


def sync_source_inventory(con: sqlite3.Connection, relationship_db: Path) -> None:
    """Copy the compact source identity layer needed for provenance and cross-bundle PPtrs."""
    source_hash = con.execute(
        "SELECT value FROM metadata WHERE key='relationship_db_sha256'"
    ).fetchone()[0]
    synced_hash = con.execute(
        "SELECT value FROM metadata WHERE key='source_inventory_relationship_db_sha256'"
    ).fetchone()
    if synced_hash and synced_hash[0] == source_hash and con.execute(
        "SELECT count(*) FROM source_asset"
    ).fetchone()[0] > 0:
        return

    # SQLite's ATTACH URI handling is build-dependent on Windows; a parameterized
    # native path is reliable here. This function only SELECTs from the attached DB.
    con.execute("ATTACH DATABASE ? AS rel", (str(relationship_db.resolve()),))
    try:
        con.execute(
            """INSERT OR REPLACE INTO source_asset
               (logical_name,remote_name,archive_sha256,declared_size,archive_size,content_type)
               SELECT logical_name,remote_name,archive_sha256,declared_size,archive_size,content_type
               FROM rel.asset WHERE archived=1 AND archive_sha256 IS NOT NULL"""
        )
        con.execute(
            """INSERT OR REPLACE INTO source_serialized_file
               (logical_name,serialized_file_name,unity_version,external_count,externals_json)
               SELECT logical_name,serialized_file_name,unity_version,external_count,externals_json
               FROM rel.bundle_serialized_file"""
        )
        con.execute(
            """INSERT OR REPLACE INTO source_external_ref
               (logical_name,serialized_file_name,file_id,external_path,guid,external_type,target_serialized_file_name)
               SELECT logical_name,serialized_file_name,file_id,external_path,guid,external_type,target_serialized_file_name
               FROM rel.bundle_external_ref"""
        )
        con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('source_inventory_relationship_db_sha256',?)",
            (source_hash,),
        )
        con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('source_inventory_synced_at',?)",
            (utcnow(),),
        )
        con.commit()
    finally:
        con.execute("DETACH DATABASE rel")


def reconcile_reference_graph(con: sqlite3.Connection, force: bool = False) -> None:
    """One-time migration for resumable DBs created by older resolver revisions.

    The graph is derived data: normalized object content/raw hashes retain source
    evidence. Rebuilding this layer is therefore safer than carrying stale null
    PPtr rows or case-sensitive external-file decisions across resumes.
    """
    row = con.execute(
        "SELECT value FROM metadata WHERE key='reference_graph_version'"
    ).fetchone()
    if not force and row and int(row[0]) >= REFERENCE_GRAPH_VERSION:
        return

    con.execute("BEGIN")
    # Unity's path_id=0 PPtr is a null sentinel rather than a graph edge. Older
    # pilots retained it, which inflated edge counts and depressed resolution rates.
    con.execute("DELETE FROM unity_object_reference WHERE path_id=0")
    con.execute(
        "UPDATE unity_object_reference SET target_logical_name=NULL, "
        "target_resolution_rule=NULL, target_object_id=NULL"
    )

    con.execute(
        """UPDATE unity_object_reference AS r
           SET target_logical_name=(SELECT s.logical_name FROM unity_object s WHERE s.object_id=r.object_id),
               target_resolution_rule='same-serialized-file'
           WHERE r.file_id=0"""
    )
    con.execute(
        """UPDATE unity_object_reference AS r
           SET target_resolution_rule='builtin-resource'
           WHERE r.file_id<>0
             AND lower(COALESCE(r.target_serialized_file_name,'')) IN
                 ('unity default resources','unity_builtin_extra')"""
    )
    con.execute(
        """UPDATE unity_object_reference AS r
           SET target_logical_name=(SELECT s.logical_name FROM unity_object s WHERE s.object_id=r.object_id),
               target_resolution_rule='same-bundle-external'
           WHERE r.file_id<>0
             AND COALESCE(r.target_resolution_rule,'')<>'builtin-resource'
             AND EXISTS(
                 SELECT 1
                 FROM source_serialized_file sf
                 JOIN unity_object s ON s.object_id=r.object_id
                 WHERE sf.logical_name=s.logical_name
                   AND sf.serialized_file_name=r.target_serialized_file_name COLLATE NOCASE
             )"""
    )
    con.execute(
        """UPDATE unity_object_reference AS r
           SET target_logical_name=(
                   SELECT sf.logical_name FROM source_serialized_file sf
                   WHERE sf.serialized_file_name=r.target_serialized_file_name COLLATE NOCASE
                   LIMIT 1
               ),
               target_resolution_rule='serialized-file-unique'
           WHERE r.file_id<>0
             AND r.target_logical_name IS NULL
             AND COALESCE(r.target_resolution_rule,'')<>'builtin-resource'
             AND 1=(
                 SELECT count(*) FROM source_serialized_file sf
                 WHERE sf.serialized_file_name=r.target_serialized_file_name COLLATE NOCASE
             )"""
    )
    con.execute(
        """UPDATE unity_object_reference
           SET target_resolution_rule='unresolved-external'
           WHERE file_id<>0 AND target_logical_name IS NULL
             AND COALESCE(target_resolution_rule,'')=''"""
    )
    con.execute(
        """UPDATE unity_object_reference AS r
           SET target_object_id=(
               SELECT t.object_id
               FROM unity_object t
               JOIN unity_object s ON s.object_id=r.object_id
               WHERE t.logical_name=r.target_logical_name
                 AND t.serialized_file_name=COALESCE(NULLIF(r.target_serialized_file_name,''),s.serialized_file_name) COLLATE NOCASE
                 AND t.path_id=r.path_id
               LIMIT 1
           )
           WHERE r.target_logical_name IS NOT NULL"""
    )
    con.execute(
        """UPDATE unity_monobehaviour AS m
           SET script_class=(
               SELECT CASE WHEN ms.namespace IS NULL OR ms.namespace=''
                           THEN ms.class_name ELSE ms.namespace||'.'||ms.class_name END
               FROM unity_object_reference r
               JOIN unity_monoscript ms ON ms.object_id=r.target_object_id
               WHERE r.object_id=m.object_id AND r.field_path='$.m_Script'
               LIMIT 1
           )"""
    )
    con.execute(
        """UPDATE unity_object
           SET script_class=(SELECT m.script_class FROM unity_monobehaviour m WHERE m.object_id=unity_object.object_id),
               script_file_id=(SELECT m.script_file_id FROM unity_monobehaviour m WHERE m.object_id=unity_object.object_id),
               script_path_id=(SELECT m.script_path_id FROM unity_monobehaviour m WHERE m.object_id=unity_object.object_id)
           WHERE type_name='MonoBehaviour'"""
    )
    # GameObject component lists and Transform ancestry are also derived relations.
    # Materialize them once per ingest batch instead of repeating correlated UPDATEs
    # for every bundle; this keeps the single SQLite writer from throttling parsers.
    con.execute(
        """UPDATE unity_component AS c
           SET component_object_id=(
               SELECT t.object_id
               FROM unity_object g
               JOIN unity_object t
                 ON t.logical_name=g.logical_name
                AND t.serialized_file_name=g.serialized_file_name
                AND t.path_id=c.path_id
               WHERE g.object_id=c.gameobject_object_id
               LIMIT 1
           )"""
    )
    con.execute(
        """UPDATE unity_transform AS x
           SET gameobject_object_id=(
                   SELECT g.object_id
                   FROM unity_object xobj
                   JOIN unity_object g
                     ON g.logical_name=xobj.logical_name
                    AND g.serialized_file_name=xobj.serialized_file_name
                    AND g.path_id=x.gameobject_path_id
                   WHERE xobj.object_id=x.object_id
                   LIMIT 1
               ),
               parent_object_id=CASE WHEN x.parent_path_id=0 THEN NULL ELSE (
                   SELECT p.object_id
                   FROM unity_object xobj
                   JOIN unity_object p
                     ON p.logical_name=xobj.logical_name
                    AND p.serialized_file_name=xobj.serialized_file_name
                    AND p.path_id=x.parent_path_id
                   WHERE xobj.object_id=x.object_id
                   LIMIT 1
               ) END"""
    )
    con.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('reference_graph_version',?)",
        (str(REFERENCE_GRAPH_VERSION),),
    )
    con.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('reference_graph_reconciled_at',?)",
        (utcnow(),),
    )
    con.commit()


def pptr(v: Any) -> tuple[int, int] | None:
    if not isinstance(v, Mapping):
        return None
    if "m_FileID" in v and "m_PathID" in v:
        try:
            return int(v.get("m_FileID") or 0), int(v.get("m_PathID") or 0)
        except Exception:
            return None
    return None


def scalar_row(path: str, value: Any, truncated: int = 0):
    path = safe_text(path)
    if value is None:
        return (path, "null", None, None, None, None, None, truncated)
    if isinstance(value, bool):
        return (path, "bool", None, None, None, int(value), None, truncated)
    if isinstance(value, int):
        return (path, "int", None, value, None, None, None, truncated)
    if isinstance(value, float):
        if math.isfinite(value):
            return (path, "real", None, None, value, None, None, truncated)
        return (path, "real-special", str(value), None, None, None, None, truncated)
    if isinstance(value, str):
        return (path, "text", safe_text(value), None, None, None, None, truncated)
    return None


def normalize_tree(v: Any, path: str = "$", max_list_items: int = 2048, fields=None, refs=None):
    if fields is None:
        fields = []
    if refs is None:
        refs = []
    s = scalar_row(path, v)
    if s is not None:
        fields.append(s)
        if isinstance(v, float) and not math.isfinite(v):
            return str(v), fields, refs
        return v, fields, refs
    if isinstance(v, (bytes, bytearray, memoryview)):
        data = bytes(v)
        desc = {"$binary": True, "size": len(data), "sha256": sha256_bytes(data)}
        fields.append((path, "binary", None, len(data), None, None, json_dumps(desc), 1))
        return desc, fields, refs
    if isinstance(v, Mapping):
        pr = pptr(v)
        # (0, 0) is Unity's null PPtr sentinel, not a graph edge. Keeping it
        # bloats the reference table and makes resolution statistics misleading.
        if pr is not None and pr != (0, 0):
            refs.append((path, pr[0], pr[1]))
        out = {}
        for k, child in v.items():
            sk = safe_text(k)
            kp = safe_text(f"{path}.{sk}")
            n, _, _ = normalize_tree(child, kp, max_list_items, fields, refs)
            out[sk] = n
        return out, fields, refs
    if isinstance(v, (list, tuple)):
        total = len(v)
        limit = min(total, max_list_items)
        out = []
        for i in range(limit):
            n, _, _ = normalize_tree(v[i], f"{path}[{i}]", max_list_items, fields, refs)
            out.append(n)
        if total > limit:
            out.append({"$truncated": total - limit, "$total": total})
            fields.append((path, "array", None, total, None, None, None, 1))
        return out, fields, refs
    text = safe_text(v)
    fields.append((path, type(v).__name__, text[:1024], None, None, None, None, int(len(text) > 1024)))
    return text[:1024], fields, refs


def classify_payload(data: bytes, depth: int = 0):
    result = {"classification": "binary", "encoding": None, "compression": None, "preview": None, "parsed": None, "data": data}
    if depth < 2 and data.startswith(b"\x1f\x8b"):
        try:
            inner = gzip.decompress(data)
            result = classify_payload(inner, depth + 1)
            result["compression"] = "gzip"
            result["data"] = inner
            return result
        except Exception:
            pass
    if depth < 2 and len(data) > 2 and data[0] == 0x78:
        try:
            inner = zlib.decompress(data)
            result = classify_payload(inner, depth + 1)
            result["compression"] = "zlib"
            result["data"] = inner
            return result
        except Exception:
            pass
    if data.startswith(b"SQLite format 3\x00"):
        result["classification"] = "sqlite"
        return result
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            text = data.decode(enc)
        except Exception:
            continue
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text[:4096])
        denom = max(1, min(len(text), 4096))
        if printable / denom < 0.85:
            continue
        result["encoding"] = enc
        result["preview"] = text[:4096]
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
                result["classification"] = "json"
                result["parsed"] = parsed
                return result
            except Exception:
                pass
        if "\t" in text and "\n" in text:
            result["classification"] = "tsv"
        elif "," in text and "\n" in text:
            result["classification"] = "csv-like"
        else:
            result["classification"] = "text"
        return result
    try:
        parsed = msgpack.unpackb(data, raw=False, strict_map_key=False)
        if isinstance(parsed, (dict, list, tuple)):
            result["classification"] = "messagepack"
            result["parsed"] = parsed
            return result
    except Exception:
        pass
    if len(data) <= 64:
        result["classification"] = "tiny-binary"
    return result


def semantic_tags(type_name: str, name: str | None, tree: Any) -> str:
    hay = safe_text(type_name + " " + (name or "")).lower()
    if isinstance(tree, Mapping):
        hay += " " + " ".join(safe_text(k).lower() for k in list(tree.keys())[:128])
    return ",".join(t for t in SEMANTIC_TERMS if t in hay)


def ext_target_map(assets_file) -> dict[int, str]:
    out = {0: safe_text(getattr(assets_file, "name", "") or "")}
    for idx, ext in enumerate(getattr(assets_file, "externals", None) or [], 1):
        raw = safe_text(getattr(ext, "path", "") or "").replace("\\", "/")
        out[idx] = raw.rsplit("/", 1)[-1] if raw else ""
    return out


def scan_bundle(job):
    logical, archive_sha, archive_root, max_list_items = job
    p = Path(archive_root) / "objects" / archive_sha[:2] / archive_sha
    started = utcnow()
    base = {"logical_name": logical, "archive_sha256": archive_sha, "started_at": started, "objects": [], "error": ""}
    if not p.is_file():
        base["error"] = "cas_missing"
        return base
    try:
        env = UnityPy.load(str(p))
    except Exception as e:
        base["error"] = f"unity_load:{type(e).__name__}:{e}"
        return base

    for obj in env.objects:
        af = obj.assets_file
        sf = safe_text(getattr(af, "name", "") or "")
        type_name = obj.type.name
        class_id = int(getattr(obj, "class_id", 0) or 0)
        byte_size = int(getattr(obj, "byte_size", 0) or 0)
        raw = b""
        raw_sha = None
        raw_error = None
        try:
            raw = obj.get_raw_data()
            raw_sha = sha256_bytes(raw)
            if not byte_size:
                byte_size = len(raw)
        except Exception as e:
            raw_error = f"raw:{type(e).__name__}:{e}"

        rec = {
            "serialized_file_name": sf,
            "path_id": int(obj.path_id),
            "class_id": class_id,
            "type_name": type_name,
            "byte_size": byte_size,
            "raw_sha256": raw_sha,
            "parse_status": "metadata-only",
            "parser_error": safe_text(raw_error or ""),
            "object_name": None,
            "normalized_json": None,
            "fields": [],
            "refs": [],
            "script": None,
            "monoscript": None,
            "textasset": None,
            "gameobject": None,
            "transform": None,
            "semantic_tags": "",
        }
        try:
            tree = obj.read_typetree()
            norm, fields, refs = normalize_tree(tree, max_list_items=max_list_items)
            name = tree.get("m_Name") if isinstance(tree, Mapping) else None
            rec["object_name"] = safe_text(name) if name not in (None, "") else None
            rec["fields"] = filter_fields(type_name, fields)
            emap = ext_target_map(af)
            rec["refs"] = [(fp, fid, pid, emap.get(fid)) for fp, fid, pid in refs]
            rec["semantic_tags"] = semantic_tags(type_name, rec["object_name"], tree)
            # Keep complete structured fallback for rich content and bounded non-binary engine
            # objects. Large/binary-heavy payloads remain traceable through raw SHA/CAS plus
            # searchable root metadata and the complete PPtr edge set.
            if type_name in RICH_CONTENT_TYPES or (type_name not in BINARY_HEAVY_TYPES and byte_size <= STRUCTURED_JSON_MAX_RAW):
                rec["normalized_json"] = compressed_json(norm)
            rec["parse_status"] = "parsed"

            if type_name == "MonoBehaviour" and isinstance(tree, Mapping):
                sr = pptr(tree.get("m_Script"))
                if sr:
                    rec["script"] = {"file_id": sr[0], "path_id": sr[1]}
            elif type_name == "MonoScript" and isinstance(tree, Mapping):
                rec["monoscript"] = {
                    "assembly_name": safe_text(tree.get("m_AssemblyName") or ""),
                    "namespace": safe_text(tree.get("m_Namespace") or ""),
                    "class_name": safe_text(tree.get("m_ClassName") or ""),
                }
            elif type_name == "TextAsset" and isinstance(tree, Mapping):
                payload = tree.get("m_Script", b"")
                if isinstance(payload, str):
                    payload = payload.encode("utf-8", "surrogatepass")
                if not isinstance(payload, (bytes, bytearray, memoryview)):
                    payload = bytes(payload) if payload is not None else b""
                payload = bytes(payload)
                c = classify_payload(payload)
                parsed_json = None
                if c.get("parsed") is not None:
                    pn, pfields, prefs = normalize_tree(c["parsed"], "$.textasset_payload", max_list_items=max_list_items)
                    rec["fields"].extend(pfields)
                    rec["refs"].extend((fp, fid, pid, None) for fp, fid, pid in prefs)
                    parsed_json = compressed_json(pn)
                rec["textasset"] = {
                    "payload_size": len(payload),
                    "payload_sha256": sha256_bytes(payload),
                    "classification": c["classification"],
                    "encoding": c.get("encoding"),
                    "compression": c.get("compression"),
                    "text_preview": safe_text(c.get("preview")) if c.get("preview") is not None else None,
                    "parsed_json": parsed_json,
                }
            elif type_name == "GameObject" and isinstance(tree, Mapping):
                comps = []
                for i, item in enumerate(tree.get("m_Component") or []):
                    pr = pptr(item.get("component") if isinstance(item, Mapping) and "component" in item else item)
                    if pr:
                        comps.append((i, pr[0], pr[1]))
                rec["gameobject"] = {"components": comps}
            elif type_name in ("Transform", "RectTransform") and isinstance(tree, Mapping):
                go = pptr(tree.get("m_GameObject")) or (0, 0)
                parent = pptr(tree.get("m_Father")) or (0, 0)
                def compact(k):
                    v = tree.get(k)
                    return json_dumps(v) if v is not None else None
                rec["transform"] = {
                    "go": go,
                    "parent": parent,
                    "local_position_json": compact("m_LocalPosition"),
                    "local_rotation_json": compact("m_LocalRotation"),
                    "local_scale_json": compact("m_LocalScale"),
                    "anchor_min_json": compact("m_AnchorMin"),
                    "anchor_max_json": compact("m_AnchorMax"),
                    "anchored_position_json": compact("m_AnchoredPosition"),
                    "size_delta_json": compact("m_SizeDelta"),
                    "pivot_json": compact("m_Pivot"),
                }
            rec["fields"] = cap_index_fields(type_name, rec["fields"])
        except Exception as e:
            rec["parse_status"] = "failed"
            rec["parser_error"] = safe_text(f"typetree:{type(e).__name__}:{e}")
        base["objects"].append(rec)
    return base


def field_insert_tuple(object_id: int, row):
    path, vt, text, iv, rv, bv, jv, trunc = row
    return (object_id, path, vt, text, iv, rv, bv, jv, trunc)


def resolve_links(con: sqlite3.Connection, logical: str) -> None:
    """Resolve object-level PPtrs with provenance, including unique cross-bundle CAB refs."""
    current_scope = "r.object_id IN (SELECT object_id FROM unity_object WHERE logical_name=?)"

    # file_id == 0 is a real edge within the same serialized file (null (0,0)
    # sentinels were already filtered during normalization).
    con.execute(
        f"""UPDATE unity_object_reference AS r
            SET target_logical_name=(SELECT s.logical_name FROM unity_object s WHERE s.object_id=r.object_id),
                target_resolution_rule='same-serialized-file'
            WHERE {current_scope} AND r.file_id=0""",
        (logical,),
    )

    # Unity built-in resources are intentionally outside the archived AssetBundles.
    con.execute(
        f"""UPDATE unity_object_reference AS r
            SET target_logical_name=NULL,
                target_resolution_rule='builtin-resource'
            WHERE {current_scope} AND r.file_id<>0
              AND lower(COALESCE(r.target_serialized_file_name,'')) IN
                  ('unity default resources','unity_builtin_extra')""",
        (logical,),
    )

    # External serialized files can still be members of the same logical bundle.
    con.execute(
        f"""UPDATE unity_object_reference AS r
            SET target_logical_name=(SELECT s.logical_name FROM unity_object s WHERE s.object_id=r.object_id),
                target_resolution_rule='same-bundle-external'
            WHERE {current_scope} AND r.file_id<>0
              AND COALESCE(r.target_resolution_rule,'')<>'builtin-resource'
              AND EXISTS(
                  SELECT 1
                  FROM source_serialized_file sf
                  JOIN unity_object s ON s.object_id=r.object_id
                  WHERE sf.logical_name=s.logical_name
                    AND sf.serialized_file_name=r.target_serialized_file_name COLLATE NOCASE
              )""",
        (logical,),
    )

    # archive:/CAB-... references often point to a serialized file owned by a
    # different logical AssetBundle. Resolve only when the serialized filename is
    # globally unique in the canonical source inventory; ambiguous names fail closed.
    con.execute(
        f"""UPDATE unity_object_reference AS r
            SET target_logical_name=(
                    SELECT sf.logical_name FROM source_serialized_file sf
                    WHERE sf.serialized_file_name=r.target_serialized_file_name COLLATE NOCASE
                    LIMIT 1
                ),
                target_resolution_rule='serialized-file-unique'
            WHERE {current_scope} AND r.file_id<>0
              AND r.target_logical_name IS NULL
              AND COALESCE(r.target_resolution_rule,'')<>'builtin-resource'
              AND 1=(
                  SELECT count(*) FROM source_serialized_file sf
                  WHERE sf.serialized_file_name=r.target_serialized_file_name COLLATE NOCASE
              )""",
        (logical,),
    )
    con.execute(
        f"""UPDATE unity_object_reference AS r
            SET target_resolution_rule='unresolved-external'
            WHERE {current_scope} AND r.file_id<>0
              AND r.target_logical_name IS NULL
              AND COALESCE(r.target_resolution_rule,'')=''""",
        (logical,),
    )

    # Resolve current source edges whose target objects are already present.
    con.execute(
        f"""UPDATE unity_object_reference AS r
            SET target_object_id=(
                SELECT t.object_id
                FROM unity_object t
                JOIN unity_object s ON s.object_id=r.object_id
                WHERE t.logical_name=r.target_logical_name
                  AND t.serialized_file_name=COALESCE(NULLIF(r.target_serialized_file_name,''),s.serialized_file_name) COLLATE NOCASE
                  AND t.path_id=r.path_id
                LIMIT 1
            )
            WHERE {current_scope} AND r.target_logical_name IS NOT NULL""",
        (logical,),
    )

    # Also close older cross-bundle edges that were waiting for this logical bundle
    # to be parsed. This makes chunked/resumed runs converge without a full reparse.
    con.execute(
        """UPDATE unity_object_reference AS r
           SET target_object_id=(
               SELECT t.object_id FROM unity_object t
               WHERE t.logical_name=?
                 AND t.serialized_file_name=r.target_serialized_file_name COLLATE NOCASE
                 AND t.path_id=r.path_id
               LIMIT 1
           )
           WHERE r.target_logical_name=? AND r.target_object_id IS NULL""",
        (logical, logical),
    )

    con.execute(
        """UPDATE unity_component AS c
           SET component_object_id=(SELECT t.object_id FROM unity_object t JOIN unity_object g ON g.object_id=c.gameobject_object_id
                                    WHERE t.logical_name=g.logical_name AND t.serialized_file_name=g.serialized_file_name
                                      AND t.path_id=c.path_id LIMIT 1)
           WHERE c.gameobject_object_id IN (SELECT object_id FROM unity_object WHERE logical_name=?)""",
        (logical,),
    )
    con.execute(
        """UPDATE unity_transform AS x
           SET gameobject_object_id=(SELECT g.object_id FROM unity_object g JOIN unity_object xobj ON xobj.object_id=x.object_id
                                     WHERE g.logical_name=xobj.logical_name AND g.serialized_file_name=xobj.serialized_file_name
                                       AND g.path_id=x.gameobject_path_id LIMIT 1),
               parent_object_id=(SELECT p.object_id FROM unity_object p JOIN unity_object xobj ON xobj.object_id=x.object_id
                                 WHERE p.logical_name=xobj.logical_name AND p.serialized_file_name=xobj.serialized_file_name
                                   AND p.path_id=x.parent_path_id LIMIT 1)
           WHERE x.object_id IN (SELECT object_id FROM unity_object WHERE logical_name=?)""",
        (logical,),
    )

    # Resolve MonoBehaviour -> MonoScript -> managed class for both current source
    # objects and older behaviours whose cross-bundle m_Script target just arrived.
    affected_mb = """SELECT r.object_id FROM unity_object_reference r
                     LEFT JOIN unity_object src ON src.object_id=r.object_id
                     LEFT JOIN unity_object tgt ON tgt.object_id=r.target_object_id
                     WHERE r.field_path='$.m_Script'
                       AND (src.logical_name=? OR tgt.logical_name=?)"""
    con.execute(
        f"""UPDATE unity_monobehaviour AS m
            SET script_class=(
                SELECT CASE WHEN ms.namespace IS NULL OR ms.namespace=''
                            THEN ms.class_name ELSE ms.namespace||'.'||ms.class_name END
                FROM unity_object_reference r
                JOIN unity_monoscript ms ON ms.object_id=r.target_object_id
                WHERE r.object_id=m.object_id AND r.field_path='$.m_Script'
                LIMIT 1
            )
            WHERE m.object_id IN ({affected_mb})""",
        (logical, logical),
    )
    con.execute(
        f"""UPDATE unity_object
            SET script_class=(SELECT m.script_class FROM unity_monobehaviour m WHERE m.object_id=unity_object.object_id),
                script_file_id=(SELECT m.script_file_id FROM unity_monobehaviour m WHERE m.object_id=unity_object.object_id),
                script_path_id=(SELECT m.script_path_id FROM unity_monobehaviour m WHERE m.object_id=unity_object.object_id)
            WHERE object_id IN ({affected_mb})""",
        (logical, logical),
    )


def write_bundle(con: sqlite3.Connection, result: dict, commit: bool = True) -> dict:
    logical = result["logical_name"]
    sha = result["archive_sha256"]
    if result.get("error"):
        if not con.in_transaction:
            con.execute("BEGIN")
        con.execute(
            "INSERT OR REPLACE INTO bundle_state(logical_name,archive_sha256,status,started_at,completed_at,error) VALUES(?,?,?,?,?,?)",
            (logical, sha, "failed", result.get("started_at"), utcnow(), result["error"]),
        )
        if commit:
            con.commit()
        return {"objects": 0, "parsed": 0, "failed": 0, "refs": 0, "fields": 0, "textassets": 0}

    if not con.in_transaction:
        con.execute("BEGIN")
    con.execute("DELETE FROM parser_error WHERE logical_name=?", (logical,))
    old_ids = [r[0] for r in con.execute("SELECT object_id FROM unity_object WHERE logical_name=?", (logical,))]
    if old_ids:
        con.execute("DELETE FROM unity_object WHERE logical_name=?", (logical,))
    counts = Counter()
    for rec in result["objects"]:
        cur = con.execute(
            """INSERT INTO unity_object(logical_name,archive_sha256,serialized_file_name,path_id,class_id,type_name,byte_size,
               object_name,parse_status,parser_error,raw_sha256,normalized_json,semantic_tags)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (logical, sha, rec["serialized_file_name"], rec["path_id"], rec["class_id"], rec["type_name"], rec["byte_size"],
             rec["object_name"], rec["parse_status"], rec["parser_error"], rec["raw_sha256"], rec["normalized_json"], rec["semantic_tags"]),
        )
        oid = cur.lastrowid
        counts["objects"] += 1
        counts[rec["parse_status"]] += 1
        if rec["fields"]:
            con.executemany(
                "INSERT OR REPLACE INTO unity_object_field(object_id,field_path,value_type,text_value,int_value,real_value,bool_value,json_value,truncated) VALUES(?,?,?,?,?,?,?,?,?)",
                [field_insert_tuple(oid, r) for r in rec["fields"]],
            )
            counts["fields"] += len(rec["fields"])
        if rec["refs"]:
            con.executemany(
                "INSERT OR IGNORE INTO unity_object_reference(object_id,field_path,file_id,path_id,target_serialized_file_name) VALUES(?,?,?,?,?)",
                [(oid, fp, fid, pid, target) for fp, fid, pid, target in rec["refs"]],
            )
            counts["refs"] += len(rec["refs"])
        if rec["script"] is not None:
            s = rec["script"]
            con.execute("INSERT INTO unity_monobehaviour(object_id,script_file_id,script_path_id) VALUES(?,?,?)", (oid, s["file_id"], s["path_id"]))
        if rec["monoscript"] is not None:
            m = rec["monoscript"]
            con.execute("INSERT INTO unity_monoscript(object_id,assembly_name,namespace,class_name) VALUES(?,?,?,?)", (oid, m["assembly_name"], m["namespace"], m["class_name"]))
        if rec["textasset"] is not None:
            t = rec["textasset"]
            con.execute("INSERT INTO unity_textasset VALUES(?,?,?,?,?,?,?,?)", (oid,t["payload_size"],t["payload_sha256"],t["classification"],t["encoding"],t["compression"],t["text_preview"],t["parsed_json"]))
            counts["textassets"] += 1
        if rec["gameobject"] is not None:
            comps = rec["gameobject"]["components"]
            con.execute("INSERT INTO unity_gameobject(object_id,component_count) VALUES(?,?)", (oid,len(comps)))
            if comps:
                con.executemany("INSERT INTO unity_component(gameobject_object_id,ordinal,file_id,path_id) VALUES(?,?,?,?)", [(oid,*c) for c in comps])
        if rec["transform"] is not None:
            x = rec["transform"]
            con.execute(
                """INSERT INTO unity_transform(object_id,gameobject_file_id,gameobject_path_id,parent_file_id,parent_path_id,
                   local_position_json,local_rotation_json,local_scale_json,anchor_min_json,anchor_max_json,anchored_position_json,size_delta_json,pivot_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (oid,x["go"][0],x["go"][1],x["parent"][0],x["parent"][1],x["local_position_json"],x["local_rotation_json"],x["local_scale_json"],
                 x["anchor_min_json"],x["anchor_max_json"],x["anchored_position_json"],x["size_delta_json"],x["pivot_json"]),
            )
        if rec["parse_status"] == "failed":
            err_class = rec["parser_error"].split(":", 2)[1] if ":" in rec["parser_error"] else "unknown"
            con.execute("INSERT OR REPLACE INTO parser_error VALUES(?,?,?,?,?,?,?,?)",
                        (logical, rec["serialized_file_name"], rec["path_id"], rec["type_name"], err_class, rec["parser_error"], rec["byte_size"], rec["raw_sha256"]))
    # PPtr/component/Transform/MonoScript links are derived materializations.  Do
    # not run the correlated resolver once per bundle: on a 166k-bundle archive it
    # turns the single SQLite writer into the dominant bottleneck.  main() performs
    # one global reconciliation after the current ingest batch; an interrupted run
    # remains resumable because source object/ref rows and bundle_state are durable.
    con.execute(
        """INSERT OR REPLACE INTO bundle_state(logical_name,archive_sha256,status,object_count,parsed_count,partial_count,failed_count,
           reference_count,scalar_count,textasset_count,started_at,completed_at,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (logical, sha, "ok" if counts["failed"] == 0 else "partial", counts["objects"], counts["parsed"], counts["metadata-only"], counts["failed"],
         counts["refs"], counts["fields"], counts["textassets"], result.get("started_at"), utcnow(), ""),
    )
    if commit:
        con.commit()
    return counts


def select_jobs(rel: sqlite3.Connection, out: sqlite3.Connection, archive_root: Path, limit: int | None, retry_failed: bool, pilot: bool):
    done_statuses = ("ok", "partial") if retry_failed else ("ok", "partial", "failed")
    done = {r[0]: r[1] for r in out.execute("SELECT logical_name,status FROM bundle_state") if r[1] in done_statuses}
    base_rows = rel.execute("""
        SELECT a.logical_name,a.archive_sha256,bs.object_count,bs.object_types_json
        FROM asset a JOIN bundle_scan bs USING(logical_name)
        WHERE a.archived=1 AND a.archive_sha256 IS NOT NULL AND bs.scan_status='ok'
    """).fetchall()
    base = []
    for logical, archive_sha256, object_count, object_types_json in base_rows:
        try:
            object_types = json.loads(object_types_json or "{}")
            type_names = set(object_types)
        except Exception:
            type_names = set()
        base.append((
            logical,
            archive_sha256,
            object_count,
            len(type_names),
            int("MonoBehaviour" in type_names),
            int("TextAsset" in type_names),
            int("GameObject" in type_names),
            int(bool(type_names & {"AnimationClip", "AnimatorController"})),
        ))
    rows = [r for r in base if r[0] not in done]
    if pilot:
        def score(r):
            logical=r[0].lower()
            semantic=sum(1 for t in SEMANTIC_TERMS if t in logical)
            return (semantic*20 + r[4]*8+r[5]*8+r[6]*4+r[7]*4 + min(r[3],20), -min(r[2],20000))
        ranked=sorted(rows,key=score,reverse=True)
        picked=[]; seen=set()
        # Force representative class coverage before filling by overall score.
        for pred in (
            lambda r:r[5],
            lambda r:r[4] and r[6],
            lambda r:r[7],
            lambda r:any(t in r[0].lower() for t in SEMANTIC_TERMS),
        ):
            hit=next((r for r in ranked if r[0] not in seen and pred(r)),None)
            if hit:
                picked.append(hit); seen.add(hit[0])
        for r in ranked:
            if r[0] not in seen:
                picked.append(r); seen.add(r[0])
        rows=picked
    else:
        rows.sort(key=lambda r: r[0])
    if limit is not None:
        rows = rows[:limit]
    return [(r[0], r[1], str(archive_root), None) for r in rows]


def summarize(con: sqlite3.Connection, db: Path, rel_db: Path, output: Path) -> dict:
    # Flush committed WAL pages before hashing/sizing the database so the audit identity
    # represents the complete structured snapshot, not only the pre-checkpoint main file.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def one(sql, args=()): return con.execute(sql,args).fetchone()[0]
    rel = sqlite3.connect("file:" + rel_db.resolve().as_posix() + "?mode=ro", uri=True)
    try:
        source_bundles = rel.execute(
            "SELECT count(*) FROM asset a JOIN bundle_scan b USING(logical_name) "
            "WHERE a.archived=1 AND a.archive_sha256 IS NOT NULL AND b.scan_status='ok'"
        ).fetchone()[0]
        source_objects = rel.execute("SELECT count(*) FROM bundle_object").fetchone()[0]
        source_serialized_files = rel.execute("SELECT count(*) FROM bundle_serialized_file").fetchone()[0]
        source_archive_bytes = rel.execute(
            "SELECT COALESCE(sum(archive_size),0) FROM asset "
            "WHERE archived=1 AND archive_sha256 IS NOT NULL"
        ).fetchone()[0]
        source_object_bytes = rel.execute(
            "SELECT COALESCE(sum(byte_size),0) FROM bundle_object"
        ).fetchone()[0]
        source_class_rows = rel.execute(
            """SELECT type_name,count(*) AS objects,count(DISTINCT logical_name) AS bundles,
                      COALESCE(sum(byte_size),0) AS serialized_bytes
               FROM bundle_object GROUP BY type_name ORDER BY objects DESC"""
        ).fetchall()
    finally:
        rel.close()

    bundles = one("SELECT count(*) FROM bundle_state")
    objects = one("SELECT count(*) FROM unity_object")
    refs_total = one("SELECT count(*) FROM unity_object_reference")
    refs_resolved = one("SELECT count(*) FROM unity_object_reference WHERE target_object_id IS NOT NULL")
    refs_builtin = one("SELECT count(*) FROM unity_object_reference WHERE target_resolution_rule='builtin-resource'")
    refs_resolvable = refs_total - refs_builtin
    refs_resolvable_resolved = one(
        "SELECT count(*) FROM unity_object_reference "
        "WHERE target_resolution_rule<>'builtin-resource' AND target_object_id IS NOT NULL"
    )
    refs_internal = one("SELECT count(*) FROM unity_object_reference WHERE file_id=0")
    refs_internal_resolved = one("SELECT count(*) FROM unity_object_reference WHERE file_id=0 AND target_object_id IS NOT NULL")
    refs_external = one("SELECT count(*) FROM unity_object_reference WHERE file_id<>0")
    refs_external_builtin = one(
        "SELECT count(*) FROM unity_object_reference WHERE file_id<>0 AND target_resolution_rule='builtin-resource'"
    )
    refs_external_resolvable = refs_external - refs_external_builtin
    refs_external_resolved = one(
        "SELECT count(*) FROM unity_object_reference WHERE file_id<>0 "
        "AND target_resolution_rule<>'builtin-resource' AND target_object_id IS NOT NULL"
    )
    refs_unresolved_resolvable = refs_resolvable - refs_resolvable_resolved
    components = one("SELECT count(*) FROM unity_component")
    components_resolved = one("SELECT count(*) FROM unity_component WHERE component_object_id IS NOT NULL")
    transforms_with_parent = one("SELECT count(*) FROM unity_transform WHERE parent_path_id<>0")
    transforms_parent_resolved = one("SELECT count(*) FROM unity_transform WHERE parent_path_id<>0 AND parent_object_id IS NOT NULL")
    monobehaviours = one("SELECT count(*) FROM unity_monobehaviour")
    monobehaviours_resolved = one("SELECT count(*) FROM unity_monobehaviour WHERE script_class IS NOT NULL AND script_class<>''")
    db_size = db.stat().st_size if db.exists() else 0

    def ratio(n: int, d: int) -> float:
        return round((n * 100.0 / d), 4) if d else 0.0

    artifact = {
        "schema": "mltd-current-unity-assets-summary-v2",
        "schema_version": SCHEMA_VERSION,
        "database": str(db),
        "database_sha256": sha256_path(db),
        "database_size_bytes": db_size,
        "relationship_db": str(rel_db),
        "relationship_db_sha256": sha256_path(rel_db),
        "generated_at": utcnow(),
        "source_inventory": {
            "bundles": source_bundles,
            "serialized_files": source_serialized_files,
            "objects": source_objects,
            "archive_payload_bytes": source_archive_bytes,
            "serialized_object_bytes": source_object_bytes,
            "object_classes": len(source_class_rows),
        },
        "coverage": {
            "bundles_processed": bundles,
            "bundles_total": source_bundles,
            "bundle_percent": ratio(bundles, source_bundles),
            "objects_processed": objects,
            "objects_total": source_objects,
            "object_percent": ratio(objects, source_objects),
        },
        "counts": {
            "bundles": bundles,
            "bundles_ok": one("SELECT count(*) FROM bundle_state WHERE status='ok'"),
            "bundles_partial": one("SELECT count(*) FROM bundle_state WHERE status='partial'"),
            "bundles_failed": one("SELECT count(*) FROM bundle_state WHERE status='failed'"),
            "serialized_files_observed": one("SELECT count(*) FROM (SELECT DISTINCT logical_name,serialized_file_name FROM unity_object)"),
            "objects": objects,
            "objects_parsed": one("SELECT count(*) FROM unity_object WHERE parse_status='parsed'"),
            "objects_metadata_only": one("SELECT count(*) FROM unity_object WHERE parse_status='metadata-only'"),
            "objects_failed": one("SELECT count(*) FROM unity_object WHERE parse_status='failed'"),
            "objects_named": one("SELECT count(*) FROM unity_object WHERE object_name IS NOT NULL AND object_name<>''"),
            "objects_with_normalized_json": one("SELECT count(*) FROM unity_object WHERE normalized_json IS NOT NULL"),
            "normalized_json_compressed_bytes": one("SELECT COALESCE(sum(length(normalized_json)),0) FROM unity_object"),
            "object_raw_bytes": one("SELECT COALESCE(sum(byte_size),0) FROM unity_object"),
            "fields": one("SELECT count(*) FROM unity_object_field"),
            "references": refs_total,
            "references_resolved": refs_resolved,
            "references_builtin": refs_builtin,
            "references_resolvable": refs_resolvable,
            "references_resolvable_resolved": refs_resolvable_resolved,
            "references_resolvable_unresolved": refs_unresolved_resolvable,
            "references_internal": refs_internal,
            "references_internal_resolved": refs_internal_resolved,
            "references_external": refs_external,
            "references_external_builtin": refs_external_builtin,
            "references_external_resolvable": refs_external_resolvable,
            "references_external_resolved": refs_external_resolved,
            "monobehaviours": monobehaviours,
            "monobehaviours_script_resolved": monobehaviours_resolved,
            "monoscripts": one("SELECT count(*) FROM unity_monoscript"),
            "textassets": one("SELECT count(*) FROM unity_textasset"),
            "textasset_payload_bytes": one("SELECT COALESCE(sum(payload_size),0) FROM unity_textasset"),
            "gameobjects": one("SELECT count(*) FROM unity_gameobject"),
            "components": components,
            "components_resolved": components_resolved,
            "transforms": one("SELECT count(*) FROM unity_transform"),
            "transforms_with_parent": transforms_with_parent,
            "transforms_parent_resolved": transforms_parent_resolved,
            "semantic_tagged_objects": one("SELECT count(*) FROM unity_object WHERE semantic_tags<>''"),
        },
        "rates_percent": {
            "object_structured_parse": ratio(one("SELECT count(*) FROM unity_object WHERE parse_status='parsed'"), objects),
            "normalized_json_coverage": ratio(one("SELECT count(*) FROM unity_object WHERE normalized_json IS NOT NULL"), objects),
            "reference_resolution": ratio(refs_resolved, refs_total),
            "reference_resolution_resolvable": ratio(refs_resolvable_resolved, refs_resolvable),
            "internal_reference_resolution": ratio(refs_internal_resolved, refs_internal),
            "external_reference_resolution_resolvable": ratio(refs_external_resolved, refs_external_resolvable),
            "component_resolution": ratio(components_resolved, components),
            "transform_parent_resolution": ratio(transforms_parent_resolved, transforms_with_parent),
            "monobehaviour_script_resolution": ratio(monobehaviours_resolved, monobehaviours),
        },
        "density": {
            "database_bytes_per_object": round(db_size / objects, 2) if objects else 0.0,
            "fields_per_object": round(one("SELECT count(*) FROM unity_object_field") / objects, 4) if objects else 0.0,
            "references_per_object": round(refs_total / objects, 4) if objects else 0.0,
        },
        "bundle_status_histogram": dict(con.execute("SELECT status,count(*) FROM bundle_state GROUP BY status ORDER BY count(*) DESC")),
        "parse_status_histogram": dict(con.execute("SELECT parse_status,count(*) FROM unity_object GROUP BY parse_status ORDER BY count(*) DESC")),
        "object_class_histogram": dict(con.execute("SELECT type_name,count(*) FROM unity_object GROUP BY type_name ORDER BY count(*) DESC")),
        "source_object_class_histogram": {r[0]: r[1] for r in source_class_rows},
        "source_object_class_bundle_histogram": {r[0]: r[2] for r in source_class_rows},
        "source_object_class_serialized_bytes": {r[0]: r[3] for r in source_class_rows},
        "object_class_parse_histogram": {
            type_name: {"total": total, "parsed": parsed, "metadata_only": metadata_only, "failed": failed}
            for type_name,total,parsed,metadata_only,failed in con.execute(
                """SELECT type_name,count(*),
                          sum(CASE WHEN parse_status='parsed' THEN 1 ELSE 0 END),
                          sum(CASE WHEN parse_status='metadata-only' THEN 1 ELSE 0 END),
                          sum(CASE WHEN parse_status='failed' THEN 1 ELSE 0 END)
                   FROM unity_object GROUP BY type_name ORDER BY count(*) DESC"""
            )
        },
        "field_class_histogram": dict(con.execute(
            """SELECT o.type_name,count(*) n FROM unity_object_field f
               JOIN unity_object o ON o.object_id=f.object_id
               GROUP BY o.type_name ORDER BY n DESC"""
        )),
        "reference_source_class_histogram": dict(con.execute(
            """SELECT o.type_name,count(*) n FROM unity_object_reference r
               JOIN unity_object o ON o.object_id=r.object_id
               GROUP BY o.type_name ORDER BY n DESC"""
        )),
        "reference_resolution_rule_histogram": dict(con.execute(
            "SELECT COALESCE(target_resolution_rule,'(none)'),count(*) FROM unity_object_reference GROUP BY 1 ORDER BY count(*) DESC"
        )),
        "parse_error_histogram": {f"{a}:{b}": n for a,b,n in con.execute("SELECT type_name,error_class,count(*) FROM parser_error GROUP BY type_name,error_class ORDER BY count(*) DESC")},
        "textasset_classification_histogram": dict(con.execute("SELECT classification,count(*) FROM unity_textasset GROUP BY classification ORDER BY count(*) DESC")),
        "semantic_tag_histogram": dict(con.execute("SELECT semantic_tags,count(*) FROM unity_object WHERE semantic_tags<>'' GROUP BY semantic_tags ORDER BY count(*) DESC")),
        "managed_script_class_histogram": dict(con.execute("SELECT script_class,count(*) FROM unity_monobehaviour WHERE script_class IS NOT NULL AND script_class<>'' GROUP BY script_class ORDER BY count(*) DESC LIMIT 100")),
        "managed_script_assembly_histogram": dict(con.execute(
            "SELECT assembly_name,count(*) FROM unity_monoscript GROUP BY assembly_name ORDER BY count(*) DESC LIMIT 100"
        )),
        "top_scalar_field_paths": dict(con.execute("SELECT field_path,count(*) FROM unity_object_field GROUP BY field_path ORDER BY count(*) DESC LIMIT 100")),
    }
    processed_by_class = artifact["object_class_histogram"]
    artifact["object_class_coverage"] = {
        type_name: {
            "source_objects": source_count,
            "processed_objects": processed_by_class.get(type_name, 0),
            "coverage_percent": ratio(processed_by_class.get(type_name, 0), source_count),
            "source_bundles": bundle_count,
            "source_serialized_bytes": serialized_bytes,
        }
        for type_name, source_count, bundle_count, serialized_bytes in source_class_rows
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return artifact


def main():
    ap = argparse.ArgumentParser(description="Build resumable structured Unity asset DB from MLTD archived bundles.")
    ap.add_argument("--relationship-db", type=Path, default=ROOT / "build/current-asset-relationships.sqlite")
    ap.add_argument("--archive-root", type=Path, default=ROOT / "work/full-asset-archive-1077100")
    ap.add_argument("--output-db", type=Path, default=ROOT / "build/current-unity-assets.sqlite")
    ap.add_argument("--summary", type=Path, default=ROOT / "build/current-unity-assets-summary.json")
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4)//2)))
    ap.add_argument("--commit-bundles", type=int, default=25,
                    help="Commit the single SQLite writer every N completed bundles (resume may redo at most N-1 bundles after an abrupt stop).")
    ap.add_argument("--limit-bundles", type=int)
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--max-list-items", type=int, default=2048)
    args = ap.parse_args()

    rel_db = args.relationship_db.resolve()
    archive_root = args.archive_root.resolve()
    db = args.output_db.resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(db)
    out.execute("PRAGMA foreign_keys=ON")
    init_db(out, rel_db)
    sync_source_inventory(out, rel_db)
    reconcile_reference_graph(out)
    rel = sqlite3.connect("file:" + rel_db.as_posix() + "?mode=ro", uri=True)
    jobs = select_jobs(rel, out, archive_root, args.limit_bundles, args.retry_failed, args.pilot)
    jobs = [(a,b,c,args.max_list_items) for a,b,c,_ in jobs]
    rel.close()
    print(json_dumps({"selected_bundles":len(jobs),"workers":args.workers,"output_db":str(db)}), flush=True)
    if jobs:
        drop_final_query_indexes(out)
    totals = Counter()
    started = dt.datetime.now(dt.timezone.utc)
    if jobs:
        # Keep a bounded unordered work window. executor.map() preserves input order,
        # so one unusually heavy bundle can otherwise stall the single SQLite writer
        # while later workers have already finished. Submitting the entire 166k-job
        # archive at once would also waste memory, hence the small rolling window.
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            job_iter = iter(jobs)
            pending = set()
            window = max(args.workers * 2, 1)
            for _ in range(min(window, len(jobs))):
                try:
                    pending.add(pool.submit(scan_bundle, next(job_iter)))
                except StopIteration:
                    break
            i = 0
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    i += 1
                    c = write_bundle(out, result, commit=False)
                    totals.update(c)
                    if i % max(1, args.commit_bundles) == 0:
                        out.commit()
                    try:
                        pending.add(pool.submit(scan_bundle, next(job_iter)))
                    except StopIteration:
                        pass
                    if i % 10 == 0 or i == len(jobs):
                        elapsed=max((dt.datetime.now(dt.timezone.utc)-started).total_seconds(),0.001)
                        print(json_dumps({"bundles":i,"total":len(jobs),"objects":totals["objects"],"parsed":totals["parsed"],"failed":totals["failed"],
                                          "fields":totals["fields"],"refs":totals["refs"],"bundle_per_min":round(i*60/elapsed,2)}), flush=True)
        out.commit()
        # Build read/query indexes once instead of maintaining them row-by-row during ingest.
        # The reference reconciler benefits from the NOCASE object-key index.
        create_final_query_indexes(out)
        reconcile_reference_graph(out, force=True)
    else:
        create_final_query_indexes(out)
    artifact = summarize(out, db, rel_db, args.summary.resolve())
    out.close()
    print(json.dumps(artifact["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
