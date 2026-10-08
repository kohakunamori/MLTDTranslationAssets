"""Read-only Event-unit v4 reviewer focus report and kana detector false alarms.

Neither a punctuation false-alarm signal nor deterministic QA PASS constitutes
independent semantic review. No source QA verdict or candidate is rewritten.
"""
from __future__ import annotations

import copy
import json

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.report_event_unit_review_focus import (
    AUDIT_SHA, DOT_ONLY_OLD_REVIEW, ORIGINAL, REVIEW, REVIEW_FILE,
    REVIEW_MANIFEST, STAGE_MANIFEST, STAGE_SHA, V4_SHA,
    build_focus, detect_middle_dot_only_false_alarms, verify_and_build,
)


@pytest.fixture(scope="module")
def frozen():
    return (
        read_jsonl(REVIEW_FILE),
        json.loads(ORIGINAL.read_text(encoding="utf8")),
        json.loads(REVIEW_MANIFEST.read_text(encoding="utf8")),
    )


def test_current_v4_source_sha_and_review_counts_are_read_only(frozen):
    reviews, original, manifest = frozen
    assert sha_file(REVIEW_FILE) == V4_SHA == manifest["review_worklist_sha256"]
    assert sha_file(ORIGINAL) == AUDIT_SHA
    assert sha_file(STAGE_MANIFEST) == STAGE_SHA
    result = build_focus(reviews, original, manifest, limit=3)
    assert result["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert result["known_targeted_pending_review_sources"] == 1186
    assert result["agent_drafts_unreviewed"] == 102
    assert result["undrafted_qa_review_sources"] == 811
    assert result["remaining_independent_review_pending"] == 1186
    assert result["punctuation_only_qa_warning_is_not_semantic_clearance"] is True
    assert result["qa_verdicts_and_original_review_queue_modified"] is False
    assert result["undrafted_qa_issue_occurrences"] == {
        "guest_narrowed_to_audience": 17,
        "hanashi_narrowed_to_story": 21,
        "japanese_kana_residual": 11,
        "kondo_future_rendered_as_this_time": 3,
        "sentence_final_adversative_missing": 161,
        "unsupported_first_person_plural_addition": 471,
        "unsupported_indefinite_object_addition": 142,
    }
    assert result["selection_total"] == 811
    assert result["selection_showing"] == 3
    assert all(r["review_status"] == "pending" for r in result["review_examples"])
    assert all(r["independent_review_complete"] is False
               for r in result["review_examples"])


def test_two_literal_middle_dot_false_alarms_are_not_translation_drafts(frozen):
    review, audit, _ = frozen
    found = detect_middle_dot_only_false_alarms(audit, review)
    assert len(found) == 2
    assert {r["source_sha256"] for r in found} == DOT_ONLY_OLD_REVIEW
    assert all(r["prior_qa_verdict_unchanged"] == "REVIEW" for r in found)
    assert all(r["independent_review_complete"] is False for r in found)
    assert all(r["safe_to_mount_as_final_overlay"] is False for r in found)
    assert all(r["source"].count("・") ==
               r["machine_candidate_unreviewed"].count("・") for r in found)
    assert all("・" in r["machine_candidate_unreviewed"] for r in found)
    assert all(r["detector_match"].startswith("Japanese middle dot")
               for r in found)


def test_wordplay_kana_and_katakana_names_are_not_falsely_exempted(frozen):
    review, original, manifest = frozen
    candidates = build_focus(
        review, original, manifest, issue="japanese_kana_residual", limit=11
    )
    assert candidates["selection_total"] == 11
    assert candidates["selection_showing"] == 11
    assert sum(
        bool(x["possible_middle_dot_qa_false_alarm"])
        for x in candidates["review_examples"]
    ) == 2
    normal_kana = {
        "04c8dd5c68f3e0",
        "2e7b7a341488ee",
        "35cb049cf549ab",
        "3ec251eeee6674",
        "4effe57793a7c2",
        "835c4b1bd4916b",
        "b50e3e8b549b5",
        "fe982c74462c34",
        "ff84e9ae3abb74",
    }
    assert len(normal_kana) == 9
    for item in candidates["review_examples"]:
        if item["source_sha256"][:14] in normal_kana:
            assert item["possible_middle_dot_qa_false_alarm"] is False
    assert {x["source_sha256"] for x in candidates["review_examples"]
            if x["possible_middle_dot_qa_false_alarm"]} == DOT_ONLY_OLD_REVIEW


@pytest.mark.parametrize("kind", [
    "tampered_ja_source",
    "tampered_zh_translation",
    "removed_kana_issue",
    "false_semantic_approval",
    "added_real_kana_to_dot_only",
    "changed_japanese_dot_count",
    "double_current_review",
    "short_original_audit",
    "missing_current_review",
    "wrong_original_qa_verdict",
])
def test_punctuation_detector_fails_closed_on_changed_evidence(frozen, kind):
    reviewers, audit, _ = copy.deepcopy(frozen)
    dot_sid = sorted(DOT_ONLY_OLD_REVIEW)[0]
    current = next(x for x in reviewers if x["source_sha256"] == dot_sid)
    prior = next(x for x in audit if x["source_sha256"] == dot_sid)
    if kind == "tampered_ja_source":
        current["source"] += "其他"
    elif kind == "tampered_zh_translation":
        current["machine_candidate_unreviewed"] += "其他"
    elif kind == "removed_kana_issue":
        current["current_machine_qa_issues"] = []
    elif kind == "false_semantic_approval":
        current["independent_review_complete"] = True
    elif kind == "added_real_kana_to_dot_only":
        prior["translation"] += "ふ"
    elif kind == "changed_japanese_dot_count":
        prior["source"] += "・"
    elif kind == "double_current_review":
        reviewers.append(copy.deepcopy(current))
    elif kind == "short_original_audit":
        audit.pop()
    elif kind == "missing_current_review":
        reviewers.pop()
    elif kind == "wrong_original_qa_verdict":
        prior["qa_verdict"] = "PASS"
    with pytest.raises(ValueError):
        detect_middle_dot_only_false_alarms(audit, reviewers)


@pytest.mark.parametrize("issue,expected", [
    ("japanese_kana_residual", 11),
    ("hanashi_narrowed_to_story", 21),
    ("kondo_future_rendered_as_this_time", 3),
    ("guest_narrowed_to_audience", 17),
    ("unsupported_indefinite_object_addition", 142),
    ("sentence_final_adversative_missing", 161),
    ("unsupported_first_person_plural_addition", 471),
])
def test_issue_filters_do_not_lose_source_context(frozen, issue, expected):
    r, audit, manifest = frozen
    data = build_focus(r, audit, manifest, issue=issue, limit=2)
    assert data["selection_issue"] == issue
    assert data["selection_total"] == expected
    assert data["selection_showing"] == 2
    assert all(issue in x["issue_codes"] for x in data["review_examples"])
    assert all(x["examples"] for x in data["review_examples"])


def test_focus_rejects_unknown_filter_or_stale_safety_gate(frozen):
    r, audit, manifest = frozen
    with pytest.raises(ValueError):
        build_focus(r, audit, manifest, issue="not_a_real_rule")
    with pytest.raises(ValueError):
        build_focus(r, audit, manifest, limit=-1)
    forged = copy.deepcopy(manifest)
    forged["independent_review_complete"] = True
    with pytest.raises(ValueError):
        build_focus(r, audit, forged)


def test_validated_cli_path_produces_same_read_only_population():
    report = verify_and_build(issue="japanese_kana_residual", limit=0)
    assert report["selection_total"] == 11
    assert len(report["punctuation_only_kana_false_positive_cues"]) == 2
    assert report["selection_showing"] == 0

