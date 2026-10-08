"""Immutable 1077100 Event-unit QA-cue audit and source-bound draft regressions."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from scripts.build_event_unit_qa_cue_recheck import (
    AUDIT, DEST, REJECT_SOURCE, REVIEW, STAGE_MANIFEST, TAIL, build,
    reevaluate, sha_file,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id


@pytest.fixture(scope="module")
def report():
    data = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    return data


def test_frozen_recheck_files_have_matching_shas_and_never_grant_release(report):
    assert report["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert report["input_stage_manifest_sha256"] == sha_file(STAGE_MANIFEST)
    assert report["input_qa_audit_sha256"] == sha_file(AUDIT)
    assert report["input_original_review_sha256"] == sha_file(REVIEW)
    assert report["input_tail_draft_sha256"] == sha_file(TAIL)
    assert report["current_verdicts"] == {"PASS": 12275, "REVIEW": 901, "REJECT": 1}
    assert report["source_transitions"] == {
        "PASS->PASS": 12043,
        "REVIEW->PASS": 232,
        "REVIEW->REVIEW": 901,
        "REJECT->REJECT": 1,
    }
    assert report["remaining_independent_review"] == 1181
    assert report["safe_to_mount_as_final_overlay"] is False
    assert report["independent_review_complete"] is False
    assert report["production_translation_files_modified"] is False
    assert report["qa_stage_bundles_modified"] is False
    assert report["nas_modified"] is False
    for name, expected in report["files_sha256"].items():
        assert sha_file(DEST / name) == expected


def test_review_populations_are_disjoint_and_not_falsely_marked_semantically_reviewed():
    names = {
        "still-review.jsonl": 901,
        "qa-cleared-still-unreviewed.jsonl": 232,
        "still-reject.jsonl": 1,
    }
    seen = set()
    for name, size in names.items():
        rows = read_jsonl(DEST / name)
        assert len(rows) == size
        for row in rows:
            sid = row["source_sha256"]
            assert sid not in seen
            seen.add(sid)
            assert sid == source_id(row["source"])
            assert row["independent_review_complete"] is False
            assert row["semantic_accuracy_verified"] is False
            assert row["release_gate"] == "needs_independent_review"
            assert row["safe_to_mount_as_final_overlay"] is False
            if name == "qa-cleared-still-unreviewed.jsonl":
                assert row["prior_qa_verdict"] == "REVIEW"
                assert row["qa_verdict"] == "PASS"
                assert row["issues"] == []
                assert row["automatically_cleared_qa_issues_not_semantic_review"]
    assert len(seen) == 1134
    assert seen.isdisjoint({row["source_sha256"] for row in read_jsonl(TAIL)})


def test_numeric_unit_candidate_preserves_source_and_sits_outside_producer():
    rows = read_jsonl(DEST / "reject-numeric-correction-draft.jsonl")
    assert len(rows) == 1
    draft = rows[0]
    assert draft["source"] == REJECT_SOURCE
    assert draft["source_sha256"] == source_id(REJECT_SOURCE)
    assert "10个百分点" in draft["machine_candidate_unreviewed"]
    assert "10%" in draft["translation_draft"]
    assert "个百分点" not in draft["translation_draft"]
    result = evaluate_row(
        {"source": draft["source"], "source_sha256": draft["source_sha256"]},
        {"source": draft["source"], "source_sha256": draft["source_sha256"],
         "translation": draft["translation_draft"],
         "status": "agent_draft_unreviewed"},
        load_glossary(None),
    )
    assert result["qa_verdict"] == draft["qa_verdict_draft"] == "PASS"
    assert draft["release_gate"] == "needs_independent_review"
    assert draft["semantic_accuracy_verified"] is False
    assert draft["independent_review_complete"] is False


@pytest.fixture(scope="module")
def all_inputs():
    return (
        json.loads(AUDIT.read_text(encoding="utf8")),
        read_jsonl(REVIEW), read_jsonl(TAIL),
    )


@pytest.mark.parametrize("changed", [
    "stale_source", "wrong_prior_review", "missing_tail_reference",
    "duplicate_original_review", "altered_translation",
])
def test_fails_closed_when_frozen_review_input_tampered(all_inputs, changed):
    old = [x.copy() for x in all_inputs[0]]
    review = [x.copy() for x in all_inputs[1]]
    tail = [x.copy() for x in all_inputs[2]]
    if changed == "stale_source":
        old[0]["source"] = "これは別の原文です"
    elif changed == "wrong_prior_review":
        i = next(i for i, x in enumerate(review)
                 if x["qa_verdict"] == "REVIEW")
        review[i] = {**review[i], "source": "内容已变化"}
    elif changed == "missing_tail_reference":
        tail.pop()
    elif changed == "duplicate_original_review":
        review.append(review[0])
    elif changed == "altered_translation":
        i = next(i for i, x in enumerate(old)
                 if x["qa_verdict"] == "REVIEW")
        old[i] = {**old[i], "translation": "擅自修改的候选"}
    with pytest.raises((ValueError, KeyError)):
        reevaluate(old, review, tail)


def test_repeated_build_will_not_overwrite_immutable_report():
    with pytest.raises(FileExistsError):
        build()

