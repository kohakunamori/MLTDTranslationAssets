#!/usr/bin/env python3
"""Single source-bound review entrypoint for frozen 9.0.200/1077100 Event-unit.

Unifies prior review queues and already-authored drafts WITHOUT accepting any
translation or writing a release ledger, producer JSONL, resource or NAS.
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
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
DEST = AUDITS / "event-unit-unified-review-client-9.0.200-assets-1077100"

ORIGINAL = AUDITS / "event-unit-review-pack-client-9.0.200-assets-1077100"
PLURAL = AUDITS / "event-unit-plural-qa-recheck-client-9.0.200-assets-1077100"
QA1 = AUDITS / "event-unit-qa-cue-recheck-client-9.0.200-assets-1077100"
TAIL = AUDITS / "event-unit-tail-draft-client-9.0.200-assets-1077100"
NAMES = AUDITS / "event-unit-name-title-repair-drafts-client-9.0.200-assets-1077100"
SEMANTIC = AUDITS / "event-unit-semantic-repair-drafts-client-9.0.200-assets-1077100"

INPUTS = {
    "initial_review_1181": (
        ORIGINAL / "review-queue.jsonl",
        "f0531bee00a9b7d1037753aee974da406cf842d0ecef77ce130db2bdafba2bd1",
    ),
    "current_review_860": (
        PLURAL / "still-review.jsonl",
        "7d4527281b38ee4277a4606bbf07485038c2d7da2044649df4025b050f18e7dd",
    ),
    "qa_cleared_unreviewed_273": (
        PLURAL / "all-qa-cleared-still-unreviewed.jsonl",
        "59d54b6eb308b9c2d4971bfda37100ab34529ed3bd7d3a431c09e6395a2a3d07",
    ),
    "original_reject_1": (
        QA1 / "still-reject.jsonl",
        "2e256686d6d056cb7f36854d583f57c8e8c58c7bdd55f2da872696bdefbfb48d",
    ),
    "unreviewed_tail_47": (
        TAIL / "47-source-bound-drafts.jsonl",
        "edd3500fd7f94c131dd9a7d4870e539cc7435fc3a01eaa7eb45712ddf2e95cea",
    ),
    "unreviewed_names_9": (
        NAMES / "nine-source-name-title-repair-drafts.jsonl",
        "efa26864652916107cce258f0609d5fb5c4e51801f4efd208ee1d55667e237a5",
    ),
    "unreviewed_semantic_21": (
        SEMANTIC / "21-source-bound-semantic-correction-drafts.jsonl",
        "2c0c47d1088224a4cd24b40809b45dfc8ccbd7543f0a4889447b4b052adc32ca",
    ),
    "unreviewed_numeric_1": (
        QA1 / "reject-numeric-correction-draft.jsonl",
        "cb337a47308550f4617b6897c6696423a327f27b4f8dea5a245d0452e3df019c",
    ),
}
MANIFESTS = {
    "initial_review": ORIGINAL / "review-pack-manifest.json",
    "second_rule_qa": PLURAL / "manifest.json",
    "first_rule_qa": QA1 / "manifest.json",
    "legacy_tail": TAIL / "draft-manifest.json",
    "character_names": NAMES / "manifest.json",
    "contextual_semantic": SEMANTIC / "manifest.json",
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
    "draft_still_qa_review": 1,
    "qa_review_with_qa_pass_draft": 29,
    "missing_machine_with_qa_pass_draft": 47,
    "qa_review_without_draft": 830,
    "rule_cleared_qa_pass_needs_semantic_review": 273,
}


def _by_sha(items: list[dict], name: str) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in items:
        sid = row.get("source_sha256")
        source = row.get("source")
        if (not isinstance(source, str) or not source or sid != source_id(source)
            or sid in index):
            raise ValueError(f"duplicate, stale or unbound source SHA in {name}: {sid}")
        index[sid] = row
    return index


def validate_manifest_chain(identity: dict) -> None:
    evidence = {
        k: json.loads(path.read_text(encoding="utf8"))
        for k, path in MANIFESTS.items()
    }
    for name, manifest in evidence.items():
        if (manifest.get("version_identity") != identity
            or manifest.get("safe_to_mount_as_final_overlay") is not False
            or manifest.get("independent_review_complete", False) is not False
            or manifest.get("nas_modified") is not False):
            raise ValueError(f"stale/approved manifest cannot become pending review: {name}")
    old = evidence["initial_review"]
    second = evidence["second_rule_qa"]
    first = evidence["first_rule_qa"]
    tail = evidence["legacy_tail"]
    names = evidence["character_names"]
    semantic = evidence["contextual_semantic"]
    if (
        old.get("review_queue_sha256") != INPUTS["initial_review_1181"][1]
        or old.get("review_queue_unique") != 1181
        or old.get("reviewed") is not False
        or second.get("file_hashes", {}).get("still-review.jsonl") !=
            INPUTS["current_review_860"][1]
        or second.get("file_hashes", {}).get(
            "all-qa-cleared-still-unreviewed.jsonl"
        ) != INPUTS["qa_cleared_unreviewed_273"][1]
        or second.get("current_QA_verdicts") !=
            {"PASS": 12316, "REVIEW": 860, "REJECT": 1}
        or second.get("remaining_independent_review") != 1181
        or first.get("files_sha256", {}).get("still-reject.jsonl") !=
            INPUTS["original_reject_1"][1]
        or first.get("files_sha256", {}).get(
            "reject-numeric-correction-draft.jsonl"
        ) != INPUTS["unreviewed_numeric_1"][1]
        or tail.get("draft_sha256") != INPUTS["unreviewed_tail_47"][1]
        or tail.get("review_queue_sha256") != INPUTS["initial_review_1181"][1]
        or names.get("draft_file_sha256") != INPUTS["unreviewed_names_9"][1]
        or semantic.get("draft_sha256") != INPUTS["unreviewed_semantic_21"][1]
        or semantic.get("source_review_queue_sha256") !=
            INPUTS["current_review_860"][1]
    ):
        raise ValueError("QA/draft lineage conflicts with frozen source review")


def unify(inputs: dict[str, list[dict]]) -> tuple[list[dict], dict]:
    groups = {key: _by_sha(items, key) for key, items in inputs.items()}
    expected_counts = {
        "initial_review_1181": 1181,
        "current_review_860": 860,
        "qa_cleared_unreviewed_273": 273,
        "original_reject_1": 1,
        "unreviewed_tail_47": 47,
        "unreviewed_names_9": 9,
        "unreviewed_semantic_21": 21,
        "unreviewed_numeric_1": 1,
    }
    if {key: len(value) for key, value in groups.items()} != expected_counts:
        raise ValueError("unexpected frozen Event-unit cohort counts")
    initial = groups["initial_review_1181"]
    pending = groups["current_review_860"]
    cleared = groups["qa_cleared_unreviewed_273"]
    rejected = groups["original_reject_1"]
    tail = groups["unreviewed_tail_47"]
    numeric = groups["unreviewed_numeric_1"]
    names = groups["unreviewed_names_9"]
    semantic = groups["unreviewed_semantic_21"]
    if (
        set(pending) & set(cleared) or set(pending) & set(rejected)
        or set(cleared) & set(rejected)
        or set(tail) & (set(pending) | set(cleared) | set(rejected))
        or set(initial) != (set(pending) | set(cleared) |
                            set(rejected) | set(tail))
        or set(numeric) != set(rejected)
        or not set(names).issubset(pending)
        or not set(semantic).issubset(pending)
        or set(names) & set(semantic)
    ):
        raise ValueError("original 1181 source partition or draft overlap changed")
    draft_by_id: dict[str, tuple[str, dict]] = {}
    for group_name, collection in [
        ("unreviewed_numeric_1", numeric),
        ("unreviewed_tail_47", tail),
        ("unreviewed_names_9", names),
        ("unreviewed_semantic_21", semantic),
    ]:
        for sid, candidate in collection.items():
            if sid in draft_by_id:
                raise ValueError(f"two different unreviewed drafts for one source {sid}")
            draft_by_id[sid] = (group_name, candidate)
    if len(draft_by_id) != 78:
        raise ValueError("unexpected proposed correction candidate population")
    worklist = []
    counts: Counter[str] = Counter()
    for sid, original in initial.items():
        machine = original.get("machine_candidate_unreviewed")
        if (original.get("review_status") != "pending"
            or original.get("release_gate") != "needs_independent_review"
            or original.get("qa_verdict") not in (
                "MISSING_MACHINE", "REVIEW", "REJECT"
            )
            or not original.get("examples")
            or not isinstance(original.get("occurrences"), int)
            or original["occurrences"] <= 0):
            raise ValueError(f"original review item has release/identity drift: {sid}")
        if sid in tail:
            current = tail[sid]
            if original["qa_verdict"] != "MISSING_MACHINE" or machine not in (None, ""):
                raise ValueError("unreviewed 47 have new machine translations")
            existing_verdict = "MISSING_MACHINE"
            existing_issues = original["issues"]
            previously_cleared = []
        else:
            current = (pending.get(sid) or cleared.get(sid)
                       or rejected.get(sid))
            if current is None:
                raise ValueError(f"review source unaccounted for: {sid}")
            if (original["qa_verdict"] != current["prior_qa_verdict"]
                or original["machine_candidate_unreviewed"] !=
                    current["machine_candidate_unreviewed"]
                or current.get("review_status") != "pending"
                or current.get("release_gate") != "needs_independent_review"
                or current.get("independent_review_complete") is not False
                or current.get("semantic_accuracy_verified") is not False
                or current.get("safe_to_mount_as_final_overlay") is not False):
                raise ValueError(f"stale/current review candidate mismatch: {sid}")
            existing_verdict = current["qa_verdict"]
            existing_issues = current["issues"]
            previously_cleared = current[
                "automatically_cleared_qa_issues_not_semantic_review"
            ]
        # A REVIEW/REJECT status with no issue is a forged QA snapshot.
        # Conversely a current machine QA PASS cannot retain unresolved issues.
        if ((existing_verdict in ("REVIEW", "REJECT") and not existing_issues)
            or (existing_verdict == "PASS" and existing_issues)):
            raise ValueError(f"QA verdict/issue mismatch in source row: {sid}")
        if (original["source"] != current["source"]
            or original["examples"] != current["examples"]
            or original["occurrences"] != current["occurrences"]
            or original["legacy_traditional_references_unreviewed"] !=
                current["legacy_traditional_references_unreviewed"]):
            raise ValueError(f"source context differs across QA stages: {sid}")
        draft_origin = None
        draft = None
        draft_verdict = None
        draft_issues = []
        if sid in draft_by_id:
            draft_origin, evidence = draft_by_id[sid]
            draft = evidence.get("translation_draft")
            verdict_key = {
                "unreviewed_numeric_1": "qa_verdict_draft",
                "unreviewed_tail_47": "qa_verdict",
                "unreviewed_names_9": "qa_verdict_draft",
                "unreviewed_semantic_21": "draft_qa_verdict",
            }[draft_origin]
            issue_key = {
                "unreviewed_numeric_1": "qa_issues_draft",
                "unreviewed_tail_47": "issues",
                "unreviewed_names_9": "qa_issues_draft",
                "unreviewed_semantic_21": "draft_qa_issues",
            }[draft_origin]
            draft_verdict = evidence.get(verdict_key)
            draft_issues = evidence.get(issue_key)
            if (not isinstance(draft, str) or not draft
                or draft_verdict not in ("PASS", "REVIEW")
                or not isinstance(draft_issues, list)
                or evidence.get("status") != "agent_draft_unreviewed"
                or evidence.get("independent_review_complete") is not False
                or evidence.get("semantic_accuracy_verified") is not False
                or evidence.get("safe_to_mount_as_final_overlay", False) is not False
                or evidence.get("machine_candidate_unreviewed", machine) != machine
                or evidence["source"] != original["source"]
                or evidence["examples"] != original["examples"]
                or evidence["occurrences"] != original["occurrences"]):
                raise ValueError(f"unapproved/stale correction draft: {sid}")
            if ((draft_origin == "unreviewed_tail_47" and
                 evidence.get("release_gate") != "needs_review") or
                (draft_origin != "unreviewed_tail_47" and
                 evidence.get("release_gate") != "needs_independent_review")):
                raise ValueError(f"draft release status drift: {sid}")
            if draft_verdict == "PASS" and draft_issues:
                raise ValueError(f"reported technical QA PASS has issues: {sid}")
        if sid in rejected:
            bucket = "rejected_original_with_correction_draft"
        elif sid in tail:
            bucket = "missing_machine_with_qa_pass_draft"
        elif sid in pending and draft is not None:
            bucket = ("qa_review_with_qa_pass_draft"
                      if draft_verdict == "PASS"
                      else "draft_still_qa_review")
        elif sid in pending:
            bucket = "qa_review_without_draft"
        else:
            bucket = "rule_cleared_qa_pass_needs_semantic_review"
        counts[bucket] += 1
        worklist.append({
            "source_sha256": sid,
            "source": original["source"],
            "examples": original["examples"],
            "occurrences": original["occurrences"],
            "legacy_traditional_references_unreviewed":
                original["legacy_traditional_references_unreviewed"],
            "initial_qa_verdict": original["qa_verdict"],
            "current_machine_qa_verdict": existing_verdict,
            "current_machine_qa_issues": existing_issues,
            "auto_cleared_issue_evidence_not_semantic_review": previously_cleared,
            "machine_candidate_unreviewed": machine,
            "agent_correction_draft_unreviewed": draft,
            "agent_draft_source_group": draft_origin,
            "agent_draft_deterministic_qa_verdict": draft_verdict,
            "agent_draft_deterministic_qa_issues": draft_issues,
            "candidate_for_human_review_unreviewed":
                draft if draft is not None else machine,
            "review_bucket": bucket,
            "review_bucket_order": BUCKETS.index(bucket),
            "independent_review_complete": False,
            "semantic_accuracy_verified": False,
            "review_status": "pending",
            "release_gate": "needs_independent_review",
            "safe_to_mount_as_final_overlay": False,
        })
    if dict(counts) != EXPECTED_BUCKET_COUNTS:
        raise ValueError(f"unified review worklist partition changed: {counts}")
    worklist.sort(key=lambda r: (
        r["review_bucket_order"], -r["occurrences"], r["source_sha256"]
    ))
    return worklist, {
        "original_review_unique": len(initial),
        "still_requires_independent_semantic_review": len(worklist),
        "current_machine_qa_verdicts": {
            "PASS": 273, "REVIEW": 860, "REJECT": 1,
            "MISSING_MACHINE": 47,
        },
        "unreviewed_draft_unique": len(draft_by_id),
        "unreviewed_draft_technical_QA_pass": sum(
            x["agent_draft_deterministic_qa_verdict"] == "PASS"
            for x in worklist
        ),
        "unreviewed_draft_technical_QA_review": sum(
            x["agent_draft_deterministic_qa_verdict"] == "REVIEW"
            for x in worklist
        ),
        "no_draft_yet": len(worklist) - len(draft_by_id),
        "review_buckets": dict(counts),
    }


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(AUDITS.resolve()):
        raise ValueError("unified reviewer index must reside inside audits only")
    if dest.exists():
        raise FileExistsError(f"immutable unified reviewer index exists: {dest}")
    temporary = dest.with_name(dest.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"unfinished unified reviewer index exists: {temporary}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    for key, (path, expected_sha) in INPUTS.items():
        if sha_file(path) != expected_sha:
            raise ValueError(f"frozen source-bound {key} file has changed: {path}")
    validate_manifest_chain(identity)
    items, counts = unify({
        key: read_jsonl(path) for key, (path, _) in INPUTS.items()
    })
    temporary.mkdir(parents=True)
    queue_path = temporary / "review-worklist.jsonl"
    with queue_path.open("w", encoding="utf8", newline="\n") as writer:
        for row in items:
            writer.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "kind": "frozen-event-unit-single-review-entrypoint-UNREVIEWED",
        "version_identity": identity,
        "source_manifests_sha256": {
            key: sha_file(path) for key, path in MANIFESTS.items()
        },
        "source_files_sha256": {
            key: expected_sha for key, (_, expected_sha) in INPUTS.items()
        },
        "review_worklist_filename": queue_path.name,
        "review_worklist_sha256": sha_file(queue_path),
        "bucket_order": list(BUCKETS),
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "all_reviews_pending": True,
        "release_gate": "needs_independent_review",
        "safe_to_mount_as_final_overlay": False,
        "production_translations_modified": False,
        "existing_QA_bundles_modified": False,
        "official_original_assets_modified": False,
        "nas_modified": False,
        **counts,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    os.replace(temporary, dest)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-root", type=Path, default=DEST)
    args = ap.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

