#!/usr/bin/env python3
"""Isolated 10-bundle technical pilot: exact reviewed-pending drafts, never release.

Uses frozen original Unity bundles + existing 12,204 QA-only translations.
Does not mutate 852 old QA outputs, official assets, producer JSONL or NAS.
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
from scripts.build_event_unit_name_title_repair_drafts import (
    DEST as NINE_ROOT, TITLE_SHA,
)
from scripts.build_event_unit_qa_cue_recheck import DEST as CUE_ROOT
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id

MATERIALIZER_ROOT = ROOT / "work/agents/image-localization/reviewed937-texture-stage"
sys.path.insert(0, str(MATERIALIZER_ROOT))
from build_event_unit_bundle_pilot import materialize  # noqa: E402

BUILD = ROOT / "build/localization-90200"
BASE = BUILD / "staging-event-unit-qa-with-tail"
BASE_MANIFEST = BASE / "event-unit-QA-candidate-manifest.json"
COHORT_PATH = MATERIALIZER_ROOT / "event-unit-source1077100-index.json"
COHORT_BUNDLE_ROOT = MATERIALIZER_ROOT / "event-unit-source1077100"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
NINE_FILE = NINE_ROOT / "nine-source-name-title-repair-drafts.jsonl"
NUMERIC_FILE = CUE_ROOT / "reject-numeric-correction-draft.jsonl"
DEST = BUILD / "staging-event-unit-qa-targeted-10-unreviewed-drafts"


def collect_trial_sources(
    baseline: dict, nine: list[dict], numeric: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    if len(nine) != 9 or len(numeric) != 1:
        raise ValueError("frozen correction draft cardinality changed")
    if (baseline.get("safe_to_mount_as_final_overlay") is not False
        or baseline.get("independent_reviewed") is not False
        or baseline.get("release_gate") != "not_evaluated"
        or baseline.get("bundles_written") != 852
        or baseline.get("text_fields_changed") != 12204
        or baseline.get("unreviewed_agent_tail_draft_unique") != 47):
        raise ValueError("existing 852-bundle QA-only baseline changed")
    base_rows = {}
    for bundle in baseline["bundles"]:
        for change in bundle["changes"]:
            sid, source, value = (
                change["path_source_sha256"], change["original"], change["localized"]
            )
            if sid != source_id(source):
                raise ValueError("invalid baseline source SHA")
            row = base_rows.setdefault(sid, {
                "source_sha256": sid, "source": source, "translation": value,
                "status": "qa_stage_only",
            })
            if row["source"] != source or row["translation"] != value:
                raise ValueError("duplicate SHA has conflicting baseline text")
    corrections = {}
    for row in [*nine, *numeric]:
        sid, source = row.get("source_sha256"), row.get("source")
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in corrections or sid in base_rows
            or row.get("qa_verdict_draft") not in ("PASS", "REVIEW")
            or row.get("independent_review_complete") is not False
            or row.get("semantic_accuracy_verified") is not False
            or row.get("release_gate") != "needs_independent_review"
            or row.get("safe_to_mount_as_final_overlay") is not False
            or row.get("status") != "agent_draft_unreviewed"
            or row.get("occurrences") != 1
            or len(row.get("examples", [])) != 1):
            raise ValueError(f"invalid/unapproved targeted trial source: {sid}")
        if row["qa_verdict_draft"] == "REVIEW" and sid == TITLE_SHA:
            raise ValueError("contaminated title should be technical QA PASS")
        check = evaluate_row(
            {"source_sha256": sid, "source": source},
            {"source_sha256": sid, "source": source,
             "translation": row["translation_draft"],
             "status": "agent_draft_unreviewed"},
            load_glossary(None),
        )
        if (check["qa_verdict"] != row["qa_verdict_draft"]
            or check["issues"] != row["qa_issues_draft"]):
            raise ValueError(f"targeted draft technical QA changed: {sid}")
        corrections[sid] = row
    if len(corrections) != 10:
        raise ValueError("unexpected 10-draft source identity")
    merged = {**base_rows}
    for sid, row in corrections.items():
        merged[sid] = {
            "source_sha256": sid, "source": row["source"],
            "translation": row["translation_draft"],
            "status": "agent_draft_unreviewed",
        }
    return merged, corrections


def verify_trial_delta(
    previous: dict, current: dict, desired: dict[str, dict],
) -> list[dict]:
    if (previous["remote"] != current["remote"]
        or previous["source_sha256"] != current["source_sha256"]
        or current.get("roundtrip_verified") is not True
        or current.get("non_text_objects_byte_identical") is not True):
        raise ValueError("baseline source/roundtrip invalid")
    before = {(x["command_index"], x["path_source_sha256"]): x
              for x in previous["changes"]}
    after = {(x["command_index"], x["path_source_sha256"]): x
             for x in current["changes"]}
    if (len(before) != len(previous["changes"])
        or len(after) != len(current["changes"])
        or any(after.get(key) != item for key, item in before.items())):
        raise ValueError("pre-existing QA text modified in targeted trial")
    extras = [value for key, value in after.items() if key not in before]
    if not extras or len(extras) != len(desired):
        raise ValueError("extra targeted field count mismatches expected")
    observed = set()
    for item in extras:
        sid = item["path_source_sha256"]
        if (sid not in desired or sid in observed
            or item["original"] != desired[sid]["source"]
            or item["localized"] != desired[sid]["translation_draft"]):
            raise ValueError("unexpected targeted QA correction or extra field")
        observed.add(sid)
    if observed != set(desired):
        raise ValueError("unmaterialized targeted source")
    return extras


def build(dest: Path = DEST) -> dict:
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    baseline = json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    nine_manifest_path = NINE_ROOT / "manifest.json"
    cue_manifest_path = CUE_ROOT / "manifest.json"
    nine_manifest = json.loads(nine_manifest_path.read_text(encoding="utf8"))
    cue_manifest = json.loads(cue_manifest_path.read_text(encoding="utf8"))
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf8"))
    if (baseline["version_identity"] != identity
        or nine_manifest["version_identity"] != identity
        or cue_manifest["version_identity"] != identity
        or nine_manifest["draft_file_sha256"] != sha_file(NINE_FILE)
        or cue_manifest["files_sha256"][NUMERIC_FILE.name] != sha_file(NUMERIC_FILE)
        or nine_manifest["independent_review_complete"] is not False
        or cue_manifest["independent_review_complete"] is not False
        or cohort.get("complete") is not True
        or cohort.get("verified") != 852
        or cohort.get("source_index_sha256") != identity["asset_index_sha256"]):
        raise ValueError("frozen stage/correction input identity changed")
    merged, corrections = collect_trial_sources(
        baseline, read_jsonl(NINE_FILE), read_jsonl(NUMERIC_FILE),
    )
    remote_to_drafts = {}
    for item in corrections.values():
        remote = item["examples"][0]["remote"]
        remote_to_drafts.setdefault(remote, {})[item["source_sha256"]] = item
    if len(remote_to_drafts) != 10 or len(corrections) != 10:
        raise ValueError("frozen 10 target remote bundles have collided/changed")
    old = {r["remote"]: r for r in baseline["bundles"]}
    originals = {r["remote"]: r for r in cohort["bundles"]}
    if len(old) != len(originals) or len(old) != 852:
        raise ValueError("frozen baseline bundle universe changed")
    dest = dest.resolve()
    if (not dest.is_relative_to(BUILD.resolve())
        or dest.is_relative_to((BUILD / "overlay").resolve())
        or dest.is_relative_to((ROOT / "work/local-assets").resolve())):
        raise ValueError("targeted review QA trial must stay in isolated build")
    if dest.exists():
        raise FileExistsError(f"immutable targeted QA trial already exists: {dest}")
    temporary = dest.with_name(dest.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"uncommitted targeted QA trial exists: {temporary}")
    temporary.mkdir(parents=True)
    records = []
    for remote in sorted(remote_to_drafts):
        prior, original = old[remote], originals[remote]
        source = COHORT_BUNDLE_ROOT / remote
        if (prior["logical"] != original["logical"]
            or prior["original_bytes"] != original["declared_bytes"]
            or prior["source_sha256"] != original["sha256"]
            or sha_file(source) != original["sha256"]):
            raise ValueError("frozen original bundle drift")
        baseline_file = BASE / "jp-android" / remote
        if sha_file(baseline_file) != prior["localized_sha256"]:
            raise ValueError("existing QA-only bundle bytes changed")
        target = temporary / "jp-android" / remote
        result = materialize(
            original["logical"], remote, original["declared_bytes"],
            source, target, merged, require_complete=False,
        )
        expected = remote_to_drafts[remote]
        extras = verify_trial_delta(prior, result, expected)
        result["source_bundle_manifest_sha256"] = sha_file(COHORT_PATH)
        result["source_original_bundle_sha256"] = original["sha256"]
        result["old_QA_bundle_sha256"] = prior["localized_sha256"]
        result["output_path"] = str(dest / "jp-android" / remote)
        result["release_gate"] = "NOT_EVALUATED"
        result["new_unreviewed_draft_fields"] = extras
        records.append(result)
    report = {
        "schema_version": 1,
        "kind": "event-unit-ten-unreviewed-drafts-QA-only-10-bundle-technical-delta",
        "version_identity": identity,
        "base_852_manifest_sha256": sha_file(BASE_MANIFEST),
        "source_852_cohort_sha256": sha_file(COHORT_PATH),
        "nine_draft_manifest_sha256": sha_file(nine_manifest_path),
        "nine_draft_sha256": sha_file(NINE_FILE),
        "numeric_draft_manifest_sha256": sha_file(cue_manifest_path),
        "numeric_draft_sha256": sha_file(NUMERIC_FILE),
        "existing_QA_bundles_preserved": 852,
        "existing_QA_changed_text_fields_preserved": baseline["text_fields_changed"],
        "additional_unreviewed_source_unique": len(corrections),
        "additional_unreviewed_text_fields": sum(
            len(r["new_unreviewed_draft_fields"]) for r in records
        ),
        "isolated_original_source_bundle_roundtrips": len(records),
        "new_draft_qa_verdicts": dict(Counter(
            x["qa_verdict_draft"] for x in corrections.values()
        )),
        "overlay_merge_authorized": False,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "original_source_archive_modified": False,
        "production_translations_modified": False,
        "baseline_QA_stage_modified": False,
        "nas_modified": False,
        "bundles": records,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
    )
    os.replace(temporary, dest)
    return {k: v for k, v in report.items() if k != "bundles"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

