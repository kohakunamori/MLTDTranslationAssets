#!/usr/bin/env python3
"""Extract source-bound official terminology candidates for MLTD glossary review."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import read_jsonl, validate_translation

RULES = (
    (re.compile(r"^ld_idol_fullname_\d+$"), "idol_name"),
    (re.compile(r"^ld_unit_name_\d+$"), "unit_name"),
    (re.compile(r"^ld_song_name_\d+$"), "song_name"),
    (re.compile(r"^ld_event_name_\d+$"), "event_name"),
    (re.compile(r"^ld_title_name_\d+$"), "title_name"),
)


def category_for_key(key: str) -> str | None:
    for pattern, category in RULES:
        if pattern.match(key):
            return category
    return None


def build(rows: list[dict]) -> tuple[list[dict], dict]:
    by_source: dict[str, list[dict]] = defaultdict(list)
    selected_rows = 0
    for row in rows:
        if str(row.get("status", "")) != "official_legacy":
            continue
        key = str(row.get("key", ""))
        category = category_for_key(key)
        if not category:
            continue
        source = str(row.get("source", ""))
        translation = str(row.get("translation", ""))
        if not source or not translation:
            continue
        try:
            validate_translation(source, translation)
        except Exception:
            continue
        selected_rows += 1
        by_source[source].append({
            "key": key,
            "category": category,
            "translation": translation,
            "logical": str(row.get("current_logical", "")),
            "provenance": str(row.get("provenance", "official-legacy-zh")),
        })

    out: list[dict] = []
    ambiguous = 0
    for source, evidence in sorted(by_source.items()):
        translations = sorted({str(x["translation"]) for x in evidence})
        categories = sorted({str(x["category"]) for x in evidence})
        if len(translations) != 1:
            ambiguous += 1
            status = "ambiguous_official_mapping"
            preferred = ""
        else:
            status = "needs_zhcn_normalization"
            preferred = translations[0]
        out.append({
            "source_term": source,
            "official_traditional": preferred,
            "official_candidates": translations,
            "categories": categories,
            "status": status,
            "evidence_count": len(evidence),
            "evidence": evidence[:12],
        })

    summary = {
        "schema_version": 1,
        "official_rows_scanned": len(rows),
        "selected_term_rows": selected_rows,
        "unique_source_terms": len(out),
        "unambiguous_official_terms": sum(x["status"] == "needs_zhcn_normalization" for x in out),
        "ambiguous_official_terms": ambiguous,
        "categories": {
            category: sum(category in x["categories"] for x in out)
            for category in sorted({c for x in out for c in x["categories"]})
        },
        "auto_merge_into_zhcn_glossary": False,
        "reason": "Historical official text is Traditional Chinese/regional wording and must be normalized/reviewed before becoming a preferred zh-CN term.",
    }
    return out, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legacy", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    args = ap.parse_args()
    rows, summary = build(read_jsonl(args.legacy))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
