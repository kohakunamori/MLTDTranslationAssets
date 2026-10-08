#!/usr/bin/env python3
"""Followup 12-source / 11-original-bundle Event-unit QA-only technical trial.

Rebuilds from frozen original 1077100 UnityFS bundles; preserves existing 852
baseline QA modifications. Existing combined 31-source output is NOT overwritten.
One remote overlaps earlier combined trial and must later be rebuilt, NOT copied.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_followup_12_drafts import (
    DEST as DRAFT_ROOT, REVIEW_FILE,
)
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE, BASE_MANIFEST, COHORT_BUNDLE_ROOT, COHORT_PATH, INDEX, SNAPSHOT,
    materialize, verify_trial_delta,
)

BUILD = ROOT / "build/localization-90200"
DRAFT_FILE = DRAFT_ROOT / "12-source-bound-corrections.jsonl"
DRAFT_MANIFEST = DRAFT_ROOT / "manifest.json"
PRIOR_COMPOSITE = BUILD / "staging-event-unit-combined-31-unreviewed-drafts"
DEST = BUILD / "staging-event-unit-followup-12-qa-only"
DRAFT_SHA = "8109fe152769052a959cc66156e4697e711e0b51dd9ffc66d148cb7e5c4868d9"
BASE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"


def validate_sources(
    baseline: dict, proposals: list[dict], worklist: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    # Validate every old machine/tail QA command before accepting additions:
    # a caller must not swap text in an unrelated baseline bundle.
    if baseline != json.loads(BASE_MANIFEST.read_text(encoding="utf8")):
        raise ValueError("baseline manifest text or command data changed")
    if (baseline.get("release_gate") != "not_evaluated"
        or baseline.get("safe_to_mount_as_final_overlay") is not False
        or baseline.get("independent_reviewed") is not False
        or baseline.get("bundles_written") != 852
        or baseline.get("text_fields_changed") != 12204):
        raise ValueError("frozen QA baseline release/field identity changed")
    known = {}
    for row in worklist:
        sid = row.get("source_sha256")
        if (sid in known or sid != source_id(row.get("source", ""))
            or row.get("release_gate") != "needs_independent_review"):
            raise ValueError("duplicate/unapproved unified reviewer")
        known[sid] = row
    if len(known) != 1181:
        raise ValueError("unified reviewer population changed")
    base_rows = {}
    for bundle in baseline["bundles"]:
        for change in bundle["changes"]:
            sid, source, zh = (
                change["path_source_sha256"], change["original"], change["localized"]
            )
            if sid != source_id(source):
                raise ValueError("baseline QA source is not SHA bound")
            old = base_rows.setdefault(
                sid, {"source_sha256": sid, "source": source,
                      "translation": zh, "status": "QA-only-baseline"},
            )
            if old["source"] != source or old["translation"] != zh:
                raise ValueError("same Japanese source has conflicting old QA translations")
    selected = {}
    counts: Counter[str] = Counter()
    for item in proposals:
        sid, source = item.get("source_sha256"), item.get("source")
        old = known.get(sid)
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in selected or sid in base_rows or old is None
            or old["source"] != source
            or old.get("review_bucket") != "qa_review_without_draft"
            or old["machine_candidate_unreviewed"] !=
                item.get("machine_candidate_unreviewed")
            or old["current_machine_qa_issues"] !=
                item.get("original_machine_QA_issues")
            or old["examples"] != item.get("examples")
            or old["occurrences"] != item.get("occurrences")
            or item.get("occurrences") != 1
            or len(item.get("examples", [])) != 1
            or item.get("status") != "agent_draft_unreviewed"
            or item.get("review_status") != "pending"
            or item.get("release_gate") != "needs_independent_review"
            or item.get("independent_review_complete") is not False
            or item.get("semantic_accuracy_verified") is not False
            or item.get("safe_to_mount_as_final_overlay") is not False
            or item.get("draft_qa_verdict") not in ("PASS", "REVIEW")):
            raise ValueError(f"unreviewed followup draft is not source-bound: {sid}")
        fresh = evaluate_row(
            {"source": source, "source_sha256": sid},
            {"source": source, "source_sha256": sid,
             "translation": item["translation_draft"],
             "status": "agent_draft_unreviewed"},
            load_glossary(None),
        )
        if (fresh["qa_verdict"] != item["draft_qa_verdict"]
            or fresh["issues"] != item["draft_qa_issues"]):
            raise ValueError(f"followup QA evidence no longer valid: {sid}")
        selected[sid] = item
        counts[fresh["qa_verdict"]] += 1
    if (len(selected) != 12
        or counts != Counter({"PASS": 11, "REVIEW": 1})):
        raise ValueError("followup pilot source/QA cardinality differs")
    translations = {
        **base_rows,
        **{sid: {"source_sha256": sid, "source": row["source"],
                 "translation": row["translation_draft"],
                 "status": "agent_draft_unreviewed"}
           for sid, row in selected.items()},
    }
    return translations, selected


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(BUILD.resolve()):
        raise ValueError("QA-only output cannot leave isolated build")
    if dest.exists():
        raise FileExistsError("immutable followup QA trial already exists")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError("incomplete followup QA-only build already exists")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    base = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    drafts = json.loads(DRAFT_MANIFEST.read_text(encoding="utf8"))
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf8"))
    combined_path = PRIOR_COMPOSITE / "manifest.json"
    combined = json.loads(combined_path.read_text(encoding="utf8"))
    if (sha_file(BASE_MANIFEST) != BASE_SHA
        or base["version_identity"] != identity
        or drafts.get("version_identity") != identity
        or drafts.get("draft_sha256") != DRAFT_SHA
        or sha_file(DRAFT_FILE) != DRAFT_SHA
        or drafts.get("unified_review_worklist_sha256") != sha_file(REVIEW_FILE)
        or drafts.get("draft_unique") != 12
        or drafts.get("draft_qa_verdicts") != {"PASS": 11, "REVIEW": 1}
        or drafts.get("safe_to_mount_as_final_overlay") is not False
        or drafts.get("independent_review_complete") is not False
        or cohort.get("complete") is not True
        or cohort.get("verified") != 852
        or cohort.get("source_index_sha256") != identity["asset_index_sha256"]
        or combined.get("version_identity") != identity
        or combined.get("additional_source_unique") != 31
        or combined.get("safe_to_mount_as_final_overlay") is not False
        or combined.get("overlay_merge_authorized") is not False):
        raise ValueError("old QA/source/unreviewed followup identity changed")
    trans, desired = validate_sources(
        base, read_jsonl(DRAFT_FILE), read_jsonl(REVIEW_FILE),
    )
    by_remote: dict[str, dict[str, dict]] = defaultdict(dict)
    for sid, row in desired.items():
        by_remote[row["examples"][0]["remote"]][sid] = row
    if len(by_remote) != 11 or sum(len(v) for v in by_remote.values()) != 12:
        raise ValueError("new 12 sources expected in exactly 11 unique remotes")
    old_by_remote = {b["remote"]: b for b in base["bundles"]}
    original_by_remote = {b["remote"]: b for b in cohort["bundles"]}
    prior_by_remote = {b["remote"]: b for b in combined["bundles"]}
    if len(old_by_remote) != len(original_by_remote) != 852:
        raise ValueError("base/cohort original remote universe differs")
    overlapping = sorted(set(by_remote) & set(prior_by_remote))
    if len(overlapping) != 1:
        raise ValueError("expected one shared remote with earlier 31-source trial")
    temp.mkdir(parents=True)
    records = []
    for remote in sorted(by_remote):
        original = original_by_remote[remote]
        previous = old_by_remote[remote]
        src_path = COHORT_BUNDLE_ROOT / remote
        old_path = BASE / "jp-android" / remote
        if (original["logical"] != previous["logical"]
            or original["sha256"] != previous["source_sha256"]
            or original["declared_bytes"] != previous["original_bytes"]
            or sha_file(src_path) != original["sha256"]
            or sha_file(old_path) != previous["localized_sha256"]):
            raise ValueError(f"frozen original or old QA bundle drift: {remote}")
        output = temp / "jp-android" / remote
        result = materialize(
            original["logical"], remote, original["declared_bytes"],
            src_path, output, trans, require_complete=False,
        )
        extra = verify_trial_delta(previous, result, by_remote[remote])
        if len(extra) != len(by_remote[remote]):
            raise ValueError("unmaterialized source-bound followup field")
        result["original_bundle_sha256"] = original["sha256"]
        result["existing_QA_bundle_sha256"] = previous["localized_sha256"]
        result["new_unreviewed_draft_fields"] = extra
        result["overlaps_prior_31_bundle"] = remote in prior_by_remote
        result["overlap_requires_full_rebuild_not_byte_copy"] = (
            remote in prior_by_remote
        )
        result["output_path"] = str(dest / "jp-android" / remote)
        result["release_gate"] = "NOT_EVALUATED"
        records.append(result)
    report = {
        "schema_version": 1,
        "kind": "event-unit-followup-12-source-11-bundle-QA-only",
        "version_identity": identity,
        "baseline_852_manifest_sha256": sha_file(BASE_MANIFEST),
        "source_852_cohort_sha256": sha_file(COHORT_PATH),
        "followup_12_manifest_sha256": sha_file(DRAFT_MANIFEST),
        "followup_12_draft_sha256": sha_file(DRAFT_FILE),
        "unified_review_worklist_sha256": sha_file(REVIEW_FILE),
        "prior_31_manifest_sha256": sha_file(combined_path),
        "baseline_12204_changed_fields_preserved": 12204,
        "additional_unreviewed_source_unique": 12,
        "additional_unreviewed_text_fields": sum(
            len(r["new_unreviewed_draft_fields"]) for r in records
        ),
        "roundtrip_bundle_count": len(records),
        "qa_verdicts": {"PASS": 11, "REVIEW": 1},
        "prior_31_overlapping_remotes": overlapping,
        "prior_31_combination_requires_rebuild": bool(overlapping),
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "overlay_merge_authorized": False,
        "original_official_assets_modified": False,
        "production_translations_modified": False,
        "prior_852_QA_bundles_modified": False,
        "prior_31_QA_bundles_modified": False,
        "nas_modified": False,
        "bundles": records,
    }
    (temp / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
    )
    os.replace(temp, dest)
    return {k: v for k, v in report.items() if k != "bundles"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root", type=Path, default=DEST)
    args = p.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

