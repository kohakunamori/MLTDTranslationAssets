#!/usr/bin/env python3
"""Bundle all 50 unreviewed Event-unit corrections in 47 disjoint UnityFS files.

43-source stage + one prior subtitle-bleed fix + six contextual corrections.
Each source has already roundtripped and is copied SHA-exact. No release.
"""
from __future__ import annotations

import argparse
import json
import os
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
    BASE, BASE_MANIFEST, INDEX, SNAPSHOT,
)

BUILD = ROOT / "build/localization-90200"
FORTY_THREE = BUILD / "staging-event-unit-combined-43-unreviewed-drafts"
BLEED = BUILD / "staging-event-unit-previous-line-bleed-one-qa-only"
SIX = BUILD / "staging-event-unit-six-contextual-qa-only"
DRAFT_ONE = BUILD / "audits/event-unit-previous-line-bleed-draft-client-9.0.200-assets-1077100/one-source-bound-correction.jsonl"
DRAFT_SIX = BUILD / "audits/event-unit-six-contextual-corrections-client-9.0.200-assets-1077100/six-source-bound-corrections.jsonl"
V2 = BUILD / "audits/event-unit-unified-review-v2-client-9.0.200-assets-1077100/review-worklist.jsonl"
DEST = BUILD / "staging-event-unit-combined-50-unreviewed-drafts"

INPUT_SHA = {
    "43": "bf4f5b113e663c2b840c5e516853ae06c6f6292c604c0d2775f6e7b4638c6dbc",
    "bleed": "a96f81789142b1b55ba2de62e27b9c30357a74bc9b6a9690e54a887d40803683",
    "six": "6a8a57325e6bc1992b1079726175b7af9df3d96b926df4ebabb767dd1885e73a",
    "bleed_draft": "ccad273838fa5631ec57028cc60942a12029626b8ca744065c5feaa0261ac725",
    "six_draft": "9800e4606848f10acfe51d393477d4cc8188016ce40c4510db88199e9b6f2885",
    "v2": "61a7da214fbdcb905ee947a8894c5b306e6bb5f3de326c1161ee8d877b367927",
}
BASE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"


def normalize_stages(
    old: dict, stage_43: dict, stage_bleed: dict, stage_six: dict,
    reviewer: list[dict], one: list[dict], six: list[dict],
) -> tuple[list[tuple[str, Path, dict, list[dict]]], dict]:
    if (old.get("text_fields_changed") != 12204
        or old.get("bundles_written") != 852
        or old.get("safe_to_mount_as_final_overlay") is not False
        or old.get("independent_reviewed") is not False
        or len(old.get("bundles", [])) != 852):
        raise ValueError("frozen 852 original QA-stage changed")
    review = {r["source_sha256"]: r for r in reviewer}
    expected = {r["source_sha256"]: r for r in one + six}
    if (len(review) != 1181 or len(review) != len(reviewer)
        or len(one) != 1 or len(six) != 6
        or len(expected) != 7 or len(expected) != len(one + six)):
        raise ValueError("source-bound draft/reviewer universe changed")
    old_by_remote = {b["remote"]: b for b in old["bundles"]}
    if len(old_by_remote) != 852:
        raise ValueError("original 852 remote duplication")
    previous = [
        ("previous_43", FORTY_THREE, stage_43, stage_43.get("bundles", []),
         {"PASS": 41, "REVIEW": 2}, 43, 40),
        ("previous_line_bleed", BLEED, stage_bleed,
         [stage_bleed["bundle"]] if "bundle" in stage_bleed else [],
         {"PASS": 1}, 1, 1),
        ("contextual_six", SIX, stage_six, stage_six.get("bundles", []),
         {"PASS": 6}, 6, 6),
    ]
    seen_remote, seen_source = set(), set()
    combined = []
    counts = Counter()
    by_batch = Counter()
    for group, root, manifest, bundles, qa_counts, sources, remotes in previous:
        if (manifest.get("independent_review_complete") is not False
            or manifest.get("semantic_accuracy_verified") is not False
            or manifest.get("safe_to_mount_as_final_overlay") is not False
            or manifest.get("overlay_merge_authorized") is not False
            or len(bundles) != remotes
            or manifest.get("additional_unreviewed_source_unique") != sources):
            raise ValueError(f"stale/releasable prior trial: {group}")
        if group == "previous_43" and manifest.get("draft_QA_verdicts") != qa_counts:
            raise ValueError("previous 43 QA count changed")
        if group == "previous_line_bleed" and manifest.get("technical_qa_verdicts") != qa_counts:
            raise ValueError("previous-line QA count changed")
        if group == "contextual_six" and manifest.get("draft_deterministic_qa") != qa_counts:
            raise ValueError("six-source QA count changed")
        for bundle in bundles:
            remote = bundle["remote"]
            if (not remote.endswith(".unity3d")
                or "/" in remote or "\\" in remote
                or remote in seen_remote or remote not in old_by_remote):
                raise ValueError(f"duplicate/escaped/unknown original remote: {remote}")
            seen_remote.add(remote)
            baseline = old_by_remote[remote]
            if (bundle.get("original_bundle_sha256",
                            bundle.get("original_sha256")) != baseline["source_sha256"]
                or bundle.get("baseline_852_bundle_sha256",
                              bundle.get("baseline_852_qa_sha256",
                                         bundle.get("baseline_QA_bundle_sha256")))
                    != baseline["localized_sha256"]
                or bundle.get("roundtrip_verified") is not True
                or bundle.get("non_text_objects_byte_identical") is not True
                or bundle.get("release_gate", "NOT_EVALUATED") != "NOT_EVALUATED"):
                raise ValueError(f"prior 852 resource/roundtrip evidence differs: {remote}")
            fields = bundle["additional_unreviewed_fields"]
            if not fields:
                raise ValueError("empty trial delta not expected")
            for change in fields:
                sid, jp, zh = (
                    change["path_source_sha256"], change["original"],
                    change["localized"],
                )
                if sid in seen_source or sid != source_id(jp):
                    raise ValueError("duplicate or mismatched Japanese source SHA")
                seen_source.add(sid)
                audit = review.get(sid) if group == "previous_43" else expected.get(sid)
                if (audit is None or audit["source"] != jp
                    or audit.get("independent_review_complete") is not False
                    or audit.get("semantic_accuracy_verified") is not False
                    or audit.get("safe_to_mount_as_final_overlay") is not False):
                    raise ValueError("prior source audit not unreviewed")
                if group == "previous_43":
                    if (audit.get("agent_correction_draft_unreviewed") != zh
                        or audit.get("review_status") != "pending"):
                        raise ValueError("43-bundle translation changed")
                    status = audit["agent_draft_deterministic_qa_verdict"]
                else:
                    if (audit.get("translation_draft") != zh
                        or audit.get("status") != "agent_draft_unreviewed"):
                        raise ValueError("new source-bound translation drift")
                    status = audit["draft_qa_verdict"]
                    v2row = review.get(sid)
                    if (v2row is None or
                        v2row.get("review_bucket") != "qa_review_without_draft"
                        or v2row.get("source") != jp
                        or v2row.get("agent_correction_draft_unreviewed") is not None):
                        raise ValueError("new source was already drafted in v2")
                if status not in ("PASS", "REVIEW"):
                    raise ValueError("non-reviewable technical QA status")
                counts[status] += 1
                by_batch[group] += 1
            combined.append((group, root, bundle, fields))
    if (len(combined) != 47 or len(seen_source) != 50
        or counts != Counter({"PASS": 48, "REVIEW": 2})
        or by_batch != Counter({
            "previous_43": 43, "previous_line_bleed": 1,
            "contextual_six": 6,
        })):
        raise ValueError("50 sources/47 distinct bundles or QA counts changed")
    return sorted(combined, key=lambda x: x[2]["remote"]), {
        "baseline_12204_QA_text_fields_unchanged": 12204,
        "additional_unreviewed_source_unique": len(seen_source),
        "additional_unreviewed_text_fields": sum(len(x[3]) for x in combined),
        "bundle_count": len(combined),
        "draft_deterministic_qa": dict(counts),
        "source_counts_by_trial": dict(by_batch),
    }


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(BUILD.resolve()):
        raise ValueError("QA-only candidate must be isolated inside build")
    if dest.exists():
        raise FileExistsError(f"immutable 50-source QA candidate exists: {dest}")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError(f"incomplete 50-source QA candidate exists: {temp}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200",
        asset_version="1077100", asset_index=INDEX,
    )
    paths = {
        "43": FORTY_THREE / "manifest.json",
        "bleed": BLEED / "manifest.json",
        "six": SIX / "manifest.json",
        "bleed_draft": DRAFT_ONE,
        "six_draft": DRAFT_SIX,
        "v2": V2,
    }
    for name, path in paths.items():
        expected = INPUT_SHA[name]
        if expected and sha_file(path) != expected:
            raise ValueError(f"frozen {name} evidence changed: {path}")
    if sha_file(BASE_MANIFEST) != BASE_SHA:
        raise ValueError("frozen 852-bundle QA stage SHA changed")
    original = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    stages = {
        name: json.loads(paths[name].read_text(encoding="utf8"))
        for name in ("43", "bleed", "six")
    }
    if any(m.get("version_identity") != identity for m in [original, *stages.values()]):
        raise ValueError("client+assets version different among stages")
    rows, summary = normalize_stages(
        original, stages["43"], stages["bleed"], stages["six"],
        read_jsonl(V2), read_jsonl(DRAFT_ONE), read_jsonl(DRAFT_SIX),
    )
    temp.mkdir(parents=True)
    entries = []
    for group, root, bundle, changes in rows:
        remote = bundle["remote"]
        infile = root / "jp-android" / remote
        if sha_file(infile) != bundle["localized_sha256"]:
            raise ValueError(f"{group} source-bundle bytes changed: {remote}")
        if sha_file(BASE / "jp-android" / remote) != (
            original["bundles"][next(i for i, x in enumerate(original["bundles"])
                                    if x["remote"] == remote)]["localized_sha256"]
        ):
            raise ValueError("prior 852 QA source-bundle bytes changed")
        output = temp / "jp-android" / remote
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(infile, output)
        if sha_file(output) != bundle["localized_sha256"]:
            raise ValueError(f"isolated composite byte copy drift: {remote}")
        entries.append({
            "remote": remote,
            "logical": bundle["logical"],
            "source_trial": group,
            "original_bundle_sha256": bundle.get(
                "original_bundle_sha256", bundle.get("original_sha256")
            ),
            "baseline_852_qa_sha256": bundle.get(
                "baseline_852_bundle_sha256",
                bundle.get("baseline_852_qa_sha256",
                           bundle.get("baseline_QA_bundle_sha256")),
            ),
            "localized_sha256": bundle["localized_sha256"],
            "output_bytes": output.stat().st_size,
            "output_path": str(dest / "jp-android" / remote),
            "roundtrip_verified_prior_trial": True,
            "non_text_objects_byte_identical_prior_trial": True,
            "additional_unreviewed_fields": changes,
            "release_gate": "NOT_EVALUATED",
        })
    report = {
        "schema_version": 1,
        "kind": "event-unit-50-source-47-bundle-composite-QA-only-NO-RELEASE",
        "version_identity": identity,
        "baseline_852_manifest_sha256": sha_file(BASE_MANIFEST),
        "input_sha256": {k: sha_file(p) for k, p in paths.items()},
        **summary,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "overlay_merge_authorized": False,
        "production_translations_modified": False,
        "prior_852_and_other_QA_stages_modified": False,
        "official_original_assets_modified": False,
        "nas_modified": False,
        "bundles": entries,
    }
    (temp / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
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

