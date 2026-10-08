"""Combined unreviewed Event-unit 31-source QA trial has zero delta overlap."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_10_targeted_review_drafts import verify_trial_delta
from scripts.stage_event_unit_combined_31_qa_pilot import (
    BASE, BASE_MANIFEST, DEST, TEN, TWENTY_ONE,
    UNIFIED_MANIFEST, UNIFIED_WORKLIST, build, validate_composite,
)


@pytest.fixture(scope="module")
def lineage():
    return (
        json.loads(BASE_MANIFEST.read_text(encoding="utf8")),
        json.loads((TEN / "manifest.json").read_text(encoding="utf8")),
        json.loads((TWENTY_ONE / "manifest.json").read_text(encoding="utf8")),
        read_jsonl(UNIFIED_WORKLIST),
    )


def test_actual_30_bundles_and_31_source_drafts_are_exactly_one_to_one(lineage):
    baseline, ten, twenty_one, worklist = lineage
    report = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    assert report["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert report["baseline_852_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert report["prior_10_manifest_sha256"] == sha_file(TEN / "manifest.json")
    assert report["prior_21_manifest_sha256"] == sha_file(TWENTY_ONE / "manifest.json")
    assert report["unified_review_manifest_sha256"] == sha_file(UNIFIED_MANIFEST)
    assert report["unified_review_worklist_sha256"] == sha_file(UNIFIED_WORKLIST)
    assert report["existing_baseline_QA_text_fields_unchanged"] == 12204
    assert report["additional_source_unique"] == 31
    assert report["additional_text_fields"] == 31
    assert report["isolated_composite_bundle_count"] == len(report["bundles"]) == 30
    assert report["source_unique_by_prior_trial"] == {
        "prior_10_name_numeric": 10,
        "prior_21_contextual_semantic": 21,
    }
    assert report["prior_trial_QA_verdicts"] == {"PASS": 30, "REVIEW": 1}
    for k in (
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
        "production_translations_modified", "existing_852_QA_bundles_modified",
        "original_official_assets_modified", "nas_modified",
    ):
        assert report[k] is False
    assert report["release_gate"] == "not_evaluated"

    previous = {x["remote"]: x for x in baseline["bundles"]}
    reviewers = {x["source_sha256"]: x for x in worklist}
    remote_ids, source_ids = set(), set()
    for row in report["bundles"]:
        remote = row["remote"]
        assert remote not in remote_ids
        remote_ids.add(remote)
        assert remote in previous
        assert row["roundtrip_verified_prior_trial"] is True
        assert row["non_text_objects_byte_identical_prior_trial"] is True
        assert row["release_gate"] == "NOT_EVALUATED"
        old = previous[remote]
        assert row["baseline_QA_bundle_sha256"] == old["localized_sha256"]
        assert sha_file(BASE / "jp-android" / remote) == old["localized_sha256"]
        prior_root = (
            TEN if row["source_trial"] == "prior_10_name_numeric"
            else TWENTY_ONE
        )
        assert sha_file(prior_root / "jp-android" / remote) == (
            row["source_trial_bundle_sha256"]
        )
        assert sha_file(DEST / "jp-android" / remote) == row["localized_sha256"]
        assert row["localized_sha256"] == row["source_trial_bundle_sha256"]
        assert (DEST / "jp-android" / remote).stat().st_size == row["output_bytes"]
        changes = {
            item["path_source_sha256"]: {
                "source": item["original"],
                "translation_draft": item["localized"],
            } for item in row["additional_unreviewed_fields"]
        }
        original_entry = next(
            x for x in (
                ten["bundles"] if prior_root == TEN else twenty_one["bundles"]
            ) if x["remote"] == remote
        )
        verified = verify_trial_delta(old, original_entry, changes)
        assert verified == row["additional_unreviewed_fields"]
        for item in verified:
            sid = item["path_source_sha256"]
            assert sid not in source_ids
            source_ids.add(sid)
            assert sid == source_id(item["original"])
            assert reviewers[sid]["agent_correction_draft_unreviewed"] == (
                item["localized"]
            )
            assert reviewers[sid]["independent_review_complete"] is False
    assert len(remote_ids) == 30
    assert len(source_ids) == 31


@pytest.mark.parametrize("failure", [
    "ten_missing_bundle", "twenty_one_missing_bundle",
    "remote_overlap", "forged_prior_review", "reviewer_candidate_drift",
    "reviewer_source_drift", "reviewer_accepted", "source_double_appearance",
    "forged_baseline", "extra_unreviewed_field", "changed_earlier_qa_translation",
])
def test_combined_trial_rejects_overlap_and_broken_source_evidence(
    lineage, failure,
):
    base, ten, twenty_one, reviewer = copy.deepcopy(lineage)
    if failure == "ten_missing_bundle":
        ten["bundles"].pop()
    elif failure == "twenty_one_missing_bundle":
        twenty_one["bundles"].pop()
    elif failure == "remote_overlap":
        twenty_one["bundles"][0]["remote"] = ten["bundles"][0]["remote"]
    elif failure == "forged_prior_review":
        twenty_one["independent_review_complete"] = True
    elif failure == "reviewer_candidate_drift":
        sid = ten["bundles"][0]["new_unreviewed_draft_fields"][0][
            "path_source_sha256"
        ]
        next(x for x in reviewer if x["source_sha256"] == sid)[
            "agent_correction_draft_unreviewed"
        ] = "修改过的候选"
    elif failure == "reviewer_source_drift":
        sid = ten["bundles"][0]["new_unreviewed_draft_fields"][0][
            "path_source_sha256"
        ]
        next(x for x in reviewer if x["source_sha256"] == sid)[
            "source"
        ] = "違うソース"
    elif failure == "reviewer_accepted":
        sid = ten["bundles"][0]["new_unreviewed_draft_fields"][0][
            "path_source_sha256"
        ]
        next(x for x in reviewer if x["source_sha256"] == sid)[
            "release_gate"
        ] = "accepted"
    elif failure == "source_double_appearance":
        twenty_one["bundles"][0]["new_unreviewed_semantic_draft_fields"][0][
            "path_source_sha256"
        ] = ten["bundles"][0]["new_unreviewed_draft_fields"][0][
            "path_source_sha256"
        ]
    elif failure == "forged_baseline":
        base["independent_reviewed"] = True
    elif failure == "extra_unreviewed_field":
        twenty_one["bundles"][0]["new_unreviewed_semantic_draft_fields"].append(
            copy.deepcopy(
                twenty_one["bundles"][0]["new_unreviewed_semantic_draft_fields"][0]
            )
        )
    elif failure == "changed_earlier_qa_translation":
        old_remote = ten["bundles"][0]["remote"]
        old = next(x for x in base["bundles"] if x["remote"] == old_remote)
        old["changes"][0]["localized"] += "被擅自改动"
    with pytest.raises((ValueError, KeyError, StopIteration)):
        validate_composite(base, ten, twenty_one, reviewer)


def test_existing_combined_trial_is_immutable():
    with pytest.raises(FileExistsError):
        build()

