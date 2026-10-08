#!/usr/bin/env python3
"""QA-only 31-source / 30-Unity-bundle composite of two disjoint trials.

Copies already verified exact output bundle bytes, never modifies 852 existing
QA bundles, production translation JSONL, official index, overlay or NAS.
All 31 semantic/name/numeric candidate translations remain UNREVIEWED.
"""
from __future__ import annotations

import argparse
import json
import shutil
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
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE, BASE_MANIFEST, INDEX, SNAPSHOT, verify_trial_delta,
)

BUILD = ROOT / "build/localization-90200"
TEN = BUILD / "staging-event-unit-qa-targeted-10-unreviewed-drafts"
TWENTY_ONE = BUILD / "staging-event-unit-semantic-21-qa-only"
UNIFIED = BUILD / "audits/event-unit-unified-review-client-9.0.200-assets-1077100"
UNIFIED_MANIFEST = UNIFIED / "manifest.json"
UNIFIED_WORKLIST = UNIFIED / "review-worklist.jsonl"
DEST = BUILD / "staging-event-unit-combined-31-unreviewed-drafts"

BASELINE_MANIFEST_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
TEN_MANIFEST_SHA = "b0fbc7c3038f0021cf5b925df2898fb291a55e034bc5ce9474e898458597a961"
TWENTY_ONE_MANIFEST_SHA = "b9e4c9d8c7d651d3debf86433583c307dad1b503a00deb3621649e27fee3b592"
UNIFIED_QUEUE_SHA = "c9c89a8337489cbee0d40f29248fdc7372f7c61b94f60c625753770bf64edc2e"


def validate_composite(
    baseline: dict, ten: dict, twenty_one: dict, reviewers: list[dict],
) -> tuple[list[dict], dict]:
    if (baseline.get("release_gate") != "not_evaluated"
        or baseline.get("independent_reviewed") is not False
        or baseline.get("safe_to_mount_as_final_overlay") is not False
        or len(baseline.get("bundles", [])) != 852
        or baseline.get("text_fields_changed") != 12204):
        raise ValueError("frozen 852 baseline QA candidate changed")
    if (ten.get("safe_to_mount_as_final_overlay") is not False
        or twenty_one.get("safe_to_mount_as_final_overlay") is not False
        or ten.get("overlay_merge_authorized") is not False
        or twenty_one.get("overlay_merge_authorized") is not False
        or ten.get("independent_review_complete") is not False
        or twenty_one.get("independent_review_complete") is not False
        or ten.get("semantic_accuracy_verified") is not False
        or twenty_one.get("semantic_accuracy_verified") is not False):
        raise ValueError("trial falsely claims acceptance or release authorization")
    if (ten.get("additional_unreviewed_source_unique") != 10
        or ten.get("additional_unreviewed_text_fields") != 10
        or ten.get("isolated_original_source_bundle_roundtrips") != 10
        or ten.get("new_draft_qa_verdicts") != {"PASS": 9, "REVIEW": 1}
        or twenty_one.get("new_unreviewed_semantic_source_unique") != 21
        or twenty_one.get("new_unreviewed_semantic_text_fields") != 21
        or twenty_one.get("new_bundle_roundtrip_count") != 20
        or twenty_one.get("draft_qa_verdicts") != {"PASS": 21}
        or len(ten["bundles"]) != 10
        or len(twenty_one["bundles"]) != 20):
        raise ValueError("frozen 10 and 21 trial cardinality changed")
    previous = {x["remote"]: x for x in baseline["bundles"]}
    if len(previous) != 852:
        raise ValueError("duplicate remote in original QA stage")
    reviewer_by_sid = {
        x["source_sha256"]: x for x in reviewers
    }
    if (len(reviewers) != 1181 or len(reviewer_by_sid) != 1181):
        raise ValueError("unified review source population changed")
    records = []
    source_ids: set[str] = set()
    remote_ids: set[str] = set()
    qa_counts: Counter[str] = Counter()
    sources_by_batch: Counter[str] = Counter()
    for origin_name, stage, change_key in (
        ("prior_10_name_numeric", ten, "new_unreviewed_draft_fields"),
        ("prior_21_contextual_semantic", twenty_one,
         "new_unreviewed_semantic_draft_fields"),
    ):
        for bundle in stage["bundles"]:
            remote = bundle["remote"]
            if (remote in remote_ids or remote not in previous
                or "/" in remote or "\\" in remote or not remote.endswith(".unity3d")):
                raise ValueError("source remote overlaps, escapes, or is unknown")
            remote_ids.add(remote)
            prior = previous[remote]
            if (bundle.get("old_QA_bundle_sha256",
                           bundle.get("old_qa_bundle_sha256")) !=
                        prior["localized_sha256"]
                or bundle["source_sha256"] != prior["source_sha256"]
                or bundle["logical"] != prior["logical"]
                or bundle.get("roundtrip_verified") is not True
                or bundle.get("non_text_objects_byte_identical") is not True
                or bundle.get("release_gate") != "NOT_EVALUATED"):
                raise ValueError("preexisting QA stage/source material drift")
            intended = {}
            for change in bundle[change_key]:
                sid = change["path_source_sha256"]
                reviewer = reviewer_by_sid.get(sid)
                if (reviewer is None or sid in source_ids
                    or sid != source_id(change["original"])
                    or reviewer["source"] != change["original"]
                    or reviewer["agent_correction_draft_unreviewed"] !=
                        change["localized"]
                    or reviewer["agent_draft_deterministic_qa_verdict"] not in (
                        "PASS", "REVIEW"
                    )
                    or reviewer["release_gate"] != "needs_independent_review"
                    or reviewer["review_status"] != "pending"
                    or reviewer["independent_review_complete"] is not False
                    or reviewer["semantic_accuracy_verified"] is not False
                    or reviewer["safe_to_mount_as_final_overlay"] is not False):
                    raise ValueError("unreviewed correction no longer source-bound")
                source_ids.add(sid)
                qa_counts[reviewer["agent_draft_deterministic_qa_verdict"]] += 1
                sources_by_batch[origin_name] += 1
                intended[sid] = {
                    "source": change["original"],
                    "translation_draft": change["localized"],
                }
            extras = verify_trial_delta(prior, bundle, intended)
            if len(extras) != len(intended):
                raise ValueError("prior trial changed an unexpected text field")
            records.append({
                "remote": remote,
                "logical": bundle["logical"],
                "source_trial": origin_name,
                "source_trial_bundle_sha256": bundle["localized_sha256"],
                "baseline_QA_bundle_sha256": prior["localized_sha256"],
                "original_bundle_sha256": bundle["source_sha256"],
                "roundtrip_verified_prior_trial": True,
                "non_text_objects_byte_identical_prior_trial": True,
                "additional_unreviewed_fields": extras,
                "release_gate": "NOT_EVALUATED",
            })
    if (len(records) != 30 or len(source_ids) != 31
        or qa_counts != Counter({"PASS": 30, "REVIEW": 1})
        or sources_by_batch != Counter({
            "prior_10_name_numeric": 10,
            "prior_21_contextual_semantic": 21,
        })):
        raise ValueError("unexpected combined 31-source/30-bundle delta")
    records.sort(key=lambda x: x["remote"])
    return records, {
        "existing_baseline_QA_text_fields_unchanged": 12204,
        "additional_source_unique": len(source_ids),
        "additional_text_fields": sum(
            len(x["additional_unreviewed_fields"]) for x in records
        ),
        "isolated_composite_bundle_count": len(records),
        "prior_trial_QA_verdicts": dict(qa_counts),
        "source_unique_by_prior_trial": dict(sources_by_batch),
    }


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if (not dest.is_relative_to(BUILD.resolve())
        or dest.is_relative_to((ROOT / "work/local-assets").resolve())):
        raise ValueError("combined QA-only folder must remain isolated in build")
    if dest.exists():
        raise FileExistsError(f"immutable combined QA candidate exists: {dest}")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError(f"incomplete combined QA candidate exists: {temp}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    ten_manifest_file = TEN / "manifest.json"
    twenty_one_manifest_file = TWENTY_ONE / "manifest.json"
    evidence = json.loads(UNIFIED_MANIFEST.read_text(encoding="utf8"))
    if (sha_file(BASE_MANIFEST) != BASELINE_MANIFEST_SHA
        or sha_file(ten_manifest_file) != TEN_MANIFEST_SHA
        or sha_file(twenty_one_manifest_file) != TWENTY_ONE_MANIFEST_SHA
        or sha_file(UNIFIED_WORKLIST) != UNIFIED_QUEUE_SHA
        or evidence.get("version_identity") != identity
        or evidence.get("review_worklist_sha256") != UNIFIED_QUEUE_SHA
        or evidence.get("still_requires_independent_semantic_review") != 1181
        or evidence.get("unreviewed_draft_unique") != 78
        or evidence.get("independent_review_complete") is not False
        or evidence.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("frozen unified review/stage identity changed")
    baseline = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    ten = json.loads(ten_manifest_file.read_text(encoding="utf8"))
    twenty_one = json.loads(
        twenty_one_manifest_file.read_text(encoding="utf8")
    )
    if any(
        x.get("version_identity") != identity
        for x in (baseline, ten, twenty_one)
    ):
        raise ValueError("client/assets version identities differ")
    records, counts = validate_composite(
        baseline, ten, twenty_one, read_jsonl(UNIFIED_WORKLIST),
    )
    temp.mkdir(parents=True)
    for row in records:
        source_root = (
            TEN if row["source_trial"] == "prior_10_name_numeric"
            else TWENTY_ONE
        )
        remote = row["remote"]
        old_file = BASE / "jp-android" / remote
        source_file = source_root / "jp-android" / remote
        if (sha_file(old_file) != row["baseline_QA_bundle_sha256"]
            or sha_file(source_file) != row["source_trial_bundle_sha256"]):
            raise ValueError(f"trial or original QA byte drift: {remote}")
        target = temp / "jp-android" / remote
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target)
        if sha_file(target) != row["source_trial_bundle_sha256"]:
            raise ValueError(f"isolated composite copy byte drift: {remote}")
        row["localized_sha256"] = sha_file(target)
        row["output_bytes"] = target.stat().st_size
        row["output_path"] = str(dest / "jp-android" / remote)
    report = {
        "schema_version": 1,
        "kind": "event-unit-combined-31-source-30-bundle-QA-only-NO-RELEASE",
        "version_identity": identity,
        "baseline_852_manifest_sha256": sha_file(BASE_MANIFEST),
        "prior_10_manifest_sha256": sha_file(ten_manifest_file),
        "prior_21_manifest_sha256": sha_file(twenty_one_manifest_file),
        "unified_review_manifest_sha256": sha_file(UNIFIED_MANIFEST),
        "unified_review_worklist_sha256": sha_file(UNIFIED_WORKLIST),
        **counts,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
        "overlay_merge_authorized": False,
        "release_gate": "not_evaluated",
        "production_translations_modified": False,
        "existing_852_QA_bundles_modified": False,
        "original_official_assets_modified": False,
        "nas_modified": False,
        "bundles": records,
    }
    (temp / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    temp.rename(dest)
    return {k: v for k, v in report.items() if k != "bundles"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

