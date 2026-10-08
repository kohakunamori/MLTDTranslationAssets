"""One proven previous-line duplication is removed in QA-only UnityFS trial."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_previous_line_bleed_draft import (
    DEST as DRAFT_ROOT, QA_AUDIT, REPAIR, TARGET_SHA,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.stage_event_unit_previous_line_bleed_qa_pilot import (
    BASE, BASE_MANIFEST, DEST, DRAFT_FILE, DRAFT_MANIFEST,
    PRIOR, WORKLIST, stage,
)


def test_single_unreviewed_draft_materialized_while_old_QA_preserved():
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    b = m["bundle"]
    assert m["version_identity"]["version_key"] == (
        "jp-client-9.0.200-assets-1077100"
    )
    assert m["source_852_manifest_sha256"] == sha_file(BASE_MANIFEST)
    assert m["source_one_draft_manifest_sha256"] == sha_file(DRAFT_MANIFEST)
    assert m["source_one_draft_sha256"] == sha_file(DRAFT_FILE)
    assert m["source_v2_review_sha256"] == sha_file(WORKLIST)
    assert m["prior_43_manifest_sha256"] == sha_file(PRIOR / "manifest.json")
    assert m["baseline_12204_text_fields_preserved"] == 12204
    assert m["additional_unreviewed_source_unique"] == 1
    assert m["additional_text_fields"] == 1
    assert m["bundle_count"] == 1
    assert m["technical_qa_verdicts"] == {"PASS": 1}
    assert b["roundtrip_verified"] is True
    assert b["non_text_objects_byte_identical"] is True
    assert b["logical"] == "event_unit_talk_1474.unity3d"
    out = DEST / "jp-android" / b["remote"]
    assert out.is_file()
    assert out.stat().st_size == b["output_bytes"]
    assert sha_file(out) == b["localized_sha256"]
    assert sha_file(BASE / "jp-android" / b["remote"]) == (
        b["baseline_852_qa_sha256"]
    )
    previous = json.loads((PRIOR / "manifest.json").read_text(encoding="utf8"))
    assert b["remote"] not in {x["remote"] for x in previous["bundles"]}
    assert len(b["additional_unreviewed_fields"]) == 1
    delta = b["additional_unreviewed_fields"][0]
    assert delta["path_source_sha256"] == TARGET_SHA
    assert source_id(delta["original"]) == TARGET_SHA
    assert delta["command_index"] == 9
    assert delta["localized"] == REPAIR
    draft = read_jsonl(DRAFT_FILE)
    assert len(draft) == 1
    assert delta["original"] == draft[0]["source"]
    assert draft[0]["preceding_translation_unreviewed"] not in (
        delta["localized"]
    )
    assert draft[0]["independent_review_complete"] is False
    for k in (
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "overlay_merge_authorized",
        "original_official_assets_modified",
        "production_translations_modified",
        "prior_852_bundle_stage_modified",
        "prior_43_bundle_stage_modified", "nas_modified",
    ):
        assert m[k] is False
    assert m["release_gate"] == "not_evaluated"


def test_no_semantic_autoreview_or_provisional_name_claim():
    draft = read_jsonl(DRAFT_FILE)[0]
    qa = evaluate_row(
        {"source": draft["source"], "source_sha256": TARGET_SHA},
        {"source": draft["source"], "source_sha256": TARGET_SHA,
         "translation": draft["translation_draft"],
         "status": "agent_draft_unreviewed"},
        load_glossary(None),
    )
    assert qa["qa_verdict"] == "PASS" and not qa["issues"]
    assert draft["character_rendering_is_authoritative"] is False
    assert draft["release_gate"] == "needs_independent_review"
    assert draft["review_status"] == "pending"
    assert draft["semantic_accuracy_verified"] is False


def test_previous_line_one_bundle_immutable():
    with pytest.raises(FileExistsError):
        stage()

