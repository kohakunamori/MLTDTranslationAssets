"""Freeze 21 contextual Event-unit repair DRAFTS and their QA-only safety gates."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_semantic_repair_drafts import (
    DEST, GLOSSARY, GROUPS, INDEX, INPUT, INPUT_MANIFEST, QUALITY, SNAPSHOT,
    build, candidate_rows, sha_file,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id


@pytest.fixture(scope="module")
def original_rows():
    return read_jsonl(INPUT)


def test_real_21_contextual_drafts_are_exact_sha_bound_and_review_only():
    manifest = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    rows = read_jsonl(DEST / manifest["draft_file"])
    before = {r["source_sha256"]: r for r in read_jsonl(INPUT)}
    assert manifest["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert manifest["source_review_manifest_sha256"] == sha_file(INPUT_MANIFEST)
    assert manifest["source_review_queue_sha256"] == sha_file(INPUT)
    assert manifest["quality_rules_sha256"] == sha_file(QUALITY)
    assert manifest["glossary_sha256"] == sha_file(GLOSSARY)
    assert sha_file(DEST / manifest["draft_file"]) == manifest["draft_sha256"]
    assert manifest["draft_unique"] == len(rows) == 21
    assert manifest["draft_occurrences"] == 21
    assert manifest["draft_deterministic_qa_verdicts"] == {"PASS": 21}
    assert manifest["repair_reasons"] == {
        "group_cardinality_and_group_pronouns": 5,
        "unwarranted_or_awkward_explicit_subject": 4,
        "sentence_final_adversative_and_name": 3,
        "indefinite_semantic_repair": 9,
    }
    assert manifest["independent_review_complete"] is False
    assert manifest["semantic_accuracy_verified"] is False
    assert manifest["safe_to_mount_as_final_overlay"] is False
    assert manifest["production_translations_modified"] is False
    assert manifest["existing_852_bundle_QA_stage_modified"] is False
    assert manifest["nas_modified"] is False
    ids = set()
    for row in rows:
        sid = row["source_sha256"]
        assert sid not in ids
        ids.add(sid)
        assert sid == source_id(row["source"])
        prev = before[sid]
        assert row["source"] == prev["source"]
        assert row["machine_candidate_unreviewed"] == prev["machine_candidate_unreviewed"]
        assert row["examples"] == prev["examples"]
        assert row["occurrences"] == prev["occurrences"]
        assert row["prior_qa_verdict"] == "REVIEW"
        assert row["prior_qa_issues"] == prev["issues"]
        assert row["translation_draft"] != row["machine_candidate_unreviewed"]
        assert row["status"] == "agent_draft_unreviewed"
        assert row["review_status"] == "pending"
        assert row["independent_review_complete"] is False
        assert row["semantic_accuracy_verified"] is False
        assert row["release_gate"] == "needs_independent_review"
        assert row["safe_to_mount_as_final_overlay"] is False
        check = evaluate_row(
            {"source_sha256": sid, "source": row["source"],
             "examples": row["examples"], "occurrences": row["occurrences"]},
            {"source_sha256": sid, "source": row["source"],
             "translation": row["translation_draft"],
             "status": "agent_draft_unreviewed"},
            load_glossary(None),
        )
        assert check["qa_verdict"] == row["draft_qa_verdict"] == "PASS"
        assert check["issues"] == row["draft_qa_issues"] == []


@pytest.mark.parametrize("kind", [
    "lost_review_row", "duplicate_review_row", "source_changed",
    "old_candidate_changed", "release_forged", "review_forged",
    "extra_unresolved_issue",
])
def test_source_bound_repair_universe_fails_closed(original_rows, kind):
    rows = copy.deepcopy(original_rows)
    sid_prefix = next(iter(next(iter(GROUPS.values()))))
    target = next(i for i, r in enumerate(rows)
                  if r["source_sha256"].startswith(sid_prefix))
    if kind == "lost_review_row":
        rows.pop(target)
    elif kind == "duplicate_review_row":
        rows.append(rows[target])
    elif kind == "source_changed":
        rows[target]["source"] = "これは間違ったソースだ"
    elif kind == "old_candidate_changed":
        rows[target]["machine_candidate_unreviewed"] = (
            GROUPS["group_cardinality_and_group_pronouns"][sid_prefix]
        )
    elif kind == "release_forged":
        rows[target]["release_gate"] = "accepted"
    elif kind == "review_forged":
        rows[target]["independent_review_complete"] = True
    elif kind == "extra_unresolved_issue":
        rows[target]["issues"].append({
            "code": "forbidden_term_present", "severity": "review",
        })
    with pytest.raises(ValueError):
        candidate_rows(rows)


@pytest.mark.parametrize("kind", [
    "lost_manual_translation", "duplicate_manual_sid",
    "broken_chinese_numeric", "fake_group_reason", "cross_group_collision",
])
def test_manual_authoring_mapping_fails_closed(original_rows, kind):
    groups = copy.deepcopy(GROUPS)
    group = "group_cardinality_and_group_pronouns"
    sid = next(iter(groups[group]))
    if kind == "lost_manual_translation":
        groups[group].pop(sid)
    elif kind == "duplicate_manual_sid":
        groups["indefinite_semantic_repair"][sid] = groups[group][sid]
    elif kind == "broken_chinese_numeric":
        groups[group][sid] = "四个人一起跳舞"
    elif kind == "fake_group_reason":
        groups["not-a-real-issue"] = groups.pop(group)
    elif kind == "cross_group_collision":
        groups[group][sid] = "就这样吧"
    with pytest.raises(ValueError):
        candidate_rows(original_rows, groups)


def test_existing_semantic_draft_audit_cannot_be_overwritten():
    with pytest.raises(FileExistsError):
        build()



def test_character_short_name_semantic_fix_uses_frozen_official_name_evidence():
    terms = json.loads(
        (INPUT.parents[4] / "localization/quality/authoritative-terms.json")
        .read_text(encoding="utf8")
    )
    evidence = terms["entries"]["馬場このみ"]
    assert evidence["target"] == "马场木实"
    assert evidence["evidence"] == "official_legacy_zhcn_exact_source"
    row = next(x for x in read_jsonl(DEST / "21-source-bound-semantic-correction-drafts.jsonl")
               if x["source_sha256"].startswith("0f47c1f46f1a73"))
    assert "このみ" in row["source"]
    assert "这个" in row["machine_candidate_unreviewed"]
    assert "木实" in row["translation_draft"]
    assert "这个" not in row["translation_draft"]
