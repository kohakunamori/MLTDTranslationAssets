#!/usr/bin/env python3
"""Build a complete manifest-level relationship database for frozen MLTD assets.

The database is intentionally evidence-tiered:
- manifest facts: logical name -> catalog key -> remote physical name -> declared size;
- archive facts: downloaded status, physical SHA-256, actual size;
- lexical families: normalized naming signatures over every logical asset;
- semantic entity links: current song/idol/card/costume-resource/stage/event tokens.

No naming-derived relation is promoted to business truth.  In particular,
catalog_hash is preserved as an opaque manifest key; it is not treated as a
content hash because archived calibration shows different physical SHA-256
values can share it.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import msgpack


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def load_manifest(path: Path) -> dict[str, list]:
    root = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(root, (list, tuple)) or len(root) != 1 or not isinstance(root[0], dict):
        raise ValueError("expected MLTD asset index shape [map]")
    table = root[0]
    for logical, value in table.items():
        if not isinstance(logical, str):
            raise ValueError(f"non-string logical name: {logical!r}")
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError(f"invalid manifest row for {logical!r}")
        if not isinstance(value[0], str) or not isinstance(value[1], str):
            raise ValueError(f"invalid string fields for {logical!r}")
        if not isinstance(value[2], int) or value[2] < 0:
            raise ValueError(f"invalid declared size for {logical!r}")
    return table


def suffix_chain(logical: str) -> str:
    p = logical.split(".")
    return ".".join(p[1:]) if len(p) > 1 else ""


def stem_of(logical: str) -> str:
    return logical.split(".", 1)[0]


def prefix_fields(stem: str) -> tuple[str, str]:
    toks = stem.split("_")
    return toks[0], "_".join(toks[:2])


def load_entities(fullsave_path: Path, catalog_path: Path):
    fullsave = json.loads(fullsave_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    lc = fullsave.get("local_content") or {}

    songs: dict[str, dict] = {}
    for row in lc.get("songs") or []:
        master = row.get("master") or {}
        rid = str(master.get("resource_id") or "")
        if not rid:
            continue
        songs[rid] = {
            "mst_song_id": int(master.get("mst_song_id") or 0),
            "resource_id": rid,
            "stage_id": int(master.get("stage_id") or 0),
            "stage_ts_id": int(master.get("stage_ts_id") or 0),
            "song_name": str((row.get("current_catalog") or {}).get("name") or ""),
            "policy": (row.get("policy") or {}).get("song") or {},
        }

    idols: dict[str, dict] = {}
    for row in (catalog.get("princess") or {}).get("idols") or []:
        rid = str(row.get("resourceId") or "")
        if not rid:
            continue
        idols[rid] = {
            "mst_idol_id": int(row.get("id") or 0),
            "resource_id": rid,
            "alphabet_name": str(row.get("alphabetName") or ""),
        }

    cards: dict[str, dict] = {}
    for row in (catalog.get("princess") or {}).get("cards") or []:
        rid = str(row.get("resourceId") or "")
        if not rid:
            continue
        cards[rid] = {
            "mst_card_id": int(row.get("id") or 0),
            "mst_idol_id": int(row.get("idolId") or 0),
            "resource_id": rid,
            "variation": int(row.get("variation") or 0),
            "rarity": int(row.get("rarity") or 0),
        }

    costume_groups: dict[str, dict] = {}
    for row in lc.get("costumes") or []:
        rid = str(row.get("resource_id") or "")
        if not rid:
            continue
        g = costume_groups.setdefault(
            rid,
            {
                "resource_id": rid,
                "mst_costume_ids": [],
                "mst_idol_ids": [],
                "costume_names": [],
            },
        )
        g["mst_costume_ids"].append(int(row.get("mst_costume_id") or 0))
        g["mst_idol_ids"].append(int(row.get("mst_idol_id") or 0))
        name = str(row.get("costume_name") or "")
        if name and name not in g["costume_names"]:
            g["costume_names"].append(name)

    return songs, idols, cards, costume_groups


def build_alt_regex(values: list[str]):
    values = [v for v in values if v]
    if not values:
        return None
    values.sort(key=lambda x: (-len(x), x))
    return re.compile(
        r"(?<![A-Za-z0-9])(" + "|".join(re.escape(v) for v in values) + r")(?![A-Za-z0-9])"
    )


CARD_RE = re.compile(r"\d{3}[a-z]{3}\d{4}", re.I)
IDOL_RE = re.compile(r"\d{3}[a-z]{3}", re.I)
STAGE_RE = re.compile(r"stage0*(\d+)_ts0*(\d+)", re.I)
EVENT_RE = re.compile(r"(?:^|_)event_?0*(\d{2,5})(?:_|$)", re.I)
SEASON_RE = re.compile(r"(?:^|_)season_?0*(\d{1,5})(?:_|$)", re.I)
STORY_RE = re.compile(r"(?:^|_)(?:main|event|card|job|memory|special)?_?story_?0*(\d{1,6})(?:_|$)", re.I)


def domain_tags(logical: str) -> list[str]:
    n = logical.lower()
    tags: set[str] = set()
    if n.startswith(("song3_", "jacket_", "live_info_", "scrobj_", "songname_", "bgm_inst_", "cam_", "dan_", "unitmsg_", "unitselecttips")):
        tags.add("song")
    if STAGE_RE.search(n) or n.startswith(("stage2d_", "ltmap_", "vj_")):
        tags.add("stage")
    if "card" in n or n.startswith(("mycard_",)):
        tags.add("card")
    if "costume" in n or n.startswith(("cfs_", "cas_", "ca_", "ch_")):
        tags.add("costume")
    if n.startswith(("idol_", "idolmsg_", "chara2d_", "character2d_", "chara_", "chr_")):
        tags.add("idol")
    if "event" in n:
        tags.add("event")
    if "story" in n or n.startswith(("adv_", "main_", "memory_", "special_")):
        tags.add("story")
    if n.endswith(".acb.unity3d") or n.endswith(".awb.unity3d") or n.startswith(("bgm_", "vc_", "sse_")):
        tags.add("audio")
    if n.endswith(".gtx.unity3d") or "localiz" in n or n.startswith("fhout_"):
        tags.add("text_or_localization")
    if n.endswith(".mp4.unity3d"):
        tags.add("video")
    if not tags:
        tags.add("other")
    return sorted(tags)


def normalize_family(
    logical: str,
    song_rx,
    costume_rx,
    card_resources: set[str],
    idol_resources: set[str],
    song_resources: set[str],
) -> str:
    stem = stem_of(logical).lower()
    suffix = suffix_chain(logical).lower()

    # Long exact resource classes first.
    if costume_rx is not None:
        stem = costume_rx.sub("<costume>", stem)

    def card_sub(m):
        v = m.group(0)
        return "<card>" if v in card_resources else v

    stem = CARD_RE.sub(card_sub, stem)

    def idol_sub(m):
        v = m.group(0)
        return "<idol>" if v in idol_resources else v

    stem = IDOL_RE.sub(idol_sub, stem)

    if song_rx is not None:
        stem = song_rx.sub("<song>", stem)

    # UnitSelect tips is the major concatenated song-resource exception.
    if stem.startswith("unitselecttips"):
        tail = stem[len("unitselecttips") :]
        if tail in song_resources:
            stem = "unitselecttips<song>"

    stem = STAGE_RE.sub("stage<stage>_ts<ts>", stem)
    # Normalize numeric tokens delimited by punctuation/underscores while
    # preserving lexical numbers embedded in family words such as song3/bg2d.
    stem = re.sub(r"(?<![A-Za-z])\d+(?![A-Za-z])", "<n>", stem)
    return f"{stem}.{suffix}" if suffix else stem


def main() -> int:
    ap = argparse.ArgumentParser()
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
    ap.add_argument("--fullsave", type=Path, default=Path("build/local-fullsave-content.json"))
    ap.add_argument("--catalog", type=Path, default=Path("build/current-catalog-source.json"))
    ap.add_argument("--output", type=Path, default=Path("build/current-asset-relationships.sqlite"))
    ap.add_argument("--summary", type=Path, default=Path("build/current-asset-relationships-summary.json"))
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="explicitly replace output/summary and SQLite sidecars; default is fail-closed",
    )
    args = ap.parse_args()

    for p in (args.asset_index, args.archive_index, args.fullsave, args.catalog):
        if not p.is_file():
            raise FileNotFoundError(p)

    def phase(name: str, fn):
        started = time.perf_counter()
        print(f"phase-start {name}", flush=True)
        value = fn()
        print(f"phase-done {name} seconds={time.perf_counter() - started:.3f}", flush=True)
        return value

    manifest = phase("load-manifest", lambda: load_manifest(args.asset_index))
    songs, idols, cards, costumes = phase(
        "load-entities", lambda: load_entities(args.fullsave, args.catalog)
    )
    song_resources = set(songs)
    idol_resources = set(idols)
    card_resources = set(cards)
    costume_resources = set(costumes)
    song_rx = phase("compile-song-regex", lambda: build_alt_regex(list(song_resources)))
    costume_rx = phase(
        "compile-costume-regex", lambda: build_alt_regex(list(costume_resources))
    )

    def load_archive():
        archive_uri = "file:" + args.archive_index.resolve().as_posix() + "?mode=ro"
        archive = sqlite3.connect(archive_uri, uri=True)
        archive.row_factory = sqlite3.Row
        archive_rows = {
            row["name"]: dict(row)
            for row in archive.execute(
                """SELECT name,sha256,size,status,content_type,etag,last_modified,
                          cache_control,fetched_at
                   FROM entries WHERE version=? AND scope=?""",
                (args.archive_version, args.archive_scope),
            )
        }
        archive_version = archive.execute(
            """SELECT version,asset_root,manifest_name,manifest_sha256,object_count,complete
               FROM versions WHERE version=? AND scope=?""",
            (args.archive_version, args.archive_scope),
        ).fetchone()
        archive.close()
        return archive_rows, archive_version

    archive_rows, archive_version = phase("load-archive-index", load_archive)
    if archive_version is None:
        raise RuntimeError(
            f"archive version/scope not found: {args.archive_version!r}/{args.archive_scope!r}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    output_sidecars = [
        args.output,
        Path(str(args.output) + "-journal"),
        Path(str(args.output) + "-wal"),
        Path(str(args.output) + "-shm"),
    ]
    existing_outputs = [p for p in output_sidecars + [args.summary] if p.exists()]
    if existing_outputs and not args.overwrite:
        raise FileExistsError(
            "refusing to replace existing relationship artifacts without --overwrite: "
            + ", ".join(str(p) for p in existing_outputs)
        )
    if args.overwrite:
        for p in existing_outputs:
            if p.is_file():
                p.unlink()
    print("phase-start create-schema", flush=True)
    schema_started = time.perf_counter()
    con = sqlite3.connect(args.output)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.executescript(
        """
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);

        CREATE TABLE asset(
            logical_name TEXT PRIMARY KEY,
            catalog_hash TEXT NOT NULL,
            remote_name TEXT NOT NULL UNIQUE,
            declared_size INTEGER NOT NULL,
            suffix_chain TEXT NOT NULL,
            prefix1 TEXT NOT NULL,
            prefix2 TEXT NOT NULL,
            family_signature TEXT NOT NULL,
            archived INTEGER NOT NULL,
            archive_status INTEGER,
            archive_sha256 TEXT,
            archive_size INTEGER,
            size_matches INTEGER,
            content_type TEXT,
            last_modified TEXT
        );

        CREATE TABLE asset_tag(
            logical_name TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY(logical_name,tag),
            FOREIGN KEY(logical_name) REFERENCES asset(logical_name)
        );

        CREATE TABLE asset_token(
            logical_name TEXT NOT NULL,
            token_type TEXT NOT NULL,
            token_value TEXT NOT NULL,
            evidence TEXT NOT NULL,
            PRIMARY KEY(logical_name,token_type,token_value,evidence),
            FOREIGN KEY(logical_name) REFERENCES asset(logical_name)
        );

        CREATE TABLE entity(
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            attrs_json TEXT NOT NULL,
            PRIMARY KEY(entity_type,entity_id)
        );

        CREATE TABLE asset_entity(
            logical_name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            evidence TEXT NOT NULL,
            PRIMARY KEY(logical_name,entity_type,entity_id,evidence),
            FOREIGN KEY(logical_name) REFERENCES asset(logical_name)
        );

        CREATE TABLE family(
            family_signature TEXT PRIMARY KEY,
            member_count INTEGER NOT NULL,
            archived_count INTEGER NOT NULL,
            tags_json TEXT NOT NULL,
            suffixes_json TEXT NOT NULL,
            prefixes_json TEXT NOT NULL
        );

        CREATE TABLE catalog_group(
            catalog_hash TEXT PRIMARY KEY,
            member_count INTEGER NOT NULL,
            fetched_count INTEGER NOT NULL,
            distinct_archive_sha256 INTEGER NOT NULL,
            distinct_sizes INTEGER NOT NULL,
            family_count INTEGER NOT NULL,
            primary_tags_json TEXT NOT NULL
        );

        CREATE TABLE content_group(
            archive_sha256 TEXT PRIMARY KEY,
            member_count INTEGER NOT NULL,
            logicals_json TEXT NOT NULL
        );

        -- Filled incrementally by scan-current-asset-bundle-relationships.py.
        CREATE TABLE bundle_scan(
            logical_name TEXT PRIMARY KEY,
            scan_status TEXT NOT NULL,
            error TEXT NOT NULL,
            object_count INTEGER NOT NULL,
            object_types_json TEXT NOT NULL,
            assetbundle_name TEXT NOT NULL,
            streamed_scene INTEGER,
            assetbundle_object_count INTEGER NOT NULL DEFAULT 0,
            serialized_file_count INTEGER NOT NULL DEFAULT 0,
            container_raw_count INTEGER NOT NULL DEFAULT 0,
            container_skipped_count INTEGER NOT NULL DEFAULT 0,
            dependency_raw_count INTEGER NOT NULL DEFAULT 0,
            dependency_skipped_count INTEGER NOT NULL DEFAULT 0,
            scanned_at TEXT NOT NULL
        );
        CREATE TABLE bundle_serialized_file(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            unity_version TEXT NOT NULL,
            external_count INTEGER NOT NULL,
            externals_json TEXT NOT NULL,
            PRIMARY KEY(logical_name,serialized_file_name)
        );
        CREATE TABLE bundle_external_ref(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            external_path TEXT NOT NULL,
            guid TEXT NOT NULL,
            external_type INTEGER NOT NULL,
            target_serialized_file_name TEXT,
            PRIMARY KEY(logical_name,serialized_file_name,file_id)
        );
        CREATE TABLE bundle_object(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            path_id INTEGER NOT NULL,
            type_id INTEGER,
            class_id INTEGER,
            type_name TEXT NOT NULL,
            byte_size INTEGER,
            PRIMARY KEY(logical_name,serialized_file_name,path_id)
        );
        CREATE TABLE bundle_assetbundle(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            assetbundle_path_id INTEGER NOT NULL,
            observed_bundle_name TEXT NOT NULL,
            streamed_scene INTEGER,
            container_shape TEXT NOT NULL,
            container_raw_count INTEGER NOT NULL,
            container_skipped_count INTEGER NOT NULL,
            dependency_raw_count INTEGER NOT NULL,
            dependency_skipped_count INTEGER NOT NULL,
            PRIMARY KEY(logical_name,serialized_file_name,assetbundle_path_id)
        );
        CREATE TABLE bundle_container(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            assetbundle_path_id INTEGER NOT NULL,
            container_ordinal INTEGER NOT NULL,
            container_path TEXT NOT NULL,
            preload_index INTEGER,
            preload_size INTEGER,
            asset_file_id INTEGER,
            asset_path_id INTEGER,
            resolved_logical_name TEXT,
            resolution_rule TEXT,
            PRIMARY KEY(
                logical_name,serialized_file_name,assetbundle_path_id,
                container_ordinal
            )
        );

        CREATE TABLE bundle_dependency(
            logical_name TEXT NOT NULL,
            serialized_file_name TEXT NOT NULL,
            assetbundle_path_id INTEGER NOT NULL,
            dependency_ordinal INTEGER NOT NULL,
            dependency_name TEXT NOT NULL,
            resolved_logical_name TEXT,
            resolution_rule TEXT,
            PRIMARY KEY(
                logical_name,serialized_file_name,assetbundle_path_id,dependency_ordinal
            )
        );
        CREATE TABLE bundle_name_relation(
            logical_name TEXT NOT NULL,
            observed_bundle_name TEXT NOT NULL,
            resolved_logical_name TEXT,
            resolution_rule TEXT NOT NULL,
            PRIMARY KEY(logical_name,observed_bundle_name)
        );
        CREATE TABLE deep_scan_metadata(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE VIEW bundle_container_object_edge AS
        SELECT
            c.logical_name,
            c.serialized_file_name AS source_serialized_file_name,
            c.assetbundle_path_id,
            c.container_ordinal,
            c.container_path,
            c.asset_file_id,
            c.asset_path_id,
            CASE
              WHEN c.asset_file_id=0 THEN c.serialized_file_name
              ELSE e.target_serialized_file_name
            END AS target_serialized_file_name,
            o.type_name AS target_type_name,
            o.type_id AS target_type_id,
            o.class_id AS target_class_id,
            o.byte_size AS target_byte_size
        FROM bundle_container c
        LEFT JOIN bundle_external_ref e
          ON c.asset_file_id>0
         AND e.logical_name=c.logical_name
         AND e.serialized_file_name=c.serialized_file_name
         AND e.file_id=c.asset_file_id
        LEFT JOIN bundle_object o
          ON o.logical_name=c.logical_name
         AND o.serialized_file_name=(
             CASE
               WHEN c.asset_file_id=0 THEN c.serialized_file_name
               ELSE e.target_serialized_file_name
             END
         )
         AND o.path_id=c.asset_path_id;
        """
    )
    print(
        f"phase-done create-schema seconds={time.perf_counter() - schema_started:.3f}",
        flush=True,
    )

    meta = {
        "schema": "mltd-current-asset-relationships-v2",
        "build_complete": "0",
        "manifest_entry_count": str(len(manifest)),
        "asset_index": str(args.asset_index),
        "asset_index_sha256": sha256_file(args.asset_index),
        "archive_index": str(args.archive_index),
        "archive_index_sha256": sha256_file(args.archive_index),
        "fullsave": str(args.fullsave),
        "fullsave_sha256": sha256_file(args.fullsave),
        "catalog": str(args.catalog),
        "catalog_sha256": sha256_file(args.catalog),
        "archive_requested_version": str(args.archive_version),
        "archive_requested_scope": str(args.archive_scope),
    }
    if archive_version is not None:
        meta.update(
            {
                "archive_version": str(archive_version[0]),
                "archive_asset_root": str(archive_version[1]),
                "archive_manifest_name": str(archive_version[2]),
                "archive_manifest_sha256": str(archive_version[3]),
                "archive_object_count": str(archive_version[4]),
                "archive_complete": str(archive_version[5]),
            }
        )
    con.executemany("INSERT INTO metadata VALUES (?,?)", meta.items())

    # Semantic entities.
    for rid, row in songs.items():
        con.execute(
            "INSERT INTO entity VALUES (?,?,?,?)",
            ("song", str(row["mst_song_id"]), rid, json.dumps(row, ensure_ascii=False, separators=(",", ":"))),
        )
    for rid, row in idols.items():
        con.execute(
            "INSERT INTO entity VALUES (?,?,?,?)",
            ("idol", str(row["mst_idol_id"]), rid, json.dumps(row, ensure_ascii=False, separators=(",", ":"))),
        )
    for rid, row in cards.items():
        con.execute(
            "INSERT INTO entity VALUES (?,?,?,?)",
            ("card", rid, rid, json.dumps(row, ensure_ascii=False, separators=(",", ":"))),
        )
    for rid, row in costumes.items():
        con.execute(
            "INSERT INTO entity VALUES (?,?,?,?)",
            ("costume_resource", rid, rid, json.dumps(row, ensure_ascii=False, separators=(",", ":"))),
        )

    domain_counts = collections.Counter()
    suffix_counts = collections.Counter()
    prefix1_counts = collections.Counter()
    family_acc: dict[str, dict] = {}
    catalog_acc: dict[str, dict] = {}
    content_acc: dict[str, list[str]] = collections.defaultdict(list)
    size_mismatch = 0
    fetched = 0

    for i, (logical, rec) in enumerate(manifest.items(), 1):
        cat_hash, remote, declared = rec
        stem = stem_of(logical).lower()
        suffix = suffix_chain(logical).lower()
        p1, p2 = prefix_fields(stem)
        tags = domain_tags(logical)
        fam = normalize_family(
            logical,
            song_rx,
            costume_rx,
            card_resources,
            idol_resources,
            song_resources,
        )
        ar = archive_rows.get(remote) or {}
        status = ar.get("status")
        ash = str(ar.get("sha256") or "")
        asize = ar.get("size")
        # A fully admitted archive object can retain HTTP 206 provenance when the
        # downloader used a ranged/resumed transfer.  Presence of the content-addressed
        # SHA-256 binding is the material archive fact; both 200 and 206 are successful
        # payload responses.  Size integrity remains tracked independently below.
        is_archived = int(status in (200, 206) and bool(ash))
        size_matches = None if asize is None else int(int(asize) == int(declared))
        if is_archived:
            fetched += 1
            content_acc[ash].append(logical)
        if size_matches == 0:
            size_mismatch += 1

        con.execute(
            """INSERT INTO asset VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                logical,
                cat_hash,
                remote,
                int(declared),
                suffix,
                p1,
                p2,
                fam,
                is_archived,
                int(status) if status is not None else None,
                ash or None,
                int(asize) if asize is not None else None,
                size_matches,
                str(ar.get("content_type") or ""),
                str(ar.get("last_modified") or ""),
            ),
        )
        for tag in tags:
            con.execute("INSERT OR IGNORE INTO asset_tag VALUES (?,?)", (logical, tag))
            domain_counts[tag] += 1
        suffix_counts[suffix] += 1
        prefix1_counts[p1] += 1

        fa = family_acc.setdefault(
            fam,
            {
                "count": 0,
                "archived": 0,
                "tags": collections.Counter(),
                "suffixes": collections.Counter(),
                "prefixes": collections.Counter(),
            },
        )
        fa["count"] += 1
        fa["archived"] += is_archived
        fa["tags"].update(tags)
        fa["suffixes"][suffix] += 1
        fa["prefixes"][p1] += 1

        ca = catalog_acc.setdefault(
            cat_hash,
            {
                "count": 0,
                "fetched": 0,
                "sha": set(),
                "sizes": set(),
                "families": set(),
                "tags": collections.Counter(),
            },
        )
        ca["count"] += 1
        ca["fetched"] += is_archived
        if ash:
            ca["sha"].add(ash)
        ca["sizes"].add(int(declared))
        ca["families"].add(fam)
        ca["tags"].update(tags)

        # Idol and card identities can coexist; card assets often embed the idol code.
        seen_idols = set()
        for m in IDOL_RE.finditer(stem):
            rid = m.group(0)
            if rid in idol_resources and rid not in seen_idols:
                seen_idols.add(rid)
                iid = str(idols[rid]["mst_idol_id"])
                con.execute(
                    "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                    (logical, "idol_resource", rid, "logical-name"),
                )
                con.execute(
                    "INSERT OR IGNORE INTO asset_entity VALUES (?,?,?,?)",
                    (logical, "idol", iid, "logical-name:idol-resource"),
                )

        seen_cards = set()
        for m in CARD_RE.finditer(stem):
            rid = m.group(0)
            if rid in card_resources and rid not in seen_cards:
                seen_cards.add(rid)
                con.execute(
                    "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                    (logical, "card_resource", rid, "logical-name"),
                )
                con.execute(
                    "INSERT OR IGNORE INTO asset_entity VALUES (?,?,?,?)",
                    (logical, "card", rid, "logical-name:card-resource"),
                )

        song_hits = set()
        if song_rx is not None:
            song_hits.update(m.group(1) for m in song_rx.finditer(stem))
        if stem.startswith("unitselecttips"):
            tail = stem[len("unitselecttips") :]
            if tail in song_resources:
                song_hits.add(tail)
        for rid in sorted(song_hits):
            sid = str(songs[rid]["mst_song_id"])
            con.execute(
                "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                (logical, "song_resource", rid, "logical-name"),
            )
            con.execute(
                "INSERT OR IGNORE INTO asset_entity VALUES (?,?,?,?)",
                (logical, "song", sid, "logical-name:song-resource"),
            )

        costume_hits = set()
        if costume_rx is not None:
            costume_hits.update(m.group(1) for m in costume_rx.finditer(stem))
        for rid in sorted(costume_hits):
            con.execute(
                "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                (logical, "costume_resource", rid, "logical-name"),
            )
            con.execute(
                "INSERT OR IGNORE INTO asset_entity VALUES (?,?,?,?)",
                (logical, "costume_resource", rid, "logical-name:costume-resource"),
            )

        for m in STAGE_RE.finditer(stem):
            sid, tsid = int(m.group(1)), int(m.group(2))
            key = f"{sid}:{tsid}"
            con.execute(
                "INSERT OR IGNORE INTO entity VALUES (?,?,?,?)",
                (
                    "stage_pair",
                    key,
                    f"stage{sid:03d}_ts{tsid:02d}",
                    json.dumps({"stage_id": sid, "stage_ts_id": tsid}, separators=(",", ":")),
                ),
            )
            con.execute(
                "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                (logical, "stage_pair", key, "logical-name"),
            )
            con.execute(
                "INSERT OR IGNORE INTO asset_entity VALUES (?,?,?,?)",
                (logical, "stage_pair", key, "logical-name:stage-pattern"),
            )

        for m in EVENT_RE.finditer(stem):
            con.execute(
                "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                (logical, "event_id", str(int(m.group(1))), "logical-name"),
            )
        for m in SEASON_RE.finditer(stem):
            con.execute(
                "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                (logical, "season_id", str(int(m.group(1))), "logical-name"),
            )
        for m in STORY_RE.finditer(stem):
            con.execute(
                "INSERT OR IGNORE INTO asset_token VALUES (?,?,?,?)",
                (logical, "story_id", str(int(m.group(1))), "logical-name"),
            )

        if i % 10000 == 0:
            con.commit()
            print(f"ingest {i}/{len(manifest)}", flush=True)

    for fam, row in family_acc.items():
        con.execute(
            "INSERT INTO family VALUES (?,?,?,?,?,?)",
            (
                fam,
                row["count"],
                row["archived"],
                json.dumps(row["tags"].most_common(), separators=(",", ":")),
                json.dumps(row["suffixes"].most_common(), separators=(",", ":")),
                json.dumps(row["prefixes"].most_common(), separators=(",", ":")),
            ),
        )
    for key, row in catalog_acc.items():
        con.execute(
            "INSERT INTO catalog_group VALUES (?,?,?,?,?,?,?)",
            (
                key,
                row["count"],
                row["fetched"],
                len(row["sha"]),
                len(row["sizes"]),
                len(row["families"]),
                json.dumps(row["tags"].most_common(), separators=(",", ":")),
            ),
        )
    for ash, logicals in content_acc.items():
        con.execute(
            "INSERT INTO content_group VALUES (?,?,?)",
            (ash, len(logicals), json.dumps(sorted(logicals), separators=(",", ":"))),
        )

    con.commit()
    print("creating secondary indexes", flush=True)
    con.executescript(
        """
        CREATE INDEX idx_asset_catalog_hash ON asset(catalog_hash);
        CREATE INDEX idx_asset_family ON asset(family_signature);
        CREATE INDEX idx_asset_prefix1 ON asset(prefix1);
        CREATE INDEX idx_asset_prefix2 ON asset(prefix2);
        CREATE INDEX idx_asset_archive_sha ON asset(archive_sha256);
        CREATE INDEX idx_asset_archived ON asset(archived);
        CREATE INDEX idx_asset_tag ON asset_tag(tag);
        CREATE INDEX idx_token_type_value ON asset_token(token_type,token_value);
        CREATE INDEX idx_entity_resource ON entity(entity_type,resource_id);
        CREATE INDEX idx_asset_entity_entity ON asset_entity(entity_type,entity_id);
        CREATE INDEX idx_bundle_object_type ON bundle_object(type_name);
        CREATE INDEX idx_bundle_container_path ON bundle_container(container_path);
        CREATE INDEX idx_bundle_container_pptr ON bundle_container(
            logical_name,serialized_file_name,asset_file_id,asset_path_id
        );
        CREATE INDEX idx_bundle_dep_resolved ON bundle_dependency(resolved_logical_name);
        """
    )
    con.commit()

    # Calibrate the opaque manifest catalog key against real downloaded SHA-256.
    duplicate_catalog_groups = con.execute(
        "SELECT COUNT(*) FROM catalog_group WHERE member_count > 1"
    ).fetchone()[0]
    tested_catalog_groups = con.execute(
        "SELECT COUNT(*) FROM catalog_group WHERE member_count > 1 AND fetched_count >= 2"
    ).fetchone()[0]
    catalog_groups_same_physical_sha = con.execute(
        """SELECT COUNT(*) FROM catalog_group
           WHERE member_count > 1 AND fetched_count >= 2
             AND distinct_archive_sha256 = 1"""
    ).fetchone()[0]
    catalog_groups_different_physical_sha = con.execute(
        """SELECT COUNT(*) FROM catalog_group
           WHERE member_count > 1 AND fetched_count >= 2
             AND distinct_archive_sha256 > 1"""
    ).fetchone()[0]

    tag_coverage = {}
    for tag, count in con.execute(
        """SELECT t.tag,COUNT(*) FROM asset_tag t GROUP BY t.tag ORDER BY COUNT(*) DESC"""
    ):
        archived_count = con.execute(
            """SELECT COUNT(*) FROM asset a JOIN asset_tag t USING(logical_name)
               WHERE t.tag=? AND a.archived=1""",
            (tag,),
        ).fetchone()[0]
        tag_coverage[tag] = {
            "assets": count,
            "archived": archived_count,
            "archive_ratio": round(archived_count / count, 6) if count else 0.0,
        }

    top_families = [
        {"family_signature": r[0], "members": r[1], "archived": r[2]}
        for r in con.execute(
            "SELECT family_signature,member_count,archived_count FROM family ORDER BY member_count DESC,family_signature LIMIT 100"
        )
    ]
    top_catalog_groups = [
        {
            "catalog_hash": r[0],
            "members": r[1],
            "fetched": r[2],
            "distinct_archive_sha256": r[3],
            "family_count": r[4],
        }
        for r in con.execute(
            """SELECT catalog_hash,member_count,fetched_count,distinct_archive_sha256,family_count
               FROM catalog_group ORDER BY member_count DESC,catalog_hash LIMIT 100"""
        )
    ]

    summary = {
        "schema_version": 1,
        "status": "manifest-complete_bundle-deep-scan-pending",
        "inputs": {
            "asset_index": {"path": str(args.asset_index), "sha256": sha256_file(args.asset_index)},
            "archive_index": {"path": str(args.archive_index), "sha256": sha256_file(args.archive_index)},
            "fullsave": {"path": str(args.fullsave), "sha256": sha256_file(args.fullsave)},
            "catalog": {"path": str(args.catalog), "sha256": sha256_file(args.catalog)},
        },
        "counts": {
            "manifest_assets": len(manifest),
            "unique_remote_names": len({v[1] for v in manifest.values()}),
            "archived_assets": fetched,
            "unarchived_assets": len(manifest) - fetched,
            "size_mismatches": size_mismatch,
            "families": len(family_acc),
            "catalog_groups": len(catalog_acc),
            "duplicate_catalog_groups": duplicate_catalog_groups,
            "exact_physical_sha_groups": len(content_acc),
            "song_entities": len(songs),
            "idol_entities": len(idols),
            "card_entities": len(cards),
            "costume_resource_entities": len(costumes),
            "asset_entity_links": con.execute("SELECT COUNT(*) FROM asset_entity").fetchone()[0],
            "asset_tokens": con.execute("SELECT COUNT(*) FROM asset_token").fetchone()[0],
        },
        "catalog_hash_calibration": {
            "tested_duplicate_groups_with_at_least_two_downloaded_members": tested_catalog_groups,
            "same_physical_sha256_groups": catalog_groups_same_physical_sha,
            "different_physical_sha256_groups": catalog_groups_different_physical_sha,
            "interpretation": (
                "catalog_hash is an opaque manifest grouping/routing key, not a physical content digest; "
                "never equate members solely because this field matches."
            ),
        },
        "archive_coverage_by_tag": tag_coverage,
        "suffix_counts": dict(suffix_counts.most_common()),
        "prefix1_top": dict(prefix1_counts.most_common(200)),
        "top_families": top_families,
        "top_catalog_groups": top_catalog_groups,
        "output_db": str(args.output),
    }
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("summary_sha256", sha256_file(args.summary)))
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("build_complete", "1"))
    con.commit()
    con.close()

    print(
        json.dumps(
            {
                "output": str(args.output),
                "summary": str(args.summary),
                "summary_sha256": sha256_file(args.summary),
                "counts": summary["counts"],
                "catalog_hash_calibration": summary["catalog_hash_calibration"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
