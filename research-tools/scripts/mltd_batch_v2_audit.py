#!/usr/bin/env python3
"""Read-only progress/identity audit for Batch V2 A/B, including partial runs.

No API calls, no production memory reads, no writes; only explicit sample
and isolated benchmark root are inspected.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from scripts.mltd_batch_v2_benchmark import load_jsonl, verify_sample
from scripts.translate_mltd_api_pool import classify_task


def audit(sample: Path, live_root: Path, arms: tuple[str, ...]) -> dict:
    rows = load_jsonl(sample)
    verify_sample(rows, len(rows))
    sha = hashlib.sha256(sample.read_bytes()).hexdigest()
    manifest_path = live_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("missing experiment manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["sample_sha256"] != sha or manifest["count"] != len(rows):
        raise ValueError("sample identity/count mismatch; refusing experiment audit")
    known = {r["source_sha256"]: r for r in rows}
    result = {"sample_sha256": sha, "sample_size": len(rows),
              "model": manifest["model"],
              "sample_tasks": dict(Counter(classify_task(r) for r in rows)),
              "arms": {}}
    for arm in arms:
        if arm not in ("single", "8", "16", "dynamic", "dynamic-mixed"):
            raise ValueError("unknown arm")
        folder = live_root / arm
        accepted_path = folder / "translations.jsonl"
        failed_path = folder / "failed.jsonl"
        summary_path = folder / "summary.json"
        accepted = load_jsonl(accepted_path) if accepted_path.is_file() else []
        failed = load_jsonl(failed_path) if failed_path.is_file() else []
        aid = [r["source_sha256"] for r in accepted]
        fid = [r["source_sha256"] for r in failed]
        if (len(aid) != len(set(aid)) or len(fid) != len(set(fid)) or
                set(aid) & set(fid) or (set(aid) | set(fid)) - set(known)):
            raise ValueError(f"{arm}: duplicate/contradictory/unknown IDs")
        for record in accepted:
            source = known[record["source_sha256"]]["source"]
            if record.get("source") != source:
                raise ValueError(f"{arm}: accepted source text changed")
            if record.get("benchmark", {}).get("mock") is not False:
                raise ValueError(f"{arm}: mock/untagged result in LIVE audit")
        summary = (json.loads(summary_path.read_text(encoding="utf-8"))
                   if summary_path.is_file() else {})
        if summary and (summary.get("accepted_total") != len(accepted) or
                        summary.get("terminal_failed_total") != len(failed)):
            raise ValueError(f"{arm}: ledger disagrees with durable results")
        done = set(aid) | set(fid)
        review_flagged = sum(bool(r.get("qa")) for r in accepted)
        quarantines = sorted(folder.glob("provider-route-*-quarantine-*.jsonl"))
        rejected = sum(len(load_jsonl(p)) for p in quarantines)
        if rejected and summary.get("eligible_for_fair_AB_comparison") is not False:
            raise ValueError(f"{arm}: quarantined provider failures not marked unfair")
        completed = len(done)
        ready = (completed == len(rows) and
                 bool(summary) and
                 summary.get("status") not in (
                     "blocked_provider",
                     "partial_provider_route_rejections_quarantined") and
                 summary.get("eligible_for_fair_AB_comparison", True))
        result["arms"][arm] = {
            "accepted": len(accepted),
            "terminal_translation_failed": len(failed),
            "pending": len(rows) - completed,
            "nonblocking_qa_flagged": review_flagged,
            "provider_rejections_quarantined": rejected,
            "requests_total_from_last_checkpoint": summary.get("requests_total"),
            "prompt_tokens_from_last_checkpoint": summary.get("prompt_tokens"),
            "last_checkpoint_status": summary.get("status", "no_final_checkpoint"),
            "eligible_for_fair_AB_comparison": (
                bool(ready)),
            "source_ids_validated": True,
            "completion": "complete" if ready else "partial_or_unfair",
        }
    result["all_arms_complete_and_fair"] = all(
        r["eligible_for_fair_AB_comparison"]
        for r in result["arms"].values())
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sample", type=Path, required=True)
    p.add_argument("--live-root", type=Path, required=True)
    p.add_argument("--arms", nargs="+",
                   choices=["single", "8", "16", "dynamic", "dynamic-mixed"],
                   default=["single", "dynamic"])
    args = p.parse_args()
    print(json.dumps(audit(args.sample, args.live_root, tuple(args.arms)),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
