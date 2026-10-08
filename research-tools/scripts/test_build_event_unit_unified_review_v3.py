"""Immutable v3 Event-unit reviewer: 97 unreviewed drafts and 811 QA REVIEW undrafted."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_unified_review_v3 import (
    BUCKETS, DEST, EXPECTED_BUCKET_COUNTS, INPUT_FILES, INPUT_MANIFESTS,
    build, update_worklist,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id


@pytest.fixture(scope="module")
def sources():
    return (
        read_jsonl(INPUT_FILES["v2"][0]),
        read_jsonl(INPUT_FILES["bleed"][0]),
        read_jsonl(INPUT_FILES["six"][0]),
        json.loads(INPUT_MANIFESTS["trial50"][0].read_text(encoding="utf8")),
    )


def test_v3_persisted_1181_source_provenance_and_review_only(sources):
    manifest = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    path = DEST / manifest["review_worklist_filename"]
    rows = read_jsonl(path)
    v2, bleed, six, stage = sources
    assert sha_file(path) == manifest["review_worklist_sha256"]
    assert manifest["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    for name, (f, digest) in INPUT_FILES.items():
        assert sha_file(f) == digest == manifest["source_files_sha256"][name]
    for name, (f, digest) in INPUT_MANIFESTS.items():
        assert sha_file(f) == digest == manifest["source_manifests_sha256"][name]
    assert len(rows) == manifest["original_review_source_unique"] == 1181
    assert len({r["source_sha256"] for r in rows}) == 1181
    assert manifest["independent_semantic_reviews_still_required"] == 1181
    assert manifest["available_unreviewed_agent_drafts"] == 97
    assert manifest["draft_deterministic_qa_verdicts"] == {"PASS": 95, "REVIEW": 2}
    assert manifest["qa_review_without_targeted_draft"] == 811
    assert manifest["all_without_targeted_draft"] == 1084
    assert manifest["review_buckets"] == EXPECTED_BUCKET_COUNTS
    assert manifest["unreviewed_drafts_materialized_in_50_bundle_trial"] == 50
    assert Counter(x["review_bucket"] for x in rows) == EXPECTED_BUCKET_COUNTS
    assert Counter(x["current_machine_qa_verdict"] for x in rows) == {
        "PASS": 273, "REVIEW": 860, "REJECT": 1, "MISSING_MACHINE": 47
    }
    assert [r["review_bucket_order"] for r in rows] == sorted(
        r["review_bucket_order"] for r in rows
    )
    for field in (
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "production_translations_modified",
        "old_QA_bundle_stages_modified", "official_original_assets_modified",
        "nas_modified",
    ):
        assert manifest[field] is False
    old = {r["source_sha256"]: r for r in v2}
    added = {r["source_sha256"]: r for r in bleed + six}
    assert len(added) == 7
    materialized = {x["path_source_sha256"]: x
                    for bundle in stage["bundles"]
                    for x in bundle["additional_unreviewed_fields"]}
    for row in rows:
        sid = row["source_sha256"]
        assert sid == source_id(row["source"])
        assert row["source"] == old[sid]["source"]
        assert row["machine_candidate_unreviewed"] == old[sid]["machine_candidate_unreviewed"]
        assert row["examples"] == old[sid]["examples"]
        assert row["current_machine_qa_verdict"] == old[sid]["current_machine_qa_verdict"]
        assert row["review_status"] == "pending"
        assert row["release_gate"] == "needs_independent_review"
        assert row["independent_review_complete"] is False
        assert row["semantic_accuracy_verified"] is False
        assert row["safe_to_mount_as_final_overlay"] is False
        if sid in added:
            assert row["prior_review_bucket_v2"] == "qa_review_without_draft"
            assert row["review_bucket"] == "qa_review_with_qa_pass_draft"
            assert row["agent_correction_draft_unreviewed"] == added[sid]["translation_draft"]
            assert row["candidate_for_human_review_unreviewed"] == added[sid]["translation_draft"]
            assert row["agent_draft_deterministic_qa_verdict"] == "PASS"
            assert row["agent_draft_deterministic_qa_issues"] == []
            assert materialized[sid]["localized"] == added[sid]["translation_draft"]
        else:
            for key, value in old[sid].items():
                assert row[key] == value
    assert rows == update_worklist(*sources)[0]


@pytest.mark.parametrize("failure", (
    "duplicate_old", "missing_old", "old_sha_wrong", "old_accepted",
    "old_machine_changed", "old_drafted_already",
    "missing_new", "duplicate_new", "new_sha_wrong", "new_translation_changed",
    "new_context_changed", "new_accepted", "new_qa_review", "wrong_bleed_provenance",
    "missing_bundle", "duplicate_bundle", "missing_field",
    "changed_prior_trial", "changed_new_trial", "pilot_accepted",
))
def test_v3_fails_closed_when_input_or_release_evidence_mutates(sources, failure):
    old, bleed, six, trial = copy.deepcopy(sources)
    focus = bleed[0]["source_sha256"]
    index = next(i for i, x in enumerate(old) if x["source_sha256"] == focus)
    if failure == "duplicate_old":
        old.append(old[index])
    elif failure == "missing_old":
        old.pop(index)
    elif failure == "old_sha_wrong":
        old[index]["source"] += "偽造"
    elif failure == "old_accepted":
        old[index]["independent_review_complete"] = True
    elif failure == "old_machine_changed":
        old[index]["machine_candidate_unreviewed"] += "擅改"
    elif failure == "old_drafted_already":
        old[index]["agent_correction_draft_unreviewed"] = "其他草稿"
    elif failure == "missing_new":
        six.pop()
    elif failure == "duplicate_new":
        six.append(six[0])
    elif failure == "new_sha_wrong":
        bleed[0]["source"] += "偽造"
    elif failure == "new_translation_changed":
        six[0]["translation_draft"] += "擅改"
    elif failure == "new_context_changed":
        six[0]["examples"][0]["previous"] += "假的"
    elif failure == "new_accepted":
        six[0]["independent_review_complete"] = True
    elif failure == "new_qa_review":
        six[0]["draft_qa_verdict"] = "REVIEW"
    elif failure == "wrong_bleed_provenance":
        bleed[0]["character_rendering_is_authoritative"] = True
    elif failure == "missing_bundle":
        trial["bundles"].pop()
    elif failure == "duplicate_bundle":
        trial["bundles"].append(trial["bundles"][0])
    elif failure == "missing_field":
        b = next(b for b in trial["bundles"] if any(
            x["path_source_sha256"] == focus
            for x in b["additional_unreviewed_fields"]
        ))
        b["additional_unreviewed_fields"].clear()
    elif failure == "changed_prior_trial":
        first = next(x for x in old if x["agent_correction_draft_unreviewed"] is not None)
        sid = first["source_sha256"]
        field = next(x for b in trial["bundles"] for x in b["additional_unreviewed_fields"]
                     if x["path_source_sha256"] == sid)
        field["localized"] += "伪造"
    elif failure == "changed_new_trial":
        field = next(x for b in trial["bundles"] for x in b["additional_unreviewed_fields"]
                     if x["path_source_sha256"] == focus)
        field["localized"] += "伪造"
    elif failure == "pilot_accepted":
        trial["overlay_merge_authorized"] = True
    with pytest.raises((ValueError, KeyError)):
        update_worklist(old, bleed, six, trial)


def test_frozen_v3_review_is_immutable():
    with pytest.raises(FileExistsError):
        build()

