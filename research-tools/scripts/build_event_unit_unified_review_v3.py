#!/usr/bin/env python3
"""Immutable source-bound v3 reviewer: 97 unreviewed drafts, 811 QA REVIEW undrafted.

Join existing v2 with one previous-line-bleed and six contextual candidates
only if all 50 materialized pilot corrections match exact text/source SHA.
All original machine QA and all independent semantic review gates stay frozen.
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
V2 = AUDITS / "event-unit-unified-review-v2-client-9.0.200-assets-1077100"
BLEED = AUDITS / "event-unit-previous-line-bleed-draft-client-9.0.200-assets-1077100"
SIX = AUDITS / "event-unit-six-contextual-corrections-client-9.0.200-assets-1077100"
STAGE = BUILD / "staging-event-unit-combined-50-unreviewed-drafts"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
DEST = AUDITS / "event-unit-unified-review-v3-client-9.0.200-assets-1077100"

INPUT_FILES = {
    "v2": (V2 / "review-worklist.jsonl",
           "61a7da214fbdcb905ee947a8894c5b306e6bb5f3de326c1161ee8d877b367927"),
    "bleed": (BLEED / "one-source-bound-correction.jsonl",
              "ccad273838fa5631ec57028cc60942a12029626b8ca744065c5feaa0261ac725"),
    "six": (SIX / "six-source-bound-corrections.jsonl",
            "9800e4606848f10acfe51d393477d4cc8188016ce40c4510db88199e9b6f2885"),
}
INPUT_MANIFESTS = {
    "v2": (V2 / "manifest.json",
           "17775ef9d4e94dec13531e1a787a2f95f19bc6da113b422f0d236acc0d179b97"),
    "bleed": (BLEED / "manifest.json",
              "683d6f7e99f59e409f7f1addc09834c77c0aa75527db32e451b2739deea1cec9"),
    "six": (SIX / "manifest.json",
            "3731fcef5c6f30b9ba0b11d5632c3180f6f9b1c580733e5fe5f79046867d736e"),
    "trial50": (STAGE / "manifest.json",
                "9480b987c2ba9d17c74fccbc4da8dcd3e92cd80ace8e5aa1c35aeab7aa6b8bcd"),
}
BUCKETS = (
    "rejected_original_with_correction_draft",
    "draft_still_qa_review",
    "qa_review_with_qa_pass_draft",
    "missing_machine_with_qa_pass_draft",
    "qa_review_without_draft",
    "rule_cleared_qa_pass_needs_semantic_review",
)
EXPECTED_BUCKET_COUNTS = {
    "rejected_original_with_correction_draft": 1,
    "draft_still_qa_review": 2,
    "qa_review_with_qa_pass_draft": 47,
    "missing_machine_with_qa_pass_draft": 47,
    "qa_review_without_draft": 811,
    "rule_cleared_qa_pass_needs_semantic_review": 273,
}


def update_worklist(
    previous: list[dict], bleed: list[dict], six: list[dict], pilot: dict
) -> tuple[list[dict], dict]:
    earlier = {}
    for row in previous:
        sid, source = row.get("source_sha256"), row.get("source")
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in earlier):
            raise ValueError("duplicate/stale v2 review source SHA")
        earlier[sid] = row
    if len(earlier) != 1181:
        raise ValueError("v2 source population changed")
    if (len(bleed) != 1 or len(six) != 6
        or len({x["source_sha256"] for x in bleed + six}) != 7):
        raise ValueError("new source repair population wrong")
    proposed = {}
    for group, group_rows in (
        ("unreviewed_previous_line_bleed", bleed),
        ("unreviewed_six_contextual", six),
    ):
        for item in group_rows:
            sid, source = item.get("source_sha256"), item.get("source")
            if (not isinstance(source, str) or sid != source_id(source)
                or sid in proposed):
                raise ValueError("duplicate/stale unreviewed draft source")
            proposed[sid] = (group, item)

    materialized: dict[str, dict] = {}
    remotes = set()
    for bundle in pilot.get("bundles", []):
        remote = bundle.get("remote")
        if (remote in remotes or
            bundle.get("release_gate") != "NOT_EVALUATED"
            or bundle.get("roundtrip_verified_prior_trial") is not True
            or bundle.get("non_text_objects_byte_identical_prior_trial") is not True):
            raise ValueError("duplicate/invalid 50-source resource")
        remotes.add(remote)
        for field in bundle.get("additional_unreviewed_fields", []):
            sid, source = field.get("path_source_sha256"), field.get("original")
            if (not isinstance(source, str) or sid != source_id(source)
                or sid in materialized):
                raise ValueError("materialized text source duplicate or wrong SHA")
            materialized[sid] = field
    if (len(remotes) != 47 or len(materialized) != 50
        or pilot.get("additional_unreviewed_source_unique") != 50
        or pilot.get("additional_unreviewed_text_fields") != 50
        or pilot.get("draft_deterministic_qa") != {"PASS": 48, "REVIEW": 2}
        or pilot.get("independent_review_complete") is not False
        or pilot.get("semantic_accuracy_verified") is not False
        or pilot.get("safe_to_mount_as_final_overlay") is not False
        or pilot.get("overlay_merge_authorized") is not False):
        raise ValueError("50-source pilot cardinality or review gate differs")

    output = []
    counts: Counter[str] = Counter()
    for sid, row in earlier.items():
        if (row.get("review_status") != "pending"
            or row.get("independent_review_complete") is not False
            or row.get("semantic_accuracy_verified") is not False
            or row.get("safe_to_mount_as_final_overlay") is not False
            or row.get("release_gate") != "needs_independent_review"):
            raise ValueError("v2 row was approved, cannot auto-merge")
        updated = dict(row)
        updated["prior_review_bucket_v2"] = row["review_bucket"]
        if sid in proposed:
            group, candidate = proposed[sid]
            if (row.get("review_bucket") != "qa_review_without_draft"
                or row.get("agent_correction_draft_unreviewed") is not None
                or row.get("candidate_for_human_review_unreviewed") !=
                    row.get("machine_candidate_unreviewed")
                or row.get("current_machine_qa_verdict") != "REVIEW"
                or row["source"] != candidate.get("source")
                or row.get("machine_candidate_unreviewed") !=
                    candidate.get("machine_candidate_unreviewed")
                or row.get("examples") != candidate.get("examples")
                or row.get("occurrences") != candidate.get("occurrences")
                or candidate.get("status") != "agent_draft_unreviewed"
                or candidate.get("review_status") != "pending"
                or candidate.get("independent_review_complete") is not False
                or candidate.get("semantic_accuracy_verified") is not False
                or candidate.get("safe_to_mount_as_final_overlay") is not False
                or candidate.get("release_gate") != "needs_independent_review"
                or candidate.get("draft_qa_verdict") != "PASS"
                or candidate.get("draft_qa_issues") != []):
                raise ValueError(f"new source draft is stale, invalid or approved: {sid}")
            if group == "unreviewed_previous_line_bleed":
                if (candidate.get("old_machine_QA_issues") !=
                    row.get("current_machine_qa_issues")
                    or candidate.get("duplicate_previous_subtitle_removed") is not True
                    or candidate.get("character_rendering_is_authoritative") is not False):
                    raise ValueError("previous-line-bleed evidence differs")
            elif (candidate.get("old_machine_QA_issues") !=
                  row.get("current_machine_qa_issues")
                  or candidate.get("old_machine_QA_verdict") != "REVIEW"):
                raise ValueError("six-contextual draft QA identity differs")
            trial_field = materialized.get(sid)
            if (trial_field is None or
                trial_field["original"] != candidate["source"] or
                trial_field["localized"] != candidate["translation_draft"]):
                raise ValueError(f"unreviewed source not present in 50-bundle trial: {sid}")
            updated["agent_correction_draft_unreviewed"] = candidate["translation_draft"]
            updated["agent_draft_source_group"] = group
            updated["agent_draft_deterministic_qa_verdict"] = "PASS"
            updated["agent_draft_deterministic_qa_issues"] = []
            updated["candidate_for_human_review_unreviewed"] = candidate["translation_draft"]
            updated["review_bucket"] = "qa_review_with_qa_pass_draft"
            if group == "unreviewed_previous_line_bleed":
                updated["previous_line_contamination_removed_unreviewed"] = True
                updated["character_rendering_is_authoritative"] = False
            else:
                updated["six_contextual_repair_reason"] = candidate["repair_reason"]
        elif sid in materialized:
            if (row.get("agent_correction_draft_unreviewed") !=
                    materialized[sid]["localized"]
                or row["source"] != materialized[sid]["original"]):
                raise ValueError("prior 43 source-bound draft differs from composite")
        updated["review_bucket_order"] = BUCKETS.index(updated["review_bucket"])
        counts[updated["review_bucket"]] += 1
        output.append(updated)
    if (set(proposed) - set(materialized)
        or len(proposed) != 7
        or counts != Counter(EXPECTED_BUCKET_COUNTS)):
        raise ValueError("new v3 1181-source review partition differs")
    output.sort(key=lambda r: (
        r["review_bucket_order"], -r["occurrences"], r["source_sha256"]
    ))
    drafted = [r for r in output if r["agent_correction_draft_unreviewed"] is not None]
    verdicts = Counter(r["agent_draft_deterministic_qa_verdict"] for r in drafted)
    if len(drafted) != 97 or verdicts != Counter({"PASS": 95, "REVIEW": 2}):
        raise ValueError("97 draft technical QA/reviewer population differs")
    return output, {
        "original_review_source_unique": len(output),
        "independent_semantic_reviews_still_required": len(output),
        "available_unreviewed_agent_drafts": len(drafted),
        "draft_deterministic_qa_verdicts": dict(verdicts),
        "qa_review_without_targeted_draft": EXPECTED_BUCKET_COUNTS["qa_review_without_draft"],
        "all_without_targeted_draft": len(output) - len(drafted),
        "review_buckets": dict(counts),
        "unchanged_machine_qa_verdicts": {
            "PASS": 273, "REVIEW": 860, "REJECT": 1, "MISSING_MACHINE": 47,
        },
        "new_in_v3": {
            "previous_line_bleed_unreviewed": 1,
            "six_contextual_unreviewed": 6,
        },
        "unreviewed_drafts_materialized_in_50_bundle_trial": len(materialized),
    }


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(AUDITS.resolve()):
        raise ValueError("v3 reviewer must stay inside isolated audits")
    if dest.exists():
        raise FileExistsError(f"immutable v3 reviewer already exists: {dest}")
    temporary = dest.with_name(dest.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"incomplete v3 reviewer already exists: {temporary}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200",
        asset_version="1077100", asset_index=INDEX,
    )
    for name, (path, sha) in INPUT_FILES.items():
        if sha_file(path) != sha:
            raise ValueError(f"frozen v3 source file drift: {name}")
    manifests = {}
    for name, (path, sha) in INPUT_MANIFESTS.items():
        if sha_file(path) != sha:
            raise ValueError(f"frozen v3 manifest drift: {name}")
        manifest = json.loads(path.read_text(encoding="utf8"))
        if (manifest.get("version_identity") != identity
            or manifest.get("independent_review_complete") is not False
            or manifest.get("safe_to_mount_as_final_overlay") is not False):
            raise ValueError(f"wrong version or auto-approved source: {name}")
        manifests[name] = manifest
    if (manifests["v2"].get("review_worklist_sha256") != INPUT_FILES["v2"][1]
        or manifests["bleed"].get("draft_sha256") != INPUT_FILES["bleed"][1]
        or manifests["six"].get("draft_sha256") != INPUT_FILES["six"][1]
        or manifests["trial50"].get("input_sha256", {}).get("v2") != INPUT_FILES["v2"][1]
        or manifests["trial50"].get("input_sha256", {}).get("bleed_draft") != INPUT_FILES["bleed"][1]
        or manifests["trial50"].get("input_sha256", {}).get("six_draft") != INPUT_FILES["six"][1]):
        raise ValueError("source/draft/technical-pilot manifest SHA chain broken")
    rows, counts = update_worklist(
        read_jsonl(INPUT_FILES["v2"][0]),
        read_jsonl(INPUT_FILES["bleed"][0]),
        read_jsonl(INPUT_FILES["six"][0]),
        manifests["trial50"],
    )
    temporary.mkdir(parents=True)
    work = temporary / "review-worklist.jsonl"
    with work.open("w", encoding="utf8", newline="\n") as writer:
        for row in rows:
            writer.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schema_version": 3,
        "kind": "event-unit-v3-source-bound-unified-review-ALL-UNREVIEWED",
        "version_identity": identity,
        "source_files_sha256": {k: digest for k, (_, digest) in INPUT_FILES.items()},
        "source_manifests_sha256": {k: digest for k, (_, digest) in INPUT_MANIFESTS.items()},
        "review_worklist_filename": work.name,
        "review_worklist_sha256": sha_file(work),
        "bucket_order": list(BUCKETS),
        "review_status": "pending",
        "release_gate": "needs_independent_review",
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
        "production_translations_modified": False,
        "old_QA_bundle_stages_modified": False,
        "official_original_assets_modified": False,
        "nas_modified": False,
        **counts,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    os.replace(temporary, dest)
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root", type=Path, default=DEST)
    args = p.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

