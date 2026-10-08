#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a source-bound repair queue from independent translation reviews.

Only REVIEW/REJECT rows are selected by default.  The original queue row remains
authoritative for source identity; the previous machine candidate and reviewer
feedback are attached as evidence for a fresh Translator call.  Hidden benchmark
references are intentionally never accepted as input to this tool.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import index_unique


def review_index(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        if not sid:
            raise ValueError("review row missing source_sha256")
        if sid in out:
            raise ValueError(f"duplicate review source_sha256: {sid}")
        out[sid] = row
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--reviews", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument(
        "--verdict",
        action="append",
        choices=("REVIEW", "REJECT"),
        help="review verdict to repair; repeatable; defaults to REVIEW+REJECT",
    )
    args = ap.parse_args()

    queue = index_unique(read_jsonl(args.queue), "repair-queue-source")
    candidates = index_unique(read_jsonl(args.candidates), "repair-candidates")
    reviews = review_index(args.reviews)
    selected_verdicts = set(args.verdict or ("REVIEW", "REJECT"))

    missing_candidate = sorted(set(reviews) - set(candidates))
    missing_queue = sorted(set(reviews) - set(queue))
    if missing_candidate:
        raise ValueError(
            f"review rows missing candidates: count={len(missing_candidate)} first={missing_candidate[0]}"
        )
    if missing_queue:
        raise ValueError(
            f"review rows missing queue source: count={len(missing_queue)} first={missing_queue[0]}"
        )

    output: list[dict] = []
    counts = Counter()
    for sid, review in reviews.items():
        verdict = str(review.get("verdict", "")).upper()
        counts[f"review:{verdict or 'UNKNOWN'}"] += 1
        if verdict not in selected_verdicts:
            continue

        source_row = dict(queue[sid])
        candidate = candidates[sid]
        source = str(source_row.get("source", ""))
        candidate_source = str(candidate.get("source", source))
        if candidate_source != source:
            raise ValueError(f"{sid}: candidate source text differs from current queue source")

        previous_translation = str(candidate.get("translation", ""))
        if not previous_translation:
            raise ValueError(f"{sid}: selected repair candidate has empty translation")

        source_row["previous_translation"] = previous_translation
        source_row["previous_candidate_status"] = str(candidate.get("status", ""))
        source_row["review_feedback"] = {
            "verdict": verdict,
            "scores": review.get("scores", {}),
            "blocking_errors": review.get("blocking_errors", []),
            "notes": str(review.get("notes", "")),
            "reviewer_id": str(review.get("reviewer_id", "")),
            "reviewer_provenance": str(review.get("reviewer_provenance", "")),
        }
        source_row["queue_reason"] = f"review_repair:{verdict.lower()}"
        output.append(source_row)
        counts["selected_for_repair"] += 1

    write_jsonl(args.output, output)
    summary = {
        "schema_version": 1,
        "kind": "mltd-translation-repair-queue",
        "queue_rows": len(queue),
        "candidate_rows": len(candidates),
        "review_rows": len(reviews),
        "selected_verdicts": sorted(selected_verdicts),
        "repair_rows": len(output),
        "counts": dict(counts),
        "output": str(args.output),
        "hidden_reference_used": False,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
