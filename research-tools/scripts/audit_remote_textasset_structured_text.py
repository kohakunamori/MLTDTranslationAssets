#!/usr/bin/env python3
"""Audit structured remote TextAsset string leaves against known localization sources.

JSON/MessagePack payload leaves indexed under $.textasset_payload are scanned
without reopening archived bundles. Plain/csv/tsv payloads are tracked separately
because the DB stores only their preview, not a parsed complete leaf index.
"""
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


def known_ids(paths: list[Path]) -> set[str]:
    result: set[str] = set()
    for path in paths:
        for row in iter_jsonl(path) or ():
            source = str(row.get("source", ""))
            sid = str(row.get("source_sha256", "")).strip()
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
        default=Path("build/localization-90200/remote-textasset-structured-jp-audit.json"),
    )
    args = ap.parse_args()

    known = known_ids(args.known)
    started = time.time()
    con = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")

    classifications = dict(
        con.execute(
            "SELECT classification,count(*) FROM unity_textasset GROUP BY classification"
        ).fetchall()
    )

    indexed_string_occurrences = 0
    jp_occurrences = 0
    rows: dict[str, dict] = {}
    logical_prefixes = Counter()
    field_paths = Counter()
    classifications_with_unknown = Counter()

    objects = list(
        con.execute(
            """
            SELECT t.object_id, o.logical_name, o.object_name, t.classification
            FROM unity_textasset t
            JOIN unity_object o ON o.object_id=t.object_id
            """
        )
    )
    for offset in range(0, len(objects), CHUNK_SIZE):
        chunk = objects[offset : offset + CHUNK_SIZE]
        metadata = {
            int(object_id): (logical, object_name, classification)
            for object_id, logical, object_name, classification in chunk
        }
        placeholders = ",".join("?" for _ in chunk)
        query = f"""
            SELECT object_id, field_path, text_value
            FROM unity_object_field
            WHERE object_id IN ({placeholders})
              AND field_path LIKE '$.textasset_payload%'
              AND text_value IS NOT NULL
              AND text_value<>''
        """
        for object_id, field_path, text in con.execute(query, tuple(metadata)):
            indexed_string_occurrences += 1
            if not JP_KANA_RE.search(text):
                continue
            jp_occurrences += 1
            sid = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if sid in known:
                continue
            logical, object_name, classification = metadata[int(object_id)]
            prefix = str(logical).split("/", 1)[0]
            logical_prefixes[prefix] += 1
            field_paths[str(field_path or "")] += 1
            classifications_with_unknown[str(classification or "")] += 1
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
                        "object_name": object_name,
                        "classification": classification,
                        "field_path": field_path,
                    }
                )

    con.close()
    values = sorted(rows.values(), key=lambda row: (-row["occurrences"], row["source"]))
    result = {
        "schema_version": 1,
        "kind": "mltd-remote-textasset-structured-jp-audit",
        "database": str(args.database),
        "known_source_ids": len(known),
        "textasset_classification_histogram": classifications,
        "scope": "complete indexed leaves for parsed JSON/MessagePack payloads",
        "nonparsed_payload_note": (
            "csv-like/tsv/text payloads are not complete in parsed leaf EAV; "
            "their full-payload coverage requires targeted archive extraction."
        ),
        "indexed_string_occurrences": indexed_string_occurrences,
        "jp_kana_occurrences": jp_occurrences,
        "unknown_unique": len(values),
        "top_logical_prefixes": logical_prefixes.most_common(50),
        "top_field_paths": field_paths.most_common(50),
        "unknown_classifications": classifications_with_unknown,
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
