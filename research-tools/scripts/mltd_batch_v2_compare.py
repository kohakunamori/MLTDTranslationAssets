#!/usr/bin/env python3
"""Render a source-ID-bound paired review of two isolated LIVE MLTD A/B arms.

No API requests. No production translation writes. The CSV records all sampled
sources, including rejected/missing results, for manual side-by-side review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
from typing import Any

from scripts.mltd_batch_v2_benchmark import load_jsonl, verify_sample
from scripts.translate_mltd_api_pool import classify_task, speaker_info


def review(sample: Path, live_root: Path, left: str, right: str) -> dict[str, Any]:
    sample_rows = load_jsonl(sample)
    verify_sample(sample_rows, len(sample_rows))
    manifest = json.loads((live_root / "manifest.json").read_text(encoding="utf-8"))
    import hashlib
    if manifest["sample_sha256"] != hashlib.sha256(sample.read_bytes()).hexdigest():
        raise ValueError("different A/B sample; refusing comparison")
    runs = {}
    for arm in (left, right):
        summary_path = live_root / arm / "summary.json"
        runs[arm] = {
            "summary": json.loads(summary_path.read_text(encoding="utf-8")),
            "translations": {
                row["source_sha256"]: row
                for row in load_jsonl(live_root / arm / "translations.jsonl")
            },
            "failed": {
                row["source_sha256"]: row
                for row in load_jsonl(live_root / arm / "failed.jsonl")
            },
        }
        if runs[arm]["summary"]["kind"] != "live":
            raise ValueError("mock results cannot be used for language-quality review")
        if runs[arm]["summary"].get("eligible_for_fair_AB_comparison") is False:
            raise ValueError(
                "provider-rejected requests contaminated this live arm; "
                "do not calculate a fair A/B saving from this experiment")
        if len(runs[arm]["translations"]) != len(load_jsonl(live_root / arm / "translations.jsonl")):
            raise ValueError("duplicate accepted source IDs")
    rows = []
    counters = Counter()
    task_counts = {}
    for src in sample_rows:
        sid = src["source_sha256"]
        task = classify_task(src)
        speakers, _ = speaker_info(src)
        task_stat = task_counts.setdefault(task, Counter())
        task_stat["sample_rows"] += 1
        record = {"source_sha256": sid, "task": task,
                  "speaker_codes": ",".join(speakers), "source": src["source"],
                  "human_review": "", "human_notes": ""}
        pair = []
        for arm in (left, right):
            translation = runs[arm]["translations"].get(sid)
            failure = runs[arm]["failed"].get(sid)
            record[arm + "_translation"] = (
                translation.get("translation", "") if translation else "")
            record[arm + "_state"] = (
                "accepted_with_nonblocking_qa" if translation and translation.get("qa")
                else "accepted" if translation
                else "terminal_failed" if failure else "not_run_or_pending")
            record[arm + "_qa_codes"] = ",".join(
                issue.get("code", "") for issue in
                (translation or {}).get("qa", {}).get("issues", [])
                if isinstance(issue, dict))
            record[arm + "_error"] = (failure or {}).get("error", "")
            pair.append(translation)
            counters[arm + "_" + record[arm + "_state"]] += 1
            task_stat[arm + "_" + record[arm + "_state"]] += 1
        if pair[0] and pair[1]:
            if pair[0]["translation"] == pair[1]["translation"]:
                counters["identical_pairs"] += 1
                task_stat["identical_pairs"] += 1
            else:
                counters["different_pairs"] += 1
                task_stat["different_pairs"] += 1
        else:
            counters["incomplete_pairs"] += 1
            task_stat["incomplete_pairs"] += 1
        source_breaks = src["source"].count("\n")
        record[left + "_source_line_break_mismatch"] = bool(
            pair[0] and pair[0]["translation"].count("\n") != source_breaks)
        record[right + "_source_line_break_mismatch"] = bool(
            pair[1] and pair[1]["translation"].count("\n") != source_breaks)
        for arm in (left, right):
            if record[arm + "_source_line_break_mismatch"]:
                counters[arm + "_source_line_break_mismatch"] += 1
                task_stat[arm + "_source_line_break_mismatch"] += 1
        record["line_break_count_mismatch"] = bool(
            pair[0] and pair[1] and
            pair[0]["translation"].count("\n") != pair[1]["translation"].count("\n"))
        if record["line_break_count_mismatch"]:
            counters["line_break_count_mismatch"] += 1
            task_stat["line_break_count_mismatch"] += 1
        record["review_priority"] = (
            "high" if record["line_break_count_mismatch"] or
            any(record[arm + "_qa_codes"] or
                record[arm + "_source_line_break_mismatch"]
                for arm in (left, right))
            else "medium" if pair[0] and pair[1] and
            pair[0]["translation"] != pair[1]["translation"]
            else "low" if pair[0] and pair[1] else "pending")
        counters["review_priority_" + record["review_priority"]] += 1
        rows.append(record)
    fields = [
        "source_sha256", "task", "speaker_codes", "source",
        left + "_translation", right + "_translation",
        left + "_state", right + "_state",
        left + "_qa_codes", right + "_qa_codes",
        left + "_error", right + "_error",
        left + "_source_line_break_mismatch",
        right + "_source_line_break_mismatch",
        "line_break_count_mismatch", "review_priority",
        "human_review", "human_notes",
    ]
    csv_path = live_root / f"human-review-{left}-vs-{right}.csv"
    # Retain an editor's manual decisions when recomputing the report after
    # resuming a partial arm; never silently erase completed human review.
    if csv_path.is_file():
        with csv_path.open(encoding="utf-8-sig", newline="") as previous_file:
            reviewed = {
                item["source_sha256"]: item
                for item in csv.DictReader(previous_file)
                if item.get("source_sha256")
            }
        for item in rows:
            before = reviewed.get(item["source_sha256"], {})
            item["human_review"] = before.get("human_review", "")
            item["human_notes"] = before.get("human_notes", "")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    metrics = {}
    for arm in (left, right):
        summary = runs[arm]["summary"]
        attempts = summary.get("requests_total", 0)
        covered = len(runs[arm]["translations"])
        prompt = summary.get("prompt_tokens", 0)
        cached = summary.get("cached_tokens", 0)
        elapsed = summary.get("elapsed_seconds")
        metrics[arm] = {
            "accepted": covered,
            "terminal_failed": len(runs[arm]["failed"]),
            "provider_requests": attempts,
            "accepted_per_request": round(covered / attempts, 4)
                if attempts else None,
            "requests_per_100_accepted": round(100 * attempts / covered, 2)
                if covered else None,
            "elapsed_seconds": elapsed,
            "accepted_per_minute": round(60 * covered / elapsed, 3)
                if isinstance(elapsed, (int, float)) and elapsed > 0 else None,
            "seconds_per_accepted": round(elapsed / covered, 3)
                if isinstance(elapsed, (int, float)) and covered else None,
            "prompt_tokens": prompt,
            "uncached_prompt_tokens": max(prompt - cached, 0),
            "completion_tokens": summary.get("completion_tokens", 0),
            "reasoning_tokens": summary.get("reasoning_tokens", 0),
            "input_tokens_per_accepted": round(prompt / covered, 2)
                if covered else None,
            "uncached_input_tokens_per_accepted": round(
                max(prompt - cached, 0) / covered, 2) if covered else None,
        }
    fully_paired = all(
        len(runs[arm]["translations"]) + len(runs[arm]["failed"]) == len(rows)
        and runs[arm]["summary"].get("status") not in (
            "blocked_provider", "partial_provider_route_rejections_quarantined")
        for arm in (left, right))
    request_first = {
        "objective": "reduce_provider_requests_without_worsening_blocking_QA",
        "human_review_pending": True,
        "same_accepted_source_ids": (
            set(runs[left]["translations"]) == set(runs[right]["translations"])),
        "same_terminal_failed_source_ids": (
            set(runs[left]["failed"]) == set(runs[right]["failed"])),
        "request_reduction_percent": None,
        "accepted_throughput_gain_percent": None,
        "wall_time_reduction_percent": None,
        "observed_request_target_met": False,
        "observed_quality_gate_met": False,
        "eligible_for_production_auto_merge": False,
    }
    if fully_paired:
        left_requests = metrics[left]["provider_requests"]
        right_requests = metrics[right]["provider_requests"]
        if left_requests:
            request_first["request_reduction_percent"] = round(
                100 * (1 - right_requests / left_requests), 2)
            # User goal is fewer calls, not an arbitrary 50% threshold.
            # Strict dynamic has already removed most single-item calls;
            # evaluate experimental arms against the immediately prior arm.
            request_first["observed_request_target_met"] = (
                right_requests < left_requests)
        a = metrics[left]["accepted_per_minute"]
        b = metrics[right]["accepted_per_minute"]
        if a and b:
            request_first["accepted_throughput_gain_percent"] = round(
                100 * (b / a - 1), 2)
        ta = metrics[left]["elapsed_seconds"]
        tb = metrics[right]["elapsed_seconds"]
        if isinstance(ta, (int, float)) and ta > 0 and isinstance(tb, (int, float)):
            request_first["wall_time_reduction_percent"] = round(
                100 * (1 - tb / ta), 2)
        request_first["observed_quality_gate_met"] = (
            request_first["same_accepted_source_ids"] and
            request_first["same_terminal_failed_source_ids"])
    result = {
        "sample_rows": len(rows), "left": left, "right": right,
        "request_first_objective": request_first,
        "all_sources_accounted_for": fully_paired,
        "eligible_for_full_AB_savings": fully_paired,
        "sample_sha256": manifest["sample_sha256"],
        "pair_counts": dict(counters),
        "by_task": {task: dict(values) for task, values in sorted(task_counts.items())},
        "metrics": metrics,
        "human_review_csv": str(csv_path),
        "caution": (
            "Identical translations do not prove quality, differences are not "
            "automatically mistakes; human_review and human_notes remain blank "
            "until a person assesses source fidelity and naturalness."
        ),
    }
    report = live_root / f"quality-comparison-{left}-vs-{right}.json"
    report.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sample", type=Path, required=True)
    p.add_argument("--live-root", type=Path, required=True)
    p.add_argument("--left", default="single")
    p.add_argument("--right", default="dynamic")
    args = p.parse_args()
    print(json.dumps(review(args.sample, args.live_root, args.left, args.right),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
