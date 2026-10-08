#!/usr/bin/env python3
"""Build a deterministic hidden-reference benchmark from historical official MLTD Chinese rows."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import read_jsonl, validate_translation


def sid(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_key(seed: str, source_sha256: str) -> str:
    return hashlib.sha256((seed + ":" + source_sha256).encode("utf-8")).hexdigest()


def build_candidates(rows: list[dict]) -> tuple[list[dict], dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if str(row.get("status", "")) != "official_legacy":
            continue
        source = str(row.get("source", ""))
        translation = str(row.get("translation", ""))
        if not source or not translation:
            continue
        try:
            validate_translation(source, translation)
        except Exception:
            continue
        grouped[source].append(row)

    candidates: list[dict] = []
    ambiguous = 0
    for source, source_rows in grouped.items():
        translations = {str(r.get("translation", "")) for r in source_rows}
        if len(translations) != 1:
            ambiguous += 1
            continue
        row = source_rows[0]
        logical = str(row.get("current_logical", ""))
        candidates.append({
            "source_sha256": sid(source),
            "source": source,
            "reference_translation": next(iter(translations)),
            "category": logical.split("_", 1)[0] if logical else "unknown",
            "current_logical": logical,
            "bundle": row.get("bundle", ""),
            "key": row.get("key", ""),
            "provenance": row.get("provenance", "official-legacy-zh"),
            "occurrences_in_official_rows": len(source_rows),
        })
    return candidates, {
        "official_rows": sum(1 for r in rows if str(r.get("status", "")) == "official_legacy"),
        "unique_official_sources": len(grouped),
        "ambiguous_reference_sources": ambiguous,
        "unambiguous_reference_sources": len(candidates),
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legacy", type=Path, required=True)
    ap.add_argument("--source-output", type=Path, required=True)
    ap.add_argument("--reference-output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--per-category", type=int, default=200)
    ap.add_argument("--seed", default="mltd-quality-v1")
    args = ap.parse_args()
    if args.per_category <= 0:
        raise SystemExit("--per-category must be >0")

    raw = read_jsonl(args.legacy)
    candidates, summary = build_candidates(raw)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        by_category[row["category"]].append(row)

    selected: list[dict] = []
    category_counts: dict[str, dict] = {}
    for category in sorted(by_category):
        rows = sorted(by_category[category], key=lambda r: stable_key(args.seed, r["source_sha256"]))
        chosen = rows[:args.per_category]
        selected.extend(chosen)
        category_counts[category] = {"available": len(rows), "selected": len(chosen)}

    selected.sort(key=lambda r: (r["category"], stable_key(args.seed, r["source_sha256"])))
    public_rows = [{
        "source_sha256": row["source_sha256"],
        "source": row["source"],
        "category": row["category"],
        "current_logical": row["current_logical"],
        "bundle": row["bundle"],
        "key": row["key"],
    } for row in selected]
    reference_rows = [{
        "source_sha256": row["source_sha256"],
        "reference_translation": row["reference_translation"],
        "provenance": row["provenance"],
    } for row in selected]

    write_jsonl(args.source_output, public_rows)
    write_jsonl(args.reference_output, reference_rows)
    summary.update({
        "schema_version": 1,
        "seed": args.seed,
        "per_category": args.per_category,
        "selected": len(selected),
        "category_counts": category_counts,
        "reference_is_hidden_from_translator": True,
    })
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
