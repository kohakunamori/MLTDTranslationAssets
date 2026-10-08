#!/usr/bin/env python3
"""Combine all 43 unreviewed Event-unit drafts into 40 isolated QA-only bundles.

39 disjoint trial bundle files copied SHA-exact. One overlapping remote is
REBUILT from frozen original UnityFS with BOTH source-bound corrections; never
overwrite one draft with another. No producer/release/NAS/device changes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_event_unit_review_pack import sha_file
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE, BASE_MANIFEST, COHORT_BUNDLE_ROOT, COHORT_PATH, INDEX, SNAPSHOT,
    materialize, verify_trial_delta,
)

BUILD = ROOT / "build/localization-90200"
PRIOR = BUILD / "staging-event-unit-combined-31-unreviewed-drafts"
NEW = BUILD / "staging-event-unit-followup-12-qa-only"
DRAFT_ROOT = BUILD / "audits/event-unit-followup-12-semantic-drafts-client-9.0.200-assets-1077100"
DRAFT_FILE = DRAFT_ROOT / "12-source-bound-corrections.jsonl"
UNIFIED = BUILD / "audits/event-unit-unified-review-client-9.0.200-assets-1077100"
UNIFIED_WORKLIST = UNIFIED / "review-worklist.jsonl"
DEST = BUILD / "staging-event-unit-combined-43-unreviewed-drafts"

BASE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
PRIOR_SHA = "40ecff6685f7b02fa89b5db80d99d2a261dd38cc48d25d4801365ca78ee528a4"
FOLLOWUP_SHA = "a88a214aebed0ee5bc7d82f2fb5b12da9e8ed13524734d49a1543cfdac331f1a"
DRAFT_SHA = "8109fe152769052a959cc66156e4697e711e0b51dd9ffc66d148cb7e5c4868d9"


def validate_43(
    baseline: dict, old_stage: dict, new_stage: dict,
    old_worklist: list[dict], new_drafts: list[dict],
) -> tuple[dict[str, dict], dict[str, dict], dict, list[str]]:
    if (baseline.get("text_fields_changed") != 12204
        or baseline.get("release_gate") != "not_evaluated"
        or baseline.get("safe_to_mount_as_final_overlay") is not False
        or baseline.get("independent_reviewed") is not False
        or len(baseline.get("bundles", [])) != 852):
        raise ValueError("frozen 852 QA stage no longer pending")
    if (old_stage.get("additional_source_unique") != 31
        or old_stage.get("isolated_composite_bundle_count") != 30
        or old_stage.get("prior_trial_QA_verdicts") !=
            {"PASS": 30, "REVIEW": 1}
        or new_stage.get("additional_unreviewed_source_unique") != 12
        or new_stage.get("roundtrip_bundle_count") != 11
        or new_stage.get("qa_verdicts") != {"PASS": 11, "REVIEW": 1}
        or len(old_stage.get("bundles", [])) != 30
        or len(new_stage.get("bundles", [])) != 11):
        raise ValueError("31+12 technical trial population changed")
    for m in (old_stage, new_stage):
        if (m.get("independent_review_complete") is not False
            or m.get("semantic_accuracy_verified") is not False
            or m.get("safe_to_mount_as_final_overlay") is not False
            or m.get("overlay_merge_authorized") is not False):
            raise ValueError("prior technical trial falsely claims release approval")
    work = {x["source_sha256"]: x for x in old_worklist}
    followup = {x["source_sha256"]: x for x in new_drafts}
    if (len(work) != len(old_worklist) or len(work) != 1181
        or len(followup) != len(new_drafts) or len(followup) != 12):
        raise ValueError("duplicate or missing source-bound reviewer/draft")
    by_baseline = {x["remote"]: x for x in baseline["bundles"]}
    if len(by_baseline) != 852:
        raise ValueError("duplicate original QA bundle")
    old_by_remote = {x["remote"]: x for x in old_stage["bundles"]}
    new_by_remote = {x["remote"]: x for x in new_stage["bundles"]}
    if (len(old_by_remote) != 30 or len(new_by_remote) != 11
        or not set(old_by_remote).issubset(by_baseline)
        or not set(new_by_remote).issubset(by_baseline)):
        raise ValueError("unexpected remote or duplicate prior trial remote")
    overlap = sorted(set(old_by_remote) & set(new_by_remote))
    if (len(overlap) != 1 or
        overlap != new_stage.get("prior_31_overlapping_remotes")):
        raise ValueError("expected exactly one overlapping 31/12 Unity bundle")
    translations: dict[str, dict] = {}
    for b in baseline["bundles"]:
        for item in b["changes"]:
            sid, src, zh = (
                item["path_source_sha256"], item["original"], item["localized"]
            )
            if sid != source_id(src):
                raise ValueError("baseline source identity changed")
            prev = translations.setdefault(
                sid, {"source_sha256": sid, "source": src,
                      "translation": zh, "status": "QA-only-baseline"},
            )
            if prev["source"] != src or prev["translation"] != zh:
                raise ValueError("baseline conflicting duplicate source translation")
    extras: dict[str, dict[str, dict]] = defaultdict(dict)
    qa_count: Counter[str] = Counter()
    unique_sids = set()
    for batch, manifest, key in (
        ("previous_31", old_stage, "additional_unreviewed_fields"),
        ("followup_12", new_stage, "new_unreviewed_draft_fields"),
    ):
        for bundle in manifest["bundles"]:
            remote = bundle["remote"]
            prior = by_baseline[remote]
            if (bundle.get("roundtrip_verified",
                           bundle.get("roundtrip_verified_prior_trial")) is not True
                or bundle.get("non_text_objects_byte_identical",
                              bundle.get("non_text_objects_byte_identical_prior_trial"))
                    is not True
                or bundle.get("release_gate") != "NOT_EVALUATED"
                or (batch == "followup_12" and
                    bundle["source_sha256"] != prior["source_sha256"])
                or (batch == "previous_31" and
                    bundle["original_bundle_sha256"] != prior["source_sha256"])):
                raise ValueError("followup source bundle/roundtrip drift")
            expected = {}
            for change in bundle[key]:
                sid, src, zh = (
                    change["path_source_sha256"], change["original"],
                    change["localized"],
                )
                evidence = work.get(sid) if batch == "previous_31" else followup.get(sid)
                if (sid in unique_sids or sid in translations
                    or sid != source_id(src) or evidence is None
                    or evidence["source"] != src
                    or (batch == "previous_31" and
                        evidence.get("agent_correction_draft_unreviewed") != zh)
                    or (batch == "followup_12" and
                        evidence.get("translation_draft") != zh)
                    or evidence.get("independent_review_complete") is not False
                    or evidence.get("semantic_accuracy_verified") is not False
                    or evidence.get("safe_to_mount_as_final_overlay") is not False):
                    raise ValueError("candidate source stale, duplicated or already approved")
                if batch == "previous_31":
                    qa = evidence["agent_draft_deterministic_qa_verdict"]
                    if evidence.get("review_status") != "pending":
                        raise ValueError("old review status changed")
                else:
                    qa = evidence["draft_qa_verdict"]
                    if evidence.get("status") != "agent_draft_unreviewed":
                        raise ValueError("followup draft status changed")
                if qa not in ("PASS", "REVIEW"):
                    raise ValueError("candidate technical QA invalid")
                qa_count[qa] += 1
                unique_sids.add(sid)
                extras[remote][sid] = {
                    "source": src, "translation_draft": zh,
                    "source_batch": batch, "qa_verdict": qa,
                }
                expected[sid] = {"source": src, "translation_draft": zh}
                translations[sid] = {
                    "source_sha256": sid, "source": src,
                    "translation": zh, "status": "agent_draft_unreviewed",
                }
            if batch == "previous_31":
                if sum(1 for x in bundle[key] if x["path_source_sha256"] in expected) != len(expected):
                    raise ValueError("old trial delta corrupted")
                old_entry = by_baseline[remote]
                before = {(x["command_index"], x["path_source_sha256"]): x
                          for x in old_entry["changes"]}
                # Composite 31 manifest uses a list of source-only extra
                # changes and a stored verified prior-stage non-text flag.
                for change in bundle[key]:
                    k = (change["command_index"], change["path_source_sha256"])
                    if k in before:
                        raise ValueError("old composite touches old QA command")
            else:
                verify_trial_delta(by_baseline[remote], bundle, expected)
    if (len(unique_sids) != 43
        or len(extras) != 40
        or sum(len(x) for x in extras.values()) != 43
        or qa_count != Counter({"PASS": 41, "REVIEW": 2})
        or len(extras[overlap[0]]) != 2):
        raise ValueError("combined candidate count or overlap changed")
    return by_baseline, extras, translations, overlap


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(BUILD.resolve()):
        raise ValueError("QA-only combined output outside build")
    if dest.exists():
        raise FileExistsError("immutable 43-source QA-only output exists")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError("incomplete 43-source QA-only output exists")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    old_path = PRIOR / "manifest.json"
    new_path = NEW / "manifest.json"
    if (sha_file(BASE_MANIFEST) != BASE_SHA
        or sha_file(old_path) != PRIOR_SHA
        or sha_file(new_path) != FOLLOWUP_SHA
        or sha_file(DRAFT_FILE) != DRAFT_SHA):
        raise ValueError("frozen prior QA/correction manifest checksum changed")
    baseline = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    prior = json.loads(old_path.read_text(encoding="utf8"))
    current = json.loads(new_path.read_text(encoding="utf8"))
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf8"))
    if (any(item.get("version_identity") != identity
            for item in (baseline, prior, current))
        or cohort.get("verified") != 852
        or cohort.get("source_index_sha256") != identity["asset_index_sha256"]
        or prior.get("baseline_852_manifest_sha256") != BASE_SHA
        or current.get("baseline_852_manifest_sha256") != BASE_SHA):
        raise ValueError("version/cohort mismatches frozen 1077100")
    by_old, extra, translations, overlap = validate_43(
        baseline, prior, current, read_jsonl(UNIFIED_WORKLIST),
        read_jsonl(DRAFT_FILE),
    )
    cohort_by_remote = {x["remote"]: x for x in cohort["bundles"]}
    records = []
    temp.mkdir(parents=True)
    for remote in sorted(extra):
        old = by_old[remote]
        old_path_in = BASE / "jp-android" / remote
        if sha_file(old_path_in) != old["localized_sha256"]:
            raise ValueError("baseline 852 QA bundle checksum drift")
        dst = temp / "jp-android" / remote
        dst.parent.mkdir(parents=True, exist_ok=True)
        sources = extra[remote]
        if remote in overlap:
            original = cohort_by_remote[remote]
            src_file = COHORT_BUNDLE_ROOT / remote
            if (original["logical"] != old["logical"]
                or original["sha256"] != old["source_sha256"]
                or sha_file(src_file) != original["sha256"]):
                raise ValueError("overlapping remote original provenance drift")
            built = materialize(
                original["logical"], remote, original["declared_bytes"],
                src_file, dst, translations, require_complete=False,
            )
            confirmed = verify_trial_delta(old, built, sources)
            if len(confirmed) != 2:
                raise ValueError("shared remote has lost one of its two edits")
            mode = "original_rebuild_both_drafts"
            non_text = built["non_text_objects_byte_identical"]
            roundtrip = built["roundtrip_verified"]
            output_sha = built["localized_sha256"]
        else:
            if remote in {x["remote"] for x in prior["bundles"]}:
                root = PRIOR
                prev_record = next(x for x in prior["bundles"] if x["remote"] == remote)
                output_sha = prev_record["localized_sha256"]
                non_text = prev_record["non_text_objects_byte_identical_prior_trial"]
                roundtrip = prev_record["roundtrip_verified_prior_trial"]
                mode = "sha_exact_copy_from_prior_31"
            else:
                root = NEW
                prev_record = next(x for x in current["bundles"] if x["remote"] == remote)
                output_sha = prev_record["localized_sha256"]
                non_text = prev_record["non_text_objects_byte_identical"]
                roundtrip = prev_record["roundtrip_verified"]
                mode = "sha_exact_copy_from_followup_12"
            source_path = root / "jp-android" / remote
            if sha_file(source_path) != output_sha:
                raise ValueError("source technical trial changed after QA")
            shutil.copyfile(source_path, dst)
            if sha_file(dst) != output_sha:
                raise ValueError("combined byte copy changed QA trial")
            confirmed = [
                {"path_source_sha256": sid,
                 "original": x["source"],
                 "localized": x["translation_draft"]}
                for sid, x in sorted(sources.items())
            ]
        if not roundtrip or not non_text:
            raise ValueError("Unity QA-only output no longer roundtrip safe")
        records.append({
            "remote": remote, "logical": old["logical"],
            "original_bundle_sha256": old["source_sha256"],
            "baseline_852_bundle_sha256": old["localized_sha256"],
            "localized_sha256": output_sha,
            "output_bytes": dst.stat().st_size,
            "output_path": str(dest / "jp-android" / remote),
            "source_mode": mode,
            "roundtrip_verified": True,
            "non_text_objects_byte_identical": True,
            "additional_unreviewed_fields": confirmed,
            "release_gate": "NOT_EVALUATED",
        })
    if (len(records) != 40
        or Counter(x["source_mode"] for x in records) != Counter({
            "sha_exact_copy_from_prior_31": 29,
            "sha_exact_copy_from_followup_12": 10,
            "original_rebuild_both_drafts": 1,
        })):
        raise ValueError("43 source/40 bundle mode unexpected")
    manifest = {
        "schema_version": 1,
        "kind": "event-unit-43-unreviewed-source-40-bundle-QA-only-rebuild-overlap",
        "version_identity": identity,
        "baseline_852_manifest_sha256": sha_file(BASE_MANIFEST),
        "prior_31_manifest_sha256": sha_file(PRIOR / "manifest.json"),
        "followup_12_manifest_sha256": sha_file(NEW / "manifest.json"),
        "followup_12_draft_sha256": sha_file(DRAFT_FILE),
        "unified_review_worklist_sha256": sha_file(UNIFIED_WORKLIST),
        "source_852_cohort_sha256": sha_file(COHORT_PATH),
        "original_12204_QA_text_fields_preserved": 12204,
        "additional_unreviewed_source_unique": 43,
        "additional_unreviewed_text_fields": 43,
        "bundle_count": len(records),
        "draft_QA_verdicts": {"PASS": 41, "REVIEW": 2},
        "overlap_remotes_rebuilt_from_original": overlap,
        "copied_from_prior_31": 29,
        "copied_from_followup_12": 10,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "overlay_merge_authorized": False,
        "production_translations_modified": False,
        "prior_852_QA_bundles_modified": False,
        "prior_31_QA_bundles_modified": False,
        "prior_12_QA_bundles_modified": False,
        "official_original_assets_modified": False,
        "nas_modified": False,
        "bundles": records,
    }
    (temp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    os.replace(temp, dest)
    return {key: value for key, value in manifest.items() if key != "bundles"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root", type=Path, default=DEST)
    args = p.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

