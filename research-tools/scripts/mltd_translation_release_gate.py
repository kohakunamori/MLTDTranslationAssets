#!/usr/bin/env python3
"""Merge deterministic QA and independent reviews into releasable MLTD translations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipelines.text.mltd_localize_gtx import read_jsonl, translation_status_is_accepted
from scripts.mltd_translation_quality import index_unique


def review_index(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        sid = str(row.get("source_sha256", ""))
        if not sid:
            raise ValueError("review row missing source_sha256")
        if sid in out:
            raise ValueError(f"duplicate review source_sha256: {sid}")
        out[sid] = row
    return out


def is_official(candidate: dict) -> bool:
    """Return true only for explicitly zh-CN-reviewed official material.

    Historical official Traditional Chinese and raw OpenCC conversions are evidence, not
    automatically release-ready Simplified Chinese.  They must be promoted to an explicit
    reviewed status before they may bypass independent AI review.
    """
    provenance = str(candidate.get("provenance", "")).lower()
    status = str(candidate.get("status", "")).lower()
    approved_statuses = {
        "official_legacy_zhcn_reviewed",
        "official_legacy_simplified_reviewed",
    }
    return (
        provenance.startswith("official")
        and status in approved_statuses
        and translation_status_is_accepted(status)
    )


def _score(review: dict, name: str) -> int:
    scores = review.get("scores", {})
    if not isinstance(scores, dict):
        return 0
    try:
        return int(scores.get(name, 0))
    except (TypeError, ValueError):
        return 0


def _review_identity(review: dict | None) -> str:
    if review is None:
        return ""
    explicit = str(review.get("reviewer_id", "")).strip()
    if explicit:
        return explicit
    return "|".join([
        str(review.get("reviewer_provenance", "")).strip(),
        str(review.get("reviewer_model", "")).strip(),
    ])


def _review_reasons(review: dict | None, prefix: str = "reviewer") -> list[str]:
    # Preserve the original primary-review reason codes because downstream tools may
    # already consume them.  Only additional review channels receive a prefix.
    primary = prefix == "independent_review"
    if review is None:
        return ["independent_review_missing" if primary else f"{prefix}_missing"]
    reasons: list[str] = []
    if str(review.get("verdict", "")).upper() != "PASS":
        reasons.append("reviewer_not_pass" if primary else f"{prefix}_not_pass")
    if _score(review, "semantic_accuracy") != 5:
        reasons.append("semantic_accuracy_not_maximum" if primary else f"{prefix}_semantic_accuracy_not_maximum")
    if review.get("blocking_errors", []):
        reasons.append("reviewer_blocking_errors" if primary else f"{prefix}_blocking_errors")
    return reasons


def classify(
    candidate: dict,
    qa: dict,
    review: dict | None,
    risk: dict | None = None,
    second_review: dict | None = None,
) -> tuple[str, list[str]]:
    qa_verdict = str(qa.get("qa_verdict", "")).upper()
    if qa_verdict == "REJECT":
        return "rejected", ["deterministic_qa_reject"]
    if qa_verdict != "PASS":
        return "needs_review", ["deterministic_qa_not_pass"]

    # Source-bound historical official translations may bypass AI review, but never
    # deterministic structural QA.  Risk routing applies to machine output only.
    if is_official(candidate):
        return "accepted", []

    reasons = _review_reasons(review, "independent_review")
    if reasons:
        return "needs_review", reasons

    risk_level = str((risk or {}).get("risk_level", "low")).lower()
    if risk_level in {"high", "critical"}:
        # High-risk story/dialogue/cross-context rows require zero detected
        # terminology/context mistakes and strong voice/fluency scores.
        if _score(review, "terminology") != 5:
            reasons.append("strict_review_terminology_not_maximum")
        if _score(review, "context_consistency") != 5:
            reasons.append("strict_review_context_consistency_not_maximum")
        if _score(review, "character_voice") != 5:
            reasons.append("strict_review_character_voice_not_maximum")
        if _score(review, "fluency") != 5:
            reasons.append("strict_review_fluency_not_maximum")

    if risk_level == "critical":
        second_reasons = _review_reasons(second_review, "second_independent_review")
        reasons.extend(second_reasons)
        if second_review is not None and not second_reasons:
            first_identity = _review_identity(review)
            second_identity = _review_identity(second_review)
            if not first_identity or not second_identity:
                reasons.append("independent_reviewer_identity_missing")
            elif first_identity == second_identity:
                reasons.append("second_review_not_independent")
            if _score(second_review, "terminology") != 5:
                reasons.append("second_strict_review_terminology_not_maximum")
            if _score(second_review, "context_consistency") != 5:
                reasons.append("second_strict_review_context_consistency_not_maximum")
            if _score(second_review, "character_voice") != 5:
                reasons.append("second_strict_review_character_voice_not_maximum")
            if _score(second_review, "fluency") != 5:
                reasons.append("second_strict_review_fluency_not_maximum")

    return ("needs_review", reasons) if reasons else ("accepted", [])


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--qa", type=Path, required=True)
    ap.add_argument("--reviews", type=Path)
    ap.add_argument("--second-reviews", type=Path)
    ap.add_argument("--risk", type=Path, help="JSONL emitted by classify_translation_risk.py")
    ap.add_argument("--accepted", type=Path, required=True)
    ap.add_argument("--needs-review", type=Path, required=True)
    ap.add_argument("--rejected", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    candidates = index_unique(read_jsonl(args.candidates), "candidates")
    qa = review_index(read_jsonl(args.qa))
    reviews = review_index(read_jsonl(args.reviews)) if args.reviews and args.reviews.is_file() else {}
    second_reviews = review_index(read_jsonl(args.second_reviews)) if args.second_reviews and args.second_reviews.is_file() else {}
    risks = review_index(read_jsonl(args.risk)) if args.risk and args.risk.is_file() else {}

    buckets = {"accepted": [], "needs_review": [], "rejected": []}
    for sid, candidate in candidates.items():
        qa_row = qa.get(sid)
        if qa_row is None:
            bucket, reasons = "rejected", ["deterministic_qa_missing"]
        else:
            bucket, reasons = classify(
                candidate,
                qa_row,
                reviews.get(sid),
                risks.get(sid),
                second_reviews.get(sid),
            )
        row = dict(candidate)
        row["release_gate"] = bucket
        row["release_reasons"] = reasons
        if qa_row is not None:
            row["qa_verdict"] = qa_row.get("qa_verdict")
            row["qa_issues"] = qa_row.get("issues", [])
        if sid in reviews:
            row["review"] = reviews[sid]
        if sid in second_reviews:
            row["second_review"] = second_reviews[sid]
        if sid in risks:
            row["risk"] = risks[sid]
        buckets[bucket].append(row)

    write_jsonl(args.accepted, buckets["accepted"])
    write_jsonl(args.needs_review, buckets["needs_review"])
    write_jsonl(args.rejected, buckets["rejected"])
    summary = {
        "schema_version": 1,
        "candidates": len(candidates),
        "accepted": len(buckets["accepted"]),
        "needs_review": len(buckets["needs_review"]),
        "rejected": len(buckets["rejected"]),
        "official_review_bypass_only_after_qa_pass": True,
        "machine_semantic_accuracy_required": 5,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # Rejected rows are expected output of the gate; successful classification exits 0.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
