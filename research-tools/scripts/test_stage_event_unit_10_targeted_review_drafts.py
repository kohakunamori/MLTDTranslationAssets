"""Ten-source review-only Unity roundtrip pilot is exactly delta-only."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE, BASE_MANIFEST, COHORT_PATH, DEST, NINE_FILE, NUMERIC_FILE,
    build, collect_trial_sources, verify_trial_delta,
)


@pytest.fixture(scope="module")
def material():
    return (
        json.loads(BASE_MANIFEST.read_text(encoding="utf8")),
        read_jsonl(NINE_FILE), read_jsonl(NUMERIC_FILE),
    )


def test_actual_ten_bundles_preserve_existing_text_and_never_release(material):
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    assert m["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert m["base_852_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert m["source_852_cohort_sha256"] == sha_file(COHORT_PATH)
    assert m["nine_draft_sha256"] == sha_file(NINE_FILE)
    assert m["numeric_draft_sha256"] == sha_file(NUMERIC_FILE)
    assert m["additional_unreviewed_source_unique"] == 10
    assert m["additional_unreviewed_text_fields"] == 10
    assert m["existing_QA_changed_text_fields_preserved"] == 12204
    assert m["isolated_original_source_bundle_roundtrips"] == len(m["bundles"]) == 10
    assert m["new_draft_qa_verdicts"] == {"PASS": 9, "REVIEW": 1}
    assert m["overlay_merge_authorized"] is False
    assert m["safe_to_mount_as_final_overlay"] is False
    assert m["independent_review_complete"] is False
    assert m["production_translations_modified"] is False
    assert m["baseline_QA_stage_modified"] is False
    assert m["nas_modified"] is False
    prior = {r["remote"]: r for r in material[0]["bundles"]}
    candidates = {
        x["source_sha256"]: x for x in material[1] + material[2]
    }
    seen = set()
    for x in m["bundles"]:
        assert x["roundtrip_verified"] is True
        assert x["non_text_objects_byte_identical"] is True
        assert x["release_gate"] == "NOT_EVALUATED"
        path = DEST / "jp-android" / x["remote"]
        assert sha_file(path) == x["localized_sha256"]
        assert path.stat().st_size == x["output_bytes"]
        assert x["old_QA_bundle_sha256"] == prior[x["remote"]]["localized_sha256"]
        assert sha_file(BASE / "jp-android" / x["remote"]) == x["old_QA_bundle_sha256"]
        intended = {
            x["new_unreviewed_draft_fields"][0]["path_source_sha256"]:
                candidates[x["new_unreviewed_draft_fields"][0]["path_source_sha256"]]
        }
        edits = verify_trial_delta(prior[x["remote"]], x, intended)
        assert len(edits) == 1
        seen.update(intended)
    assert seen == set(candidates)


@pytest.mark.parametrize("failure", [
    "missing_nine", "duplicate_nine", "unexpected_accepted",
    "machine_overlap", "translation_changed", "numeric_missing",
])
def test_trial_rejects_draft_input_drift(material, failure):
    old, nine, numeric = copy.deepcopy(material)
    if failure == "missing_nine":
        nine.pop()
    elif failure == "duplicate_nine":
        nine.append(nine[0])
    elif failure == "unexpected_accepted":
        nine[0]["release_gate"] = "accepted"
    elif failure == "machine_overlap":
        change = old["bundles"][0]["changes"][0]
        nine[0]["source_sha256"] = change["path_source_sha256"]
        nine[0]["source"] = change["original"]
    elif failure == "translation_changed":
        nine[0]["translation_draft"] = "这里被恶意篡改"
    elif failure == "numeric_missing":
        numeric.pop()
    with pytest.raises(ValueError):
        collect_trial_sources(old, nine, numeric)


def test_trial_rejects_malicious_extra_delta(material):
    old, nine, numeric = material
    manifest = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    row = manifest["bundles"][0]
    baseline = next(x for x in old["bundles"] if x["remote"] == row["remote"])
    fake = copy.deepcopy(row)
    fake["changes"][0]["localized"] += "被改过"
    sid = fake["new_unreviewed_draft_fields"][0]["path_source_sha256"]
    intended = {
        sid: next(x for x in nine + numeric if x["source_sha256"] == sid)
    }
    with pytest.raises(ValueError):
        verify_trial_delta(baseline, fake, intended)


def test_immutable_trial_will_not_be_overwritten():
    with pytest.raises(FileExistsError):
        build()

