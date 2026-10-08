"""Source-identity, QA and non-release regressions for 47 event-unit cue drafts."""
from __future__ import annotations

import json
from collections import Counter

import pytest

from scripts.build_event_unit_legacy_tail_drafts import (
    DRAFT_BY_SOURCE, OUTPUT, draft_rows, sha_file,
)
from scripts.build_event_unit_review_pack import OUTPUT as REVIEW_ROOT
from scripts.mltd_translation_quality import source_id


def _rows():
    return [
        {
            "source_sha256": source_id(s), "source": s,
            "qa_verdict": "MISSING_MACHINE", "review_status": "pending",
            "release_gate": "needs_independent_review",
            "machine_candidate_unreviewed": "",
            "legacy_traditional_references_unreviewed": ["参考译文"],
            "examples": [{"previous": "前", "next": "後"}], "occurrences": 2,
        } for s in DRAFT_BY_SOURCE
    ]


def test_47_manual_drafts_all_qa_pass_and_remain_review_only():
    rows, verdicts = draft_rows(_rows())
    assert len(rows) == 47
    assert verdicts == {"PASS": 47}
    assert len({r["source_sha256"] for r in rows}) == 47
    assert all(r["source_sha256"] == source_id(r["source"]) for r in rows)
    assert all(r["status"] == "agent_draft_unreviewed"
               and r["release_gate"] == "needs_review"
               and r["independent_review_complete"] is False
               and r["semantic_accuracy_verified"] is False
               and r["safe_to_auto_promote"] is False for r in rows)
    assert rows[0]["examples"] == [{"previous": "前", "next": "後"}]


@pytest.mark.parametrize("bad", ["missing", "extra", "duplicate", "mismatch_sha",
                                  "approved", "not_missing", "has_machine"])
def test_draft_universe_and_release_gate_fail_closed(bad):
    rows = _rows()
    if bad == "missing":
        rows.pop()
    elif bad == "extra":
        rows.append({**rows[0], "source_sha256": source_id("新しい源"), "source": "新しい源"})
    elif bad == "duplicate":
        rows.append(rows[0])
    elif bad == "mismatch_sha":
        rows[0] = {**rows[0], "source_sha256": "0" * 64}
    elif bad == "approved":
        rows[0] = {**rows[0], "release_gate": "accepted"}
    elif bad == "not_missing":
        rows[0] = {**rows[0], "qa_verdict": "REVIEW"}
    elif bad == "has_machine":
        rows[0] = {**rows[0], "machine_candidate_unreviewed": "译文"}
    with pytest.raises(ValueError):
        draft_rows(rows)


def test_real_frozen_draft_sha_and_review_population():
    manifest = json.loads((OUTPUT / "draft-manifest.json").read_text(encoding="utf8"))
    path = OUTPUT / manifest["draft_filename"]
    result = [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]
    assert manifest["draft_sha256"] == sha_file(path)
    assert manifest["review_pack_manifest_sha256"] == sha_file(REVIEW_ROOT / "review-pack-manifest.json")
    assert manifest["review_queue_sha256"] == sha_file(REVIEW_ROOT / "review-queue.jsonl")
    assert len(result) == manifest["draft_unique"] == 47
    assert Counter(row["qa_verdict"] for row in result) == {"PASS": 47}
    assert len({row["source_sha256"] for row in result}) == 47
    assert manifest["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert manifest["independent_review_complete"] is False
    assert manifest["safe_to_mount_as_final_overlay"] is False
    assert manifest["candidate_is_machine_translation"] is False

