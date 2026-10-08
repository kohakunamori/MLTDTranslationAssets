#!/usr/bin/env python3
"""Immutable source-bound deterministic-QA recheck and reviewer worklist.

Technical QA PASS is not independent linguistic review, nor permission to mount
the QA stage on the client/NAS. Existing 852 Unity bundles remain untouched.
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
AUDIT = STAGE / "event-unit-QA-candidate-audit.json"
STAGE_MANIFEST = STAGE / "event-unit-QA-candidate-manifest.json"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
REVIEW = BUILD / "audits/event-unit-review-pack-client-9.0.200-assets-1077100/review-queue.jsonl"
TAIL = BUILD / "audits/event-unit-tail-draft-client-9.0.200-assets-1077100/47-source-bound-drafts.jsonl"
QUALITY = ROOT / "scripts/mltd_translation_quality.py"
GLOSSARY = ROOT / "localization/quality/glossary.json"
DEST = BUILD / "audits/event-unit-qa-cue-recheck-client-9.0.200-assets-1077100"
ORIGINAL_QA_SHA = "f2c8e3b4d6fb8428abc62cb2fb0b9e7b7fb41c22be2ea7ff60a0f10187aec30d"
ORIGINAL_STAGE_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
ALLOWED_REMOVED_ISSUES = {
    "sentence_final_adversative_missing",
    "unsupported_first_person_plural_addition",
    "unsupported_indefinite_object_addition",
}
REJECT_SOURCE = (
    "ですが、それでも10パーセント……。\n"
    "ということは、劇場のみんなの人数を考えると……。"
)


def reevaluate(
    rows: list[dict], original_review: list[dict], tail: list[dict],
) -> tuple[dict[str, list[dict]], dict]:
    original_by_sid = {x["source_sha256"]: x for x in original_review}
    tail_by_sid = {x["source_sha256"]: x for x in tail}
    if (len(original_review) != len(original_by_sid) or len(original_by_sid) != 1181
        or len(tail) != len(tail_by_sid) or len(tail_by_sid) != 47):
        raise ValueError("frozen source-bound original-review / tail population changed")
    if any(x["qa_verdict"] != "MISSING_MACHINE" for x in
           (original_by_sid[sid] for sid in tail_by_sid)):
        raise ValueError("tail is no longer aligned with missing-machine review")
    glossary = load_glossary(None)
    qa_counts: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    still_review: list[dict] = []
    cleared: list[dict] = []
    rejects: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        sid, src = row.get("source_sha256"), row.get("source")
        if (not isinstance(src, str) or sid != source_id(src)
            or sid in seen or row.get("qa_verdict") not in ("PASS", "REVIEW", "REJECT")):
            raise ValueError("stale/duplicate source SHA in current QA audit")
        seen.add(sid)
        if not isinstance(row.get("translation"), str) or not row["translation"]:
            raise ValueError(f"invalid old candidate text: {sid}")
        current = evaluate_row(
            {"source_sha256": sid, "source": src,
             "examples": row["examples"], "occurrences": row["occurrences"]},
            {key: row[key] for key in (
                "source_sha256", "source", "translation", "provenance",
                "model", "status",
            )},
            glossary,
        )
        if {key: current[key] for key in current
            if key not in ("issues", "qa_verdict")} != {
            key: row[key] for key in row if key not in ("issues", "qa_verdict")
        }:
            raise ValueError(f"QA re-evaluation changed source/candidate metadata: {sid}")
        old_issues = {json.dumps(x, sort_keys=True, ensure_ascii=False)
                      for x in row["issues"]}
        next_issues = {json.dumps(x, sort_keys=True, ensure_ascii=False)
                       for x in current["issues"]}
        if not next_issues.issubset(old_issues):
            raise ValueError(f"QA re-evaluation introduced a new issue: {sid}")
        resolved = [
            json.loads(x) for x in old_issues - next_issues
        ]
        if any(x["code"] not in ALLOWED_REMOVED_ISSUES for x in resolved):
            raise ValueError(f"QA re-evaluation cleared an unrelated issue: {sid}")
        for item in resolved:
            removed[item["code"]] += 1
        previous = row["qa_verdict"]
        verdict = current["qa_verdict"]
        if previous == "PASS" and verdict != "PASS":
            raise ValueError(f"previous QA PASS regressed: {sid}")
        if previous == "REJECT" and verdict != "REJECT":
            raise ValueError(f"numeric source reject must remain rejected: {sid}")
        if previous == "REVIEW" and verdict not in ("REVIEW", "PASS"):
            raise ValueError(f"previous REVIEW became newly rejected: {sid}")
        transitions[f"{previous}->{verdict}"] += 1
        qa_counts[verdict] += 1
        if previous == verdict == "PASS":
            continue
        if sid not in original_by_sid:
            raise ValueError(f"non-original-review source was reclassified: {sid}")
        original = original_by_sid[sid]
        if (original["source"] != src
            or original["qa_verdict"] != previous
            or original["machine_candidate_unreviewed"] != row["translation"]
            or original["examples"] != row["examples"]
            or original["occurrences"] != row["occurrences"]
            or original["review_status"] != "pending"
            or original["release_gate"] != "needs_independent_review"):
            raise ValueError(f"stale original independent-review source context: {sid}")
        item = {
            **original, "prior_qa_verdict": previous,
            "qa_verdict": verdict, "issues": current["issues"],
            "automatically_cleared_qa_issues_not_semantic_review": resolved,
            "independent_review_complete": False,
            "semantic_accuracy_verified": False,
            "review_status": "pending",
            "release_gate": "needs_independent_review",
            "safe_to_mount_as_final_overlay": False,
        }
        if verdict == "REJECT":
            rejects.append(item)
        elif verdict == "REVIEW":
            still_review.append(item)
        else:
            cleared.append(item)
    if (len(seen) != 13177
        or qa_counts != Counter({"PASS": 12275, "REVIEW": 901, "REJECT": 1})
        or transitions != Counter({
            "PASS->PASS": 12043, "REVIEW->PASS": 232,
            "REVIEW->REVIEW": 901, "REJECT->REJECT": 1,
        })
        or len(still_review) != 901 or len(cleared) != 232 or len(rejects) != 1
        or set(tail_by_sid) & {x["source_sha256"] for x in
                              (still_review + cleared + rejects)}):
        raise ValueError("unreviewed 1077100 Event-unit QA recheck unexpected cohort")
    if rejects[0]["source"] != REJECT_SOURCE:
        raise ValueError("numeric source REJECT changed")
    still_review.sort(key=lambda x: (-x["occurrences"], x["source_sha256"]))
    cleared.sort(key=lambda x: (-x["occurrences"], x["source_sha256"]))
    reject = rejects[0]
    fixed = reject["machine_candidate_unreviewed"].replace("10个百分点", "10%")
    if fixed == reject["machine_candidate_unreviewed"]:
        raise ValueError("expected numeric-unit mistranslation not found")
    draft = {
        "source_sha256": reject["source_sha256"],
        "source": reject["source"],
        "translation_draft": fixed,
        "machine_candidate_unreviewed": reject["machine_candidate_unreviewed"],
        "qa_verdict_before": "REJECT",
        "qa_verdict_draft": "",
        "qa_issues_draft": [],
        "examples": reject["examples"],
        "occurrences": reject["occurrences"],
        "status": "agent_draft_unreviewed",
        "review_status": "pending",
        "release_gate": "needs_independent_review",
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
    }
    new_qa = evaluate_row(
        {"source_sha256": draft["source_sha256"], "source": draft["source"],
         "examples": draft["examples"], "occurrences": draft["occurrences"]},
        {"source_sha256": draft["source_sha256"], "source": draft["source"],
         "translation": fixed, "status": "agent_draft_unreviewed"},
        glossary,
    )
    if new_qa["qa_verdict"] != "PASS" or new_qa["issues"]:
        raise ValueError("numeric correction draft does not clear technical QA")
    draft["qa_verdict_draft"] = new_qa["qa_verdict"]
    draft["qa_issues_draft"] = new_qa["issues"]
    return {
        "still-review.jsonl": still_review,
        "qa-cleared-still-unreviewed.jsonl": cleared,
        "still-reject.jsonl": rejects,
        "reject-numeric-correction-draft.jsonl": [draft],
    }, {
        "current_verdicts": dict(qa_counts),
        "source_transitions": dict(transitions),
        "removed_issue_counts": dict(removed),
        "remaining_independent_review": len(still_review) + len(cleared) + len(rejects) + len(tail),
        "unreviewed_existing_tail": len(tail),
        "unreviewed_numeric_correction_draft": 1,
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
    manifest = json.loads(STAGE_MANIFEST.read_text(encoding="utf8"))
    if (sha_file(STAGE_MANIFEST) != ORIGINAL_STAGE_SHA
        or sha_file(AUDIT) != ORIGINAL_QA_SHA
        or manifest.get("version_identity") != identity
        or manifest.get("qa_verdicts") != {"PASS": 12043, "REVIEW": 1133,
                                          "REJECT": 1}
        or manifest.get("release_gate") != "not_evaluated"
        or manifest.get("safe_to_mount_as_final_overlay") is not False
        or manifest.get("independent_reviewed") is not False
        or manifest.get("bundles_scanned") != 852):
        raise ValueError("prior 852-bundle Event-unit QA stage changed")
    rows = json.loads(AUDIT.read_text(encoding="utf8"))
    original_review = read_jsonl(REVIEW)
    tail = read_jsonl(TAIL)
    files, counts = reevaluate(rows, original_review, tail)
    dest = dest.resolve()
    if not dest.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("isolated QA/review-only audit must reside inside audits")
    if dest.exists():
        raise FileExistsError(f"immutable review audit already exists: {dest}")
    temporary = dest.with_name(dest.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"incomplete audit already exists: {temporary}")
    temporary.mkdir(parents=True)
    hashes = {}
    for name, items in files.items():
        path = temporary / name
        with path.open("w", encoding="utf8", newline="\n") as out:
            for row in items:
                out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        hashes[name] = sha_file(path)
    report = {
        "schema_version": 1,
        "kind": "event-unit-qa-cue-source-recheck-REVIEW-ONLY",
        "version_identity": identity,
        "input_stage_manifest_sha256": sha_file(STAGE_MANIFEST),
        "input_qa_audit_sha256": sha_file(AUDIT),
        "input_original_review_sha256": sha_file(REVIEW),
        "input_tail_draft_sha256": sha_file(TAIL),
        "quality_rules_sha256": sha_file(QUALITY),
        "glossary_sha256": sha_file(GLOSSARY),
        "files_sha256": hashes,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "production_translation_files_modified": False,
        "qa_stage_bundles_modified": False,
        "nas_modified": False,
        **counts,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
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

