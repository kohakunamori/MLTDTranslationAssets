#!/usr/bin/env python3
import sys
from pathlib import Path
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))
"""Classify MLTD translation queue rows into review-risk tiers.

The classifier is deterministic.  It does not judge translation quality; it decides
how much independent review a future machine translation must receive.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mltd_localize_gtx import PROTECTED_TOKEN_RE, read_jsonl
from mltd_translation_quality import source_id

DIALOGUE_CATEGORIES = {
    "birth", "card", "event", "job", "main", "memory", "season", "special", "live_mc"
}


def classify_row(row: dict) -> dict:
    source = str(row.get("source", ""))
    sid = str(row.get("source_sha256", "")) or source_id(source)
    if not source or sid != source_id(source):
        raise ValueError(f"invalid source identity: {sid!r}")

    usage = row.get("usage_profile", {})
    if not isinstance(usage, dict):
        usage = {}
    categories = {str(x) for x in usage.get("categories", []) if str(x)}
    speakers = {str(x) for x in usage.get("speaker_codes", []) if str(x)}
    reasons: list[str] = []
    score = 0

    if str(row.get("queue_reason", "")).startswith("non_gtx_") or str(usage.get("source_kind", "")).startswith("non_gtx_"):
        score += 2
        reasons.append("non_gtx_asset_text")

    if str(row.get("queue_reason", "")) == "ambiguous_seed":
        score += 6
        reasons.append("ambiguous_translation_memory")
    if bool(usage.get("multi_speaker")) or len(speakers) > 1:
        score += 6
        reasons.append("multiple_speakers")
    if bool(usage.get("multi_category")) or len(categories) > 1:
        score += 4
        reasons.append("multiple_scene_categories")
    if bool(usage.get("requires_cross_context_consistency")):
        score += 4
        reasons.append("cross_context_consistency_required")

    dialogue = bool(categories & DIALOGUE_CATEGORIES)
    if dialogue:
        score += 2
        reasons.append("dialogue_or_story_text")
    if "live_mc" in categories:
        # These lines are short but strongly character-voice dependent.  Keep
        # them on the strict reviewer path even before the 52x4 sequence ->
        # speaker mapping is proven and promoted to speaker_codes.
        score += 3
        reasons.append("live_mc_character_voice")
    if len(speakers) == 1:
        score += 2
        reasons.append("character_voice_sensitive")

    visible = PROTECTED_TOKEN_RE.sub("", source)
    protected_count = len(PROTECTED_TOKEN_RE.findall(source))
    if protected_count >= 3:
        score += 2
        reasons.append("many_runtime_tokens")
    elif protected_count:
        score += 1
        reasons.append("runtime_tokens")
    if len(visible) >= 80:
        score += 3
        reasons.append("long_semantic_span")
    elif len(visible) >= 35:
        score += 1
        reasons.append("medium_semantic_span")

    # Widely reused short labels deserve consistency review even when they are not dialogue.
    try:
        occurrences = int(row.get("occurrences") or usage.get("catalogue_occurrences") or 0)
    except (TypeError, ValueError):
        occurrences = 0
    if occurrences >= 100:
        score += 2
        reasons.append("widely_reused")
    elif occurrences >= 20:
        score += 1
        reasons.append("reused")

    critical = {
        "ambiguous_translation_memory",
        "multiple_speakers",
    } & set(reasons)
    if critical or score >= 12:
        level = "critical"
        review_policy = "dual_independent_review"
    elif score >= 7:
        level = "high"
        review_policy = "strict_independent_review"
    elif score >= 3:
        level = "medium"
        review_policy = "standard_independent_review"
    else:
        level = "low"
        review_policy = "standard_independent_review"

    return {
        "source_sha256": sid,
        "risk_level": level,
        "risk_score": score,
        "risk_reasons": reasons,
        "review_policy": review_policy,
        "categories": sorted(categories),
        "speaker_codes": sorted(speakers),
        "occurrences": occurrences,
    }


def load_risk_index(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    out: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        if not sid:
            raise ValueError("risk row missing source_sha256")
        if sid in out:
            raise ValueError(f"duplicate risk source_sha256: {sid}")
        level = str(row.get("risk_level", ""))
        if level not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"invalid risk_level for {sid}: {level!r}")
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
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    args = ap.parse_args()

    rows = [classify_row(row) for row in read_jsonl(args.queue)]
    write_jsonl(args.output, rows)
    levels = Counter(row["risk_level"] for row in rows)
    policies = Counter(row["review_policy"] for row in rows)
    reasons = Counter(reason for row in rows for reason in row["risk_reasons"])
    summary = {
        "schema_version": 1,
        "queue_rows": len(rows),
        "risk_levels": dict(levels),
        "review_policies": dict(policies),
        "risk_reasons": dict(reasons.most_common()),
        "critical_requires_second_independent_review": True,
        "high_requires_strict_scores": True,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
