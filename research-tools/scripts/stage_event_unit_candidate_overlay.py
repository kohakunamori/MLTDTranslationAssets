#!/usr/bin/env python3
"""Stage frozen event-unit TextAsset translations as *non-release* QA candidates.

Uses the existing source-audited event_unit materializer; never adds release
acceptance to raw machine translations, changes production, or touches NAS.
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

from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id

ROOT = Path(__file__).resolve().parents[1]
EXISTING_MATERIALIZER = (
    ROOT / "work/agents/image-localization/reviewed937-texture-stage"
)
sys.path.insert(0, str(EXISTING_MATERIALIZER))
from build_event_unit_bundle_pilot import materialize, sha_file  # noqa: E402

SNAPSHOT = ROOT / "build/localization-90200/jp-gtx-cache-snapshot.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
QUEUE = ROOT / "build/localization-90200/event-unit-translation-queue.jsonl"
COHORT = EXISTING_MATERIALIZER / "event-unit-source1077100-index.json"
AUDIT = EXISTING_MATERIALIZER / "event-unit-source1077100-independent-audit.json"
BUNDLE_ROOT = EXISTING_MATERIALIZER / "event-unit-source1077100"
TAIL_REVIEW_ROOT = (
    ROOT / "build/localization-90200/audits/event-unit-review-pack-client-9.0.200-assets-1077100"
)
TAIL_DRAFT_ROOT = (
    ROOT / "build/localization-90200/audits/event-unit-tail-draft-client-9.0.200-assets-1077100"
)


def source_bound_machine_candidates(
    queue: dict[str, dict], paths: list[Path]
) -> tuple[dict[str, dict], dict]:
    selected: dict[str, dict] = {}
    counts: Counter[str] = Counter()
    for path in paths:
        for row in read_jsonl(path):
            source = row.get("source")
            sid = row.get("source_sha256")
            if not isinstance(source, str) or sid != source_id(source):
                raise ValueError(f"{path}: invalid candidate source identity")
            if sid not in queue:
                continue
            if source != queue[sid]["source"]:
                raise ValueError(f"{path}: current event-unit source collision")
            if str(row.get("status", "")) not in {"machine_translated", "agent_translated"}:
                continue
            translation = str(row.get("translation", ""))
            if not translation:
                continue
            if sid in selected:
                if selected[sid]["translation"] != translation:
                    counts["cross_input_translation_differences"] += 1
                continue  # first-listed producer is the priority
            selected[sid] = row
            counts[f"chosen_from_{path.name}"] += 1
    return selected, dict(counts)


def source_bound_unreviewed_tail_drafts(
    queue: dict[str, dict],
    machine: dict[str, dict],
    path: Path,
    identity: dict,
) -> tuple[dict[str, dict], dict]:
    """Accept exactly 47 source-audited *review-only* drafts for an isolated QA trial.

    This deliberately DOES NOT treat the drafts as machine-completed or releasable.
    Review-pack, draft-manifest, queue and literal source text must all agree.
    """
    if path.resolve() != (TAIL_DRAFT_ROOT / "47-source-bound-drafts.jsonl").resolve():
        raise ValueError("only the frozen source-bound event-unit 47-draft input is allowed")
    review_manifest_path = TAIL_REVIEW_ROOT / "review-pack-manifest.json"
    review_queue_path = TAIL_REVIEW_ROOT / "review-queue.jsonl"
    draft_manifest_path = TAIL_DRAFT_ROOT / "draft-manifest.json"
    review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
    draft_manifest = json.loads(draft_manifest_path.read_text(encoding="utf-8"))
    if (review_manifest.get("version_identity") != identity
        or review_manifest.get("review_queue_sha256") != sha_file(review_queue_path)
        or review_manifest.get("reviewed") is not False
        or review_manifest.get("safe_to_mount_as_final_overlay") is not False
        or review_manifest.get("review_queue_unique") != 1181
        or draft_manifest.get("version_identity") != identity
        or draft_manifest.get("review_pack_manifest_sha256") != sha_file(review_manifest_path)
        or draft_manifest.get("review_queue_sha256") != sha_file(review_queue_path)
        or draft_manifest.get("draft_filename") != path.name
        or draft_manifest.get("draft_sha256") != sha_file(path)
        or draft_manifest.get("draft_unique") != 47
        or draft_manifest.get("qa_verdicts") != {"PASS": 47}
        or draft_manifest.get("candidate_is_machine_translation") is not False
        or draft_manifest.get("independent_review_complete") is not False
        or draft_manifest.get("safe_to_mount_as_final_overlay") is not False
        or draft_manifest.get("release_gate") != "needs_independent_review"):
        raise ValueError("event-unit 47-draft source or review-only manifest differs")
    missing = set(queue) - set(machine)
    review_missing: dict[str, dict] = {}
    for row in read_jsonl(review_queue_path):
        sid, source = row.get("source_sha256"), row.get("source")
        if not isinstance(source, str) or sid != source_id(source):
            raise ValueError("invalid event-unit source in independent review pack")
        if row.get("qa_verdict") != "MISSING_MACHINE":
            continue
        if sid in review_missing or sid not in queue or source != queue[sid]["source"]:
            raise ValueError("duplicate/stale frozen missing-machine source in review pack")
        if (row.get("review_status") != "pending"
            or row.get("release_gate") != "needs_independent_review"
            or row.get("machine_candidate_unreviewed")):
            raise ValueError("frozen missing-machine review source was already changed")
        review_missing[sid] = row
    if len(missing) != 47 or set(review_missing) != missing:
        raise ValueError("frozen event-unit machine/47-draft split has changed")
    result: dict[str, dict] = {}
    for item in read_jsonl(path):
        sid, source = item.get("source_sha256"), item.get("source")
        if (not isinstance(source, str) or sid != source_id(source)
            or sid not in review_missing or sid in result):
            raise ValueError("duplicate/unknown/stale 47-draft source identity")
        frozen = review_missing[sid]
        if (item.get("status") != "agent_draft_unreviewed"
            or item.get("release_gate") != "needs_review"
            or item.get("semantic_accuracy_verified") is not False
            or item.get("independent_review_complete") is not False
            or item.get("safe_to_auto_promote") is not False
            or item.get("qa_verdict") != "PASS"
            or item.get("issues") != []
            or item.get("examples") != frozen["examples"]
            or item.get("occurrences") != frozen["occurrences"]
            or item.get("legacy_traditional_references_unreviewed")
               != frozen["legacy_traditional_references_unreviewed"]):
            raise ValueError("event-unit draft changed its source context or review status")
        translation = item.get("translation_draft")
        if not isinstance(translation, str) or not translation.strip():
            raise ValueError("empty event-unit review-only draft")
        result[sid] = {
            "source_sha256": sid, "source": source, "translation": translation,
            "status": "agent_draft_unreviewed", "release_gate": "needs_review",
            "independent_review_complete": False,
            "provenance": "frozen-47-source-bound-agent-draft-unreviewed",
        }
    if set(result) != missing:
        raise ValueError("incomplete 47-draft event-unit source universe")
    return result, {
        "source_draft_manifest_sha256": sha_file(draft_manifest_path),
        "source_draft_sha256": sha_file(path),
        "source_review_pack_manifest_sha256": sha_file(review_manifest_path),
        "source_review_queue_sha256": sha_file(review_queue_path),
        "unreviewed_tail_draft_unique": len(result),
    }


def stage(paths: list[Path], output_root: Path, tail_drafts: Path | None = None) -> dict:
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100", asset_index=INDEX
    )
    source_audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    cohort = json.loads(COHORT.read_text(encoding="utf-8"))
    if (source_audit.get("status") != "passed"
        or source_audit.get("source_id_multiset_equal") is not True
        or source_audit.get("bundles") != 852
        or source_audit.get("source_queue_sha256") != sha_file(QUEUE)
        or cohort.get("complete") is not True
        or cohort.get("verified") != 852
        or cohort.get("source_index_sha256") != identity["asset_index_sha256"]):
        raise ValueError("event-unit source cohort differs from frozen source audit")

    queue_rows = read_jsonl(QUEUE)
    queue = {x["source_sha256"]: x for x in queue_rows}
    if len(queue) != 13177 or len(queue_rows) != len(queue):
        raise ValueError("unexpected event-unit source universe")
    if any(source_id(x["source"]) != sid for sid, x in queue.items()):
        raise ValueError("event-unit queue has mismatched SHA")
    found, chosen = source_bound_machine_candidates(queue, paths)
    unreviewed_tail: dict[str, dict] = {}
    tail_evidence: dict = {}
    if tail_drafts is not None:
        if not output_root.resolve().is_relative_to(
            (ROOT / "build/localization-90200").resolve()
        ):
            raise ValueError("47-draft QA trial must remain inside build/localization-90200")
        unreviewed_tail, tail_evidence = source_bound_unreviewed_tail_drafts(
            queue, found, tail_drafts, identity
        )
    trial_candidates = {**found, **unreviewed_tail}
    qa_rows: list[dict] = []
    accepted_for_trial: dict[str, dict] = {}
    qa_verdicts: Counter[str] = Counter()
    glossary = load_glossary(None)
    for sid, item in queue.items():
        candidate = trial_candidates.get(sid)
        if candidate is None:
            qa_rows.append({"source_sha256": sid, "qa_verdict": "MISSING_MACHINE"})
            qa_verdicts["MISSING_MACHINE"] += 1
            continue
        qa = evaluate_row(item, candidate, glossary)
        qa_rows.append(qa)
        qa_verdicts[qa["qa_verdict"]] += 1
        if qa["qa_verdict"] == "PASS":
            accepted_for_trial[sid] = candidate
    if not accepted_for_trial:
        raise ValueError("no QA PASS event-unit candidate")

    if output_root.exists():
        raise FileExistsError("candidate-only event-unit root must be new")
    if output_root.resolve().is_relative_to((ROOT / "work/local-assets").resolve()):
        raise ValueError("candidate root cannot reside in the official archive")
    temporary = output_root.with_name(output_root.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError("stale/incomplete event-unit trial root; refuse overwrite")
    temporary.mkdir(parents=True)
    (temporary / "version-identity.json").write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    recorded: list[dict] = []
    count: Counter[str] = Counter()
    for row in cohort["bundles"]:
        remote, logical = row["remote"], row["logical"]
        src = BUNDLE_ROOT / remote
        if src.stat().st_size != row["declared_bytes"] or sha_file(src) != row["sha256"]:
            raise ValueError(f"frozen source hash or size mismatch: {logical}")
        output = temporary / identity["scope"] / remote
        entry = materialize(
            logical, remote, row["declared_bytes"], src, output,
            accepted_for_trial, require_complete=False
        )
        count["bundles_scanned"] += 1
        count["untranslated_source_ids_by_bundle"] += entry["missing_source_count"]
        if entry["output_path"] is not None:
            count["bundles_written"] += 1
            count["text_fields_changed"] += len(entry["changes"])
            entry["source_path"] = str(src)
            entry["output_path"] = str(output_root / identity["scope"] / remote)
            entry["release_gate"] = "NOT_EVALUATED"
            recorded.append(entry)
    if count["bundles_scanned"] != 852:
        raise ValueError("truncated frozen event-unit cohort")
    result = {
        "schema_version": 1,
        "kind": "event-unit-source-bound-QA-candidate-only",
        "version_identity": identity,
        "source_audit_sha256": sha_file(AUDIT),
        "source_queue_sha256": sha_file(QUEUE),
        "source_unique": len(queue),
        "machine_candidate_unique": len(found),
        "unreviewed_agent_tail_draft_unique": len(unreviewed_tail),
        "unreviewed_agent_tail_draft_present": bool(unreviewed_tail),
        "unreviewed_agent_tail_draft_evidence": tail_evidence,
        "qa_verdicts": dict(qa_verdicts),
        "qa_pass_unique": len(accepted_for_trial),
        "input_priorities": [str(p) for p in paths],
        "input_ambiguity_counts": chosen,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "independent_reviewed": False,
        "production_gtx_overlay_modified": False,
        "official_source_archive_modified": False,
        "nas_modified": False,
        "output_root": str(output_root),
        "scope": identity["scope"],
        **dict(count),
        "bundles": recorded,
    }
    (temporary / "event-unit-QA-candidate-manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (temporary / "event-unit-QA-candidate-audit.json").write_text(
        json.dumps(qa_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, output_root)
    return {k: v for k, v in result.items() if k != "bundles"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--translations", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--unreviewed-tail-drafts", type=Path,
        help="Frozen 47 agent suggestions, QA trial only; never a release ledger.",
    )
    args = parser.parse_args()
    print(json.dumps(
        stage(args.translations, args.output_root, args.unreviewed_tail_drafts),
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
