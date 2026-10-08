"""Frozen Event-unit second-round plural QA audit: source-bound, non-release."""
from __future__ import annotations

import copy
import json

import pytest

from scripts.build_event_unit_plural_qa_recheck import (
    DEST, GLOSSARY, QUALITY, QA, ROUND1_CLEARED, ROUND1_MANIFEST,
    ROUND1_REJECT, ROUND1_REVIEW, STAGE_MANIFEST, TAIL,
    build, reassess, sha_file,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id


@pytest.fixture(scope="module")
def inputs():
    return (
        json.loads(QA.read_text(encoding="utf8")),
        read_jsonl(ROUND1_REVIEW),
        read_jsonl(ROUND1_CLEARED),
        read_jsonl(ROUND1_REJECT),
        read_jsonl(TAIL),
    )


def test_frozen_audit_shas_cohort_and_no_review_promotion():
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    assert m["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert m["source_stage_manifest_sha256"] == sha_file(STAGE_MANIFEST)
    assert m["source_qa_audit_sha256"] == sha_file(QA)
    assert m["source_round1_manifest_sha256"] == sha_file(ROUND1_MANIFEST)
    assert m["source_round1_review_sha256"] == sha_file(ROUND1_REVIEW)
    assert m["source_round1_cleared_sha256"] == sha_file(ROUND1_CLEARED)
    assert m["source_round1_reject_sha256"] == sha_file(ROUND1_REJECT)
    assert m["unreviewed_47_draft_sha256"] == sha_file(TAIL)
    assert m["quality_rules_sha256"] == sha_file(QUALITY)
    assert m["glossary_sha256"] == sha_file(GLOSSARY)
    assert m["old_QA_verdicts"] == {"PASS": 12275, "REVIEW": 901, "REJECT": 1}
    assert m["current_QA_verdicts"] == {"PASS": 12316, "REVIEW": 860, "REJECT": 1}
    assert m["second_round_review_to_qa_pass"] == 41
    assert m["second_round_removed_issue_counts"] == {
        "unsupported_first_person_plural_addition": 42
    }
    assert m["cumulative_review_to_qa_pass"] == 273
    assert m["remaining_independent_review"] == 1181
    assert m["unreviewed_47_legacy_tail"] == 47
    assert m["unreviewed_original_numeric_reject"] == 1
    for name, digest in m["file_hashes"].items():
        assert sha_file(DEST / name) == digest
    for key in (
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "producer_modified",
        "existing_852_bundle_QA_stage_modified", "nas_modified",
    ):
        assert m[key] is False


def test_unreviewed_worklist_is_disjoint_and_cleared_items_preserve_evidence():
    remaining = read_jsonl(DEST / "still-review.jsonl")
    newly = read_jsonl(DEST / "newly-qa-cleared-still-unreviewed.jsonl")
    combined = read_jsonl(DEST / "all-qa-cleared-still-unreviewed.jsonl")
    previous = read_jsonl(ROUND1_CLEARED)
    assert len(remaining) == 860
    assert len(newly) == 41
    assert len(combined) == 273
    assert {r["source_sha256"] for r in newly} == (
        {r["source_sha256"] for r in combined}
        - {r["source_sha256"] for r in previous}
    )
    assert ({r["source_sha256"] for r in remaining}
            & {r["source_sha256"] for r in combined}) == set()
    old_by_sid = {r["source_sha256"]: r for r in previous}
    assert all(r == old_by_sid[r["source_sha256"]]
               for r in combined if r["source_sha256"] in old_by_sid)
    for row in remaining + combined:
        assert row["source_sha256"] == source_id(row["source"])
        assert row["release_gate"] == "needs_independent_review"
        assert row["semantic_accuracy_verified"] is False
        assert row["independent_review_complete"] is False
        assert row["safe_to_mount_as_final_overlay"] is False
        assert row["review_status"] == "pending"
    for row in newly:
        assert row["qa_verdict"] == "PASS"
        assert row["issues"] == []
        assert row["automatically_cleared_qa_issues_not_semantic_review"]
        assert all(
            x["code"] == "unsupported_first_person_plural_addition"
            for x in row["automatically_cleared_qa_issues_not_semantic_review"]
        )


@pytest.mark.parametrize("failure", [
    "duplicate_pending", "missing_tail", "source_mismatch",
    "candidate_mismatch", "false_release_gate", "false_independent_review",
    "alter_existing_QA_issue", "duplicate_old_cleared",
])
def test_fail_closed_against_any_review_lineage_tamper(inputs, failure):
    original, pending, cleared, rejected, tail = copy.deepcopy(inputs)
    if failure == "duplicate_pending":
        pending.append(pending[0])
    elif failure == "missing_tail":
        tail.pop()
    elif failure == "source_mismatch":
        pending[0]["source"] = "違う日本語"
    elif failure == "candidate_mismatch":
        pending[0]["machine_candidate_unreviewed"] = "伪造的汉语译文"
    elif failure == "false_release_gate":
        pending[0]["release_gate"] = "accepted"
    elif failure == "false_independent_review":
        pending[0]["independent_review_complete"] = True
    elif failure == "alter_existing_QA_issue":
        pending[0]["issues"] = []
    elif failure == "duplicate_old_cleared":
        cleared.append(cleared[0])
    with pytest.raises(ValueError):
        reassess(original, pending, cleared, rejected, tail)


def test_immutable_stage_refuses_repeated_write():
    with pytest.raises(FileExistsError):
        build()

