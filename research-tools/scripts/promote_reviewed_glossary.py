#!/usr/bin/env python3
"""Promote explicitly reviewed terminology candidates into a production glossary.

Only rows with review_status=approved are considered. Existing preferred terms are never
silently changed; an explicit --allow-update is required for reviewed replacements.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            yield row


def load_glossary(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("entries", {}), dict):
        raise ValueError("glossary must be an object with entries")
    value.setdefault("schema_version", 1)
    value.setdefault("entries", {})
    value.setdefault("kana_allowlist", [])
    return value


def build(glossary: dict, rows: list[dict], allow_update: bool) -> tuple[dict, dict]:
    result = json.loads(json.dumps(glossary, ensure_ascii=False))
    entries = result["entries"]
    approved = 0
    inserted = 0
    unchanged = 0
    updated = 0
    pending = 0
    rejected = 0

    for row in rows:
        review_status = str(row.get("review_status", "")).strip().lower()
        if review_status != "approved":
            if review_status == "rejected":
                rejected += 1
            else:
                pending += 1
            continue
        approved += 1
        source = str(row.get("source_term", "")).strip()
        preferred = str(row.get("approved_zh_cn", "")).strip() or str(row.get("suggested_zh_cn", "")).strip()
        if not source or not preferred:
            raise ValueError("approved glossary row requires source_term and approved/suggested zh-CN")

        categories = row.get("categories", [])
        category = ",".join(str(x) for x in categories) if isinstance(categories, list) else str(categories)
        review_notes = str(row.get("review_notes", "")).strip()
        notes = "Reviewed from source-bound historical official Chinese terminology."
        if review_notes:
            notes += f" {review_notes}"
        proposed = {
            "preferred": preferred,
            "forbidden": [],
            "category": category or "official_term",
            "notes": notes,
        }
        current = entries.get(source)
        if current is None:
            entries[source] = proposed
            inserted += 1
            continue
        current_preferred = str(current.get("preferred", "")) if isinstance(current, dict) else str(current)
        if current_preferred == preferred:
            unchanged += 1
            continue
        if not allow_update:
            raise ValueError(
                f"reviewed candidate conflicts with existing glossary: {source!r}: "
                f"existing={current_preferred!r} reviewed={preferred!r}"
            )
        entries[source] = proposed
        updated += 1

    summary = {
        "schema_version": 1,
        "candidate_rows": len(rows),
        "approved_rows": approved,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "pending_rows": pending,
        "rejected_rows": rejected,
        "allow_update": allow_update,
    }
    return result, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--glossary", type=Path, required=True)
    ap.add_argument("--reviews", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--allow-update", action="store_true")
    args = ap.parse_args()

    value, summary = build(load_glossary(args.glossary), list(read_jsonl(args.reviews)), args.allow_update)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.output)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
