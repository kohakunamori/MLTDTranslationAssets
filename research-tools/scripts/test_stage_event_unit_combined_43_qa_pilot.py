"""43 unreviewed source drafts coexist without loss in 40 QA-only bundles."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_combined_43_qa_pilot import (
    BASE, BASE_MANIFEST, DEST, DRAFT_FILE, NEW, PRIOR, UNIFIED_WORKLIST,
    build, validate_43,
)


@pytest.fixture(scope="module")
def materials():
    return (
        json.loads(BASE_MANIFEST.read_text(encoding="utf8")),
        json.loads((PRIOR / "manifest.json").read_text(encoding="utf8")),
        json.loads((NEW / "manifest.json").read_text(encoding="utf8")),
        read_jsonl(UNIFIED_WORKLIST),
        read_jsonl(DRAFT_FILE),
    )


def test_exact_43_draft_40_bundle_source_integrity_and_no_release(materials):
    old, prior, new, original_review, new_drafts = materials
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    assert m["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert m["baseline_852_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert m["prior_31_manifest_sha256"] == sha_file(PRIOR / "manifest.json")
    assert m["followup_12_manifest_sha256"] == sha_file(NEW / "manifest.json")
    assert m["followup_12_draft_sha256"] == sha_file(DRAFT_FILE)
    assert m["unified_review_worklist_sha256"] == sha_file(UNIFIED_WORKLIST)
    assert m["original_12204_QA_text_fields_preserved"] == 12204
    assert m["additional_unreviewed_source_unique"] == 43
    assert m["additional_unreviewed_text_fields"] == 43
    assert m["bundle_count"] == len(m["bundles"]) == 40
    assert m["draft_QA_verdicts"] == {"PASS": 41, "REVIEW": 2}
    assert m["copied_from_prior_31"] == 29
    assert m["copied_from_followup_12"] == 10
    for key in [
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
        "production_translations_modified", "prior_852_QA_bundles_modified",
        "prior_31_QA_bundles_modified", "prior_12_QA_bundles_modified",
        "official_original_assets_modified", "nas_modified",
    ]:
        assert m[key] is False
    assert m["release_gate"] == "not_evaluated"
    shared = m["overlap_remotes_rebuilt_from_original"]
    assert len(shared) == 1
    by_remote = {x["remote"]: x for x in m["bundles"]}
    assert len(by_remote) == 40
    assert Counter(x["source_mode"] for x in m["bundles"]) == {
        "sha_exact_copy_from_prior_31": 29,
        "sha_exact_copy_from_followup_12": 10,
        "original_rebuild_both_drafts": 1,
    }
    source_ids = set()
    review = {x["source_sha256"]: x for x in original_review}
    newer = {x["source_sha256"]: x for x in new_drafts}
    original = {x["remote"]: x for x in old["bundles"]}
    for remote, bundle in by_remote.items():
        assert bundle["release_gate"] == "NOT_EVALUATED"
        assert bundle["roundtrip_verified"] is True
        assert bundle["non_text_objects_byte_identical"] is True
        assert bundle["original_bundle_sha256"] == original[remote]["source_sha256"]
        assert bundle["baseline_852_bundle_sha256"] == (
            original[remote]["localized_sha256"]
        )
        assert sha_file(BASE / "jp-android" / remote) == (
            bundle["baseline_852_bundle_sha256"]
        )
        path = DEST / "jp-android" / remote
        assert sha_file(path) == bundle["localized_sha256"]
        assert path.stat().st_size == bundle["output_bytes"]
        if remote in shared:
            assert bundle["source_mode"] == "original_rebuild_both_drafts"
            assert len(bundle["additional_unreviewed_fields"]) == 2
            assert {
                x["path_source_sha256"][:14]
                for x in bundle["additional_unreviewed_fields"]
            } == {"810f9bd361514b", "043c12842fbdcd"}
        else:
            source_root = (
                PRIOR if bundle["source_mode"] ==
                    "sha_exact_copy_from_prior_31" else NEW
            )
            assert sha_file(source_root / "jp-android" / remote) == (
                bundle["localized_sha256"]
            )
        for change in bundle["additional_unreviewed_fields"]:
            sid = change["path_source_sha256"]
            assert sid not in source_ids
            source_ids.add(sid)
            assert sid == source_id(change["original"])
            expected_translation = (
                newer[sid]["translation_draft"] if sid in newer
                else review[sid]["agent_correction_draft_unreviewed"]
            )
            assert expected_translation == change["localized"]
    assert len(source_ids) == 43
    assert len(source_ids & set(newer)) == 12
    assert len(source_ids - set(newer)) == 31


@pytest.mark.parametrize("kind", [
    "lost_31_bundle", "lost_12_bundle", "wrong_31_QA_count",
    "wrong_12_QA_count", "original_review_accepted",
    "new_draft_accepted", "new_draft_candidate_mutated",
    "old_draft_candidate_mutated", "overlap_remote_removed",
    "duplicate_new_source", "baseline_mutation",
    "prior_31_release_forged", "followup_12_release_forged",
])
def test_combined_43_guards_reject_broken_delta_or_review(materials, kind):
    old, prior, new, original_review, new_drafts = copy.deepcopy(materials)
    if kind == "lost_31_bundle":
        prior["bundles"].pop()
    elif kind == "lost_12_bundle":
        new["bundles"].pop()
    elif kind == "wrong_31_QA_count":
        prior["prior_trial_QA_verdicts"] = {"PASS": 31}
    elif kind == "wrong_12_QA_count":
        new["qa_verdicts"] = {"PASS": 12}
    elif kind == "original_review_accepted":
        original_review[0]["independent_review_complete"] = True
        # Mutation at the actual previous-31 source if first isn't included.
        sid = prior["bundles"][0]["additional_unreviewed_fields"][0][
            "path_source_sha256"
        ]
        next(x for x in original_review if x["source_sha256"] == sid)[
            "independent_review_complete"
        ] = True
    elif kind == "new_draft_accepted":
        new_drafts[0]["independent_review_complete"] = True
    elif kind == "new_draft_candidate_mutated":
        new_drafts[0]["translation_draft"] += "擅自改稿"
    elif kind == "old_draft_candidate_mutated":
        sid = prior["bundles"][0]["additional_unreviewed_fields"][0][
            "path_source_sha256"
        ]
        next(x for x in original_review if x["source_sha256"] == sid)[
            "agent_correction_draft_unreviewed"
        ] += "擅自改稿"
    elif kind == "overlap_remote_removed":
        new["prior_31_overlapping_remotes"] = []
    elif kind == "duplicate_new_source":
        new_drafts.append(new_drafts[0])
    elif kind == "baseline_mutation":
        old["safe_to_mount_as_final_overlay"] = True
    elif kind == "prior_31_release_forged":
        prior["overlay_merge_authorized"] = True
    elif kind == "followup_12_release_forged":
        new["independent_review_complete"] = True
    with pytest.raises((ValueError, KeyError)):
        validate_43(old, prior, new, original_review, new_drafts)


def test_immutable_newest_composite_stage_cannot_be_overwritten():
    with pytest.raises(FileExistsError):
        build()

