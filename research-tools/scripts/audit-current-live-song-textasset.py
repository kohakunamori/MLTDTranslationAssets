#!/usr/bin/env python3
"""Audit frozen 1077100 song-tag TextAssets for explicit LIVE master-field evidence.

This scanner is evidence-only.  It searches decoded TextAsset payloads for exact
SongStatus/master field names and records bounded snippets.  A string hit is not
authority until its surrounding serialization/table semantics are validated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import UnityPy

ROOT = Path(__file__).resolve().parents[1]
TERMS = (
    b"mst_song_id",
    b"stage_id",
    b"stage_ts_id",
    b"mst_song_unit_id",
    b"song_unit_idol_id_list",
    b"mst_song_member_unit_id",
    b"song_member_unit_idol_id_list",
    b"unit_selection_type",
    b"extend_song_status",
    b"SongStatus",
    b"ExtendSongStatus",
)


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def textasset_bytes(obj) -> bytes:
    try:
        data = obj.read()
        script = getattr(data, "m_Script", None)
        if script is None:
            script = getattr(data, "script", None)
        if isinstance(script, str):
            return script.encode("utf-8", errors="replace")
        if isinstance(script, (bytes, bytearray, memoryview)):
            return bytes(script)
    except Exception:
        pass
    tree = obj.read_typetree()
    if isinstance(tree, dict):
        script = tree.get("m_Script", b"")
        if isinstance(script, str):
            return script.encode("utf-8", errors="replace")
        if isinstance(script, (bytes, bytearray, memoryview)):
            return bytes(script)
    return b""


def scan(item):
    logical, sha, path_ids, cas_root = item
    p = Path(cas_root) / sha[:2] / sha
    base = {
        "logical": logical,
        "archive_sha256": sha,
        "target_objects": len(path_ids),
    }
    if not p.is_file():
        return {**base, "error": "cas_missing"}
    try:
        env = UnityPy.load(str(p))
    except Exception as exc:
        return {**base, "error": f"unity_load:{type(exc).__name__}"}
    wanted = set(path_ids)
    hits = []
    object_errors = Counter()
    for obj in env.objects:
        if int(obj.path_id) not in wanted or obj.type.name != "TextAsset":
            continue
        try:
            raw = textasset_bytes(obj)
        except Exception as exc:
            object_errors[f"decode:{type(exc).__name__}"] += 1
            continue
        low = raw.lower()
        term_hits = []
        for term in TERMS:
            needle = term.lower()
            start = 0
            positions = []
            while True:
                pos = low.find(needle, start)
                if pos < 0:
                    break
                positions.append(pos)
                start = pos + len(needle)
                if len(positions) >= 16:
                    break
            for pos in positions:
                lo = max(0, pos - 192)
                hi = min(len(raw), pos + len(term) + 384)
                snippet = raw[lo:hi].decode("utf-8", errors="replace")
                term_hits.append({
                    "term": term.decode("ascii"),
                    "offset": pos,
                    "snippet": snippet,
                })
        if term_hits:
            name = ""
            try:
                data = obj.read()
                name = str(getattr(data, "m_Name", "") or getattr(data, "name", ""))
            except Exception:
                pass
            hits.append({
                "path_id": int(obj.path_id),
                "name": name,
                "payload_bytes": len(raw),
                "term_hits": term_hits,
            })
    out = {**base, "hits": hits}
    if object_errors:
        out["object_errors"] = dict(object_errors)
    return out if hits or object_errors else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "build/current-asset-relationships.sqlite")
    ap.add_argument("--archive-root", type=Path, default=ROOT / "work/full-asset-archive-1077100")
    ap.add_argument("--output", type=Path, default=ROOT / "build/current-live-song-textasset-audit.json")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    db = args.db.resolve()
    cas = (args.archive_root / "objects").resolve()
    con = sqlite3.connect("file:" + db.as_posix() + "?mode=ro", uri=True)
    rows = con.execute(
        """
        SELECT o.logical_name, a.archive_sha256, o.path_id
        FROM bundle_object o
        JOIN asset a USING(logical_name)
        JOIN asset_tag t USING(logical_name)
        WHERE o.type_name='TextAsset' AND t.tag='song'
          AND a.archived=1 AND a.archive_sha256 IS NOT NULL
        ORDER BY o.logical_name,o.path_id
        """
    ).fetchall()
    con.close()

    grouped = {}
    for logical, sha, pid in rows:
        grouped.setdefault(logical, [sha, []])[1].append(int(pid))
    items = [(logical, v[0], v[1], str(cas)) for logical, v in grouped.items()]

    hits = []
    bundle_errors = Counter()
    object_errors = Counter()
    term_counts = Counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i, res in enumerate(pool.map(scan, items, chunksize=8), 1):
            if res:
                if res.get("error"):
                    bundle_errors[res["error"]] += 1
                else:
                    if res.get("hits"):
                        hits.append(res)
                        for obj in res["hits"]:
                            term_counts.update(x["term"] for x in obj["term_hits"])
                    object_errors.update(res.get("object_errors") or {})
            if i % 250 == 0 or i == len(items):
                print(
                    f"scanned={i}/{len(items)} hit_bundles={len(hits)} "
                    f"bundle_errors={sum(bundle_errors.values())} object_errors={sum(object_errors.values())}",
                    flush=True,
                )

    artifact = {
        "schema": "mltd-current-live-song-textasset-audit-v1",
        "schema_version": 1,
        "inputs": {
            "relationship_db": str(db.relative_to(ROOT)),
            "relationship_db_sha256": sha256_path(db),
            "archive_root": str(args.archive_root.resolve().relative_to(ROOT)),
        },
        "search_terms": [x.decode("ascii") for x in TERMS],
        "counts": {
            "candidate_bundles": len(items),
            "candidate_textasset_objects": len(rows),
            "hit_bundles": len(hits),
            "hit_objects": sum(len(x["hits"]) for x in hits),
            "bundle_errors": sum(bundle_errors.values()),
            "object_errors": sum(object_errors.values()),
        },
        "term_hit_counts": dict(sorted(term_counts.items())),
        "errors": dict(sorted(bundle_errors.items())),
        "object_errors": dict(sorted(object_errors.items())),
        "hits": hits,
        "policy": {
            "formal_merge_allowed": False,
            "string_presence_is_not_row_authority": True,
            "requires_semantic_validation_before_use": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve().relative_to(ROOT)),
        "sha256": sha256_path(args.output),
        "counts": artifact["counts"],
        "term_hit_counts": artifact["term_hit_counts"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
