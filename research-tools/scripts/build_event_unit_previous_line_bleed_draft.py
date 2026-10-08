#!/usr/bin/env python3
"""Isolate a verified prior-dialogue contamination in one Event-unit source.

Scans frozen 13,177-source QA audit for an entire preceding Chinese subtitle
accidentally included in a later Chinese translation. Authors a ONE-source
unreviewed repair, not a producer update or release/independent approval.
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
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id

BUILD = ROOT / "build/localization-90200"
QA_AUDIT = BUILD / "staging-event-unit-qa-with-tail/event-unit-QA-candidate-audit.json"
V2 = BUILD / "audits/event-unit-unified-review-v2-client-9.0.200-assets-1077100"
WORKLIST = V2 / "review-worklist.jsonl"
V2_MANIFEST = V2 / "manifest.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
QUALITY = ROOT / "scripts/mltd_translation_quality.py"
DEST = BUILD / "audits/event-unit-previous-line-bleed-draft-client-9.0.200-assets-1077100"
AUDIT_SHA = "f2c8e3b4d6fb8428abc62cb2fb0b9e7b7fb41c22be2ea7ff60a0f10187aec30d"
WORKLIST_SHA = "61a7da214fbdcb905ee947a8894c5b306e6bb5f3de326c1161ee8d877b367927"
TARGET_SHA = "3133f65a05febc93fbc520397b82ec16953e1e3c1b0dace3874a5e2d56528122"
NAME_EVIDENCE_SHA = "850f7fc82725"  # neighboring same-bundle なる rendered 奈露, NOT official
REPAIR = "「不过，奈露发帖就到此为止了。\n从明天起就会恢复原样，请放心。」"


def detect_previous_line_bleed(audit: list[dict]) -> list[dict]:
    by_source: dict[str, dict] = {}
    for row in audit:
        sid, src = row.get("source_sha256"), row.get("source")
        if not isinstance(src, str) or sid != source_id(src) or src in by_source:
            raise ValueError("invalid, stale or duplicate frozen source audit")
        by_source[src] = row
    if len(by_source) != 13177:
        raise ValueError("frozen 13177-source QA audit changed")
    found = []
    for row in audit:
        candidate = row.get("translation")
        if not isinstance(candidate, str) or len(candidate) < 40:
            continue
        for example in row.get("examples", []):
            prev = example.get("previous")
            prev_row = by_source.get(prev) if isinstance(prev, str) else None
            prev_zh = prev_row.get("translation") if prev_row else None
            if (prev != row["source"] and isinstance(prev_zh, str)
                and len(prev_zh) >= 18 and prev_zh in candidate):
                found.append({
                    "source_sha256": row["source_sha256"],
                    "preceding_source_sha256": prev_row["source_sha256"],
                    "preceding_source": prev,
                    "preceding_translation_unreviewed": prev_zh,
                })
                break
    return found


def candidate_rows(audit: list[dict], worklist: list[dict]) -> list[dict]:
    found = detect_previous_line_bleed(audit)
    if len(found) != 1 or found[0]["source_sha256"] != TARGET_SHA:
        raise ValueError("previous subtitle contamination no longer exactly one bound source")
    reviewer = {r["source_sha256"]: r for r in worklist}
    if len(reviewer) != 1181 or TARGET_SHA not in reviewer:
        raise ValueError("frozen v2 review queue identity differs")
    row = reviewer[TARGET_SHA]
    audit_row = next(r for r in audit if r["source_sha256"] == TARGET_SHA)
    if (row.get("review_bucket") != "qa_review_without_draft"
        or row.get("current_machine_qa_verdict") != "REVIEW"
        or row.get("agent_correction_draft_unreviewed") is not None
        or row.get("review_status") != "pending"
        or row.get("release_gate") != "needs_independent_review"
        or row.get("independent_review_complete") is not False
        or row.get("semantic_accuracy_verified") is not False
        or row.get("safe_to_mount_as_final_overlay") is not False
        or row.get("source") != audit_row["source"]
        or row.get("machine_candidate_unreviewed") != audit_row["translation"]
        or row.get("examples") != audit_row["examples"]
        or row.get("occurrences") != 1):
        raise ValueError("prior-dialogue-bleed reviewer source unexpectedly changed")
    previous = found[0]["preceding_translation_unreviewed"]
    if (not row["machine_candidate_unreviewed"].startswith(previous + "\n\n")
        or previous in REPAIR):
        raise ValueError("whole previous subtitle no longer duplicated in machine output")
    same_remote = row["examples"][0]["remote"]
    name_evidence = [
        r for r in audit
        if r["source_sha256"].startswith(NAME_EVIDENCE_SHA)
    ]
    if (len(name_evidence) != 1
        or "なる" not in name_evidence[0]["source"]
        or "奈露" not in name_evidence[0]["translation"]
        or same_remote not in [x.get("remote")
                               for x in name_evidence[0]["examples"]]):
        raise ValueError("same-story provisional なる transliteration evidence changed")
    qa = evaluate_row(
        {"source_sha256": TARGET_SHA, "source": row["source"],
         "examples": row["examples"], "occurrences": row["occurrences"]},
        {"source_sha256": TARGET_SHA, "source": row["source"],
         "translation": REPAIR, "status": "agent_draft_unreviewed"},
        load_glossary(None),
    )
    if qa["qa_verdict"] != "PASS" or qa["issues"]:
        raise ValueError("source-bound repair has deterministic technical QA issue")
    return [{
        "source_sha256": TARGET_SHA,
        "source": row["source"],
        "occurrences": row["occurrences"],
        "examples": row["examples"],
        "machine_candidate_unreviewed": row["machine_candidate_unreviewed"],
        "preceding_source_sha256": found[0]["preceding_source_sha256"],
        "preceding_translation_unreviewed": previous,
        "duplicate_previous_subtitle_removed": True,
        "translation_draft": REPAIR,
        "provisional_character_rendering": "奈露",
        "character_rendering_is_authoritative": False,
        "character_rendering_same_bundle_source_sha256":
            name_evidence[0]["source_sha256"],
        "old_machine_QA_verdict": row["current_machine_qa_verdict"],
        "old_machine_QA_issues": row["current_machine_qa_issues"],
        "draft_qa_verdict": qa["qa_verdict"],
        "draft_qa_issues": qa["issues"],
        "status": "agent_draft_unreviewed",
        "review_status": "pending",
        "release_gate": "needs_independent_review",
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
    }]


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("candidate must be an isolated audit")
    if dest.exists():
        raise FileExistsError("immutable one-source proposal already exists")
    tmp = dest.with_name(dest.name + ".incomplete")
    if tmp.exists():
        raise FileExistsError("incomplete isolated one-source audit already exists")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    v2 = json.loads(V2_MANIFEST.read_text(encoding="utf8"))
    if (sha_file(QA_AUDIT) != AUDIT_SHA
        or sha_file(WORKLIST) != WORKLIST_SHA
        or v2.get("version_identity") != identity
        or v2.get("review_worklist_sha256") != WORKLIST_SHA
        or v2.get("available_unreviewed_agent_drafts") != 90
        or v2.get("qa_review_without_targeted_draft") != 818
        or v2.get("independent_review_complete") is not False
        or v2.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("frozen 9.0.200/1077100 source audit or v2 review drifted")
    rows = candidate_rows(
        json.loads(QA_AUDIT.read_text(encoding="utf8")),
        read_jsonl(WORKLIST),
    )
    tmp.mkdir(parents=True)
    target = tmp / "one-source-bound-correction.jsonl"
    with target.open("w", encoding="utf8", newline="\n") as stream:
        for item in rows:
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 1,
        "kind": "event-unit-one-source-previous-line-bleed-draft-unreviewed",
        "version_identity": identity,
        "frozen_13177_source_qa_audit_sha256": sha_file(QA_AUDIT),
        "v2_unified_review_manifest_sha256": sha_file(V2_MANIFEST),
        "v2_unified_review_worklist_sha256": sha_file(WORKLIST),
        "quality_rules_sha256": sha_file(QUALITY),
        "previous_line_contamination_detected": 1,
        "draft_file": target.name,
        "draft_sha256": sha_file(target),
        "new_unreviewed_drafts": 1,
        "unreviewed_drafts_total_with_v2": 91,
        "qa_review_without_targeted_draft_after_this_draft": 817,
        "draft_deterministic_qa": {"PASS": 1},
        "name_provenance_official": False,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
        "producer_translations_modified": False,
        "prior_QA_bundle_stages_modified": False,
        "original_official_assets_modified": False,
        "nas_modified": False,
    }
    (tmp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    os.replace(tmp, dest)
    return manifest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root", type=Path, default=DEST)
    args = p.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

