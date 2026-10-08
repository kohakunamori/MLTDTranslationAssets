"""Source-bound nine-name/title repair drafts remain unreviewed and fail-closed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_event_unit_name_title_repair_drafts import (
    AUTHORITIES, DEST, INPUT_ROOT, NAME_REPAIRS, TITLE_DRAFT, TITLE_SHA,
    build, draft_rows, sha_file,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id


@pytest.fixture(scope="module")
def material():
    return (
        read_jsonl(INPUT_ROOT / "still-review.jsonl"),
        json.loads(AUTHORITIES.read_text(encoding="utf8")),
    )


def test_actual_nine_drafts_frozen_and_never_autopromoted():
    m = json.loads((DEST / "manifest.json").read_text(encoding="utf8"))
    path = DEST / m["draft_file"]
    rows = read_jsonl(path)
    assert sha_file(path) == m["draft_file_sha256"]
    assert m["source_unique"] == len(rows) == 9
    assert m["name_draft_unique"] == 8
    assert m["contaminated_title_draft_unique"] == 1
    assert m["qa_verdicts_draft"] == {"PASS": 8, "REVIEW": 1}
    assert m["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert m["review_queue_sha256"] == sha_file(INPUT_ROOT / "still-review.jsonl")
    # The manifest hash is FROZEN provenance: it records which glossary the reviewed-
    # pending drafts were derived against.  The canonical glossary has since gained the
    # 26 image-surface idol names (owner ruling 2026-09-26), and re-deriving under the
    # wider glossary was measured to produce a byte-identical payload (see
    # work/agents/text-localization/image-terminology-unblock-20260926/HANDOFF.md §4),
    # so the frozen artifact stays valid.  Pin the frozen value explicitly: comparing it
    # to the CURRENT canonical file would silently stop checking once the glossary moves.
    assert m["authoritative_terms_sha256"] == (
        "3366ef6029c8795ddc02f2442f25901160a232ac740343d02d9ece6b2ca312a6")
    assert m["draft_file_sha256"] == sha_file(path)
    assert not m["safe_to_mount_as_final_overlay"]
    assert not m["independent_review_complete"]
    assert not m["production_translations_modified"]
    assert not m["existing_QA_stage_modified"]
    assert not m["nas_modified"]
    assert {r["source_sha256"] for r in rows} == set(NAME_REPAIRS) | {TITLE_SHA}
    for row in rows:
        assert row["source_sha256"] == source_id(row["source"])
        assert row["translation_draft"] != row["machine_candidate_unreviewed"]
        assert row["status"] == "agent_draft_unreviewed"
        assert row["review_status"] == "pending"
        assert row["independent_review_complete"] is False
        assert row["semantic_accuracy_verified"] is False
        assert row["safe_to_mount_as_final_overlay"] is False
        assert row["release_gate"] == "needs_independent_review"
        result = evaluate_row(
            {"source": row["source"], "source_sha256": row["source_sha256"]},
            {"source": row["source"], "source_sha256": row["source_sha256"],
             "translation": row["translation_draft"],
             "status": "agent_draft_unreviewed"},
            load_glossary(None),
        )
        assert result["qa_verdict"] == row["qa_verdict_draft"]
        assert result["issues"] == row["qa_issues_draft"]
        assert not any(x["code"] == "japanese_kana_residual" for x in result["issues"])
    title = next(r for r in rows if r["source_sha256"] == TITLE_SHA)
    assert title["translation_draft"] == TITLE_DRAFT
    assert "Wait title natural" in title["machine_candidate_unreviewed"]
    assert title["qa_verdict_draft"] == "PASS"


@pytest.mark.parametrize("changed", [
    "source_sha", "old_name_missing", "title_meta_missing", "old_status",
    "missing_source", "duplicate_source", "authoritative_name_drift",
    "authoritative_evidence_drift",
])
def test_unreviewed_draft_guardrails_reject_tampering(material, changed):
    src, auth = material
    rows = [r.copy() for r in src]
    auth = json.loads(json.dumps(auth, ensure_ascii=False))
    sid = next(iter(NAME_REPAIRS))
    pos = next(i for i, r in enumerate(rows) if r["source_sha256"] == sid)
    title_pos = next(i for i, r in enumerate(rows) if r["source_sha256"] == TITLE_SHA)
    if changed == "source_sha":
        rows[pos]["source"] = "違うソース"
    elif changed == "old_name_missing":
        rows[pos]["machine_candidate_unreviewed"] = "已经完全删除原来的名字了"
    elif changed == "title_meta_missing":
        rows[title_pos]["machine_candidate_unreviewed"] = TITLE_DRAFT
    elif changed == "old_status":
        rows[pos]["release_gate"] = "accepted"
    elif changed == "missing_source":
        rows.pop(pos)
    elif changed == "duplicate_source":
        rows.append(rows[pos])
    elif changed in ("authoritative_name_drift", "authoritative_evidence_drift"):
        full = NAME_REPAIRS[sid][0]
        auth["entries"][full][
            "target" if changed == "authoritative_name_drift" else "evidence"
        ] = "completely unrelated"
    with pytest.raises(ValueError):
        draft_rows(rows, auth)


def test_existing_immutable_output_refuses_overwrite():
    with pytest.raises(FileExistsError):
        build()

