"""12 source-bound followup drafts preserve QA review gates and text provenance."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_followup_12_drafts import (
    DEST, DRAFTS, QUALITY, GLOSSARY, REVIEW_FILE, REVIEW_MANIFEST,
    build, candidate_rows, sha_file,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id


@pytest.fixture(scope="module")
def sources():
    return read_jsonl(REVIEW_FILE)


def test_real_twelve_drafts_are_frozen_unreviewed_and_QA_honest(sources):
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    proposals = read_jsonl(DEST / m["draft_file"])
    before = {x["source_sha256"]: x for x in sources}
    assert m["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert m["unified_review_manifest_sha256"] == sha_file(REVIEW_MANIFEST)
    assert m["unified_review_worklist_sha256"] == sha_file(REVIEW_FILE)
    assert m["quality_rules_sha256"] == sha_file(QUALITY)
    assert m["glossary_sha256"] == sha_file(GLOSSARY)
    assert sha_file(DEST / m["draft_file"]) == m["draft_sha256"]
    assert len(proposals) == m["draft_unique"] == 12
    assert m["draft_qa_verdicts"] == {"PASS": 11, "REVIEW": 1}
    assert m["known_rule_false_positive_not_autocleared"] == 1
    assert m["source_still_missing_targeted_draft_after_this_batch"] == 818
    for flag in [
        "independent_review_complete", "semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay", "producer_translations_modified",
        "previous_852_QA_stage_modified", "official_assets_modified",
        "nas_modified",
    ]:
        assert m[flag] is False
    assert len({x["source_sha256"] for x in proposals}) == 12
    for row in proposals:
        sid = row["source_sha256"]
        prior = before[sid]
        assert prior["review_bucket"] == "qa_review_without_draft"
        assert row["source"] == prior["source"]
        assert row["examples"] == prior["examples"]
        assert row["occurrences"] == prior["occurrences"] == 1
        assert row["machine_candidate_unreviewed"] == (
            prior["machine_candidate_unreviewed"]
        )
        assert row["translation_draft"] != row["machine_candidate_unreviewed"]
        assert sid == source_id(row["source"])
        assert row["status"] == "agent_draft_unreviewed"
        assert row["review_status"] == "pending"
        assert row["release_gate"] == "needs_independent_review"
        assert row["independent_review_complete"] is False
        assert row["semantic_accuracy_verified"] is False
        assert row["safe_to_mount_as_final_overlay"] is False
        assert row["original_machine_QA_verdict"] == "REVIEW"
        check = evaluate_row(
            {"source_sha256": sid, "source": row["source"]},
            {"source_sha256": sid, "source": row["source"],
             "translation": row["translation_draft"],
             "status": "agent_draft_unreviewed"},
            load_glossary(None),
        )
        assert check["qa_verdict"] == row["draft_qa_verdict"]
        assert check["issues"] == row["draft_qa_issues"]
    negative = next(x for x in proposals if x["source_sha256"].startswith("362ac5de7af20d"))
    assert "坏事" in negative["translation_draft"]
    assert negative["draft_qa_verdict"] == "REVIEW"
    assert [x["code"] for x in negative["draft_qa_issues"]] == [
        "bad_meaning_semantics_missing"
    ]


@pytest.mark.parametrize("kind", [
    "lost_worklist_row", "duplicate_worklist_row", "changed_original_source",
    "changed_old_machine_translation", "new_candidate_has_bad_numeric",
    "new_candidate_reused_original", "forged_release_status",
    "forged_review_completion", "changed_QA_issues", "wrong_mapping_qa",
    "duplicate_manual_source",
])
def test_followup_12_draft_generation_rejects_stale_inputs(sources, kind):
    rows = copy.deepcopy(sources)
    mapping = copy.deepcopy(DRAFTS)
    prefix = next(iter(mapping))
    sid = next(x["source_sha256"] for x in rows
               if x["source_sha256"].startswith(prefix))
    original = next(x for x in rows if x["source_sha256"] == sid)
    if kind == "lost_worklist_row":
        rows.pop()
    elif kind == "duplicate_worklist_row":
        rows.append(rows[0])
    elif kind == "changed_original_source":
        original["source"] += "違う日本語"
    elif kind == "changed_old_machine_translation":
        original["machine_candidate_unreviewed"] += "伪造数据"
    elif kind == "new_candidate_has_bad_numeric":
        machine_sha, _, reason = mapping[prefix]
        mapping[prefix] = (machine_sha, "4 5 6 数字错了", reason)
    elif kind == "new_candidate_reused_original":
        machine_sha, _, reason = mapping[prefix]
        mapping[prefix] = (
            machine_sha, original["machine_candidate_unreviewed"], reason
        )
    elif kind == "forged_release_status":
        original["release_gate"] = "accepted"
    elif kind == "forged_review_completion":
        original["independent_review_complete"] = True
    elif kind == "changed_QA_issues":
        original["current_machine_qa_issues"] = []
    elif kind == "wrong_mapping_qa":
        machine_sha, _, reason = mapping[prefix]
        mapping[prefix] = (machine_sha, "仍然混入日文です", reason)
    elif kind == "duplicate_manual_source":
        mapping["362ac5de7af20"] = mapping[prefix]
    with pytest.raises((ValueError, KeyError)):
        candidate_rows(rows, mapping)


def test_cannot_overwrite_frozen_followup_12_drafts():
    with pytest.raises(FileExistsError):
        build()

