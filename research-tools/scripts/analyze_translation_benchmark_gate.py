#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare MLTD release-gate decisions with hidden benchmark evaluations.

This tool never reads the hidden reference text. It consumes only evaluator
verdicts/scores and the release-gate buckets, so it can quantify whether the
production gate would have released a translation that the hidden evaluator
considered unsafe.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import read_jsonl


def load_unique(path: Path, label: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        if not sid:
            raise ValueError(f"{label}: missing source_sha256")
        if sid in out:
            raise ValueError(f"{label}: duplicate source_sha256 {sid}")
        out[sid] = row
    return out


def load_release_buckets(paths: list[tuple[str, Path]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for expected_bucket, path in paths:
        for row in read_jsonl(path):
            sid = str(row.get("source_sha256", ""))
            if not sid:
                raise ValueError(f"{path}: missing source_sha256")
            if sid in out:
                raise ValueError(f"release buckets: duplicate source_sha256 {sid}")
            actual = str(row.get("release_gate", expected_bucket))
            if actual != expected_bucket:
                raise ValueError(
                    f"{sid}: release_gate={actual!r} does not match bucket {expected_bucket!r}"
                )
            out[sid] = row
    return out


def hidden_safe(row: dict) -> bool:
    scores = row.get("scores", {})
    try:
        semantic = int(scores.get("semantic_accuracy", 0))
    except (TypeError, ValueError):
        semantic = 0
    return str(row.get("verdict", "")).upper() == "PASS" and semantic == 5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--accepted", type=Path, required=True)
    ap.add_argument("--needs-review", type=Path, required=True)
    ap.add_argument("--rejected", type=Path, required=True)
    ap.add_argument("--evaluations", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    release = load_release_buckets([
        ("accepted", args.accepted),
        ("needs_review", args.needs_review),
        ("rejected", args.rejected),
    ])
    evaluations = load_unique(args.evaluations, "evaluations")

    extra_release = sorted(set(release) - set(evaluations))
    if extra_release:
        raise ValueError(
            f"release result contains ids without hidden evaluation: {extra_release[0]}"
        )

    evaluated_ids = sorted(set(release) & set(evaluations))
    counts = Counter()
    by_category: dict[str, Counter] = defaultdict(Counter)
    by_risk: dict[str, Counter] = defaultdict(Counter)
    unsafe_examples: list[dict] = []
    conservative_examples: list[dict] = []

    for sid in evaluated_ids:
        gate = str(release[sid].get("release_gate", ""))
        ev = evaluations[sid]
        safe = hidden_safe(ev)
        category = str(ev.get("category", "unknown"))
        risk = str((release[sid].get("risk") or {}).get("risk_level", "unknown"))

        counts[f"release:{gate}"] += 1
        counts["hidden_safe" if safe else "hidden_not_safe"] += 1
        by_category[category]["evaluated"] += 1
        by_risk[risk]["evaluated"] += 1

        if gate == "accepted" and safe:
            cls = "safe_release"
        elif gate == "accepted" and not safe:
            cls = "unsafe_false_release"
            unsafe_examples.append({
                "source_sha256": sid,
                "category": category,
                "risk_level": risk,
                "hidden_verdict": ev.get("verdict"),
                "hidden_semantic_accuracy": (ev.get("scores") or {}).get("semantic_accuracy"),
                "hidden_error_classes": ev.get("error_classes", []),
                "release_reasons": release[sid].get("release_reasons", []),
            })
        elif gate != "accepted" and safe:
            cls = "conservative_block"
            conservative_examples.append({
                "source_sha256": sid,
                "category": category,
                "risk_level": risk,
                "release_gate": gate,
                "release_reasons": release[sid].get("release_reasons", []),
            })
        else:
            cls = "correct_block"

        counts[cls] += 1
        by_category[category][cls] += 1
        by_risk[risk][cls] += 1

    accepted = counts["release:accepted"]
    unsafe = counts["unsafe_false_release"]
    result = {
        "schema_version": 1,
        "evaluated": len(evaluated_ids),
        "release_rows": len(release),
        "hidden_evaluation_rows": len(evaluations),
        "hidden_coverage_of_release": (
            len(evaluated_ids) / len(release) if release else 0.0
        ),
        "safe_release": counts["safe_release"],
        "unsafe_false_release": unsafe,
        "correct_block": counts["correct_block"],
        "conservative_block": counts["conservative_block"],
        "unsafe_false_release_rate_over_all": (
            unsafe / len(evaluated_ids) if evaluated_ids else 0.0
        ),
        "unsafe_false_release_rate_over_accepted": (
            unsafe / accepted if accepted else 0.0
        ),
        "release_counts": {
            "accepted": accepted,
            "needs_review": counts["release:needs_review"],
            "rejected": counts["release:rejected"],
        },
        "hidden_counts": {
            "safe": counts["hidden_safe"],
            "not_safe": counts["hidden_not_safe"],
        },
        "by_category": {
            key: dict(value) for key, value in sorted(by_category.items())
        },
        "by_risk": {
            key: dict(value) for key, value in sorted(by_risk.items())
        },
        "unsafe_examples": unsafe_examples[:20],
        "conservative_examples": conservative_examples[:20],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
