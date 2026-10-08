#!/usr/bin/env python3
"""Read-only Event-unit reviewer focus report for frozen 9.0.200/1077100 v4.

Triages 811 undrafted, rule-QA REVIEW originals without editing accepted data.
Detects Kana-regex false alarms caused by the Japanese middle dot ・ in otherwise
Chinese punctuation, but NEVER upgrades a QA verdict or an independent review.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import KANA_RE, read_jsonl
from scripts.mltd_translation_quality import source_id

BUILD = ROOT / "build/localization-90200"
REVIEW = BUILD / "audits/event-unit-unified-review-v4-client-9.0.200-assets-1077100"
REVIEW_FILE = REVIEW / "review-worklist.jsonl"
REVIEW_MANIFEST = REVIEW / "manifest.json"
ORIGINAL = BUILD / "staging-event-unit-qa-with-tail/event-unit-QA-candidate-audit.json"
STAGE_MANIFEST = BUILD / "staging-event-unit-combined-55-unreviewed-drafts/manifest.json"
V4_SHA = "34a7239812eaa89ab2fbb9f9859798315d5b39511afc6edbd1794e853d9a27d3"
AUDIT_SHA = "f2c8e3b4d6fb8428abc62cb2fb0b9e7b7fb41c22be2ea7ff60a0f10187aec30d"
STAGE_SHA = "8c6af74fcbf70e44c1b56f6f3ea02188fd2107bad0c1fc9f0b3369d2f7e49ff5"
DOT_ONLY_OLD_REVIEW = {
    "ce515ff707725ead5ac5d33ae484ad43b520668388f3b6b6c293b71687a91b0f",
    "5f29c252528ff24ba93ad0625a607ee7ff63aad1028b62d67040429468cee070",
}
# None of these are source translation edit suggestions. Exact source+literal
# dot preservation is required; Japanese wordplay kana like マ/ク is untouched.


def detect_middle_dot_only_false_alarms(
    original: list[dict], reviewer: list[dict],
) -> list[dict]:
    by_source = {}
    for row in original:
        sid, jp = row.get("source_sha256"), row.get("source")
        if not isinstance(jp, str) or sid != source_id(jp) or sid in by_source:
            raise ValueError("original frozen QA audit duplicate/unbound Japanese source")
        by_source[sid] = row
    if len(by_source) != 13177:
        raise ValueError("original audit no longer contains frozen 13177 sources")
    reviewer_by_id = {}
    for row in reviewer:
        sid, jp = row.get("source_sha256"), row.get("source")
        if not isinstance(jp, str) or sid != source_id(jp) or sid in reviewer_by_id:
            raise ValueError("v4 reviewer duplicate/unbound Japanese source")
        reviewer_by_id[sid] = row
    if len(reviewer_by_id) != 1186:
        raise ValueError("v4 reviewer population changed")
    audit_kana_flagged = [
        row for row in original
        if "japanese_kana_residual" in {x["code"] for x in row.get("issues", [])}
    ]
    if len(audit_kana_flagged) != 23:
        raise ValueError("original Kana QA flagged population changed")
    identified = []
    for old in audit_kana_flagged:
        translation = old.get("translation")
        if not isinstance(translation, str):
            raise ValueError("original Kana QA candidate missing translation")
        matched = [m.group() for m in KANA_RE.finditer(translation)]
        if not matched or set(matched) != {"・"}:
            continue
        sid = old["source_sha256"]
        current = reviewer_by_id.get(sid)
        if (current is None or old.get("qa_verdict") != "REVIEW"
            or current.get("review_bucket") != "qa_review_without_draft"
            or current.get("machine_candidate_unreviewed") != translation
            or current.get("source") != old["source"]
            or current.get("examples") != old["examples"]
            or current.get("occurrences") != old["occurrences"]
            or {x["code"] for x in current.get("current_machine_qa_issues", [])}
                != {"japanese_kana_residual"}
            or current.get("agent_correction_draft_unreviewed") is not None
            or current.get("independent_review_complete") is not False
            or current.get("semantic_accuracy_verified") is not False
            or current.get("safe_to_mount_as_final_overlay") is not False
            or old["source"].count("・") != translation.count("・")):
            raise ValueError(f"punctuation-only QA alarm provenance changed: {sid}")
        identified.append({
            "source_sha256": sid,
            "source": old["source"],
            "machine_candidate_unreviewed": translation,
            "examples": old["examples"],
            "detector_match": "Japanese middle dot ・ only; no phonetic kana",
            "prior_qa_verdict_unchanged": "REVIEW",
            "independent_review_complete": False,
            "safe_to_mount_as_final_overlay": False,
        })
    if {x["source_sha256"] for x in identified} != DOT_ONLY_OLD_REVIEW:
        raise ValueError("middle-dot false-positive source IDs changed")
    return sorted(identified, key=lambda x: x["source_sha256"])


def build_focus(
    reviewer: list[dict], original: list[dict], manifest: dict,
    *, issue: str | None = None, limit: int = 20,
) -> dict:
    if not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be nonnegative")
    if (manifest.get("known_targeted_review_source_unique") != 1186
        or manifest.get("available_unreviewed_agent_drafts") != 102
        or manifest.get("qa_review_without_targeted_draft") != 811
        or manifest.get("all_other_qa_pass_sources_semantically_verified") is not False
        or manifest.get("independent_review_complete") is not False
        or manifest.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("v4 reviewer manifest has stale counts or false release gate")
    only_dots = detect_middle_dot_only_false_alarms(original, reviewer)
    exempted = {x["source_sha256"] for x in only_dots}
    undrafted = [row for row in reviewer
                 if row["review_bucket"] == "qa_review_without_draft"]
    if len(undrafted) != 811:
        raise ValueError("v4 QA REVIEW undrafted population changed")
    codes = Counter(x["code"] for row in undrafted
                    for x in row["current_machine_qa_issues"])
    if issue is not None and issue not in codes:
        raise ValueError(f"no undrafted rows contain rule {issue!r}")
    filtered = [
        row for row in undrafted if
        (issue is None or issue in
            {x["code"] for x in row["current_machine_qa_issues"]})
    ]
    # Prioritize multi-issue and genuine untranslated mixed text, then
    # context-sensitive lexical warnings, then plural/adversative judgments.
    priority = {
        "japanese_kana_residual": 0,
        "hanashi_narrowed_to_story": 1,
        "kondo_future_rendered_as_this_time": 2,
        "guest_narrowed_to_audience": 3,
        "unsupported_indefinite_object_addition": 4,
        "sentence_final_adversative_missing": 5,
        "unsupported_first_person_plural_addition": 6,
    }
    def score(row: dict) -> tuple:
        codes_here = {i["code"] for i in row["current_machine_qa_issues"]}
        return (
            row["source_sha256"] in exempted,
            -len(codes_here),
            min((priority.get(k, 10) for k in codes_here), default=10),
            -row["occurrences"],
            row["source_sha256"],
        )
    filtered.sort(key=score)
    samples = []
    for r in filtered[:limit]:
        samples.append({
            "source_sha256": r["source_sha256"],
            "source": r["source"],
            "machine_candidate_unreviewed": r["machine_candidate_unreviewed"],
            "issue_codes": [x["code"] for x in r["current_machine_qa_issues"]],
            "examples": r["examples"],
            "possible_middle_dot_qa_false_alarm":
                r["source_sha256"] in exempted,
            "independent_review_complete": False,
            "review_status": "pending",
        })
    return {
        "version_key": manifest["version_identity"]["version_key"],
        "review_worklist_sha256": manifest["review_worklist_sha256"],
        "original_13177_source_audit_sha256": AUDIT_SHA,
        "known_targeted_pending_review_sources": 1186,
        "agent_drafts_unreviewed": 102,
        "undrafted_qa_review_sources": 811,
        "undrafted_qa_issue_occurrences": dict(sorted(codes.items())),
        "punctuation_only_kana_false_positive_cues": only_dots,
        "punctuation_only_qa_warning_is_not_semantic_clearance": True,
        "qa_verdicts_and_original_review_queue_modified": False,
        "remaining_independent_review_pending": 1186,
        "selection_issue": issue,
        "selection_total": len(filtered),
        "selection_showing": len(samples),
        "review_examples": samples,
    }


def verify_and_build(*, issue: str | None = None, limit: int = 20) -> dict:
    if (sha_file(REVIEW_FILE) != V4_SHA
        or sha_file(ORIGINAL) != AUDIT_SHA
        or sha_file(STAGE_MANIFEST) != STAGE_SHA):
        raise ValueError("frozen 9.0.200 + 1077100 audit/QA stage has changed")
    mf = json.loads(REVIEW_MANIFEST.read_text(encoding="utf8"))
    stage = json.loads(STAGE_MANIFEST.read_text(encoding="utf8"))
    if (mf.get("review_worklist_sha256") != V4_SHA
        or mf.get("source_manifests_sha256", {}).get("stage55") != STAGE_SHA
        or mf.get("version_identity") != stage.get("version_identity")
        or stage.get("unreviewed_candidate_source_unique") != 55
        or stage.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("v4 review/stage identity or release gate mismatch")
    return build_focus(
        read_jsonl(REVIEW_FILE),
        json.loads(ORIGINAL.read_text(encoding="utf8")), mf,
        issue=issue, limit=limit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", help="review rule to focus on", default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()
    report = verify_and_build(issue=args.issue, limit=args.limit)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(
        f"Event-unit {report['version_key']}: "
        f"{report['known_targeted_pending_review_sources']} source pending; "
        f"{report['agent_drafts_unreviewed']} drafts unreviewed; "
        f"{report['undrafted_qa_review_sources']} flagged without drafts."
    )
    print("QA flags in still-undrafted:", json.dumps(
        report["undrafted_qa_issue_occurrences"], ensure_ascii=False
    ))
    print("Punctuation-only Kana detector alarms: 2 (NOT independent semantic review).")
    for row in report["review_examples"]:
        label = "middle-dot-only QA false alarm" if row[
            "possible_middle_dot_qa_false_alarm"] else ",".join(row["issue_codes"])
        print(f"{row['source_sha256']} [{label}]")
        print(f" JP: {row['source']!r}")
        print(f" CN: {row['machine_candidate_unreviewed']!r}")
        if row["examples"]:
            print(f" BEFORE: {row['examples'][0]['previous']!r}")
            print(f" AFTER: {row['examples'][0]['next']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

