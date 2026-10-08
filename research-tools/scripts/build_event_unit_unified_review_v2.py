#!/usr/bin/env python3
"""Immutable Event-unit reviewer worklist v2: 90 unreviewed drafts, 818 without.

Rebases 12 newly authored source-SHA-bound drafts on the frozen v1 1181-source
worklist; checks 43-source QA-only bundle pilot. Does NOT auto-accept any row,
change baseline machine QA, producer translations, resources, NAS or device.
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
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id

BUILD = ROOT / "build/localization-90200"
AUDITS = BUILD / "audits"
V1 = AUDITS / "event-unit-unified-review-client-9.0.200-assets-1077100"
FOLLOWUP = AUDITS / "event-unit-followup-12-semantic-drafts-client-9.0.200-assets-1077100"
STAGE = BUILD / "staging-event-unit-combined-43-unreviewed-drafts"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
DEST = AUDITS / "event-unit-unified-review-v2-client-9.0.200-assets-1077100"

INPUTS = {
    "unified_review_v1": (
        V1 / "review-worklist.jsonl",
        "c9c89a8337489cbee0d40f29248fdc7372f7c61b94f60c625753770bf64edc2e",
    ),
    "unreviewed_followup_12": (
        FOLLOWUP / "12-source-bound-corrections.jsonl",
        "8109fe152769052a959cc66156e4697e711e0b51dd9ffc66d148cb7e5c4868d9",
    ),
}
MANIFESTS = {
    "unified_review_v1": (V1 / "manifest.json",
        "b4d6b86ac1a6d286c3ad5d5e71ef7515647e94493123c8312fe3872468c8ac27"),
    "unreviewed_followup_12": (FOLLOWUP / "manifest.json",
        "d12b9e5613631874a9c448ad329e00bb9b79464ed7229354f3767e019992b63a"),
    "qa_only_43_trial": (STAGE / "manifest.json",
        "bf4f5b113e663c2b840c5e516853ae06c6f6292c604c0d2775f6e7b4638c6dbc"),
}
BUCKETS = (
    "rejected_original_with_correction_draft",
    "draft_still_qa_review",
    "qa_review_with_qa_pass_draft",
    "missing_machine_with_qa_pass_draft",
    "qa_review_without_draft",
    "rule_cleared_qa_pass_needs_semantic_review",
)
EXPECTED_COUNTS = {
    "rejected_original_with_correction_draft": 1,
    "draft_still_qa_review": 2,
    "qa_review_with_qa_pass_draft": 40,
    "missing_machine_with_qa_pass_draft": 47,
    "qa_review_without_draft": 818,
    "rule_cleared_qa_pass_needs_semantic_review": 273,
}


def rebase(
    original: list[dict], followup: list[dict], trial: dict,
) -> tuple[list[dict], dict]:
    original_by_sha: dict[str, dict] = {}
    for row in original:
        sid, source = row.get("source_sha256"), row.get("source")
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in original_by_sha):
            raise ValueError("original unified reviewer source duplicate/stale")
        original_by_sha[sid] = row
    if len(original_by_sha) != 1181:
        raise ValueError("v1 reviewer source cohort unexpectedly changed")
    proposed: dict[str, dict] = {}
    for item in followup:
        sid, source = item.get("source_sha256"), item.get("source")
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in proposed):
            raise ValueError("new unreviewed draft is duplicate or unbound")
        proposed[sid] = item
    if len(proposed) != 12:
        raise ValueError("followup source draft count changed")
    result = []
    counts: Counter[str] = Counter()
    source_ids_in_trial = {}
    for bundle in trial.get("bundles", []):
        for field in bundle.get("additional_unreviewed_fields", []):
            sid = field.get("path_source_sha256")
            if (sid in source_ids_in_trial or
                sid != source_id(field.get("original", ""))):
                raise ValueError("43-bundle materialization source duplicated/stale")
            source_ids_in_trial[sid] = field
    if (len(trial.get("bundles", [])) != 40
        or len(source_ids_in_trial) != 43
        or trial.get("draft_QA_verdicts") != {"PASS": 41, "REVIEW": 2}
        or trial.get("additional_unreviewed_source_unique") != 43
        or trial.get("safe_to_mount_as_final_overlay") is not False
        or trial.get("overlay_merge_authorized") is not False
        or trial.get("independent_review_complete") is not False):
        raise ValueError("43 technical trial source/release lineage changed")
    for sid, row in original_by_sha.items():
        if (row.get("review_status") != "pending"
            or row.get("independent_review_complete") is not False
            or row.get("semantic_accuracy_verified") is not False
            or row.get("release_gate") != "needs_independent_review"
            or row.get("safe_to_mount_as_final_overlay") is not False):
            raise ValueError(f"older reviewer source already accepted: {sid}")
        updated = dict(row)
        updated["prior_review_bucket"] = row["review_bucket"]
        if sid in proposed:
            candidate = proposed[sid]
            if (row["review_bucket"] != "qa_review_without_draft"
                or row["current_machine_qa_verdict"] != "REVIEW"
                or row["agent_correction_draft_unreviewed"] is not None
                or row["candidate_for_human_review_unreviewed"] !=
                    row["machine_candidate_unreviewed"]
                or candidate.get("machine_candidate_unreviewed") !=
                    row["machine_candidate_unreviewed"]
                or candidate.get("original_machine_QA_issues") !=
                    row["current_machine_qa_issues"]
                or candidate.get("original_machine_QA_verdict") !=
                    row["current_machine_qa_verdict"]
                or candidate.get("source") != row["source"]
                or candidate.get("examples") != row["examples"]
                or candidate.get("occurrences") != row["occurrences"]
                or candidate.get("status") != "agent_draft_unreviewed"
                or candidate.get("review_status") != "pending"
                or candidate.get("release_gate") != "needs_independent_review"
                or candidate.get("independent_review_complete") is not False
                or candidate.get("semantic_accuracy_verified") is not False
                or candidate.get("safe_to_mount_as_final_overlay") is not False
                or candidate.get("draft_qa_verdict") not in ("PASS", "REVIEW")
                or not isinstance(candidate.get("draft_qa_issues"), list)):
                raise ValueError(f"followup reviewer drift/false approval: {sid}")
            qa = candidate["draft_qa_verdict"]
            if qa == "PASS" and candidate["draft_qa_issues"]:
                raise ValueError(f"followup QA PASS wrongly retains QA issues: {sid}")
            field = source_ids_in_trial.get(sid)
            if (field is None or field.get("localized") != candidate["translation_draft"]
                or field.get("original") != candidate["source"]):
                raise ValueError(f"followup draft missing from 43-bundle pilot: {sid}")
            updated["agent_correction_draft_unreviewed"] = candidate["translation_draft"]
            updated["agent_draft_source_group"] = "unreviewed_followup_12"
            updated["agent_draft_deterministic_qa_verdict"] = qa
            updated["agent_draft_deterministic_qa_issues"] = candidate["draft_qa_issues"]
            updated["candidate_for_human_review_unreviewed"] = candidate["translation_draft"]
            updated["followup_12_repair_reason"] = candidate["repair_reason"]
            updated["review_bucket"] = (
                "qa_review_with_qa_pass_draft" if qa == "PASS" else
                "draft_still_qa_review"
            )
        elif sid in source_ids_in_trial:
            field = source_ids_in_trial[sid]
            if (row["agent_correction_draft_unreviewed"] != field.get("localized")
                or row["source"] != field.get("original")
                or row["agent_draft_deterministic_qa_verdict"] not in
                    ("PASS", "REVIEW")):
                raise ValueError("preexisting 31-source trial draft mismatch")
        updated["review_bucket_order"] = BUCKETS.index(updated["review_bucket"])
        counts[updated["review_bucket"]] += 1
        result.append(updated)
    if (set(proposed) - set(source_ids_in_trial)
        or len(source_ids_in_trial) != 43
        or len(proposed) != 12
        or counts != Counter(EXPECTED_COUNTS)):
        raise ValueError("updated review buckets or technical trial changed")
    result.sort(key=lambda x: (
        x["review_bucket_order"], -x["occurrences"], x["source_sha256"]
    ))
    drafts = [r for r in result
              if r["agent_correction_draft_unreviewed"] is not None]
    qa_draft_counts = Counter(
        r["agent_draft_deterministic_qa_verdict"] for r in drafts
    )
    if (len(drafts) != 90
        or qa_draft_counts != Counter({"PASS": 88, "REVIEW": 2})):
        raise ValueError("unreviewed draft semantic/QA counts changed")
    return result, {
        "original_review_source_unique": len(result),
        "independent_semantic_reviews_still_required": len(result),
        "available_unreviewed_agent_drafts": len(drafts),
        "draft_deterministic_qa_verdicts": dict(qa_draft_counts),
        "qa_review_without_targeted_draft": EXPECTED_COUNTS[
            "qa_review_without_draft"
        ],
        "all_without_targeted_draft": len(result) - len(drafts),
        "review_buckets": dict(counts),
        "existing_machine_qa_status_unmodified": {
            "PASS": 273, "REVIEW": 860, "REJECT": 1,
            "MISSING_MACHINE": 47,
        },
        "reviewer_source_ids_materialized_in_43_bundle_pilot": len(source_ids_in_trial),
    }


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(AUDITS.resolve()):
        raise ValueError("unified v2 reviewer must remain inside audits")
    if dest.exists():
        raise FileExistsError(f"v2 reviewer worklist is immutable: {dest}")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError(f"unfinished v2 reviewer worklist: {temp}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200",
        asset_version="1077100", asset_index=INDEX,
    )
    for name, (path, expected) in INPUTS.items():
        if sha_file(path) != expected:
            raise ValueError(f"frozen reviewer input file changed: {name}")
    source_manifests = {}
    for name, (path, expected) in MANIFESTS.items():
        if sha_file(path) != expected:
            raise ValueError(f"frozen reviewer manifest changed: {name}")
        m = json.loads(path.read_text(encoding="utf8"))
        if (m.get("version_identity") != identity
            or m.get("independent_review_complete") is not False
            or m.get("safe_to_mount_as_final_overlay") is not False):
            raise ValueError(f"reviewer source already approved or has wrong version: {name}")
        source_manifests[name] = m
    if (source_manifests["unified_review_v1"].get("review_worklist_sha256")
            != INPUTS["unified_review_v1"][1]
        or source_manifests["unreviewed_followup_12"].get("draft_sha256")
            != INPUTS["unreviewed_followup_12"][1]
        or source_manifests["qa_only_43_trial"].get("bundle_count") != 40):
        raise ValueError("source manifest/file SHA chain mismatch")
    rows, summary = rebase(
        read_jsonl(INPUTS["unified_review_v1"][0]),
        read_jsonl(INPUTS["unreviewed_followup_12"][0]),
        source_manifests["qa_only_43_trial"],
    )
    temp.mkdir(parents=True)
    target = temp / "review-worklist.jsonl"
    with target.open("w", encoding="utf8", newline="\n") as writer:
        for item in rows:
            writer.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 2,
        "kind": "event-unit-source-bound-unified-review-v2-NOT-APPROVED",
        "version_identity": identity,
        "source_input_files_sha256": {
            key: digest for key, (_, digest) in INPUTS.items()
        },
        "source_manifests_sha256": {
            key: digest for key, (_, digest) in MANIFESTS.items()
        },
        "review_worklist_filename": target.name,
        "review_worklist_sha256": sha_file(target),
        "bucket_order": list(BUCKETS),
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "review_status": "pending",
        "release_gate": "needs_independent_review",
        "safe_to_mount_as_final_overlay": False,
        "production_translation_files_modified": False,
        "prior_852_QA_bundle_stage_modified": False,
        "prior_43_QA_bundle_stage_modified": False,
        "official_original_assets_modified": False,
        "nas_modified": False,
        **summary,
    }
    (temp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    os.replace(temp, dest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

