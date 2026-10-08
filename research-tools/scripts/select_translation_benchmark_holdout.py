#!/usr/bin/env python3
"""Select a deterministic untouched holdout from an existing hidden-reference benchmark."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}: JSONL row must be an object")
                rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def stable_key(seed: str, sid: str) -> str:
    return hashlib.sha256((seed + ":" + sid).encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--exclude", type=Path, action="append", default=[])
    ap.add_argument("--source-output", type=Path, required=True)
    ap.add_argument("--reference-output", type=Path, required=True)
    ap.add_argument("--queue-output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--per-category", type=int, default=2)
    ap.add_argument("--target-total", type=int, default=0, help="deterministically top up from remaining unseen rows after per-category selection")
    ap.add_argument("--seed", required=True)
    args = ap.parse_args()
    if args.per_category <= 0:
        raise SystemExit("--per-category must be >0")

    source_rows = read_jsonl(args.source)
    reference_rows = read_jsonl(args.reference)
    refs = {str(row.get("source_sha256", "")): row for row in reference_rows}
    if len(refs) != len(reference_rows):
        raise ValueError("reference contains missing/duplicate source_sha256")

    excluded: set[str] = set()
    for path in args.exclude:
        for row in read_jsonl(path):
            sid = str(row.get("source_sha256", ""))
            if sid:
                excluded.add(sid)

    by_category: dict[str, list[dict]] = defaultdict(list)
    missing_reference = 0
    for row in source_rows:
        sid = str(row.get("source_sha256", ""))
        if not sid or sid in excluded:
            continue
        if sid not in refs:
            missing_reference += 1
            continue
        category = str(row.get("category", "")) or "unknown"
        by_category[category].append(row)

    selected: list[dict] = []
    category_counts: dict[str, dict[str, int]] = {}
    for category in sorted(by_category):
        candidates = sorted(
            by_category[category],
            key=lambda row: stable_key(args.seed, str(row["source_sha256"])),
        )
        chosen = candidates[: args.per_category]
        selected.extend(chosen)
        category_counts[category] = {
            "available_after_exclusion": len(candidates),
            "selected": len(chosen),
        }

    top_up_count = 0
    if args.target_total:
        if args.target_total < len(selected):
            raise SystemExit("--target-total cannot be smaller than the stratified base selection")
        selected_id_set = {str(row["source_sha256"]) for row in selected}
        remaining = [
            row
            for rows in by_category.values()
            for row in rows
            if str(row["source_sha256"]) not in selected_id_set
        ]
        remaining.sort(key=lambda row: stable_key(args.seed + ":topup", str(row["source_sha256"])))
        needed = args.target_total - len(selected)
        if len(remaining) < needed:
            raise ValueError(f"not enough unseen rows to reach target_total={args.target_total}")
        for row in remaining[:needed]:
            selected.append(row)
            category = str(row.get("category", "")) or "unknown"
            category_counts[category]["selected"] += 1
            top_up_count += 1

    selected.sort(key=lambda row: (str(row.get("category", "")), stable_key(args.seed, str(row["source_sha256"]))))
    selected_ids = [str(row["source_sha256"]) for row in selected]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("selected holdout contains duplicate source_sha256")

    public_rows = [dict(row) for row in selected]
    hidden_rows = [refs[sid] for sid in selected_ids]
    queue_rows = []
    for row in selected:
        queue_row = dict(row)
        queue_row["examples"] = [{
            "logical": row.get("current_logical", ""),
            "bundle": row.get("bundle", ""),
            "key": row.get("key", ""),
        }]
        queue_row["occurrences"] = 1
        queue_rows.append(queue_row)

    write_jsonl(args.source_output, public_rows)
    write_jsonl(args.reference_output, hidden_rows)
    write_jsonl(args.queue_output, queue_rows)
    summary = {
        "schema_version": 1,
        "seed": args.seed,
        "per_category": args.per_category,
        "target_total": args.target_total,
        "top_up_count": top_up_count,
        "source_rows": len(source_rows),
        "reference_rows": len(reference_rows),
        "excluded_ids": len(excluded),
        "missing_reference": missing_reference,
        "selected": len(selected),
        "category_counts": category_counts,
        "reference_is_hidden_from_translator": True,
        "exclude_files": [str(path) for path in args.exclude],
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
