"""Fail-closed regression for the 47-source unreviewed event-unit QA trial."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
import scripts.stage_event_unit_candidate_overlay as stage


def frozen_split():
    rows = read_jsonl(stage.QUEUE)
    queue = {x["source_sha256"]: x for x in rows}
    review = read_jsonl(stage.TAIL_REVIEW_ROOT / "review-queue.jsonl")
    pending = {x["source_sha256"] for x in review
               if x["qa_verdict"] == "MISSING_MACHINE"}
    machine = {sid: row for sid, row in queue.items() if sid not in pending}
    identity = version_identity(
        stage.SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=stage.INDEX,
    )
    return queue, machine, identity


def test_real_drafts_are_bound_to_missing_machine_source_and_are_review_only():
    queue, machine, identity = frozen_split()
    path = stage.TAIL_DRAFT_ROOT / "47-source-bound-drafts.jsonl"
    selected, evidence = stage.source_bound_unreviewed_tail_drafts(
        queue, machine, path, identity,
    )
    assert len(queue) == 13177
    assert len(machine) == 13130
    assert len(selected) == 47
    assert set(selected) == set(queue) - set(machine)
    assert evidence["unreviewed_tail_draft_unique"] == 47
    assert evidence["source_draft_sha256"] == sha_file(path)
    assert all(x["status"] == "agent_draft_unreviewed"
               and x["release_gate"] == "needs_review"
               and x["independent_review_complete"] is False for x in selected.values())
    assert all(sid == source_id(x["source"]) for sid, x in selected.items())


def test_draft_trial_rejects_a_different_client_assets_pair():
    queue, machine, identity = frozen_split()
    identity["client_version"] = "9.0.100"
    with pytest.raises(ValueError, match="manifest differs"):
        stage.source_bound_unreviewed_tail_drafts(
            queue, machine, stage.TAIL_DRAFT_ROOT / "47-source-bound-drafts.jsonl", identity
        )


def test_draft_trial_refuses_machine_overlap():
    queue, machine, identity = frozen_split()
    review = read_jsonl(stage.TAIL_REVIEW_ROOT / "review-queue.jsonl")
    sid = next(x["source_sha256"] for x in review
               if x["qa_verdict"] == "MISSING_MACHINE")
    machine[sid] = queue[sid]
    with pytest.raises(ValueError, match="split has changed"):
        stage.source_bound_unreviewed_tail_drafts(
            queue, machine, stage.TAIL_DRAFT_ROOT / "47-source-bound-drafts.jsonl", identity
        )


def test_draft_trial_rejects_different_path():
    queue, machine, identity = frozen_split()
    with pytest.raises(ValueError, match="only the frozen"):
        stage.source_bound_unreviewed_tail_drafts(
            queue, machine, Path("another-tail.jsonl"), identity,
        )


@pytest.mark.parametrize("bad", [
    "release_status", "changed_original_text", "changed_translation",
    "missing_row", "wrong_context",
])
def test_draft_trial_cannot_import_mutated_rows_even_with_rehashed_manifest(
    monkeypatch, tmp_path: Path, bad: str,
):
    queue, machine, identity = frozen_split()
    rows = read_jsonl(stage.TAIL_DRAFT_ROOT / "47-source-bound-drafts.jsonl")
    frozen = json.loads(
        (stage.TAIL_DRAFT_ROOT / "draft-manifest.json").read_text(encoding="utf8")
    )
    if bad == "release_status":
        rows[0]["release_gate"] = "accepted"
    elif bad == "changed_original_text":
        rows[0]["source"] = "まったく別の原文"
    elif bad == "changed_translation":
        rows[0]["translation_draft"] = ""
    elif bad == "missing_row":
        rows.pop()
    elif bad == "wrong_context":
        rows[0]["examples"] = []
    path = tmp_path / "47-source-bound-drafts.jsonl"
    with path.open("w", encoding="utf8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    frozen["draft_sha256"] = sha_file(path)
    (tmp_path / "draft-manifest.json").write_text(
        json.dumps(frozen, ensure_ascii=False), encoding="utf8"
    )
    monkeypatch.setattr(stage, "TAIL_DRAFT_ROOT", tmp_path)
    with pytest.raises(ValueError):
        stage.source_bound_unreviewed_tail_drafts(queue, machine, path, identity)

