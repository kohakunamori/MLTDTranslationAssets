#!/usr/bin/env python3
"""Read-only source-bound 852-bundle QA comparison: machine-only vs +47 drafts.

This verifies TECHNICAL roundtrip/delta only.  Neither stage is a zh-CN release.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_event_unit_review_pack import sha_file
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id

BUILD = ROOT / "build/localization-90200"
BASELINE = BUILD / "staging-event-unit-qa"
WITH_TAIL = BUILD / "staging-event-unit-qa-with-tail"
TAIL = (
    BUILD / "audits/event-unit-tail-draft-client-9.0.200-assets-1077100/"
    "47-source-bound-drafts.jsonl"
)
REPORT = BUILD / "audits/event-unit-tail-qa-852-bundle-delta-1077100.json"


def index_stage(stage: Path, doc: dict, *, tail: bool) -> dict:
    if (doc.get("release_gate") != "not_evaluated"
        or doc.get("safe_to_mount_as_final_overlay") is not False
        or doc.get("independent_reviewed") is not False
        or doc.get("nas_modified") is not False
        or doc.get("version_identity", {}).get("version_key")
           != "jp-client-9.0.200-assets-1077100"
        or doc.get("source_unique") != 13177
        or doc.get("machine_candidate_unique") != 13130
        or doc.get("bundles_scanned") != 852
        or doc.get("bundles_written") != 852
        or bool(doc.get("unreviewed_agent_tail_draft_present")) != tail
        or doc.get("unreviewed_agent_tail_draft_unique", 0) != (47 if tail else 0)):
        raise ValueError("stage is incomplete, stale, or release-gated")
    result = {}
    for row in doc["bundles"]:
        remote = row["remote"]
        path = stage / "jp-android" / remote
        if (remote in result or row.get("roundtrip_verified") is not True
            or row.get("non_text_objects_byte_identical") is not True
            or row.get("release_gate") != "NOT_EVALUATED"
            or Path(row.get("output_path", "")).resolve() != path.resolve()
            or not path.is_file() or path.stat().st_size != row["output_bytes"]
            or sha_file(path) != row["localized_sha256"]):
            raise ValueError(f"stage bundle incomplete/unverified: {remote}")
        result[remote] = row
    if len(result) != 852 or sum(len(x["changes"]) for x in result.values()) != doc["text_fields_changed"]:
        raise ValueError("stage manifest bundle count or text edits differ")
    return result


def compare(
    old: dict, new: dict,
    original_qa: list[dict], current_qa: list[dict],
    drafts: list[dict],
) -> dict:
    if set(old) != set(new):
        raise ValueError("event-unit remote set changed")
    draft_by_sid = {r["source_sha256"]: r for r in drafts}
    if len(drafts) != len(draft_by_sid) or len(draft_by_sid) != 47:
        raise ValueError("47-source draft set is duplicated/truncated")
    if len(draft_by_sid) != 47 or any(
        sid != source_id(r["source"]) or r.get("qa_verdict") != "PASS"
        or r.get("release_gate") != "needs_review"
        or r.get("independent_review_complete") is not False
        for sid, r in draft_by_sid.items()
    ):
        raise ValueError("47-source draft set stale or already release-gated")
    old_qa = {row["source_sha256"]: row for row in original_qa}
    new_qa = {row["source_sha256"]: row for row in current_qa}
    if (len(old_qa) != len(original_qa) or len(new_qa) != len(current_qa)
        or len(old_qa) != 13177 or set(old_qa) != set(new_qa)):
        raise ValueError("event-unit QA source population changed")
    for sid in old_qa:
        if sid in draft_by_sid:
            if (old_qa[sid]["qa_verdict"] != "MISSING_MACHINE"
                or new_qa[sid]["qa_verdict"] != "PASS"
                or new_qa[sid].get("translation") != draft_by_sid[sid]["translation_draft"]
                or new_qa[sid].get("status") != "agent_draft_unreviewed"):
                raise ValueError(f"47-draft QA transition invalid: {sid}")
        elif old_qa[sid] != new_qa[sid]:
            raise ValueError(f"previous non-tail QA row changed: {sid}")
    baseline = {}
    current = {}
    for remote in sorted(old):
        left, right = old[remote], new[remote]
        if (left["source_sha256"] != right["source_sha256"]
            or left["original_bytes"] != right["original_bytes"]):
            raise ValueError(f"frozen source bundle changed: {remote}")
        for entry, target in ((left, baseline), (right, current)):
            for change in entry["changes"]:
                key = (remote, change["command_index"], change["path_source_sha256"])
                if key in target or change["path_source_sha256"] != source_id(change["original"]):
                    raise ValueError(f"stale/duplicate command text identity: {remote}")
                target[key] = change
    if any(current.get(key) != item for key, item in baseline.items()):
        raise ValueError("existing machine-translated command text changed")
    extra = {key: item for key, item in current.items() if key not in baseline}
    count = Counter()
    for item in extra.values():
        sid = item["path_source_sha256"]
        if sid not in draft_by_sid:
            raise ValueError(f"unexpected non-tail text replacement: {sid}")
        draft = draft_by_sid[sid]
        if item["original"] != draft["source"] or item["localized"] != draft["translation_draft"]:
            raise ValueError(f"47-draft text replacement differs: {sid}")
        count[sid] += 1
    if set(count) != set(draft_by_sid) or any(
        count[sid] != draft["occurrences"] for sid, draft in draft_by_sid.items()
    ):
        raise ValueError("47-draft command occurrences differ from frozen source")
    return {
        "stage_original_bundle_count": len(old),
        "stage_tail_bundle_count": len(new),
        "unchanged_existing_translated_commands": len(baseline),
        "additional_draft_translated_commands": len(extra),
        "additional_unique_source_ids": len(count),
        "additional_source_occurrences": sum(count.values()),
        "bundle_remotes_changed_by_tail": len({key[0] for key in extra}),
    }


def verify() -> dict:
    old_manifest = BASELINE / "event-unit-QA-candidate-manifest.json"
    new_manifest = WITH_TAIL / "event-unit-QA-candidate-manifest.json"
    old_audit = BASELINE / "event-unit-QA-candidate-audit.json"
    new_audit = WITH_TAIL / "event-unit-QA-candidate-audit.json"
    left = json.loads(old_manifest.read_text(encoding="utf8"))
    right = json.loads(new_manifest.read_text(encoding="utf8"))
    if (left["version_identity"] != right["version_identity"]
        or left["source_queue_sha256"] != right["source_queue_sha256"]
        or left["source_audit_sha256"] != right["source_audit_sha256"]
        or left["qa_verdicts"] != {"PASS": 11996, "REVIEW": 1133,
                                  "REJECT": 1, "MISSING_MACHINE": 47}
        or right["qa_verdicts"] != {"PASS": 12043, "REVIEW": 1133, "REJECT": 1}
        or left["text_fields_changed"] != 12105
        or right["text_fields_changed"] != 12204
        or right.get("unreviewed_agent_tail_draft_evidence", {}).get(
            "source_draft_sha256"
        ) != sha_file(TAIL)):
        raise ValueError("stages are not the frozen machine-only / +47 pair")
    old = index_stage(BASELINE, left, tail=False)
    new = index_stage(WITH_TAIL, right, tail=True)
    metrics = compare(
        old, new,
        json.loads(old_audit.read_text(encoding="utf8")),
        json.loads(new_audit.read_text(encoding="utf8")),
        read_jsonl(TAIL),
    )
    return {
        "schema_version": 1,
        "kind": "event-unit-47-unreviewed-draft-852-bundle-technical-QA-delta",
        "version_identity": right["version_identity"],
        "machine_only_manifest_sha256": sha_file(old_manifest),
        "with_tail_manifest_sha256": sha_file(new_manifest),
        "machine_only_QA_audit_sha256": sha_file(old_audit),
        "with_tail_QA_audit_sha256": sha_file(new_audit),
        "unreviewed_draft_sha256": sha_file(TAIL),
        "machine_only_qa_pass": left["qa_pass_unique"],
        "with_tail_qa_pass": right["qa_pass_unique"],
        "remaining_review": 1133,
        "remaining_reject": 1,
        "remaining_missing_candidate": 0,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "independent_reviewed": False,
        "nas_modified": False,
        **metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    if args.report.exists():
        raise FileExistsError(f"audit report already exists: {args.report}")
    result = verify()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temp = args.report.with_name(args.report.name + ".incomplete")
    if temp.exists():
        raise FileExistsError(f"incomplete audit report: {temp}")
    temp.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
    )
    os.replace(temp, args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

