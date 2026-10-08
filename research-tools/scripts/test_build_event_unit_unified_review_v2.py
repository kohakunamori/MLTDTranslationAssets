"""Immutable second unified Event-unit worklist joins 12 followup unreviewed drafts."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_unified_review_v2 import (
    BUCKETS, DEST, EXPECTED_COUNTS, INPUTS, MANIFESTS,
    build, rebase,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id


@pytest.fixture(scope="module")
def source_data():
    return (
        read_jsonl(INPUTS["unified_review_v1"][0]),
        read_jsonl(INPUTS["unreviewed_followup_12"][0]),
        json.loads(MANIFESTS["qa_only_43_trial"][0].read_text(encoding="utf8")),
    )


def test_published_v2_is_immutable_unreviewed_and_all_evidence_verified(source_data):
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    p = DEST / m["review_worklist_filename"]
    rows = read_jsonl(p)
    assert m["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert sha_file(p) == m["review_worklist_sha256"]
    for key, (source, digest) in INPUTS.items():
        assert sha_file(source) == m["source_input_files_sha256"][key] == digest
    for key, (source, digest) in MANIFESTS.items():
        assert sha_file(source) == m["source_manifests_sha256"][key] == digest
    assert m["original_review_source_unique"] == len(rows) == 1181
    assert m["independent_semantic_reviews_still_required"] == 1181
    assert len({r["source_sha256"] for r in rows}) == 1181
    assert m["available_unreviewed_agent_drafts"] == 90
    assert m["draft_deterministic_qa_verdicts"] == {"PASS": 88, "REVIEW": 2}
    assert m["qa_review_without_targeted_draft"] == 818
    assert m["all_without_targeted_draft"] == 1091
    assert m["review_buckets"] == EXPECTED_COUNTS
    assert m["reviewer_source_ids_materialized_in_43_bundle_pilot"] == 43
    assert Counter(x["review_bucket"] for x in rows) == EXPECTED_COUNTS
    assert Counter(x["current_machine_qa_verdict"] for x in rows) == {
        "PASS": 273, "REVIEW": 860, "REJECT": 1, "MISSING_MACHINE": 47
    }
    for field in (
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "production_translation_files_modified",
        "prior_852_QA_bundle_stage_modified", "prior_43_QA_bundle_stage_modified",
        "official_original_assets_modified", "nas_modified",
    ):
        assert m[field] is False
    assert m["review_status"] == "pending"
    assert m["release_gate"] == "needs_independent_review"
    assert [x["review_bucket_order"] for x in rows] == sorted(
        x["review_bucket_order"] for x in rows
    )
    before = {x["source_sha256"]: x for x in source_data[0]}
    followup = {x["source_sha256"]: x for x in source_data[1]}
    assert len(followup) == 12
    for row in rows:
        sid = row["source_sha256"]
        assert sid == source_id(row["source"])
        assert row["review_status"] == "pending"
        assert row["release_gate"] == "needs_independent_review"
        assert row["safe_to_mount_as_final_overlay"] is False
        assert row["independent_review_complete"] is False
        assert row["semantic_accuracy_verified"] is False
        assert row["current_machine_qa_verdict"] == before[sid]["current_machine_qa_verdict"]
        assert row["machine_candidate_unreviewed"] == before[sid]["machine_candidate_unreviewed"]
        assert row["examples"] == before[sid]["examples"]
        if sid in followup:
            new = followup[sid]
            assert row["prior_review_bucket"] == "qa_review_without_draft"
            assert row["agent_draft_source_group"] == "unreviewed_followup_12"
            assert row["agent_correction_draft_unreviewed"] == new["translation_draft"]
            assert row["candidate_for_human_review_unreviewed"] == new["translation_draft"]
            assert row["agent_draft_deterministic_qa_verdict"] == new["draft_qa_verdict"]
            assert row["agent_draft_deterministic_qa_issues"] == new["draft_qa_issues"]
            assert row["review_bucket"] == (
                "qa_review_with_qa_pass_draft" if new["draft_qa_verdict"] == "PASS"
                else "draft_still_qa_review"
            )
        else:
            for k, v in before[sid].items():
                assert row[k] == v


def test_v2_full_worklist_matches_deterministic_rebase(source_data):
    rows, summary = rebase(*source_data)
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    assert rows == read_jsonl(DEST / m["review_worklist_filename"])
    assert summary["available_unreviewed_agent_drafts"] == 90
    assert summary["review_buckets"] == EXPECTED_COUNTS


@pytest.mark.parametrize("kind", (
    "drop_source", "duplicate_source", "forged_old_acceptance",
    "forged_new_acceptance", "old_draft_conflated",
    "old_machine_changed", "old_context_changed", "followup_source_changed",
    "followup_machine_changed", "followup_context_changed",
    "followup_QA_forged", "followup_QA_issues_misrepresented",
    "extra_followup", "duplicate_followup", "missing_trial_bundle",
    "missing_trial_field", "tamper_earlier_trial_field",
    "tamper_followup_trial_field", "trial_release_forged",
))
def test_rebase_fails_closed_against_stale_sources_and_false_review(source_data, kind):
    old, drafts, stage = copy.deepcopy(source_data)
    followup = drafts[0]
    sid = followup["source_sha256"]
    pos = next(i for i, x in enumerate(old) if x["source_sha256"] == sid)
    if kind == "drop_source":
        old.pop(pos)
    elif kind == "duplicate_source":
        old.append(old[pos])
    elif kind == "forged_old_acceptance":
        old[pos]["independent_review_complete"] = True
    elif kind == "forged_new_acceptance":
        drafts[0]["independent_review_complete"] = True
    elif kind == "old_draft_conflated":
        old[pos]["agent_correction_draft_unreviewed"] = "伪造已审译文"
    elif kind == "old_machine_changed":
        old[pos]["machine_candidate_unreviewed"] = "旧机器译文漂移"
    elif kind == "old_context_changed":
        old[pos]["examples"][0]["previous"] = "旧上下文漂移"
    elif kind == "followup_source_changed":
        drafts[0]["source"] = "別の原文"
    elif kind == "followup_machine_changed":
        drafts[0]["machine_candidate_unreviewed"] = "机器译文不同"
    elif kind == "followup_context_changed":
        drafts[0]["examples"][0]["next"] = "后文不同"
    elif kind == "followup_QA_forged":
        drafts[0]["draft_qa_verdict"] = "PASS" if (
            drafts[0]["draft_qa_verdict"] == "REVIEW"
        ) else "REVIEW"
        # Source could be technical PASS and now forged REVIEW: either break
        # QA issues or technical stage count, as no older accept status exists.
        if drafts[0]["draft_qa_verdict"] == "REVIEW":
            drafts[0]["draft_qa_issues"] = [{"code": "forged"}]
    elif kind == "followup_QA_issues_misrepresented":
        drafts[0]["draft_qa_issues"] = [{"code": "forged"}]
    elif kind == "extra_followup":
        drafts.append({**drafts[0], "source_sha256": old[0]["source_sha256"]})
    elif kind == "duplicate_followup":
        drafts.append(drafts[0])
    elif kind == "missing_trial_bundle":
        stage["bundles"].pop()
    elif kind == "missing_trial_field":
        stage["bundles"][0]["additional_unreviewed_fields"].pop()
    elif kind == "tamper_earlier_trial_field":
        old_sid = next(x["source_sha256"] for x in old
                       if x["agent_draft_source_group"] == "unreviewed_names_9")
        row = next(x for x in stage["bundles"] for y in x["additional_unreviewed_fields"]
                   if y["path_source_sha256"] == old_sid)
        field = next(y for y in row["additional_unreviewed_fields"]
                     if y["path_source_sha256"] == old_sid)
        field["localized"] += "擅改"
    elif kind == "tamper_followup_trial_field":
        field = next(y for x in stage["bundles"] for y in x["additional_unreviewed_fields"]
                     if y["path_source_sha256"] == sid)
        field["localized"] += "擅改"
    elif kind == "trial_release_forged":
        stage["overlay_merge_authorized"] = True
    with pytest.raises((ValueError, KeyError, StopIteration)):
        rebase(old, drafts, stage)


def test_existing_v2_rejects_overwrite():
    with pytest.raises(FileExistsError):
        build()

