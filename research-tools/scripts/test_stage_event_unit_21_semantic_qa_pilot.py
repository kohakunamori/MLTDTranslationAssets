"""The 21-source semantic pilot preserves the frozen QA stage exactly."""
from __future__ import annotations

import copy
import json
from collections import defaultdict

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_10_targeted_review_drafts import verify_trial_delta
from scripts.stage_event_unit_21_semantic_qa_pilot import (
    BASE, BASE_MANIFEST, COHORT_PATH, DEST, DRAFT_FILE, DRAFT_MANIFEST,
    REVIEW_FILE, build, validate_drafts,
)


@pytest.fixture(scope="module")
def sources():
    return (
        json.loads(BASE_MANIFEST.read_text(encoding="utf8")),
        read_jsonl(DRAFT_FILE),
        read_jsonl(REVIEW_FILE),
    )


def test_real_20_bundle_21_source_trial_hashes_are_exact_and_review_only(sources):
    stage = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    baseline = sources[0]
    assert stage["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert stage["base_852_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert stage["source_852_cohort_sha256"] == sha_file(COHORT_PATH)
    assert stage["source_21_draft_manifest_sha256"] == sha_file(DRAFT_MANIFEST)
    assert stage["source_21_drafts_sha256"] == sha_file(DRAFT_FILE)
    assert stage["source_860_review_sha256"] == sha_file(REVIEW_FILE)
    assert stage["existing_852_baseline_translated_fields_unchanged"] == 12204
    assert stage["new_unreviewed_semantic_source_unique"] == 21
    assert stage["new_unreviewed_semantic_text_fields"] == 21
    assert stage["new_bundle_roundtrip_count"] == len(stage["bundles"]) == 20
    assert stage["draft_qa_verdicts"] == {"PASS": 21}
    for field in [
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
        "existing_852_QA_stage_modified", "production_translations_modified",
        "official_original_assets_modified", "nas_modified",
    ]:
        assert stage[field] is False
    by_old = {x["remote"]: x for x in baseline["bundles"]}
    corrections = {x["source_sha256"]: x for x in sources[1]}
    changed = set()
    for entry in stage["bundles"]:
        remote = entry["remote"]
        assert entry["roundtrip_verified"] is True
        assert entry["non_text_objects_byte_identical"] is True
        assert entry["release_gate"] == "NOT_EVALUATED"
        assert sha_file(DEST / "jp-android" / remote) == entry["localized_sha256"]
        assert (DEST / "jp-android" / remote).stat().st_size == entry["output_bytes"]
        assert entry["old_qa_bundle_sha256"] == by_old[remote]["localized_sha256"]
        assert sha_file(BASE / "jp-android" / remote) == entry["old_qa_bundle_sha256"]
        intended = {
            item["path_source_sha256"]: corrections[item["path_source_sha256"]]
            for item in entry["new_unreviewed_semantic_draft_fields"]
        }
        extras = verify_trial_delta(by_old[remote], entry, intended)
        assert len(extras) == len(intended)
        for item in extras:
            sid = item["path_source_sha256"]
            assert sid not in changed
            changed.add(sid)
            assert source_id(item["original"]) == sid
            assert item["localized"] == corrections[sid]["translation_draft"]
    assert changed == set(corrections)


@pytest.mark.parametrize("failure", [
    "duplicate_manual_draft", "lost_manual_draft", "forged_release_status",
    "missing_context", "bad_translation", "changed_original_source",
    "changed_prior_candidate", "source_sha_overlaps_baseline",
    "duplicate_original_review",
])
def test_reject_stale_or_malicious_source_and_draft_changes(sources, failure):
    base, drafts, reviews = copy.deepcopy(sources)
    if failure == "duplicate_manual_draft":
        drafts.append(drafts[0])
    elif failure == "lost_manual_draft":
        drafts.pop()
    elif failure == "forged_release_status":
        drafts[0]["release_gate"] = "accepted"
    elif failure == "missing_context":
        drafts[0]["examples"] = []
    elif failure == "bad_translation":
        drafts[0]["translation_draft"] = "4个内容变成了5个"
    elif failure == "changed_original_source":
        drafts[0]["source"] = "違う日本語の文章"
    elif failure == "changed_prior_candidate":
        drafts[0]["machine_candidate_unreviewed"] = "别人改过的候选"
    elif failure == "source_sha_overlaps_baseline":
        first = base["bundles"][0]["changes"][0]
        drafts[0]["source_sha256"] = first["path_source_sha256"]
        drafts[0]["source"] = first["original"]
    elif failure == "duplicate_original_review":
        reviews.append(reviews[0])
    with pytest.raises((ValueError, KeyError)):
        validate_drafts(base, drafts, reviews)


def test_targeted_trial_rejects_unexpected_change_to_existing_text(sources):
    baseline, drafts, _ = sources
    actual = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    entry = copy.deepcopy(actual["bundles"][0])
    old = next(x for x in baseline["bundles"] if x["remote"] == entry["remote"])
    old_entries = {
        (x["command_index"], x["path_source_sha256"])
        for x in old["changes"]
    }
    index = next(i for i, x in enumerate(entry["changes"])
                 if (x["command_index"], x["path_source_sha256"]) in old_entries)
    entry["changes"][index]["localized"] += "被意外修改"
    intended = {
        x["path_source_sha256"]: next(
            r for r in drafts if r["source_sha256"] == x["path_source_sha256"]
        )
        for x in entry["new_unreviewed_semantic_draft_fields"]
    }
    with pytest.raises(ValueError):
        verify_trial_delta(old, entry, intended)


def test_existing_immutable_pilot_refuses_repeat_build():
    with pytest.raises(FileExistsError):
        build()

