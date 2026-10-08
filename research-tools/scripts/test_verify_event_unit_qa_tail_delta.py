"""Independent read-only QA-only bundle delta verification regressions."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.mltd_localize_gtx import read_jsonl
from scripts.verify_event_unit_qa_tail_delta import (
    BASELINE, WITH_TAIL, TAIL, compare, verify,
)


@pytest.fixture(scope="module")
def raw():
    left = json.loads(
        (BASELINE / "event-unit-QA-candidate-manifest.json").read_text(encoding="utf8")
    )
    right = json.loads(
        (WITH_TAIL / "event-unit-QA-candidate-manifest.json").read_text(encoding="utf8")
    )
    old_audit = json.loads(
        (BASELINE / "event-unit-QA-candidate-audit.json").read_text(encoding="utf8")
    )
    new_audit = json.loads(
        (WITH_TAIL / "event-unit-QA-candidate-audit.json").read_text(encoding="utf8")
    )
    return (
        {x["remote"]: x for x in left["bundles"]},
        {x["remote"]: x for x in right["bundles"]},
        old_audit, new_audit, read_jsonl(TAIL),
    )


def test_real_frozen_qa_trial_every_bundle_file_is_hashed_and_all_deltas_exact():
    report = verify()
    assert report["stage_original_bundle_count"] == 852
    assert report["stage_tail_bundle_count"] == 852
    assert report["unchanged_existing_translated_commands"] == 12105
    assert report["additional_draft_translated_commands"] == 99
    assert report["additional_unique_source_ids"] == 47
    assert report["bundle_remotes_changed_by_tail"] == 91
    assert report["remaining_review"] == 1133
    assert report["remaining_reject"] == 1
    assert report["remaining_missing_candidate"] == 0
    assert report["safe_to_mount_as_final_overlay"] is False


@pytest.mark.parametrize("bad", [
    "machine_changed", "draft_changed", "qa_other_changed",
    "draft_duplicate", "draft_occurrence_changed",
])
def test_reject_tampered_delta_even_when_bundles_remain_readable(raw, bad):
    old, new, previous_qa, current_qa, drafts = copy.deepcopy(raw)
    if bad == "machine_changed":
        remote = next(r for r, x in old.items() if x["changes"])
        new[remote]["changes"][0]["localized"] += "误改"
    elif bad == "draft_changed":
        old_keys = {
            (remote, x["command_index"], x["path_source_sha256"])
            for remote, entry in old.items() for x in entry["changes"]
        }
        remote, index = next(
            (remote, i) for remote, entry in new.items()
            for i, x in enumerate(entry["changes"])
            if (remote, x["command_index"], x["path_source_sha256"]) not in old_keys
        )
        new[remote]["changes"][index]["localized"] += "误改"
    elif bad == "qa_other_changed":
        sid = next(x["source_sha256"] for x in previous_qa
                   if x["qa_verdict"] == "PASS")
        next(x for x in current_qa if x["source_sha256"] == sid)["qa_verdict"] = "REVIEW"
    elif bad == "draft_duplicate":
        drafts.append(drafts[0])
    elif bad == "draft_occurrence_changed":
        drafts[0]["occurrences"] += 1
    with pytest.raises(ValueError):
        compare(old, new, previous_qa, current_qa, drafts)

