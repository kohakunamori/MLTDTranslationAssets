#!/usr/bin/env python3
"""One isolated UnityFS roundtrip for proven duplicated previous dialogue.

The previous complete subtitle is deleted only from this one source-bound
unreviewed draft. Does not modify 852 baseline, 43-bundle QA trial, producer,
official game assets, release/NAS, or original 1181 reviewer queue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_previous_line_bleed_draft import (
    DEST as DRAFT_DIR, QA_AUDIT, WORKLIST, TARGET_SHA,
)
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE, BASE_MANIFEST, COHORT_PATH, COHORT_BUNDLE_ROOT,
    INDEX, SNAPSHOT, materialize, verify_trial_delta,
)

BUILD = ROOT / "build/localization-90200"
DRAFT_FILE = DRAFT_DIR / "one-source-bound-correction.jsonl"
DRAFT_MANIFEST = DRAFT_DIR / "manifest.json"
PRIOR = BUILD / "staging-event-unit-combined-43-unreviewed-drafts"
DEST = BUILD / "staging-event-unit-previous-line-bleed-one-qa-only"
BASE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
DRAFT_SHA = "ccad273838fa5631ec57028cc60942a12029626b8ca744065c5feaa0261ac725"


def stage(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(BUILD.resolve()):
        raise ValueError("isolated QA-only bundle cannot leave build")
    if dest.exists():
        raise FileExistsError("immutable previous-dialogue QA bundle exists")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError("unfinished isolated previous-dialogue bundle exists")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    draft_manifest = json.loads(DRAFT_MANIFEST.read_text(encoding="utf8"))
    baseline = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    prior = json.loads((PRIOR / "manifest.json").read_text(encoding="utf8"))
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf8"))
    drafts = read_jsonl(DRAFT_FILE)
    if (sha_file(BASE_MANIFEST) != BASE_SHA
        or sha_file(DRAFT_FILE) != DRAFT_SHA
        or draft_manifest.get("draft_sha256") != DRAFT_SHA
        or any(x.get("version_identity") != identity
               for x in (draft_manifest, baseline, prior))
        or draft_manifest.get("independent_review_complete") is not False
        or draft_manifest.get("safe_to_mount_as_final_overlay") is not False
        or baseline.get("text_fields_changed") != 12204
        or baseline.get("safe_to_mount_as_final_overlay") is not False
        or prior.get("additional_unreviewed_source_unique") != 43
        or prior.get("safe_to_mount_as_final_overlay") is not False
        or len(cohort.get("bundles", [])) != 852
        or cohort.get("source_index_sha256") != identity["asset_index_sha256"]
        or len(drafts) != 1):
        raise ValueError("original/QA/draft versions or statuses no longer match")
    item = drafts[0]
    sid, source = item.get("source_sha256"), item.get("source")
    if (sid != TARGET_SHA or sid != source_id(source)
        or item.get("draft_qa_verdict") != "PASS"
        or item.get("draft_qa_issues") != []
        or item.get("status") != "agent_draft_unreviewed"
        or item.get("review_status") != "pending"
        or item.get("release_gate") != "needs_independent_review"
        or item.get("independent_review_complete") is not False
        or item.get("semantic_accuracy_verified") is not False
        or item.get("safe_to_mount_as_final_overlay") is not False
        or item.get("occurrences") != 1
        or len(item.get("examples", [])) != 1):
        raise ValueError("one-source repair is no longer source-bound, QA-only")
    qa = evaluate_row(
        {"source": source, "source_sha256": sid},
        {"source": source, "source_sha256": sid,
         "translation": item["translation_draft"],
         "status": "agent_draft_unreviewed"}, load_glossary(None),
    )
    if qa["qa_verdict"] != "PASS" or qa["issues"]:
        raise ValueError("source-bound previous-dialogue repair no longer QA PASS")
    remote = item["examples"][0]["remote"]
    original = {b["remote"]: b for b in cohort["bundles"]}[remote]
    before = {b["remote"]: b for b in baseline["bundles"]}[remote]
    if remote in {b["remote"] for b in prior["bundles"]}:
        raise ValueError("single repair overlaps combined 43; requires full merged rebuild")
    if (original["logical"] != before["logical"]
        or original["sha256"] != before["source_sha256"]
        or original["declared_bytes"] != before["original_bytes"]
        or sha_file(COHORT_BUNDLE_ROOT / remote) != original["sha256"]
        or sha_file(BASE / "jp-android" / remote) != before["localized_sha256"]):
        raise ValueError("source/base 852 Unity bundle drifted")
    translations: dict[str, dict] = {}
    for b in baseline["bundles"]:
        for change in b["changes"]:
            src = change["original"]
            key = change["path_source_sha256"]
            zh = change["localized"]
            if key != source_id(src):
                raise ValueError("old QA translation no longer original-source bound")
            saved = translations.setdefault(
                key, {"source_sha256": key, "source": src,
                      "translation": zh, "status": "baseline-QA-only"}
            )
            if saved["source"] != src or saved["translation"] != zh:
                raise ValueError("old source has inconsistent baseline translations")
    if sid in translations:
        raise ValueError("contaminated subtitle source already replaced by QA baseline")
    translations[sid] = {
        "source_sha256": sid, "source": source,
        "translation": item["translation_draft"],
        "status": "agent_draft_unreviewed",
    }
    temp.mkdir(parents=True)
    file = temp / "jp-android" / remote
    staged = materialize(
        original["logical"], remote, original["declared_bytes"],
        COHORT_BUNDLE_ROOT / remote, file, translations,
        require_complete=False,
    )
    changes = verify_trial_delta(
        before, staged, {sid: {"source": source,
                              "translation_draft": item["translation_draft"]}},
    )
    if len(changes) != 1 or changes[0]["path_source_sha256"] != sid:
        raise ValueError("unexpected changed source in one-bundle repair")
    result = {
        "schema_version": 1,
        "kind": "event-unit-one-previous-dialogue-bleed-Unity-QA-only",
        "version_identity": identity,
        "source_852_manifest_sha256": sha_file(BASE_MANIFEST),
        "source_852_cohort_sha256": sha_file(COHORT_PATH),
        "source_one_draft_manifest_sha256": sha_file(DRAFT_MANIFEST),
        "source_one_draft_sha256": sha_file(DRAFT_FILE),
        "source_v2_review_sha256": sha_file(WORKLIST),
        "prior_43_manifest_sha256": sha_file(PRIOR / "manifest.json"),
        "baseline_12204_text_fields_preserved": 12204,
        "additional_unreviewed_source_unique": 1,
        "additional_text_fields": 1,
        "bundle_count": 1,
        "technical_qa_verdicts": {"PASS": 1},
        "bundle": {
            "remote": remote,
            "logical": original["logical"],
            "original_sha256": original["sha256"],
            "baseline_852_qa_sha256": before["localized_sha256"],
            "localized_sha256": staged["localized_sha256"],
            "output_bytes": staged["output_bytes"],
            "output_path": str(dest / "jp-android" / remote),
            "roundtrip_verified": staged["roundtrip_verified"],
            "non_text_objects_byte_identical":
                staged["non_text_objects_byte_identical"],
            "additional_unreviewed_fields": changes,
        },
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "overlay_merge_authorized": False,
        "original_official_assets_modified": False,
        "production_translations_modified": False,
        "prior_852_bundle_stage_modified": False,
        "prior_43_bundle_stage_modified": False,
        "nas_modified": False,
    }
    (temp / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    os.replace(temp, dest)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root", type=Path, default=DEST)
    args = p.parse_args()
    print(json.dumps(stage(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

