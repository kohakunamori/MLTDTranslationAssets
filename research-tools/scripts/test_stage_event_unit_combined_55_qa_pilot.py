"""QA-PASS Tamaki false-negative audit + 55-candidate/51-bundle safe pilot."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_tamaki_qa_pass_repairs import (
    AUDIT, REVIEW, DEST as NAME_AUDIT,
    PREVIOUSLY_DRAFTED, candidate_rows, build as build_name_audit,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_combined_55_qa_pilot import (
    BASE, BASE_MANIFEST, PRIOR, PRIOR_MANIFEST,
    DRAFT, DEST, build as build_stage, validate_patch, verify_rebuilt_bundle,
)


@pytest.fixture(scope="module")
def inputs():
    return (
        json.loads(AUDIT.read_text(encoding="utf8")),
        read_jsonl(REVIEW),
        json.loads(BASE_MANIFEST.read_text(encoding="utf8")),
        json.loads(PRIOR_MANIFEST.read_text(encoding="utf8")),
        read_jsonl(DRAFT),
    )


def test_originally_passed_machine_name_errors_isolated_and_source_bound(inputs):
    all_rows, reviewer, base, earlier, drafted = inputs
    m = json.loads((NAME_AUDIT / "manifest.json").read_text(encoding="utf8"))
    assert sha_file(NAME_AUDIT / m["draft_file"]) == m["draft_sha256"]
    assert m["source_13177_audit_sha256"] == sha_file(AUDIT)
    assert m["source_v3_1181_worklist_sha256"] == sha_file(REVIEW)
    assert m["source_852_baseline_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert m["audit_jp_tamaki_sources"] == 105
    assert m["prior_correct_name_examples"] == 95
    assert m["prior_wrong_yuhuan_examples"] == 6
    assert m["already_in_v3_draft"] == 1
    assert m["previously_outside_v3_review"] == 5
    assert m["draft_qa_verdicts"] == {"PASS": 5}
    assert m["five_new_sources_need_independent_review"] is True
    assert m["independent_review_complete"] is False
    assert m["semantic_accuracy_verified"] is False
    assert m["safe_to_mount_as_final_overlay"] is False
    assert drafted == candidate_rows(all_rows, reviewer)
    assert len(drafted) == 5
    existing = {r["source_sha256"] for r in reviewer}
    assert PREVIOUSLY_DRAFTED in existing
    originals = {r["source_sha256"]: r for r in all_rows}
    remotes = set()
    for r in drafted:
        sid = r["source_sha256"]
        assert sid not in existing
        assert sid == source_id(r["source"])
        assert "たまき" in r["source"]
        assert r["machine_candidate_unreviewed"] == originals[sid]["translation"]
        assert r["translation_draft"] == r["machine_candidate_unreviewed"].replace("玉环", "环")
        assert "玉环" not in r["translation_draft"]
        assert r["prior_machine_qa_verdict"] == r["draft_qa_verdict"] == "PASS"
        assert r["draft_qa_issues"] == []
        assert r["status"] == "agent_draft_unreviewed"
        assert r["review_status"] == "pending"
        assert r["release_gate"] == "needs_independent_review"
        assert r["independent_review_complete"] is False
        assert r["semantic_accuracy_verified"] is False
        assert r["safe_to_mount_as_final_overlay"] is False
        remote = r["examples"][0]["remote"]
        assert remote not in remotes
        remotes.add(remote)
    assert len(remotes) == 5


@pytest.mark.parametrize("kind", [
    "source_drift", "translation_drift", "source_sha_drift",
    "double_row", "reviewer_false_accept", "reviewer_source_missing",
    "new_QA_status_forged", "new_QA_issues_forged",
])
def test_five_name_audit_fails_closed(inputs, kind):
    all_rows, review = copy.deepcopy(inputs[:2])
    first = next(x for x in all_rows if x["source_sha256"].startswith("69b28b87477842"))
    if kind == "source_drift":
        first["source"] = "別の日本語"
    elif kind == "translation_drift":
        first["translation"] = "玉环不同"
    elif kind == "source_sha_drift":
        first["source_sha256"] = "f" * 64
    elif kind == "double_row":
        all_rows.append(all_rows[0])
    elif kind == "reviewer_false_accept":
        row = next(x for x in review if x["source_sha256"] == PREVIOUSLY_DRAFTED)
        row["independent_review_complete"] = True
    elif kind == "reviewer_source_missing":
        review.pop()
    elif kind == "new_QA_status_forged":
        first["qa_verdict"] = "REVIEW"
    elif kind == "new_QA_issues_forged":
        first["issues"].append({"code": "forged"})
    with pytest.raises((ValueError, KeyError)):
        candidate_rows(all_rows, review)


def test_55_candidate_manifest_bytes_and_old_trial_preservation(inputs):
    all_rows, reviewer, original, prior, drafted = inputs
    manifest = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    assert manifest["source_852_QA_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert manifest["source_50_pilot_manifest_sha256"] == sha_file(PRIOR_MANIFEST)
    assert manifest["source_five_name_drafts_sha256"] == sha_file(DRAFT)
    assert manifest["unreviewed_candidate_source_unique"] == 55
    assert manifest["bundle_count"] == len(manifest["bundles"]) == 51
    assert manifest["previous_base_QA_fields_not_changed"] == 12199
    assert manifest["previous_base_QA_fields_corrected_unreviewed"] == 5
    assert manifest["previous_50_unreviewed_fields_preserved"] == 50
    assert manifest["unreviewed_draft_QA_verdicts"] == {"PASS": 53, "REVIEW": 2}
    assert manifest["copy_unchanged_prior_50_bundle_count"] == 46
    assert manifest["new_original_rebuilt_bundle_count"] == 5
    assert manifest["shared_prior_50_rebuilt_bundle_count"] == 1
    for key in [
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
        "production_translations_modified",
        "prior_852_or_50_QA_stages_modified",
        "official_original_assets_modified", "nas_modified",
    ]:
        assert manifest[key] is False
    before = {b["remote"]: b for b in original["bundles"]}
    before_50 = {b["remote"]: b for b in prior["bundles"]}
    names = {r["source_sha256"]: r for r in drafted}
    seen_corrected, seen_previous = set(), set()
    for b in manifest["bundles"]:
        remote = b["remote"]
        assert b["roundtrip_verified"] and b["non_text_objects_byte_identical"]
        assert b["release_gate"] == "NOT_EVALUATED"
        assert b["baseline_852_bundle_sha256"] == before[remote]["localized_sha256"]
        assert sha_file(BASE / "jp-android" / remote) == before[remote]["localized_sha256"]
        assert sha_file(DEST / "jp-android" / remote) == b["localized_sha256"]
        assert (DEST / "jp-android" / remote).stat().st_size == b["output_bytes"]
        if b["source_mode"] == "sha_exact_copy_of_unchanged_prior_50":
            assert b["localized_sha256"] == before_50[remote]["localized_sha256"]
            assert sha_file(PRIOR / "jp-android" / remote) == b["localized_sha256"]
        for r in b["corrected_preexisting_baseline_QA_fields"]:
            sid = r["path_source_sha256"]
            assert sid not in seen_corrected
            seen_corrected.add(sid)
            assert r["prior_baseline_QA_translation"] == names[sid]["machine_candidate_unreviewed"]
            assert r["localized"] == names[sid]["translation_draft"]
        for r in b["preserved_previous_50_unreviewed_fields"]:
            sid = r["path_source_sha256"]
            assert sid not in seen_previous
            seen_previous.add(sid)
            assert sid == source_id(r["original"])
    assert seen_corrected == set(names)
    assert len(seen_previous) == 50
    assert len(list((DEST / "jp-android").glob("*.unity3d"))) == 51


@pytest.mark.parametrize("kind", [
    "missing_draft", "duplicate_draft", "source_drift",
    "approved_draft", "unknown_earlier_source", "prior_50_false_approval",
    "changed_baseline_translation", "forged_machine_original",
])
def test_55_source_patch_rejects_stale_and_forged_evidence(inputs, kind):
    _, _, baseline, earlier, original_drafts = inputs
    base, prior, drafts = (
        copy.deepcopy(baseline), copy.deepcopy(earlier),
        copy.deepcopy(original_drafts),
    )
    if kind == "missing_draft":
        drafts.pop()
    elif kind == "duplicate_draft":
        drafts.append(drafts[0])
    elif kind == "source_drift":
        drafts[0]["source"] = "べつの文章"
    elif kind == "approved_draft":
        drafts[0]["independent_review_complete"] = True
    elif kind == "unknown_earlier_source":
        prior["bundles"][0]["additional_unreviewed_fields"][0]["path_source_sha256"] = "f" * 64
    elif kind == "prior_50_false_approval":
        prior["safe_to_mount_as_final_overlay"] = True
    elif kind == "changed_baseline_translation":
        sid = drafts[0]["source_sha256"]
        match = next(c for b in base["bundles"] for c in b["changes"]
                     if c["path_source_sha256"] == sid)
        match["localized"] += "伪造"
    elif kind == "forged_machine_original":
        drafts[0]["machine_candidate_unreviewed"] += "假词"
    with pytest.raises((ValueError, KeyError)):
        validate_patch(base, prior, drafts)


def test_rebuilt_bundle_verifier_detects_missing_old_change_and_extra_field(inputs):
    _, _, baseline, earlier, proposed = inputs
    old = next(
        b for b in baseline["bundles"]
        if any(c["path_source_sha256"] == proposed[0]["source_sha256"]
               for c in b["changes"])
    )
    sid = proposed[0]["source_sha256"]
    built = copy.deepcopy(old)
    changed = next(c for c in built["changes"] if c["path_source_sha256"] == sid)
    changed["localized"] = proposed[0]["translation_draft"]
    corrections, extras = verify_rebuilt_bundle(old, built, {sid: proposed[0]}, {})
    assert len(corrections) == 1 and extras == []
    built["changes"].append({
        "command_index":999999, "path_source_sha256":"f"*64,
        "original":"捏造", "localized":"伪造",
    })
    with pytest.raises(ValueError):
        verify_rebuilt_bundle(old, built, {sid: proposed[0]}, {})
    built["changes"].pop()
    built["changes"][0]["localized"] += "擅自更改"
    with pytest.raises(ValueError):
        verify_rebuilt_bundle(old, built, {sid: proposed[0]}, {})


def test_never_overwrite_immutable_audits_or_pilot():
    for f in [build_name_audit, build_stage]:
        with pytest.raises(FileExistsError):
            f()

