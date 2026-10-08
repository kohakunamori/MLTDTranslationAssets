"""Guard the single proven frozen QA previous-line bilingual contamination."""
from __future__ import annotations

import copy
import json

import pytest

from scripts.build_event_unit_previous_line_bleed_draft import (
    AUDIT_SHA, DEST, QA_AUDIT, REPAIR, TARGET_SHA, WORKLIST,
    WORKLIST_SHA, build, candidate_rows, detect_previous_line_bleed,
)
from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id


@pytest.fixture(scope="module")
def inputs():
    return (
        json.loads(QA_AUDIT.read_text(encoding="utf8")),
        read_jsonl(WORKLIST),
    )


def test_exact_one_prior_dialogue_contamination_across_13177_sources(inputs):
    audit, review = inputs
    assert sha_file(QA_AUDIT) == AUDIT_SHA
    assert sha_file(WORKLIST) == WORKLIST_SHA
    found = detect_previous_line_bleed(audit)
    assert len(found) == 1
    assert found[0]["source_sha256"] == TARGET_SHA
    assert found[0]["preceding_source_sha256"] == (
        "17adf00f5e0460840343b104d4309da1b6cdc08911b2eac3ed895738101b2ce4"
    )
    assert "今天的投稿，大家都有看吗" in (
        found[0]["preceding_translation_unreviewed"]
    )
    result = candidate_rows(audit, review)
    assert len(result) == 1
    r = result[0]
    assert r["source_sha256"] == source_id(r["source"]) == TARGET_SHA
    assert r["translation_draft"] == REPAIR
    assert r["machine_candidate_unreviewed"].startswith(
        r["preceding_translation_unreviewed"] + "\n\n"
    )
    assert r["preceding_translation_unreviewed"] not in REPAIR
    assert r["character_rendering_is_authoritative"] is False
    assert r["provisional_character_rendering"] == "奈露"
    assert r["draft_qa_verdict"] == "PASS"
    assert r["draft_qa_issues"] == []
    assert r["independent_review_complete"] is False
    assert r["semantic_accuracy_verified"] is False
    assert r["safe_to_mount_as_final_overlay"] is False


def test_isolated_draft_manifest_bytes_and_release_flags():
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    rows = read_jsonl(DEST / m["draft_file"])
    assert len(rows) == 1
    assert m["version_identity"]["version_key"] == (
        "jp-client-9.0.200-assets-1077100"
    )
    assert m["draft_sha256"] == sha_file(DEST / m["draft_file"])
    assert m["frozen_13177_source_qa_audit_sha256"] == AUDIT_SHA
    assert m["v2_unified_review_worklist_sha256"] == WORKLIST_SHA
    assert m["previous_line_contamination_detected"] == 1
    assert m["unreviewed_drafts_total_with_v2"] == 91
    assert m["qa_review_without_targeted_draft_after_this_draft"] == 817
    assert m["draft_deterministic_qa"] == {"PASS": 1}
    for k in (
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "name_provenance_official",
        "producer_translations_modified", "prior_QA_bundle_stages_modified",
        "original_official_assets_modified", "nas_modified",
    ):
        assert m[k] is False


@pytest.mark.parametrize("kind", [
    "drop_source", "duplicate_source", "source_sha_forged",
    "copy_disappeared", "source_translation_changed",
    "prior_translation_changed", "source_context_changed",
    "review_approved", "name_provenance_changed",
])
def test_false_duplicate_or_stale_provenance_fails_closed(inputs, kind):
    audit, review = copy.deepcopy(inputs)
    item = next(x for x in audit if x["source_sha256"] == TARGET_SHA)
    row = next(x for x in review if x["source_sha256"] == TARGET_SHA)
    if kind == "drop_source":
        audit.remove(item)
    elif kind == "duplicate_source":
        audit.append(copy.deepcopy(item))
    elif kind == "source_sha_forged":
        item["source_sha256"] = "0" * 64
    elif kind == "copy_disappeared":
        item["translation"] = REPAIR
        row["machine_candidate_unreviewed"] = REPAIR
    elif kind == "source_translation_changed":
        item["translation"] += "额外内容"
    elif kind == "prior_translation_changed":
        prior = next(x for x in audit if x["source_sha256"].startswith(
            "17adf00f5e046084"
        ))
        prior["translation"] = "另一条台词"
    elif kind == "source_context_changed":
        item["examples"][0]["previous"] = "前文被覆盖"
    elif kind == "review_approved":
        row["independent_review_complete"] = True
    elif kind == "name_provenance_changed":
        name = next(x for x in audit if x["source_sha256"].startswith(
            "850f7fc82725"
        ))
        name["translation"] = "不一样的翻译"
    with pytest.raises((ValueError, KeyError)):
        candidate_rows(audit, review)


def test_immutable_bleed_repair_draft_refuses_overwrite():
    with pytest.raises(FileExistsError):
        build()

