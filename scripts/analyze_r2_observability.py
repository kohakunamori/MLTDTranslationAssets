#!/usr/bin/env python3
"""Summarize Cloudflare Workers cache telemetry and R2 Data Access Logs.

Input: Observability JSON-array or JSONL export(s). Aggregate only; never
print client IPs, original request URLs, object keys or content hashes.
R2 Data Access Logs are best-effort and exclude failed HTTP >=400 operations.
Workers logs are sampled by Cloudflare at the configured invocation rate.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

MARKER = "mltd_gateway_metrics "
CLASS_B = {"GetObject", "HeadObject"}
CLASS_A = {
    "ListObjectsV1", "ListObjectsV2", "PutObject", "CopyObject",
    "CreateMultipartUpload", "UploadPart", "UploadPartCopy",
    "CompleteMultipartUpload", "AbortMultipartUpload",
    "ListMultipartUploads", "ListParts",
}
FREE = {"DeleteObject", "DeleteObjects", "DeleteObjectsByPrefix"}


def load(path: Path):
    content = path.read_text(encoding="utf-8-sig")
    if not content.strip():
        return []
    if content.lstrip().startswith("["):
        data = json.loads(content)
        if not isinstance(data, list):
            raise ValueError("Expected a JSON event array")
        return data
    return [json.loads(line) for line in content.splitlines() if line.strip()]


def r2_event(entry):
    if not isinstance(entry, dict):
        return None
    metadata = entry.get("$metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    if entry.get("dataset") != "r2" and metadata.get("namespace") != "r2":
        return None
    source = entry.get("source", {})
    if not isinstance(source, dict):
        source = {}
    data = dict(source)
    for field in ("action", "scriptName", "bucket", "interface", "requestMetadata"):
        if field not in data and field in entry:
            data[field] = entry[field]
    if data.get("bucket") is None:
        data["bucket"] = metadata.get("service")
    return data


def worker_cache_event(entry):
    if not isinstance(entry, dict):
        return None
    source = entry.get("source", {})
    metadata = entry.get("$metadata", {})
    messages = [
        source.get("message") if isinstance(source, dict) else None,
        metadata.get("message") if isinstance(metadata, dict) else None,
        entry.get("message"),
    ]
    for message in messages:
        if not isinstance(message, str):
            continue
        if message.startswith(MARKER):
            data = json.loads(message[len(MARKER):])
            if data.get("event") == "mltd_gateway_cache":
                return data
    return None


def summarize(entries, bucket="mltd-localized-assets"):
    totals = Counter()
    r2_actions = Counter()
    r2_workers = Counter()
    r2_colos = Counter()
    r2_worker_action = Counter()
    caches = Counter()
    cache_colos = Counter()
    gateway_gets = Counter()
    for entry in entries:
        access = r2_event(entry)
        if access is not None:
            if access.get("bucket") != bucket:
                continue
            operation = str(access.get("action") or "unknown")
            script = str(access.get("scriptName") or "other")
            colo_data = access.get("requestMetadata") or {}
            colo = str(colo_data.get("colo") or "unknown") if isinstance(colo_data, dict) else "unknown"
            totals["r2_events"] += 1
            if operation in CLASS_B:
                totals["r2_class_b_observed"] += 1
            elif operation in CLASS_A:
                totals["r2_class_a_observed"] += 1
            elif operation in FREE:
                totals["r2_free_delete_observed"] += 1
            else:
                totals["r2_unclassified"] += 1
            r2_actions[operation] += 1
            r2_workers[script] += 1
            r2_colos[colo] += 1
            r2_worker_action[(script, operation)] += 1
            continue

        event = worker_cache_event(entry)
        if event is None:
            continue
        colo = str(event.get("colo") or "unknown")
        category = str(event.get("cache") or "none")
        totals["gateway_samples"] += 1
        caches[category] += 1
        cache_colos[(colo, category)] += 1
        gateway_gets["pack_get_attempts"] += int(event.get("r2_pack_gets") or 0)
        gateway_gets["object_get_attempts"] += int(event.get("r2_object_gets") or 0)
        if event.get("shared_pack_inflight"):
            totals["shared_pack_samples"] += 1

    return {
        "r2": {
            "bucket": bucket,
            "total_events": totals["r2_events"],
            "class_a_observed": totals["r2_class_a_observed"],
            "class_b_observed": totals["r2_class_b_observed"],
            "free_deletes_observed": totals["r2_free_delete_observed"],
            "unclassified": totals["r2_unclassified"],
            "by_action": dict(sorted(r2_actions.items())),
            "by_script": dict(sorted(r2_workers.items())),
            "by_colo": dict(sorted(r2_colos.items())),
            "by_script_action": {f"{script} / {action}": count for (script, action), count in
                                  sorted(r2_worker_action.items())},
        },
        "gateway": {
            "sampled_requests": totals["gateway_samples"],
            "cache_outcomes": dict(sorted(caches.items())),
            "cache_outcomes_by_colo": {f"{colo} / {outcome}": count for (colo, outcome), count in
                                       sorted(cache_colos.items())},
            "sampled_r2_get_attempts": dict(sorted(gateway_gets.items())),
            "shared_pack_samples": totals["shared_pack_samples"],
        },
        "limitations": [
            "R2 logs are best effort, may be delayed and exclude HTTP >=400 failures.",
            "Gateway telemetry is Worker invocation sampled; samples are not a complete request count.",
            "Attempted Gateway R2 GETs and observed R2 events do not replace actual billing metrics.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+", help="Cloudflare Observability JSON or JSONL exports")
    parser.add_argument("--bucket", default="mltd-localized-assets")
    parser.add_argument("--json", action="store_true", help="Machine-readable grouped summary")
    args = parser.parse_args(argv)
    rows = []
    for path in args.files:
        rows.extend(load(path))
    result = summarize(rows, args.bucket)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        r2, gateway = result["r2"], result["gateway"]
        print("R2 Data Access Logs (observed, best effort)")
        print(f"  events={r2['total_events']} Class A={r2['class_a_observed']} "
              f"Class B={r2['class_b_observed']} free deletes={r2['free_deletes_observed']}")
        for field in ("by_action", "by_script", "by_colo", "by_script_action"):
            print(f"  {field}: {r2[field]}")
        print("Gateway Worker logs (sampled invocations)")
        print(f"  requests={gateway['sampled_requests']} "
              f"shared_pack={gateway['shared_pack_samples']} "
              f"get_attempts={gateway['sampled_r2_get_attempts']}")
        for field in ("cache_outcomes", "cache_outcomes_by_colo"):
            print(f"  {field}: {gateway[field]}")
        for note in result["limitations"]:
            print("  Note: " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
