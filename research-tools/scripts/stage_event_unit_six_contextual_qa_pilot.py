#!/usr/bin/env python3
"""Six isolated UnityFS roundtrips from frozen original 1077100 Event-unit.

No release, production translation write, legacy QA-stage mutation, or NAS.
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
from scripts.build_event_unit_six_contextual_drafts import (
    DEST as DRAFT_ROOT, SOURCE as REVIEW,
)
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE, BASE_MANIFEST, COHORT_BUNDLE_ROOT, COHORT_PATH, INDEX, SNAPSHOT,
    materialize, verify_trial_delta,
)

BUILD = ROOT / "build/localization-90200"
DRAFT_MANIFEST = DRAFT_ROOT / "manifest.json"
DRAFT = DRAFT_ROOT / "six-source-bound-corrections.jsonl"
PRIOR_43 = BUILD / "staging-event-unit-combined-43-unreviewed-drafts"
PREV_BLEED = BUILD / "staging-event-unit-previous-line-bleed-one-qa-only"
DEST = BUILD / "staging-event-unit-six-contextual-qa-only"
BASE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
DRAFT_SHA = "9800e4606848f10acfe51d393477d4cc8188016ce40c4510db88199e9b6f2885"


def validate_sources(
    baseline: dict, proposals: list[dict], review: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    if (baseline.get("text_fields_changed") != 12204
        or baseline.get("bundles_written") != 852
        or baseline.get("independent_reviewed") is not False
        or baseline.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("frozen 852 bundle QA stage differs")
    reviewers = {x["source_sha256"]: x for x in review}
    if len(reviewers) != 1181 or len(reviewers) != len(review):
        raise ValueError("duplicate/missing 1181 reviewer entries")
    baseline_text: dict[str, dict] = {}
    for bundle in baseline["bundles"]:
        for change in bundle["changes"]:
            sid, source, zh = (
                change["path_source_sha256"], change["original"],
                change["localized"]
            )
            if sid != source_id(source):
                raise ValueError("baseline QA source SHA mismatch")
            earlier = baseline_text.setdefault(
                sid, {"source_sha256": sid, "source": source,
                      "translation": zh, "status": "QA-only-baseline"},
            )
            if earlier["source"] != source or earlier["translation"] != zh:
                raise ValueError("baseline QA text has conflicting same-source candidate")
    proposed = {}
    remotes = set()
    for row in proposals:
        sid, source = row.get("source_sha256"), row.get("source")
        original = reviewers.get(sid)
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in proposed or sid in baseline_text or original is None
            or original["source"] != source
            or original.get("review_bucket") != "qa_review_without_draft"
            or original["machine_candidate_unreviewed"] !=
                row.get("machine_candidate_unreviewed")
            or original["examples"] != row.get("examples")
            or original["occurrences"] != row.get("occurrences")
            or original["current_machine_qa_issues"] !=
                row.get("old_machine_QA_issues")
            or row.get("draft_qa_verdict") != "PASS"
            or row.get("draft_qa_issues") != []
            or row.get("status") != "agent_draft_unreviewed"
            or row.get("review_status") != "pending"
            or row.get("independent_review_complete") is not False
            or row.get("semantic_accuracy_verified") is not False
            or row.get("safe_to_mount_as_final_overlay") is not False
            or row.get("release_gate") != "needs_independent_review"
            or row.get("occurrences") != 1
            or len(row.get("examples", [])) != 1):
            raise ValueError(f"candidate differs from source-bound reviewer: {sid}")
        remote = row["examples"][0]["remote"]
        if remote in remotes:
            raise ValueError("six-source pilot requires six distinct remotes")
        remotes.add(remote)
        proposed[sid] = row
    if len(proposed) != 6 or len(remotes) != 6:
        raise ValueError("expected six source-bound changes/six resource bundles")
    translations = {
        **baseline_text,
        **{sid: {"source_sha256": sid, "source": item["source"],
                 "translation": item["translation_draft"],
                 "status": "agent_draft_unreviewed"}
           for sid, item in proposed.items()},
    }
    return translations, proposed


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(BUILD.resolve()):
        raise ValueError("isolated QA-only output cannot leave build")
    if dest.exists():
        raise FileExistsError("immutable six-source QA stage already exists")
    tmp = dest.with_name(dest.name + ".incomplete")
    if tmp.exists():
        raise FileExistsError("incomplete six-source QA stage already exists")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200",
        asset_version="1077100", asset_index=INDEX,
    )
    old = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf8"))
    draft_manifest = json.loads(DRAFT_MANIFEST.read_text(encoding="utf8"))
    prior = json.loads((PRIOR_43 / "manifest.json").read_text(encoding="utf8"))
    bleed = json.loads((PREV_BLEED / "manifest.json").read_text(encoding="utf8"))
    if (sha_file(BASE_MANIFEST) != BASE_SHA
        or sha_file(DRAFT) != DRAFT_SHA
        or draft_manifest.get("draft_sha256") != DRAFT_SHA
        or any(x.get("version_identity") != identity
               for x in (old, draft_manifest, prior, bleed))
        or cohort.get("verified") != 852
        or cohort.get("source_index_sha256") != identity["asset_index_sha256"]
        or draft_manifest.get("draft_unique") != 6
        or draft_manifest.get("draft_deterministic_QA_verdicts") != {"PASS": 6}
        or draft_manifest.get("independent_review_complete") is not False
        or draft_manifest.get("safe_to_mount_as_final_overlay") is not False
        or prior.get("additional_unreviewed_source_unique") != 43
        or prior.get("safe_to_mount_as_final_overlay") is not False
        or bleed.get("additional_unreviewed_source_unique") != 1
        or bleed.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("frozen source/draft/trial provenance differs")
    translations, chosen = validate_sources(
        old, read_jsonl(DRAFT), read_jsonl(REVIEW),
    )
    old_bundles = {row["remote"]: row for row in old["bundles"]}
    originals = {row["remote"]: row for row in cohort["bundles"]}
    earlier = {row["remote"] for row in prior["bundles"]}
    bleed_remote = bleed["bundle"]["remote"]
    if len(old_bundles) != 852 or len(originals) != 852:
        raise ValueError("duplicate original/cohort remote")
    remote_sources = {
        row["examples"][0]["remote"]: row for row in chosen.values()
    }
    if set(remote_sources) & earlier or bleed_remote in remote_sources:
        raise ValueError("do not silently overwrite earlier 43/1 QA-only resources")
    tmp.mkdir(parents=True)
    written = []
    for remote, item in sorted(remote_sources.items()):
        old_bundle = old_bundles[remote]
        original = originals[remote]
        src = COHORT_BUNDLE_ROOT / remote
        if (old_bundle["logical"] != original["logical"]
            or old_bundle["source_sha256"] != original["sha256"]
            or sha_file(src) != original["sha256"]
            or sha_file(BASE / "jp-android" / remote) != old_bundle["localized_sha256"]):
            raise ValueError(f"original/QA bundle SHA differs: {remote}")
        path = tmp / "jp-android" / remote
        built = materialize(
            original["logical"], remote, original["declared_bytes"],
            src, path, translations, require_complete=False,
        )
        delta = verify_trial_delta(
            old_bundle, built, {item["source_sha256"]: item}
        )
        if len(delta) != 1:
            raise ValueError("one expected correction did not materialize")
        built["original_bundle_sha256"] = original["sha256"]
        built["baseline_QA_bundle_sha256"] = old_bundle["localized_sha256"]
        built["additional_unreviewed_fields"] = delta
        built["output_path"] = str(dest / "jp-android" / remote)
        built["release_gate"] = "NOT_EVALUATED"
        written.append(built)
    manifest = {
        "schema_version": 1,
        "kind": "event-unit-six-contextual-6-UnityFS-bundle-QA-only",
        "version_identity": identity,
        "source_852_baseline_manifest_sha256": sha_file(BASE_MANIFEST),
        "source_852_cohort_sha256": sha_file(COHORT_PATH),
        "six_source_draft_manifest_sha256": sha_file(DRAFT_MANIFEST),
        "six_source_drafts_sha256": sha_file(DRAFT),
        "source_v2_review_sha256": sha_file(REVIEW),
        "prior_43_manifest_sha256": sha_file(PRIOR_43 / "manifest.json"),
        "prior_one_bleed_manifest_sha256": sha_file(PREV_BLEED / "manifest.json"),
        "baseline_12204_text_fields_preserved": 12204,
        "additional_unreviewed_source_unique": 6,
        "additional_unreviewed_text_fields": sum(
            len(b["additional_unreviewed_fields"]) for b in written
        ),
        "bundle_count": len(written),
        "draft_deterministic_qa": {"PASS": 6},
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "overlay_merge_authorized": False,
        "official_original_assets_modified": False,
        "production_translations_modified": False,
        "prior_852_43_bleed_QA_stages_modified": False,
        "nas_modified": False,
        "bundles": written,
    }
    (tmp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    os.replace(tmp, dest)
    return {key: value for key, value in manifest.items() if key != "bundles"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

