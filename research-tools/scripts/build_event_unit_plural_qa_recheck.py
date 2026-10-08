#!/usr/bin/env python3
"""Source-bound 2nd round of Event-unit deterministic QA cue review ONLY.

Only reassesses 901 original-review sources after explicit JP plural-form
correction. Never edits staged Unity bundles or existing review results.
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
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id

BUILD = ROOT / "build/localization-90200"
STAGE = BUILD / "staging-event-unit-qa-with-tail"
QA = STAGE / "event-unit-QA-candidate-audit.json"
STAGE_MANIFEST = STAGE / "event-unit-QA-candidate-manifest.json"
ROUND1 = BUILD / "audits/event-unit-qa-cue-recheck-client-9.0.200-assets-1077100"
ROUND1_MANIFEST = ROUND1 / "manifest.json"
ROUND1_REVIEW = ROUND1 / "still-review.jsonl"
ROUND1_CLEARED = ROUND1 / "qa-cleared-still-unreviewed.jsonl"
ROUND1_REJECT = ROUND1 / "still-reject.jsonl"
ROUND1_NUMERIC_DRAFT = ROUND1 / "reject-numeric-correction-draft.jsonl"
TAIL = BUILD / "audits/event-unit-tail-draft-client-9.0.200-assets-1077100/47-source-bound-drafts.jsonl"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
QUALITY = ROOT / "scripts/mltd_translation_quality.py"
GLOSSARY = ROOT / "localization/quality/glossary.json"
DEST = BUILD / "audits/event-unit-plural-qa-recheck-client-9.0.200-assets-1077100"

ORIGINAL_STAGE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
ORIGINAL_QA_SHA = "f2c8e3b4d6fb8428abc62cb2fb0b9e7b7fb41c22be2ea7ff60a0f10187aec30d"
ROUND1_REVIEW_SHA = "829f5ccf6ebb81d5a1d0460f6dc12f598786a14721e7be9d17083bc39379e492"
ROUND1_CLEARED_SHA = "9922a2eda51a81a234c1834c105820f80b5909f0f3151fbd917e8f858fe590e0"
ROUND1_REJECT_SHA = "2e256686d6d056cb7f36854d583f57c8e8c58c7bdd55f2da872696bdefbfb48d"


def reassess(
    original_qa: list[dict], pending: list[dict], old_cleared: list[dict],
    original_reject: list[dict], tail: list[dict],
) -> tuple[dict[str, list[dict]], dict]:
    original = {x["source_sha256"]: x for x in original_qa}
    if len(original) != len(original_qa) or len(original) != 13177:
        raise ValueError("frozen Event-unit original QA source universe mismatch")
    if Counter(x["qa_verdict"] for x in original_qa) != {
        "PASS": 12043, "REVIEW": 1133, "REJECT": 1
    }:
        raise ValueError("original QA verdict counts unexpectedly changed")
    all_rows = [*pending, *old_cleared, *original_reject, *tail]
    ids = [x["source_sha256"] for x in all_rows]
    if (len(pending) != 901 or len(old_cleared) != 232
        or len(original_reject) != 1 or len(tail) != 47
        or len(ids) != len(set(ids)) or len(ids) != 1181):
        raise ValueError("round1 unresolved/cleared/reject/tail universe changed")
    for row in all_rows:
        sid, src = row["source_sha256"], row["source"]
        if sid != source_id(src) or row.get("release_gate") != "needs_independent_review":
            if row in tail and row.get("release_gate") == "needs_review":
                pass
            else:
                raise ValueError("non-source-bound/unreviewed review ledger item")
        if row.get("independent_review_complete") is not False:
            raise ValueError("review queue falsely asserts independent approval")
        if sid in original and original[sid]["source"] != src:
            raise ValueError("source SHA bound to changed Japanese content")
    if any(x["qa_verdict"] != "REJECT" or original[x["source_sha256"]]["qa_verdict"] != "REJECT"
           for x in original_reject):
        raise ValueError("original numeric REJECT was silently waived")
    if any(x["qa_verdict"] != "PASS" or x["prior_qa_verdict"] != "REVIEW"
           for x in old_cleared):
        raise ValueError("previously cleared QA is no longer unreviewed")
    if any(x.get("status") != "agent_draft_unreviewed" for x in tail):
        raise ValueError("source-bound 47 agent drafts changed")
    glossary = load_glossary(None)
    remained = []
    newly_cleared = []
    removed: Counter[str] = Counter()
    for row in pending:
        sid, src = row["source_sha256"], row["source"]
        prior = original.get(sid)
        if (prior is None or prior["qa_verdict"] != "REVIEW"
            or row.get("qa_verdict") != "REVIEW"
            or row.get("prior_qa_verdict") != "REVIEW"
            or row["machine_candidate_unreviewed"] != prior["translation"]
            or row["examples"] != prior["examples"]
            or row["occurrences"] != prior["occurrences"]
            or row.get("review_status") != "pending"
            or row.get("semantic_accuracy_verified") is not False
            or row.get("safe_to_mount_as_final_overlay") is not False):
            raise ValueError(f"stale original source/candidate/context: {sid}")
        source_row = {
            "source_sha256": sid, "source": src,
            "examples": row["examples"], "occurrences": row["occurrences"],
        }
        candidate = {
            field: prior[field] for field in (
                "source_sha256", "source", "translation", "provenance",
                "model", "status",
            )
        }
        evaluated = evaluate_row(source_row, candidate, glossary)
        previous_issues = {json.dumps(i, sort_keys=True, ensure_ascii=False)
                           for i in row["issues"]}
        next_issues = {json.dumps(i, sort_keys=True, ensure_ascii=False)
                       for i in evaluated["issues"]}
        if not next_issues.issubset(previous_issues):
            raise ValueError(f"new issue introduced into previous reviewer row: {sid}")
        dropped = [json.loads(i) for i in previous_issues - next_issues]
        if any(i["code"] != "unsupported_first_person_plural_addition" for i in dropped):
            raise ValueError(f"unrelated QA warning accidentally cleared: {sid}")
        if evaluated["qa_verdict"] not in ("PASS", "REVIEW"):
            raise ValueError(f"review candidate has been newly rejected: {sid}")
        for issue in dropped:
            removed[issue["code"]] += 1
        updated = {
            **row, "qa_verdict": evaluated["qa_verdict"], "issues": evaluated["issues"],
            "automatically_cleared_qa_issues_not_semantic_review": (
                row["automatically_cleared_qa_issues_not_semantic_review"] + dropped
            ),
            "semantic_accuracy_verified": False,
            "independent_review_complete": False,
            "review_status": "pending",
            "release_gate": "needs_independent_review",
            "safe_to_mount_as_final_overlay": False,
        }
        if evaluated["qa_verdict"] == "PASS":
            if evaluated["issues"]:
                raise ValueError("new QA PASS has unresolved QA issue")
            newly_cleared.append(updated)
        else:
            remained.append(updated)
    if (len(remained) != 860 or len(newly_cleared) != 41
        or removed != Counter({"unsupported_first_person_plural_addition": 42})):
        raise ValueError("2nd QA review source/issue counts unexpected")
    cleared = sorted([*old_cleared, *newly_cleared],
                     key=lambda x: (-x["occurrences"], x["source_sha256"]))
    remained.sort(key=lambda x: (-x["occurrences"], x["source_sha256"]))
    newly_cleared.sort(key=lambda x: (-x["occurrences"], x["source_sha256"]))
    return {
        "still-review.jsonl": remained,
        "newly-qa-cleared-still-unreviewed.jsonl": newly_cleared,
        "all-qa-cleared-still-unreviewed.jsonl": cleared,
    }, {
        "old_QA_verdicts": {"PASS": 12275, "REVIEW": 901, "REJECT": 1},
        "current_QA_verdicts": {"PASS": 12316, "REVIEW": 860, "REJECT": 1},
        "second_round_review_to_qa_pass": 41,
        "second_round_removed_issue_counts": dict(removed),
        "cumulative_review_to_qa_pass": len(cleared),
        "remaining_independent_review": 1181,
        "unreviewed_47_legacy_tail": 47,
        "unreviewed_original_numeric_reject": 1,
        "unchanged_previously_QA_PASS": 12275,
    }


def build(dest: Path = DEST) -> dict:
    # Refuse overwrite before any re-evaluation: later QA rule versions may
    # differ, but an immutable existing audit is always protected.
    dest = dest.resolve()
    if dest.exists():
        raise FileExistsError(f"immutable audit already exists: {dest}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    first = json.loads(ROUND1_MANIFEST.read_text(encoding="utf8"))
    if (sha_file(STAGE_MANIFEST) != ORIGINAL_STAGE_SHA
        or sha_file(QA) != ORIGINAL_QA_SHA
        or first.get("version_identity") != identity
        or first.get("current_verdicts") !=
           {"PASS": 12275, "REVIEW": 901, "REJECT": 1}
        or first.get("independent_review_complete") is not False
        or first.get("safe_to_mount_as_final_overlay") is not False
        or first.get("files_sha256", {}).get(ROUND1_REVIEW.name) != ROUND1_REVIEW_SHA
        or first.get("files_sha256", {}).get(ROUND1_CLEARED.name) != ROUND1_CLEARED_SHA
        or first.get("files_sha256", {}).get(ROUND1_REJECT.name) != ROUND1_REJECT_SHA
        or sha_file(ROUND1_REVIEW) != ROUND1_REVIEW_SHA
        or sha_file(ROUND1_CLEARED) != ROUND1_CLEARED_SHA
        or sha_file(ROUND1_REJECT) != ROUND1_REJECT_SHA):
        raise ValueError("frozen round1 audit/review source identity changed")
    rows, counts = reassess(
        json.loads(QA.read_text(encoding="utf8")),
        read_jsonl(ROUND1_REVIEW),
        read_jsonl(ROUND1_CLEARED),
        read_jsonl(ROUND1_REJECT),
        read_jsonl(TAIL),
    )
    dest = dest.resolve()
    if not dest.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("2nd QA source-bound audit cannot be placed in overlay")
    if dest.exists():
        raise FileExistsError(f"immutable 2nd QA audit already exists: {dest}")
    temporary = dest.with_name(dest.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"uncommitted 2nd QA audit already exists: {temporary}")
    temporary.mkdir(parents=True)
    file_hashes = {}
    for name, items in rows.items():
        path = temporary / name
        with path.open("w", encoding="utf8", newline="\n") as stream:
            for row in items:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        file_hashes[name] = sha_file(path)
    report = {
        "schema_version": 1,
        "kind": "event-unit-2nd-QA-explicit-plural-source-recheck-REVIEW-ONLY",
        "version_identity": identity,
        "source_stage_manifest_sha256": sha_file(STAGE_MANIFEST),
        "source_qa_audit_sha256": sha_file(QA),
        "source_round1_manifest_sha256": sha_file(ROUND1_MANIFEST),
        "source_round1_review_sha256": sha_file(ROUND1_REVIEW),
        "source_round1_cleared_sha256": sha_file(ROUND1_CLEARED),
        "source_round1_reject_sha256": sha_file(ROUND1_REJECT),
        "unreviewed_47_draft_sha256": sha_file(TAIL),
        "unreviewed_numeric_draft_sha256": sha_file(ROUND1_NUMERIC_DRAFT),
        "quality_rules_sha256": sha_file(QUALITY),
        "glossary_sha256": sha_file(GLOSSARY),
        "file_hashes": file_hashes,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "producer_modified": False,
        "existing_852_bundle_QA_stage_modified": False,
        "nas_modified": False,
        **counts,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    os.replace(temporary, dest)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

