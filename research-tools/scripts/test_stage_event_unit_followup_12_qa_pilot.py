"""Followup 12-source Unity trial is non-release and original-bundle roundtrip."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.stage_event_unit_10_targeted_review_drafts import verify_trial_delta
from scripts.stage_event_unit_followup_12_qa_pilot import (
    BASE, BASE_MANIFEST, DEST, DRAFT_FILE, DRAFT_MANIFEST, PRIOR_COMPOSITE,
    REVIEW_FILE, build, validate_sources,
)


@pytest.fixture(scope="module")
def trial_sources():
    return (
        json.loads(BASE_MANIFEST.read_text(encoding="utf8")),
        read_jsonl(DRAFT_FILE),
        read_jsonl(REVIEW_FILE),
    )


def test_followup_12_real_bundle_roundtrip_and_unreviewed_delta(trial_sources):
    old, drafts, worklist = trial_sources
    report = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    assert report["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert report["baseline_852_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert report["followup_12_manifest_sha256"] == sha_file(DRAFT_MANIFEST)
    assert report["followup_12_draft_sha256"] == sha_file(DRAFT_FILE)
    assert report["unified_review_worklist_sha256"] == sha_file(REVIEW_FILE)
    assert report["prior_31_manifest_sha256"] == sha_file(PRIOR_COMPOSITE / "manifest.json")
    assert report["baseline_12204_changed_fields_preserved"] == 12204
    assert report["additional_unreviewed_source_unique"] == 12
    assert report["additional_unreviewed_text_fields"] == 12
    assert report["roundtrip_bundle_count"] == len(report["bundles"]) == 11
    assert report["qa_verdicts"] == {"PASS": 11, "REVIEW": 1}
    assert report["prior_31_combination_requires_rebuild"] is True
    assert len(report["prior_31_overlapping_remotes"]) == 1
    for flag in [
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
        "original_official_assets_modified", "production_translations_modified",
        "prior_852_QA_bundles_modified", "prior_31_QA_bundles_modified",
        "nas_modified",
    ]:
        assert report[flag] is False
    old_by_remote = {x["remote"]: x for x in old["bundles"]}
    source_drafts = {x["source_sha256"]: x for x in drafts}
    seen = set()
    for row in report["bundles"]:
        assert row["roundtrip_verified"] is True
        assert row["non_text_objects_byte_identical"] is True
        assert row["release_gate"] == "NOT_EVALUATED"
        remote = row["remote"]
        old_record = old_by_remote[remote]
        assert row["existing_QA_bundle_sha256"] == old_record["localized_sha256"]
        assert sha_file(BASE / "jp-android" / remote) == row["existing_QA_bundle_sha256"]
        assert row["overlaps_prior_31_bundle"] == (
            remote in report["prior_31_overlapping_remotes"]
        )
        assert row["overlap_requires_full_rebuild_not_byte_copy"] == (
            remote in report["prior_31_overlapping_remotes"]
        )
        path = DEST / "jp-android" / remote
        assert sha_file(path) == row["localized_sha256"]
        assert path.stat().st_size == row["output_bytes"]
        intended = {
            x["path_source_sha256"]: source_drafts[x["path_source_sha256"]]
            for x in row["new_unreviewed_draft_fields"]
        }
        changes = verify_trial_delta(old_record, row, intended)
        assert len(changes) == len(intended)
        for sid in intended:
            assert sid not in seen
            seen.add(sid)
    assert seen == set(source_drafts)


@pytest.mark.parametrize("kind", [
    "lost_draft", "duplicate_draft", "forged_release",
    "forged_semantic_review", "changed_source", "changed_machine_candidate",
    "changed_proposed_translation", "duplicate_review",
    "broken_baseline_release", "changed_baseline_translated_field",
])
def test_followup_stage_rejects_draft_and_baseline_mutation(trial_sources, kind):
    base, drafts, review = copy.deepcopy(trial_sources)
    if kind == "lost_draft":
        drafts.pop()
    elif kind == "duplicate_draft":
        drafts.append(drafts[0])
    elif kind == "forged_release":
        drafts[0]["release_gate"] = "accepted"
    elif kind == "forged_semantic_review":
        drafts[0]["independent_review_complete"] = True
    elif kind == "changed_source":
        drafts[0]["source"] += "違う日本語"
    elif kind == "changed_machine_candidate":
        drafts[0]["machine_candidate_unreviewed"] += "更改旧数据"
    elif kind == "changed_proposed_translation":
        drafts[0]["translation_draft"] += "残留日文です"
    elif kind == "duplicate_review":
        review.append(review[0])
    elif kind == "broken_baseline_release":
        base["safe_to_mount_as_final_overlay"] = True
    elif kind == "changed_baseline_translated_field":
        base["bundles"][0]["changes"][0]["localized"] = "被改写"
    with pytest.raises((ValueError, KeyError)):
        validate_sources(base, drafts, review)


def test_immutable_followup_stage_cannot_be_overwritten():
    with pytest.raises(FileExistsError):
        build()

