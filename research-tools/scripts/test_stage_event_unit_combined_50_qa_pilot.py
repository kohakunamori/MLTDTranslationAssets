"""6 source-bound corrections and 50-source QA-only composite invariants."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_six_contextual_drafts import (
    DEST as SIX_DRAFTS, SOURCE as V2, build as build_drafts, proposals,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.stage_event_unit_10_targeted_review_drafts import BASE, BASE_MANIFEST, verify_trial_delta
from scripts.stage_event_unit_six_contextual_qa_pilot import (
    DEST as SIX_PILOT, DRAFT as SIX_FILE,
    build as build_six, validate_sources,
)
from scripts.stage_event_unit_combined_50_qa_pilot import (
    DEST as FIFTY, FORTY_THREE, BLEED, SIX,
    DRAFT_ONE, DRAFT_SIX, build as build_fifty, normalize_stages,
)


@pytest.fixture(scope="module")
def six_inputs():
    return read_jsonl(V2), read_jsonl(SIX_FILE)


@pytest.fixture(scope="module")
def fifty_inputs():
    return (
        json.loads(BASE_MANIFEST.read_text(encoding="utf8")),
        json.loads((FORTY_THREE / "manifest.json").read_text(encoding="utf8")),
        json.loads((BLEED / "manifest.json").read_text(encoding="utf8")),
        json.loads((SIX / "manifest.json").read_text(encoding="utf8")),
        read_jsonl(V2),
        read_jsonl(DRAFT_ONE),
        read_jsonl(DRAFT_SIX),
    )


def test_six_drafts_sha_bound_to_original_machine_and_context(six_inputs):
    reviewer, _ = six_inputs
    m = json.loads((SIX_DRAFTS / "manifest.json").read_text(encoding="utf8"))
    rows = read_jsonl(SIX_DRAFTS / m["draft_file"])
    before = {r["source_sha256"]: r for r in reviewer}
    assert sha_file(SIX_DRAFTS / m["draft_file"]) == m["draft_sha256"]
    assert m["draft_unique"] == len(rows) == 6
    assert m["draft_bundle_count"] == 6
    assert m["draft_deterministic_QA_verdicts"] == {"PASS": 6}
    assert m["total_known_drafts_including_previous_line_bleed"] == 97
    assert m["qa_review_without_draft_after_six_and_previous_line"] == 811
    assert rows == proposals(reviewer)
    assert len({r["examples"][0]["remote"] for r in rows}) == 6
    for item in rows:
        prior = before[item["source_sha256"]]
        assert source_id(item["source"]) == item["source_sha256"]
        assert prior["review_bucket"] == "qa_review_without_draft"
        assert item["machine_candidate_unreviewed"] == prior["machine_candidate_unreviewed"]
        assert item["examples"] == prior["examples"]
        assert item["status"] == "agent_draft_unreviewed"
        assert item["review_status"] == "pending"
        assert item["release_gate"] == "needs_independent_review"
        assert item["draft_qa_verdict"] == "PASS"
        assert item["draft_qa_issues"] == []
        assert item["independent_review_complete"] is False
        assert item["semantic_accuracy_verified"] is False
        assert item["safe_to_mount_as_final_overlay"] is False
        qa = evaluate_row(
            {"source_sha256": item["source_sha256"], "source": item["source"]},
            {"source_sha256": item["source_sha256"], "source": item["source"],
             "translation": item["translation_draft"],
             "status": "agent_draft_unreviewed"}, load_glossary(None),
        )
        assert qa["qa_verdict"] == "PASS" and qa["issues"] == []
    name = next(r for r in rows if r["source_sha256"].startswith("40996b95780b24"))
    assert name["translation_draft"].startswith("环也")
    game = next(r for r in rows if r["source_sha256"].startswith("5747a643729eb7"))
    assert "巨型福笑拼脸游戏" in game["translation_draft"]


@pytest.mark.parametrize("kind", [
    "wrong_source", "wrong_machine_sha", "release_forged",
    "drafted_already", "duplicate_prefix", "reject_numeric", "missing_row",
])
def test_six_draft_authoring_fails_closed(six_inputs, kind):
    reviewer = copy.deepcopy(six_inputs[0])
    from scripts.build_event_unit_six_contextual_drafts import DRAFTS
    drafts = copy.deepcopy(DRAFTS)
    sid = next(r["source_sha256"] for r in reviewer
               if r["source_sha256"].startswith("40996b95780b24"))
    row = next(r for r in reviewer if r["source_sha256"] == sid)
    if kind == "wrong_source":
        row["source"] = "全く別の日本語"
    elif kind == "wrong_machine_sha":
        row["machine_candidate_unreviewed"] += "擅改"
    elif kind == "release_forged":
        row["release_gate"] = "accepted"
    elif kind == "drafted_already":
        row["agent_correction_draft_unreviewed"] = "已另有修订"
    elif kind == "duplicate_prefix":
        drafts["40996b95780b2"] = drafts["40996b95780b24"]
    elif kind == "reject_numeric":
        drafts["40996b95780b24"]["translation"] = "台词 9 的内容"
    elif kind == "missing_row":
        reviewer = [r for r in reviewer if r["source_sha256"] != sid]
    with pytest.raises((ValueError, KeyError)):
        proposals(reviewer, drafts)


def test_six_qa_pilot_preserves_old_qa_commands_and_original_sources(six_inputs):
    m = json.loads((SIX_PILOT / "manifest.json").read_text(encoding="utf8"))
    old = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    old_by_remote = {r["remote"]: r for r in old["bundles"]}
    review = {r["source_sha256"]: r for r in six_inputs[1]}
    assert m["bundle_count"] == len(m["bundles"]) == 6
    assert m["additional_unreviewed_source_unique"] == 6
    assert m["additional_unreviewed_text_fields"] == 6
    assert m["baseline_12204_text_fields_preserved"] == 12204
    assert m["draft_deterministic_qa"] == {"PASS": 6}
    assert sha_file(BASE_MANIFEST) == m["source_852_baseline_manifest_sha256"]
    assert sha_file(SIX_FILE) == m["six_source_drafts_sha256"]
    assert sha_file(V2) == m["source_v2_review_sha256"]
    for key in ("independent_review_complete", "semantic_accuracy_verified",
                "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
                "official_original_assets_modified", "production_translations_modified",
                "prior_852_43_bleed_QA_stages_modified", "nas_modified"):
        assert m[key] is False
    for item in m["bundles"]:
        remote = item["remote"]
        old_bundle = old_by_remote[remote]
        assert sha_file(BASE / "jp-android" / remote) == old_bundle["localized_sha256"]
        assert sha_file(SIX_PILOT / "jp-android" / remote) == item["localized_sha256"]
        assert item["output_bytes"] == (SIX_PILOT / "jp-android" / remote).stat().st_size
        assert item["roundtrip_verified"] is True
        assert item["non_text_objects_byte_identical"] is True
        expected = {
            sid: {
                "source": item["original"],
                "translation_draft": item["localized"],
            } for sid, item in (
                (x["path_source_sha256"], x)
                for x in item["additional_unreviewed_fields"]
            )
        }
        delta = verify_trial_delta(old_bundle, item, expected)
        assert len(delta) == 1
        assert delta[0]["localized"] == review[delta[0]["path_source_sha256"]]["translation_draft"]


def test_six_trial_rejects_missing_extra_or_accepted_source(six_inputs):
    base = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    for kind in ("missing", "duplicate", "accepted", "machine_drift"):
        rows, proposals_ = copy.deepcopy(six_inputs)
        if kind == "missing":
            proposals_.pop()
        elif kind == "duplicate":
            proposals_.append(proposals_[0])
        elif kind == "accepted":
            proposals_[0]["independent_review_complete"] = True
        elif kind == "machine_drift":
            proposals_[0]["machine_candidate_unreviewed"] += "其他"
        with pytest.raises((ValueError, KeyError)):
            validate_sources(base, proposals_, rows)


def test_fifty_bundles_have_exact_bytes_and_source_partition(fifty_inputs):
    base, prior43, bleed, six, reviewer, one, six_draft = fifty_inputs
    m = json.loads((FIFTY / "manifest.json").read_text(encoding="utf8"))
    assert m["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert m["baseline_852_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert m["baseline_12204_QA_text_fields_unchanged"] == 12204
    assert m["additional_unreviewed_source_unique"] == 50
    assert m["additional_unreviewed_text_fields"] == 50
    assert m["bundle_count"] == len(m["bundles"]) == 47
    assert m["draft_deterministic_qa"] == {"PASS": 48, "REVIEW": 2}
    assert m["source_counts_by_trial"] == {
        "previous_43": 43, "previous_line_bleed": 1, "contextual_six": 6,
    }
    for k in ("independent_review_complete", "semantic_accuracy_verified",
              "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
              "production_translations_modified",
              "prior_852_and_other_QA_stages_modified",
              "official_original_assets_modified", "nas_modified"):
        assert m[k] is False
    expected, counts = normalize_stages(*fifty_inputs)
    assert counts["additional_unreviewed_source_unique"] == 50
    assert len(expected) == 47
    by_original = {r["remote"]: r for r in base["bundles"]}
    by_source = {r["source_sha256"]: r for r in reviewer}
    by_source.update({r["source_sha256"]: r for r in one + six_draft})
    seen = set()
    for entry in m["bundles"]:
        remote = entry["remote"]
        assert entry["roundtrip_verified_prior_trial"] is True
        assert entry["non_text_objects_byte_identical_prior_trial"] is True
        assert entry["release_gate"] == "NOT_EVALUATED"
        assert sha_file(FIFTY / "jp-android" / remote) == entry["localized_sha256"]
        assert (FIFTY / "jp-android" / remote).stat().st_size == entry["output_bytes"]
        assert sha_file(BASE / "jp-android" / remote) == by_original[remote]["localized_sha256"]
        root = {"previous_43": FORTY_THREE,
                "previous_line_bleed": BLEED,
                "contextual_six": SIX}[entry["source_trial"]]
        assert sha_file(root / "jp-android" / remote) == entry["localized_sha256"]
        for field in entry["additional_unreviewed_fields"]:
            sid = field["path_source_sha256"]
            assert sid not in seen
            seen.add(sid)
            assert sid == source_id(field["original"])
            row = by_source[sid]
            translated = row.get("translation_draft",
                                 row.get("agent_correction_draft_unreviewed"))
            assert translated == field["localized"]
    assert len(seen) == 50


@pytest.mark.parametrize("failure", (
    "drop_bundle", "dup_bundle", "drop_new_source",
    "dup_new_source", "approve_prior", "approve_six",
    "rename_source", "edit_new_draft", "edit_old_draft",
    "wrong_original_sha", "false_QA_PASS",
))
def test_fifty_rejects_overlaps_and_forged_release(fifty_inputs, failure):
    inputs = copy.deepcopy(fifty_inputs)
    base, prior, bleed, six, reviewer, one, six_draft = inputs
    if failure == "drop_bundle":
        six["bundles"].pop()
    elif failure == "dup_bundle":
        six["bundles"].append(six["bundles"][0])
    elif failure == "drop_new_source":
        six_draft.pop()
    elif failure == "dup_new_source":
        six_draft.append(six_draft[0])
    elif failure == "approve_prior":
        prior["independent_review_complete"] = True
    elif failure == "approve_six":
        six["overlay_merge_authorized"] = True
    elif failure == "rename_source":
        six_draft[0]["source"] = "違う台詞"
    elif failure == "edit_new_draft":
        six_draft[0]["translation_draft"] += "意外增加"
    elif failure == "edit_old_draft":
        sid = prior["bundles"][0]["additional_unreviewed_fields"][0]["path_source_sha256"]
        next(r for r in reviewer if r["source_sha256"] == sid)["agent_correction_draft_unreviewed"] += "意外增加"
    elif failure == "wrong_original_sha":
        six["bundles"][0]["original_bundle_sha256"] = "a" * 64
    elif failure == "false_QA_PASS":
        six["draft_deterministic_qa"] = {"PASS": 5, "REVIEW": 1}
    with pytest.raises((ValueError, KeyError)):
        normalize_stages(*inputs)


def test_cannot_overwrite_any_frozen_stage():
    for fn in (build_drafts, build_six, build_fifty):
        with pytest.raises(FileExistsError):
            fn()

