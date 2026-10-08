#!/usr/bin/env python3
"""Production Batch V2 adapter for the canonical MLTD API translator.

Streaming groups use the tested Batch V2 JSON/identity contract, then the
legacy masking, candidate QA, provenance, writer, and single-item fallback.
No benchmark translation is imported into the production memory.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

import httpx

from scripts import mltd_batch_v2_benchmark as v2
from scripts import translate_mltd_api_pool as legacy

MAX_BATCH_CONTEXT_CHARS = 12000
MIXED_SPEAKER_WARNING = (
    "\nFor this mixed-speaker batch, each item's SPEAKER and PREVIOUS/NEXT "
    "context are local to THAT id; do not transfer speaker identity, story "
    "context or first-person voice between items."
)


def group_key(row: dict, mode: str) -> tuple:
    task = legacy.classify_task(row)
    speaker_codes, _ = legacy.speaker_info(row)
    return (
        task,
        tuple(sorted(speaker_codes)) if task == "DIALOGUE" and mode == "dynamic" else (),
    )


def item_size(row: dict, terms: dict[str, str]) -> int:
    """Estimate the serialized per-item payload (plus array separator).

    context_examples may contain many occurrences that never reach the
    model: build_user_prompt sends only the actual row-specific context.
    Counting complete evidence JSON was needlessly breaking full batches.
    Include the real masked SOURCE and the same JSON serialization as the
    request, preserving the 12k character bound without removing context.
    """
    prepared = v2.prepare(row, terms)
    record = {
        "id": prepared["sid"],
        "task": prepared["task"],
        "context": legacy.build_user_prompt(row, prepared["masked"]),
    }
    # json.dumps defaults (including spaces) match translate_group's request.
    # Two extra chars allow for the comma/space between items.
    return len(json.dumps(record, ensure_ascii=False)) + 2


async def producer_batch(
    input_path: Any,
    queue: asyncio.Queue,
    done_ids: set[str],
    max_items: int,
    stats: dict,
    mode: str,
    authoritative_terms: dict[str, str] | None = None,
) -> None:
    """Read large queue once; hold only incomplete groups, not all pending rows."""
    authoritative_terms = authoritative_terms or {}
    selected_ids: set[str] = set()
    groups: dict[tuple, list[legacy.WorkItem]] = defaultdict(list)
    sizes: dict[tuple, int] = defaultdict(int)
    selected = scanned = produced_groups = duplicate_ids = 0
    budget_flushes = item_limit_flushes = 0
    total_request_item_chars = peak_request_item_chars = 0

    async def flush(key: tuple) -> None:
        nonlocal produced_groups, total_request_item_chars, peak_request_item_chars
        if groups[key]:
            await queue.put(groups.pop(key))
            request_chars = sizes.pop(key, 0)
            total_request_item_chars += request_chars
            peak_request_item_chars = max(peak_request_item_chars, request_chars)
            produced_groups += 1

    with input_path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            scanned += 1
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{input_path}:{line_no}: expected object")
            sid = str(row.get("source_sha256", ""))
            source = str(row.get("source", ""))
            if not sid or not source or sid in done_ids:
                continue
            if sid in selected_ids:
                duplicate_ids += 1
                continue
            key = group_key(row, mode)
            item_chars = item_size(row, authoritative_terms)
            limit = v2.TASK_SIZES[key[0]]
            if groups[key] and (
                len(groups[key]) >= limit
                or sizes[key] + item_chars > MAX_BATCH_CONTEXT_CHARS
            ):
                if len(groups[key]) >= limit:
                    item_limit_flushes += 1
                else:
                    budget_flushes += 1
                await flush(key)
            groups[key].append(legacy.WorkItem(row=row))
            sizes[key] += item_chars
            selected_ids.add(sid)
            selected += 1
            if max_items and selected >= max_items:
                break
    for key in list(groups):
        await flush(key)
    stats.update({
        "scanned_rows": scanned,
        "selected_pending": selected,
        "batch_groups": produced_groups,
        "batch_item_limit_flushes": item_limit_flushes,
        "batch_context_budget_flushes": budget_flushes,
        "batch_estimated_item_chars": total_request_item_chars,
        "batch_peak_estimated_item_chars": peak_request_item_chars,
        "skipped_duplicate_pending_ids": duplicate_ids,
    })


def provider_error(error: str) -> bool:
    # Do not turn model route, gateway, transport or quota failures into
    # terminal translation failures: resume must retain these source IDs.
    return error.startswith("provider_unavailable:") or v2.is_provider_failure(error)


async def translate_group(
    items: list[legacy.WorkItem],
    client: httpx.AsyncClient,
    cfg: legacy.ModelConfig,
    rate_limiter: legacy.AsyncRequestRateLimiter | None,
    system_prompt: str,
    batch_system: str,
    glossary: dict,
    terms: dict[str, str],
    stats: dict[str, dict[str, float]],
    mode: str,
    request_guard: Any,
) -> dict[str, tuple[dict | None, str]]:
    results: dict[str, tuple[dict | None, str]] = {}
    available = []
    for item in items:
        row = item.row
        sid = str(row.get("source_sha256", ""))
        source = str(row.get("source", ""))
        if not source or sid != legacy.source_id(source):
            results[sid] = (None, "source_identity_mismatch")
        elif source in terms:
            # Original authoritative-term short circuit; zero provider requests.
            results[sid] = await legacy.translate_with_model(
                client, cfg, rate_limiter, system_prompt, row,
                glossary, terms, stats, request_guard
            )
        else:
            available.append(row)

    async def run(rows: list[dict], depth: int = 0) -> None:
        if not rows:
            return
        if len(rows) == 1:
            row = rows[0]
            results[row["source_sha256"]] = await legacy.translate_with_model(
                client, cfg, rate_limiter, system_prompt, row,
                glossary, terms, stats, request_guard
            )
            return
        if not request_guard():
            for row in rows:
                results[row["source_sha256"]] = (
                    None, "quota_exhausted:model_disabled_before_request"
                )
            return
        prepared = [v2.prepare(row, terms) for row in rows]
        task, params = legacy.task_request_params_for_row(cfg, rows[0])
        params = dict(params)
        params["max_tokens"] = max(int(params.get("max_tokens", 512)), 256 * len(rows))
        request = {"items": [
            {
                "id": p["sid"], "task": p["task"],
                "context": legacy.build_user_prompt(p["row"], p["masked"]),
            }
            for p in prepared
        ]}
        task_prefix = f"task::{task}::"
        # A single transient gateway error must not disable the only model
        # and defer the entire remaining production queue. Reuse the bounded
        # transport retry policy from the single-item translator. Never retry
        # exhausted daily quota, other non-retryable 4xx, or unknown exceptions.
        for attempt in range(cfg.retries + 1):
            if not request_guard():
                for row in rows:
                    results[row["source_sha256"]] = (
                        None, "quota_exhausted:model_disabled_before_request"
                    )
                return
            stats[cfg.id]["batch_requests"] += 1
            stats[cfg.id]["batch_items_submitted"] += len(rows)
            try:
                raw, meta = await legacy.request_translation(
                    client, cfg,
                    batch_system + (MIXED_SPEAKER_WARNING if mode == "dynamic-mixed" else ""),
                    json.dumps(request, ensure_ascii=False),
                    params, rate_limiter, request_guard,
                )
            except legacy.RequestAbortedBeforeSend:
                for row in rows:
                    results[row["source_sha256"]] = (
                        None, "quota_exhausted:model_disabled_before_request"
                    )
                return
            except (legacy.APIRequestError, httpx.TransportError, TimeoutError) as exc:
                stats[cfg.id]["requests"] += 1
                stats[cfg.id][task_prefix + "requests"] += 1
                stats[cfg.id]["request_errors"] += 1
                stats[cfg.id][task_prefix + "request_errors"] += 1
                error = f"{type(exc).__name__}:{exc}"
                if isinstance(exc, legacy.APIRequestError) and exc.status_code == 429:
                    stats[cfg.id]["rate_limit_errors"] += 1
                if legacy.api_error_is_quota_exhausted(exc):
                    error = f"quota_exhausted:{error}"
                    stats[cfg.id]["quota_exhausted_errors"] += 1
                elif legacy.should_retry_transport_error(exc) and attempt < cfg.retries:
                    delay = legacy.retry_delay_seconds(exc, attempt + 1)
                    stats[cfg.id]["retries"] += 1
                    stats[cfg.id]["retry_sleep_seconds"] += delay
                    await asyncio.sleep(delay)
                    continue
                else:
                    error = f"provider_unavailable:{error}"
                for row in rows:
                    results[row["source_sha256"]] = (None, error)
                return
            except Exception as exc:
                # An unknown provider exception cannot justify fan-out calls or
                # persisting false QA failures.
                stats[cfg.id]["requests"] += 1
                stats[cfg.id]["request_errors"] += 1
                for row in rows:
                    results[row["source_sha256"]] = (
                        None, f"provider_unavailable:{type(exc).__name__}:{exc}"
                    )
                return
            stats[cfg.id]["requests"] += 1
            stats[cfg.id][task_prefix + "requests"] += 1
            break
        stats[cfg.id]["latency_seconds"] += float(meta["latency_seconds"])
        stats[cfg.id][task_prefix + "latency_seconds"] += float(meta["latency_seconds"])
        usage = meta.get("usage", {})
        for field in (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "cached_tokens", "cache_write_tokens", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens", "reasoning_tokens",
            "accepted_prediction_tokens",
        ):
            val = usage.get(field)
            if isinstance(val, (int, float)):
                stats[cfg.id][field] += float(val)
                stats[cfg.id][task_prefix + field] += float(val)
        if meta.get("cache_hint_fallback"):
            stats[cfg.id]["cache_hint_fallbacks"] += 1
        try:
            mapped = v2.parse_batch(raw, {p["sid"] for p in prepared})
        except (ValueError, TypeError, json.JSONDecodeError):
            stats[cfg.id]["batch_parse_retries"] += 1
            try:
                mapped = v2.parse_batch(
                    raw, {p["sid"] for p in prepared}, allow_missing=True
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                mapped = {}
            stats[cfg.id]["batch_partial_salvaged"] += len(mapped)

        retry_rows = []
        for p in prepared:
            sid = p["sid"]
            if sid not in mapped:
                retry_rows.append(p["row"])
                continue
            candidate, error = legacy.validate_candidate(
                p["row"], mapped[sid], p["tokens"], p["terms"],
                glossary, p["numbers"]
            )
            if candidate is None:
                stats[cfg.id]["qa_retries"] += 1
                stats[cfg.id][task_prefix + "qa_retries"] += 1
                retry_rows.append(p["row"])
                continue
            candidate["provenance"] = {
                "provider": cfg.endpoint,
                "model_id": cfg.id,
                "model": cfg.model,
                "task": p["task"],
                "reasoning_effort": params.get(
                    "reasoning_effort", cfg.reasoning_effort
                ),
                "request_params": {**cfg.request_params, **params},
                "authoritative_terms": p["applied_terms"],
                "batch_v2": {
                    "mode": mode, "group_size": len(rows),
                    "source_id_bound": True,
                },
            }
            stats[cfg.id]["accepted"] += 1
            stats[cfg.id][task_prefix + "accepted"] += 1
            stats[cfg.id]["batch_accepted"] += 1
            results[sid] = (candidate, "")
        if retry_rows:
            stats[cfg.id]["batch_split_retries"] += 1
            if depth >= v2.MAX_RECURSION or len(retry_rows) == 1:
                for row in retry_rows:
                    await run([row], depth + 1)
            else:
                middle = len(retry_rows) // 2
                await run(retry_rows[:middle], depth + 1)
                await run(retry_rows[middle:], depth + 1)

    await run(available)
    return results


async def batch_worker(
    cfg: legacy.ModelConfig,
    client: httpx.AsyncClient,
    rate_limiter: legacy.AsyncRequestRateLimiter | None,
    work_queue: asyncio.Queue,
    result_queue: asyncio.Queue,
    system_prompt: str,
    glossary: dict,
    authoritative_terms: dict[str, str],
    all_model_ids: set[str],
    disabled_model_ids: set[str],
    model_stats: dict[str, dict[str, float]],
    mode: str,
) -> None:
    batch_system = v2.batch_system_prompt(system_prompt)
    while True:
        items = await work_queue.get()
        if items is None:
            work_queue.task_done()
            return
        if cfg.id in disabled_model_ids:
            if disabled_model_ids >= all_model_ids:
                for item in items:
                    await result_queue.put(("deferred", {
                        "source_sha256": item.row.get("source_sha256", ""),
                        "reason": "all_models_unavailable",
                    }))
            else:
                await work_queue.put(items)
                await asyncio.sleep(0.02)
            work_queue.task_done()
            continue
        eligible = [x for x in items if cfg.id not in x.failed_models]
        other = [x for x in items if cfg.id in x.failed_models]
        if eligible:
            result = await translate_group(
                eligible, client, cfg, rate_limiter, system_prompt,
                batch_system, glossary, authoritative_terms,
                model_stats, mode, lambda: cfg.id not in disabled_model_ids,
            )
            for item in eligible:
                sid = item.row["source_sha256"]
                candidate, error = result[sid]
                if candidate is not None:
                    await result_queue.put(("accepted", candidate))
                    continue
                if provider_error(error):
                    if cfg.id not in disabled_model_ids:
                        disabled_model_ids.add(cfg.id)
                        model_stats[cfg.id]["provider_circuit_breaker_trips"] += 1
                    if disabled_model_ids >= all_model_ids:
                        await result_queue.put(("deferred", {
                            "source_sha256": sid, "reason": error[:240],
                        }))
                    else:
                        other.append(item)
                    continue
                item.failed_models.add(cfg.id)
                item.errors.append(f"{cfg.id}:{error}")
                model_stats[cfg.id]["terminal_item_failures"] += 1
                if item.failed_models >= all_model_ids:
                    await result_queue.put(("failed", {
                        "source_sha256": sid,
                        "source": str(item.row.get("source", "")),
                        "errors": item.errors,
                    }))
                else:
                    other.append(item)
        if other:
            if disabled_model_ids >= all_model_ids:
                for item in other:
                    await result_queue.put(("deferred", {
                        "source_sha256": item.row.get("source_sha256", ""),
                        "reason": "all_models_unavailable",
                    }))
            else:
                # Requeue only unresolved IDs. The next model can validate them.
                await work_queue.put(other)
        work_queue.task_done()
