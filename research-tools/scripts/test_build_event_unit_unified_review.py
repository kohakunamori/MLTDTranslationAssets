"""Immutable, disjoint, source-bound 1,181-row Event-unit review entrypoint."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_unified_review import (
    BUCKETS, DEST, EXPECTED_BUCKET_COUNTS, INDEX, INPUTS, MANIFESTS,
    SNAPSHOT, build, unify, validate_manifest_chain,
)
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id


@pytest.fixture(scope="module")
def all_source_files():
    return {
        name: read_jsonl(path) for name, (path, _) in INPUTS.items()
    }


def test_authoritative_review_worklist_all_sources_and_file_provenance():
    report = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    worklist = read_jsonl(DEST / report["review_worklist_filename"])
    assert report["version_identity"]["version_key"] == (
        "jp-client-9.0.200-assets-1077100"
    )
    assert report["review_worklist_sha256"] == sha_file(
        DEST / report["review_worklist_filename"]
    )
    for key, expected in report["source_manifests_sha256"].items():
        assert sha_file(MANIFESTS[key]) == expected
    for key, digest in report["source_files_sha256"].items():
        assert sha_file(INPUTS[key][0]) == digest == INPUTS[key][1]
    assert len(worklist) == 1181
    assert len({r["source_sha256"] for r in worklist}) == 1181
    assert report["original_review_unique"] == 1181
    assert report["still_requires_independent_semantic_review"] == 1181
    assert report["unreviewed_draft_unique"] == 78
    assert report["unreviewed_draft_technical_QA_pass"] == 77
    assert report["unreviewed_draft_technical_QA_review"] == 1
    assert report["no_draft_yet"] == 1103
    assert report["review_buckets"] == EXPECTED_BUCKET_COUNTS
    assert report["bucket_order"] == list(BUCKETS)
    assert Counter(r["review_bucket"] for r in worklist) == EXPECTED_BUCKET_COUNTS
    assert Counter(r["initial_qa_verdict"] for r in worklist) == {
        "REJECT": 1, "REVIEW": 1133, "MISSING_MACHINE": 47
    }
    assert Counter(r["current_machine_qa_verdict"] for r in worklist) == {
        "REJECT": 1, "REVIEW": 860, "PASS": 273, "MISSING_MACHINE": 47
    }
    assert [r["review_bucket_order"] for r in worklist] == sorted(
        r["review_bucket_order"] for r in worklist
    )
    for field in [
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "production_translations_modified",
        "existing_QA_bundles_modified", "official_original_assets_modified",
        "nas_modified",
    ]:
        assert report[field] is False
    assert report["all_reviews_pending"] is True
    for row in worklist:
        assert row["source_sha256"] == source_id(row["source"])
        assert row["review_status"] == "pending"
        assert row["independent_review_complete"] is False
        assert row["semantic_accuracy_verified"] is False
        assert row["safe_to_mount_as_final_overlay"] is False
        assert row["release_gate"] == "needs_independent_review"
        assert row["candidate_for_human_review_unreviewed"] == (
            row["agent_correction_draft_unreviewed"]
            if row["agent_correction_draft_unreviewed"] is not None else
            row["machine_candidate_unreviewed"]
        )
        if row["agent_draft_deterministic_qa_verdict"] == "PASS":
            assert row["agent_draft_deterministic_qa_issues"] == []
        if row["review_bucket"] == "rule_cleared_qa_pass_needs_semantic_review":
            assert row["current_machine_qa_verdict"] == "PASS"
            assert row["agent_correction_draft_unreviewed"] is None
        if row["review_bucket"] == "qa_review_without_draft":
            assert row["current_machine_qa_verdict"] == "REVIEW"
            assert row["agent_correction_draft_unreviewed"] is None
        if row["review_bucket"] == "missing_machine_with_qa_pass_draft":
            assert row["current_machine_qa_verdict"] == "MISSING_MACHINE"
            assert row["machine_candidate_unreviewed"] == ""
            assert row["agent_correction_draft_unreviewed"]
            assert row["agent_draft_source_group"] == "unreviewed_tail_47"


def test_source_bound_unified_queue_exhaustive_lineage_matches_source_files(
    all_source_files,
):
    report = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    saved = read_jsonl(DEST / report["review_worklist_filename"])
    rebuilt, summary = unify(all_source_files)
    assert saved == rebuilt
    assert summary["review_buckets"] == EXPECTED_BUCKET_COUNTS
    assert summary["no_draft_yet"] == 1103
    validate_manifest_chain(
        version_identity(
            SNAPSHOT, client_version="9.0.200",
            asset_version="1077100", asset_index=INDEX,
        )
    )


@pytest.mark.parametrize("kind", [
    "duplicate_first", "missing_new_review", "duplicate_second",
    "extra_old_pass", "old_context_changed", "old_candidate_changed",
    "qa_approved_falsely", "new_qa_approved_falsely", "tail_approved_falsely",
    "draft_status_forged", "draft_original_changed", "draft_misbound",
    "duplicate_draft", "wrong_tail_machine", "qa_issue_falsely_removed",
    "bad_source_sha",
])
def test_unified_fails_closed_for_broken_input_or_forged_release(
    all_source_files, kind,
):
    entries = copy.deepcopy(all_source_files)
    originals = entries["initial_review_1181"]
    pending = entries["current_review_860"]
    cleared = entries["qa_cleared_unreviewed_273"]
    tail = entries["unreviewed_tail_47"]
    drafted = entries["unreviewed_semantic_21"]
    if kind == "duplicate_first":
        originals.append(originals[0])
    elif kind == "missing_new_review":
        pending.pop()
    elif kind == "duplicate_second":
        pending.append(pending[0])
    elif kind == "extra_old_pass":
        cleared.append(cleared[0])
    elif kind == "old_context_changed":
        orig = next(x for x in originals
                    if x["source_sha256"] == pending[0]["source_sha256"])
        orig["examples"][0]["next"] = "后续上下文被替换"
    elif kind == "old_candidate_changed":
        orig = next(x for x in originals
                    if x["source_sha256"] == pending[0]["source_sha256"])
        orig["machine_candidate_unreviewed"] = "伪造旧译文"
    elif kind == "qa_approved_falsely":
        originals[0]["review_status"] = "accepted"
    elif kind == "new_qa_approved_falsely":
        pending[0]["independent_review_complete"] = True
    elif kind == "tail_approved_falsely":
        tail[0]["release_gate"] = "approved"
    elif kind == "draft_status_forged":
        drafted[0]["status"] = "reviewed"
    elif kind == "draft_original_changed":
        drafted[0]["machine_candidate_unreviewed"] = "擅改机器翻译"
    elif kind == "draft_misbound":
        drafted[0]["source_sha256"] = drafted[1]["source_sha256"]
    elif kind == "duplicate_draft":
        drafted.append(drafted[0])
    elif kind == "wrong_tail_machine":
        orig = next(x for x in originals
                    if x["source_sha256"] == tail[0]["source_sha256"])
        orig["machine_candidate_unreviewed"] = "伪造补尾机器译文"
    elif kind == "qa_issue_falsely_removed":
        pending[0]["issues"] = []
    elif kind == "bad_source_sha":
        originals[0]["source"] = "違う日本語"
    with pytest.raises((ValueError, KeyError)):
        unify(entries)


def test_cannot_overwrite_unified_audit():
    with pytest.raises(FileExistsError):
        build()

