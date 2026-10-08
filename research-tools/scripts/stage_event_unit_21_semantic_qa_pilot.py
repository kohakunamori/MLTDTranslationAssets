#!/usr/bin/env python3
"""QA-only materialization of 21 source-bound unreviewed semantic corrections.

20 isolated original-source UnityFS bundles, preserving all 12,204 existing QA
changes byte-for-byte at command level; never auto-published or NAS-mounted.
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
from scripts.build_event_unit_semantic_repair_drafts import (
    DEST as DRAFT_ROOT, INPUT as REVIEW_FILE,
)
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE, BASE_MANIFEST, COHORT_BUNDLE_ROOT, COHORT_PATH, INDEX, SNAPSHOT,
    materialize, verify_trial_delta,
)

BUILD = ROOT / "build/localization-90200"
DRAFT_FILE = DRAFT_ROOT / "21-source-bound-semantic-correction-drafts.jsonl"
DRAFT_MANIFEST = DRAFT_ROOT / "manifest.json"
DEST = BUILD / "staging-event-unit-semantic-21-qa-only"
BASELINE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
DRAFT_SHA = "2c0c47d1088224a4cd24b40809b45dfc8ccbd7543f0a4889447b4b052adc32ca"


def validate_drafts(
    baseline: dict, draft_rows: list[dict], review_rows: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    if (baseline.get("release_gate") != "not_evaluated"
        or baseline.get("safe_to_mount_as_final_overlay") is not False
        or baseline.get("independent_reviewed") is not False
        or baseline.get("bundles_written") != 852
        or baseline.get("text_fields_changed") != 12204
        or len(baseline.get("bundles", [])) != 852):
        raise ValueError("existing non-release 852-bundle QA stage changed")
    review = {r["source_sha256"]: r for r in review_rows}
    if len(review) != len(review_rows) or len(review) != 860:
        raise ValueError("semantic review input does not match 860 sources")
    base_rows: dict[str, dict] = {}
    for bundle in baseline["bundles"]:
        for change in bundle["changes"]:
            sid, src, zh = (
                change["path_source_sha256"], change["original"], change["localized"]
            )
            if sid != source_id(src):
                raise ValueError("invalid existing QA command identity")
            prior = base_rows.setdefault(
                sid, {"source_sha256": sid, "source": src,
                      "translation": zh, "status": "QA-only-baseline"},
            )
            if prior["source"] != src or prior["translation"] != zh:
                raise ValueError("same source SHA has inconsistent baseline translation")
    selected = {}
    for item in draft_rows:
        sid, src = item.get("source_sha256"), item.get("source")
        if (not isinstance(src, str) or sid != source_id(src)
            or sid in base_rows or sid in selected or sid not in review
            or item.get("status") != "agent_draft_unreviewed"
            or item.get("review_status") != "pending"
            or item.get("release_gate") != "needs_independent_review"
            or item.get("safe_to_mount_as_final_overlay") is not False
            or item.get("independent_review_complete") is not False
            or item.get("semantic_accuracy_verified") is not False
            or item.get("draft_qa_verdict") != "PASS"
            or item.get("draft_qa_issues") != []
            or item.get("occurrences") != 1
            or len(item.get("examples", [])) != 1):
            raise ValueError(f"invalid semantic trial item: {sid}")
        original = review[sid]
        if (original["source"] != src
            or original["machine_candidate_unreviewed"] !=
                item.get("machine_candidate_unreviewed")
            or original["examples"] != item["examples"]
            or original["occurrences"] != item["occurrences"]
            or original["issues"] != item["prior_qa_issues"]
            or original["qa_verdict"] != item["prior_qa_verdict"]):
            raise ValueError(f"semantic trial original review differs: {sid}")
        qa = evaluate_row(
            {"source_sha256": sid, "source": src},
            {"source_sha256": sid, "source": src,
             "translation": item["translation_draft"],
             "status": "agent_draft_unreviewed"},
            load_glossary(None),
        )
        if qa["qa_verdict"] != "PASS" or qa["issues"]:
            raise ValueError(f"semantic trial draft QA no longer passes: {sid}")
        selected[sid] = item
    if len(selected) != 21:
        raise ValueError("expected exactly 21 distinct source-bound drafts")
    translations = {
        **base_rows,
        **{sid: {"source_sha256": sid, "source": item["source"],
                 "translation": item["translation_draft"],
                 "status": "agent_draft_unreviewed"}
           for sid, item in selected.items()},
    }
    return translations, selected


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if (not dest.is_relative_to(BUILD.resolve())
        or dest.is_relative_to((BUILD / "overlay").resolve())
        or dest.is_relative_to((ROOT / "work/local-assets").resolve())):
        raise ValueError("QA-only semantic pilot cannot write outside isolated build")
    if dest.exists():
        raise FileExistsError("semantic QA-only trial already exists")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError("incomplete semantic QA-only trial already exists")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    baseline = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf8"))
    draft_manifest = json.loads(DRAFT_MANIFEST.read_text(encoding="utf8"))
    if (sha_file(BASE_MANIFEST) != BASELINE_SHA
        or draft_manifest.get("version_identity") != identity
        or baseline.get("version_identity") != identity
        or draft_manifest.get("draft_sha256") != DRAFT_SHA
        or sha_file(DRAFT_FILE) != DRAFT_SHA
        or draft_manifest.get("source_review_queue_sha256") != sha_file(REVIEW_FILE)
        or draft_manifest.get("draft_unique") != 21
        or draft_manifest.get("draft_deterministic_qa_verdicts") != {"PASS": 21}
        or draft_manifest.get("independent_review_complete") is not False
        or draft_manifest.get("safe_to_mount_as_final_overlay") is not False
        or cohort.get("complete") is not True
        or cohort.get("verified") != 852
        or cohort.get("source_index_sha256") != identity["asset_index_sha256"]):
        raise ValueError("frozen semantic draft, stage or source cohort changed")
    translations, chosen = validate_drafts(
        baseline, read_jsonl(DRAFT_FILE), read_jsonl(REVIEW_FILE),
    )
    by_remote: dict[str, dict[str, dict]] = defaultdict(dict)
    for sid, row in chosen.items():
        by_remote[row["examples"][0]["remote"]][sid] = row
    if len(by_remote) != 20 or sum(len(v) for v in by_remote.values()) != 21:
        raise ValueError("expected 21 distinct draft source IDs in 20 original remotes")
    by_old = {x["remote"]: x for x in baseline["bundles"]}
    by_orig = {x["remote"]: x for x in cohort["bundles"]}
    if len(by_old) != 852 or len(by_orig) != 852:
        raise ValueError("duplicate remote entry in original/baseline cohort")
    temp.mkdir(parents=True)
    records = []
    for remote in sorted(by_remote):
        old = by_old.get(remote)
        original = by_orig.get(remote)
        if (old is None or original is None
            or old["logical"] != original["logical"]
            or old["source_sha256"] != original["sha256"]
            or old["original_bytes"] != original["declared_bytes"]):
            raise ValueError(f"frozen bundle source mismatch: {remote}")
        src_file = COHORT_BUNDLE_ROOT / remote
        old_file = BASE / "jp-android" / remote
        if (sha_file(src_file) != original["sha256"]
            or sha_file(old_file) != old["localized_sha256"]):
            raise ValueError(f"original/current QA bundle content drift: {remote}")
        out_file = temp / "jp-android" / remote
        built = materialize(
            original["logical"], remote, original["declared_bytes"],
            src_file, out_file, translations, require_complete=False,
        )
        new_only = verify_trial_delta(old, built, by_remote[remote])
        if len(new_only) != len(by_remote[remote]):
            raise ValueError("not all source-bound changes materialized")
        built["source_original_bundle_sha256"] = original["sha256"]
        built["old_qa_bundle_sha256"] = old["localized_sha256"]
        built["new_unreviewed_semantic_draft_fields"] = new_only
        built["output_path"] = str(dest / "jp-android" / remote)
        built["release_gate"] = "NOT_EVALUATED"
        records.append(built)
    report = {
        "schema_version": 1,
        "kind": "event-unit-21-unreviewed-semantic-QA-only-20-bundle-technical-pilot",
        "version_identity": identity,
        "base_852_manifest_sha256": sha_file(BASE_MANIFEST),
        "source_852_cohort_sha256": sha_file(COHORT_PATH),
        "source_21_draft_manifest_sha256": sha_file(DRAFT_MANIFEST),
        "source_21_drafts_sha256": sha_file(DRAFT_FILE),
        "source_860_review_sha256": sha_file(REVIEW_FILE),
        "existing_852_baseline_translated_fields_unchanged": 12204,
        "new_unreviewed_semantic_source_unique": 21,
        "new_unreviewed_semantic_text_fields": sum(
            len(row["new_unreviewed_semantic_draft_fields"]) for row in records
        ),
        "new_bundle_roundtrip_count": len(records),
        "draft_qa_verdicts": {"PASS": 21},
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "overlay_merge_authorized": False,
        "existing_852_QA_stage_modified": False,
        "production_translations_modified": False,
        "official_original_assets_modified": False,
        "nas_modified": False,
        "bundles": records,
    }
    (temp / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
    )
    os.replace(temp, dest)
    return {k: v for k, v in report.items() if k != "bundles"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

