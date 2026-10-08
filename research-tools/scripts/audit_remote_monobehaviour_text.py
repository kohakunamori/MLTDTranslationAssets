#!/usr/bin/env python3
"""Audit remote MonoBehaviour Japanese string fields against known localization sources."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")
CHUNK_SIZE = 500


def iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row


def source_ids(paths: list[Path]) -> set[str]:
    result: set[str] = set()
    for path in paths:
        for row in iter_jsonl(path) or ():
            sid = str(row.get("source_sha256", "")).strip()
            source = str(row.get("source", ""))
            if not sid and source:
                sid = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if sid:
                result.add(sid)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", type=Path, default=Path("build/current-unity-assets.sqlite"))
    ap.add_argument(
        "--known",
        type=Path,
        action="append",
        default=[
            Path("build/localization-90200/translation-memory.jsonl"),
            Path("build/localization-90200/machine-translation-nongtx-queue.jsonl"),
        ],
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/remote-monobehaviour-jp-text-audit.json"),
    )
    args = ap.parse_args()

    known = source_ids(args.known)
    started = time.time()
    con = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    text_occurrences = 0
    jp_occurrences = 0
    rows: dict[str, dict] = {}
    scripts = Counter()
    field_paths = Counter()

    # unity_object_field is a large EAV table and only has an
    # (object_id, field_path) primary-key index.  A text_value GLOB over the
    # joined table can therefore degenerate into a full EAV scan.  Drive the
    # audit from the much smaller MonoBehaviour id set and probe the field table
    # in bounded object-id chunks so SQLite can use the PK prefix.
    objects = list(
        con.execute(
            """
            SELECT mb.object_id, o.logical_name, mb.script_class
            FROM unity_monobehaviour mb
            JOIN unity_object o ON o.object_id=mb.object_id
            """
        )
    )
    for offset in range(0, len(objects), CHUNK_SIZE):
        chunk = objects[offset : offset + CHUNK_SIZE]
        metadata = {
            int(object_id): (logical, script_class)
            for object_id, logical, script_class in chunk
        }
        placeholders = ",".join("?" for _ in chunk)
        query = f"""
            SELECT object_id, field_path, text_value
            FROM unity_object_field
            WHERE object_id IN ({placeholders})
              AND text_value IS NOT NULL
              AND text_value<>''
        """
        for object_id, field_path, text in con.execute(query, tuple(metadata)):
            text_occurrences += 1
            if not JP_KANA_RE.search(text):
                continue
            jp_occurrences += 1
            sid = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if sid in known:
                continue
            logical, script_class = metadata[int(object_id)]
            scripts[str(script_class or "")] += 1
            field_paths[str(field_path or "")] += 1
            row = rows.setdefault(
                sid,
                {
                    "source_sha256": sid,
                    "source": text,
                    "occurrences": 0,
                    "examples": [],
                },
            )
            row["occurrences"] += 1
            if len(row["examples"]) < 3:
                row["examples"].append(
                    {
                        "logical_name": logical,
                        "script_class": script_class,
                        "field_path": field_path,
                    }
                )
    con.close()

    values = sorted(rows.values(), key=lambda row: (-row["occurrences"], row["source"]))
    result = {
        "schema_version": 1,
        "kind": "mltd-remote-monobehaviour-jp-text-audit",
        "database": str(args.database),
        "known_source_ids": len(known),
        "monobehaviour_text_field_occurrences": text_occurrences,
        "jp_kana_occurrences": jp_occurrences,
        "unknown_unique": len(values),
        "top_scripts": scripts.most_common(50),
        "top_field_paths": field_paths.most_common(50),
        "elapsed_seconds": round(time.time() - started, 3),
        "rows": values,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
