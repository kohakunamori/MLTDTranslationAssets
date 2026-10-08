#!/usr/bin/env python3
"""Isolated MLTD Batch V2 A/B harness. Never writes the production translation store.

Default mode is deterministic MOCK, not an API cost measurement. --live must be
explicitly requested and refuses to share a host with an active production pool.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import heapq
import json
import os
from pathlib import Path
import time
from typing import Any

import httpx

from scripts import translate_mltd_api_pool as legacy
from scripts.mltd_translation_quality import load_glossary

TASK_SIZES = {"UI": 24, "TITLE": 12, "DESCRIPTION": 6,
              "DIALOGUE": 6, "SHARED_SIMPLE": 24, "SHARED": 2}
TASKS = tuple(TASK_SIZES)
MAX_RECURSION = 6


class ProviderUnavailable(RuntimeError):
    """Live provider failure: preserve pending IDs and stop the whole arm."""


def is_provider_failure(error: str) -> bool:
    return error.startswith((
        "quota_exhausted:", "APIRequestError:", "ReadTimeout:",
        "ConnectTimeout:", "RemoteProtocolError:", "ProxyError:",
        "ConnectError:", "TimeoutException:", "HTTPStatusError:",
    ))


BATCH_OUTPUT_CONTRACT = (
    "OUTPUT CONTRACT FOR THIS BATCH ONLY: Return ONLY one JSON object "
    'with "translations" array. Each item has exactly "id" and "translation". '
    "Return exactly one item for each requested ID, no extra or duplicate IDs. "
    "Each translation must be a Simplified Chinese translation of that item's "
    "SOURCE, keeping that item's __MLTD_TOKEN_NNN__, __MLTD_TERM_NNN__, and "
    "__MLTD_NUMBER_NNN__ markers unchanged. Do not translate the context. "
    "Do not wrap the JSON in Markdown."
)


def batch_system_prompt(original: str) -> str:
    lines = original.splitlines()
    if "OUTPUT CONTRACT" not in lines:
        raise ValueError("canonical prompt lacks OUTPUT CONTRACT; fail closed")
    start = lines.index("OUTPUT CONTRACT")
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].strip()), None)
    if end is None:
        raise ValueError("cannot isolate original output contract")
    # Original instruction "one translation only" must not conflict with JSON.
    return "\n".join(lines[:start] + [BATCH_OUTPUT_CONTRACT] + lines[end:]) + "\n"


def parse_batch(raw: str, expected: set[str], *, allow_missing: bool = False) -> dict[str, str]:
    """Strict ID mapping; optionally salvage a validated subset of missing-ID output."""
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"translations"}:
        raise ValueError("expected JSON object containing only translations")
    records = data["translations"]
    if not isinstance(records, list):
        raise ValueError("translations must be array")
    result: dict[str, str] = {}
    for item in records:
        if not isinstance(item, dict) or set(item) != {"id", "translation"}:
            raise ValueError("each translation needs exactly id and translation")
        ident, value = item["id"], item["translation"]
        if not isinstance(ident, str) or ident not in expected or ident in result:
            raise ValueError("extra/duplicate/invalid id")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("empty/non-string translation")
        result[ident] = value.strip()
    if not allow_missing and set(result) != expected:
        raise ValueError(f"missing ids: {len(expected - set(result))}")
    return result


def prepare(row: dict, terms: dict[str, str]) -> dict:
    source = row["source"]
    masked, target_terms, applied = legacy.mask_authoritative_terms(source, terms)
    masked, tokens = legacy.mask_tokens(masked)
    masked, numbers = legacy.mask_numeric_literals(masked)
    return {
        "row": row, "sid": row["source_sha256"], "masked": masked,
        "tokens": tokens, "terms": target_terms, "numbers": numbers,
        "applied_terms": applied, "task": legacy.classify_task(row),
    }


def batches(rows: list[dict], mode: str) -> list[list[dict]]:
    if mode == "single":
        return [[r] for r in rows]
    if mode not in {"8", "16", "dynamic", "dynamic-mixed"}:
        raise ValueError(mode)
    # Each row carries its OWN context and stable SPEAKER codes in the JSON
    # request. dynamic-mixed is an OPT-IN experiment that packs unrelated
    # DIALOGUE speakers into a call without merging their item contexts;
    # ordinary dynamic retains strict per-speaker partitions.
    groups: dict[tuple, list] = {}
    for row in rows:
        task = legacy.classify_task(row)
        speakers, _ = legacy.speaker_info(row)
        partition = (
            task,
            tuple(sorted(speakers))
            if task == "DIALOGUE" and mode != "dynamic-mixed" else (),
        )
        groups.setdefault(partition, []).append(row)
    output = []
    for (task, _), group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        limit = TASK_SIZES[task] if mode.startswith("dynamic") else int(mode)
        current, current_chars = [], 0
        for row in group:
            # Cap dynamic per-item contexts as well as number of rows.
            estimated = len(json.dumps(row.get("context_examples", []),
                                       ensure_ascii=False)) + len(row["source"]) + 120
            if current and (len(current) >= limit or
                            current_chars + estimated > 12000):
                output.append(current)
                current, current_chars = [], 0
            current.append(row)
            current_chars += estimated
        if current:
            output.append(current)
    return output


def sample_pending(queue: Path, done_paths: list[Path], count: int, seed: str) -> list[dict]:
    """Reproducible stratified top-hash sample of uncompleted, source-bound rows."""
    if count % len(TASKS):
        raise ValueError("sample size must be divisible by six (e.g. 240)")
    done = legacy.completed_ids(done_paths)
    per_task = count
    buckets: dict[str, list] = {k: [] for k in TASKS}
    scanned = Counter()
    with queue.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            sid = row.get("source_sha256", "")
            source = row.get("source", "")
            if not source or sid in done or sid != legacy.source_id(source):
                continue
            task = legacy.classify_task(row)
            if task not in buckets:
                continue
            scanned[task] += 1
            priority = int(hashlib.sha256(f"{seed}:{sid}".encode()).hexdigest(), 16)
            heap = buckets[task]
            if len(heap) < per_task:
                heapq.heappush(heap, (-priority, sid, row))
            elif priority < -heap[0][0]:
                heapq.heapreplace(heap, (-priority, sid, row))
    if sum(len(v) for v in buckets.values()) < count:
        raise ValueError(f"not enough pending rows: {dict(scanned)}")
    # Balanced round-robin over actual pending types: some categories may have
    # zero remaining rows, so fixed 40/40/40/40/40/40 is not viable.
    ranked = {k: [row for _, _, row in sorted(v, reverse=True)]
              for k, v in buckets.items()}
    sample = []
    while len(sample) < count:
        progressed = False
        for task in TASKS:
            if ranked[task] and len(sample) < count:
                sample.append(ranked[task].pop())
                progressed = True
        if not progressed:
            raise RuntimeError("sample selection unexpectedly exhausted")
    if len({r["source_sha256"] for r in sample}) != count:
        raise ValueError("sample identities are not unique")
    return sample


def save_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as inp:
        return [json.loads(x) for x in inp if x.strip()]


def verify_sample(rows: list[dict], desired: int) -> None:
    if len(rows) != desired:
        raise ValueError("wrong sample size")
    seen = set()
    for row in rows:
        sid = row.get("source_sha256")
        if sid in seen or sid != legacy.source_id(row.get("source", "")):
            raise ValueError("invalid or duplicate source identity")
        seen.add(sid)


def production_active() -> bool:
    """Conservative guard: failures to inspect host processes block live mode."""
    try:
        import psutil
        own_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            if proc.info["pid"] == own_pid:
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "translate_mltd_api_pool.py" in cmd and "python" in (
                proc.info.get("name") or "").lower():
                return True
    except Exception:
        # A failed process inspection must never silently allow a competing pool.
        return True
    return False


async def check_model_catalog(cfg: legacy.ModelConfig) -> None:
    """Fail closed before a large A/B if this gateway no longer lists the model."""
    endpoint = cfg.endpoint.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if endpoint.endswith(suffix):
            endpoint = endpoint[:-len(suffix)]
            break
    key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(trust_env=True, timeout=15) as client:
        response = await client.get(endpoint + "/models", headers=headers)
    if response.status_code != 200:
        raise ProviderUnavailable(
            f"model catalogue HTTP {response.status_code}; live benchmark blocked")
    try:
        entries = response.json()["data"]
        advertised = {x["id"] for x in entries if isinstance(x, dict)}
    except (TypeError, KeyError, ValueError) as exc:
        raise ProviderUnavailable("invalid model catalogue; live benchmark blocked") from exc
    if cfg.model not in advertised:
        raise ProviderUnavailable(
            f"configured model {cfg.model} is not advertised by gateway; "
            "live benchmark blocked (no translation request sent)")


def mock_translation(prepped: dict) -> str:
    # Preserves markers; exercises parser/QA only, NOT actual language quality.
    return prepped["masked"]


async def run_arm(rows: list[dict], mode: str, args: argparse.Namespace,
                  cfg: Any, system: str, terms: dict, glossary: dict,
                  root: Path) -> dict:
    groups = batches(rows, mode)
    output = root / mode / "translations.jsonl"
    failed_path = root / mode / "failed.jsonl"
    summary_path = root / mode / "summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    old = load_jsonl(output) if output.exists() else []
    accepted = {x["source_sha256"]: x for x in old}
    prior_failed = load_jsonl(failed_path) if failed_path.exists() else []
    failed_ids = {x["source_sha256"] for x in prior_failed}
    known = {r["source_sha256"] for r in rows}
    if (set(accepted) | failed_ids) - known or set(accepted) & failed_ids:
        raise ValueError("output contains unknown or contradictory benchmark IDs")
    if len(old) != len(accepted) or len(prior_failed) != len(failed_ids):
        raise ValueError("duplicate persisted benchmark identities")
    previous_summary = (json.loads(summary_path.read_text(encoding="utf-8"))
                        if summary_path.exists() else {})
    metrics = Counter()
    details = Counter()
    started = time.perf_counter()
    batch_system = batch_system_prompt(system)
    async with httpx.AsyncClient(trust_env=True) as client:
        with (output.open("a", encoding="utf-8", newline="\n") as out,
              failed_path.open("a", encoding="utf-8", newline="\n") as failed_out):
            async def call(group: list[dict], depth: int = 0) -> None:
                pending = [r for r in group if r["source_sha256"] not in accepted
                           and r["source_sha256"] not in failed_ids]
                if not pending:
                    return
                if args.max_requests and metrics["requests"] >= args.max_requests:
                    return
                if mode == "single" or len(pending) == 1:
                    for row in pending:
                        if args.max_requests and metrics["requests"] >= args.max_requests:
                            return
                        task = legacy.classify_task(row)
                        if args.live:
                            # Legacy single-item translation retries internally.
                            # Restrict retries to the remaining per-arm request
                            # budget without modifying cfg or concurrency.
                            remaining = (args.max_requests - metrics["requests"]
                                         if args.max_requests else cfg.retries)
                            limited_cfg = replace(cfg, retries=min(cfg.retries, remaining))
                            stats = defaultdict(lambda: defaultdict(float))
                            result, error = await legacy.translate_with_model(
                                client, limited_cfg, None, system, row,
                                glossary, terms, stats)
                            model_stats = stats[cfg.id]
                            metrics["requests"] += int(model_stats["requests"])
                            for field in ("prompt_tokens", "completion_tokens",
                                          "cached_tokens", "reasoning_tokens"):
                                metrics[field] += model_stats[field]
                            if error:
                                details[f"error:{error[:80]}"] += 1
                        else:
                            p = prepare(row, terms)
                            result, error = legacy.validate_candidate(
                                row, mock_translation(p), p["tokens"], p["terms"],
                                glossary, p["numbers"])
                            if row["source"] in terms:
                                result, error = legacy.validate_candidate(
                                    row, terms[row["source"]], [], [], glossary)
                            metrics["requests"] += 1
                        if result:
                            persist(result, task, 1)
                        else:
                            if args.live and is_provider_failure(error):
                                details["provider_unavailable"] += 1
                                raise ProviderUnavailable(
                                    "provider unavailable; unprocessed source IDs remain pending")
                            persist_failure(row, error)
                    return
                prepared = [prepare(r, terms) for r in pending]
                request = {"items": [
                    {"id": p["sid"], "task": p["task"],
                     "context": legacy.build_user_prompt(p["row"], p["masked"])}
                    for p in prepared
                ]}
                try:
                    if args.live:
                        task, params = legacy.task_request_params_for_row(cfg, pending[0])
                        # Bound output budget in proportion to batch size; gateway may
                        # override lower provider limits. Reject rather than truncate.
                        params = dict(params)
                        params["max_tokens"] = max(
                            int(params.get("max_tokens", 512)), 256 * len(pending))
                        user_payload = json.dumps(request, ensure_ascii=False)
                        for transport_try in range(2):
                            if (args.max_requests and
                                    metrics["requests"] >= args.max_requests):
                                details["transport_retry_skipped_request_cap"] += 1
                                raise ProviderUnavailable(
                                    "request cap reached during transport retry; "
                                    "source IDs remain pending")
                            try:
                                raw, metadata = await legacy.request_translation(
                                    client, cfg,
                                    (batch_system + (
                                        "\nFor this mixed-speaker batch, each "
                                        "item's SPEAKER and PREVIOUS/NEXT context "
                                        "are local to THAT id; do not transfer "
                                        "speaker identity, story context or "
                                        "first-person voice between items."
                                    ) if mode == "dynamic-mixed" else batch_system),
                                    user_payload, params)
                            except httpx.TransportError:
                                metrics["requests"] += 1
                                details["transient_transport_attempts"] += 1
                                if transport_try:
                                    raise
                                await asyncio.sleep(1)
                                continue
                            metrics["requests"] += 1
                            for field in ("prompt_tokens", "completion_tokens",
                                          "cached_tokens", "reasoning_tokens"):
                                metrics[field] += metadata["usage"].get(field, 0)
                            break
                    else:
                        raw = json.dumps({"translations": [
                            {"id": p["sid"], "translation": mock_translation(p)}
                            for p in prepared]}, ensure_ascii=False)
                        metrics["requests"] += 1
                    mapped = parse_batch(raw, {p["sid"] for p in prepared})
                except Exception as exc:
                    # Provider failure is not a translation-format failure:
                    # do not multiply 401/429/5xx errors by splitting the batch.
                    if args.live and isinstance(exc, ProviderUnavailable):
                        raise
                    if args.live and isinstance(exc, legacy.APIRequestError):
                        metrics["requests"] += 1
                        raise
                    if args.live and isinstance(exc, httpx.TransportError):
                        # The bounded same-batch retry above already counted
                        # both transport attempts; do not fan out into halves.
                        raise
                    details[f"batch_error:{type(exc).__name__}"] += 1
                    try:
                        # Missing IDs are recoverable, but duplicates, extra IDs,
                        # malformed JSON, and wrong item shapes are not.
                        mapped = parse_batch(raw, {p["sid"] for p in prepared},
                                             allow_missing=True)
                        details["partial_batch_salvaged"] += len(mapped)
                    except (ValueError, KeyError, UnboundLocalError):
                        mapped = {}
                failed = []
                for p in prepared:
                    if p["sid"] not in mapped:
                        failed.append(p["row"])
                        continue
                    result, error = legacy.validate_candidate(
                        p["row"], mapped[p["sid"]], p["tokens"], p["terms"],
                        glossary, p["numbers"])
                    if result:
                        persist(result, p["task"], len(pending))
                    else:
                        details[f"qa:{error[:70]}"] += 1
                        failed.append(p["row"])
                if failed:
                    details["split_retries"] += 1
                    if depth >= MAX_RECURSION or len(failed) == 1:
                        for row in failed:
                            await call([row], depth + 1)
                    else:
                        mid = len(failed) // 2
                        await call(failed[:mid], depth + 1)
                        await call(failed[mid:], depth + 1)

            def persist_failure(row: dict, error: str) -> None:
                sid = row["source_sha256"]
                if sid in accepted or sid in failed_ids:
                    return
                record = {"source_sha256": sid, "error": error,
                          "benchmark_mock": not args.live}
                failed_out.write(json.dumps(record, ensure_ascii=False) + "\n")
                failed_out.flush()
                failed_ids.add(sid)
                details["failed_items"] += 1

            def persist(result: dict, task: str, group_size: int) -> None:
                sid = result["source_sha256"]
                if sid in accepted:
                    return
                record = dict(result)
                if not args.live:
                    # Fail-safe: infrastructure placeholders must never look
                    # like accepted production translations in a loose glob.
                    record["status"] = "benchmark_mock_not_translation"
                    record["source_like_placeholder"] = True
                record["benchmark"] = {"arm": mode, "task": task,
                                       "group_size": group_size,
                                       "mock": not args.live}
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                accepted[sid] = record
                details["accepted"] += 1

            for group in groups:
                if args.max_requests and metrics["requests"] >= args.max_requests:
                    break
                try:
                    await call(group)
                except (ProviderUnavailable, legacy.APIRequestError,
                        httpx.TransportError) as exc:
                    # An interrupted arm still has durable per-ID translations
                    # and a truthful partial request/token ledger for resume.
                    incomplete = {
                        "arm": mode, "kind": "live" if args.live else "mock",
                        "status": "blocked_provider",
                        "sample_count": len(rows),
                        "accepted_total": len(accepted),
                        "terminal_failed_total": len(failed_ids),
                        "completed_total": len(accepted) + len(failed_ids),
                        "requests_this_run": int(metrics["requests"]),
                        "requests_total": int(previous_summary.get("requests_total", 0)) +
                            int(metrics["requests"]),
                        "prompt_tokens": int(previous_summary.get("prompt_tokens", 0)) +
                            int(metrics["prompt_tokens"]),
                        "completion_tokens": int(previous_summary.get("completion_tokens", 0)) +
                            int(metrics["completion_tokens"]),
                        "cached_tokens": int(previous_summary.get("cached_tokens", 0)) +
                            int(metrics["cached_tokens"]),
                        "reasoning_tokens": int(previous_summary.get("reasoning_tokens", 0)) +
                            int(metrics["reasoning_tokens"]),
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                        "details": dict(details),
                    }
                    summary_path.write_text(
                        json.dumps(incomplete, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
                    raise
    elapsed = time.perf_counter() - started
    initial_input_chars = 0
    initial_shared_prompt_chars = 0
    for group in groups:
        if mode == "single" or len(group) == 1:
            for row in group:
                p = prepare(row, terms)
                initial_input_chars += len(system) + len(
                    legacy.build_user_prompt(row, p["masked"]))
                initial_shared_prompt_chars += len(system)
        else:
            items = []
            for row in group:
                p = prepare(row, terms)
                items.append({"id": p["sid"], "task": p["task"],
                              "context": legacy.build_user_prompt(row, p["masked"])})
            effective_system = batch_system + (
                "\nFor this mixed-speaker batch, each "
                "item's SPEAKER and PREVIOUS/NEXT context "
                "are local to THAT id; do not transfer "
                "speaker identity, story context or "
                "first-person voice between items."
            ) if mode == "dynamic-mixed" else batch_system
            initial_input_chars += len(effective_system) + len(
                json.dumps({"items": items}, ensure_ascii=False))
            initial_shared_prompt_chars += len(effective_system)
    result = {"arm": mode, "kind": "live" if args.live else "mock",
              "sample_count": len(rows), "planned_batches": len(groups),
              "eligible_for_fair_AB_comparison": previous_summary.get(
                  "eligible_for_fair_AB_comparison", True),
              "provider_rejected_request_rows": previous_summary.get(
                  "provider_rejected_request_rows", 0),
              "initial_input_chars_no_retry": initial_input_chars,
              "initial_shared_prompt_chars_no_retry": initial_shared_prompt_chars,
              "requests_this_run": int(metrics["requests"]),
              "requests_total": int(previous_summary.get("requests_total", 0)) + int(metrics["requests"]),
              "accepted_total": len(accepted), "newly_accepted": details["accepted"],
              "terminal_failed_total": len(failed_ids),
              "completed_total": len(accepted) + len(failed_ids),
              "elapsed_seconds": round(elapsed, 3),
              "prompt_tokens": int(previous_summary.get("prompt_tokens", 0) +
                                   metrics["prompt_tokens"]),
              "completion_tokens": int(previous_summary.get("completion_tokens", 0) +
                                       metrics["completion_tokens"]),
              "cached_tokens": int(previous_summary.get("cached_tokens", 0) +
                                    metrics["cached_tokens"]),
              "reasoning_tokens": int(previous_summary.get("reasoning_tokens", 0) +
                                       metrics["reasoning_tokens"]),
              "items_per_request": round(
                  len(accepted) / (int(previous_summary.get("requests_total", 0)) +
                                   metrics["requests"]), 3)
                  if int(previous_summary.get("requests_total", 0)) +
                     metrics["requests"] else None,
              "details": dict(details)}
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sample", type=Path,
                   default=Path("build/localization-batch-v2/benchmark-240.jsonl"))
    p.add_argument("--root", type=Path,
                   default=Path("build/localization-batch-v2"))
    p.add_argument("--queue", type=Path,
                   default=Path("build/localization-90200/machine-translation-queue-context.jsonl"))
    p.add_argument("--count", type=int, default=240)
    p.add_argument("--seed", default="mltd-batch-v2-20260920")
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--allow-shared-provider", action="store_true")
    p.add_argument("--config", type=Path, default=Path("localization/api-models.local.json"))
    p.add_argument("--model-id", default="")
    p.add_argument("--arms", nargs="+",
                   choices=["single", "8", "16", "dynamic", "dynamic-mixed"],
                   default=["single", "8", "16", "dynamic"])
    p.add_argument("--max-requests", type=int, default=0)
    p.add_argument("--skip-model-catalog-check", action="store_true",
                   help="explicit override for gateways with unlisted aliases")
    p.add_argument("--confirm-live-full", action="store_true",
                   help="explicitly authorize all four live 240-item arms")
    return p


async def async_main(args: argparse.Namespace) -> None:
    if args.prepare:
        if args.sample.exists():
            raise FileExistsError("sample already exists; never silently reselect")
        rows = sample_pending(args.queue, [
            Path("build/localization-90200/machine-translations-api.jsonl"),
            Path("build/localization-90200/machine-translations-codex.jsonl")],
            args.count, args.seed)
        save_jsonl(args.sample, rows)
        digest = hashlib.sha256(args.sample.read_bytes()).hexdigest()
        print(json.dumps({"sample": str(args.sample), "rows": len(rows),
                          "tasks": dict(Counter(legacy.classify_task(r) for r in rows)),
                          "sha256": digest}, ensure_ascii=False))
        return
    if args.live and production_active() and not args.allow_shared_provider:
        raise RuntimeError("production translator is active; live A/B is blocked")
    if args.live and args.max_requests <= 0 and not args.confirm_live_full:
        raise ValueError("live comparison requires --max-requests or --confirm-live-full")
    if args.max_requests < 0:
        raise ValueError("--max-requests must not be negative")
    rows = load_jsonl(args.sample)
    verify_sample(rows, args.count)
    cfgs = legacy.load_model_config(args.config) if args.live else []
    if args.live:
        selected = [c for c in cfgs if c.id == args.model_id] if args.model_id else cfgs
        if len(selected) != 1:
            raise ValueError("live benchmark requires exactly one configured model")
        cfg = selected[0]
        if not args.skip_model_catalog_check:
            await check_model_catalog(cfg)
        system, *_ = legacy.resolve_system_prompt(
            argparse.Namespace(prompt_source="compiled", system_prompt=Path(
                "localization/prompts/mltd-zhcn-system.md"),
                prompt_manifest=Path("localization/prompts/mltd-zhcn-system.manifest.json"),
                character_evidence=Path(
                    "build/localization-90200/character-voice-evidence.json"),
                glossary=Path("localization/quality/glossary.json")))
    else:
        cfg = None
        system = Path("localization/prompts/mltd-zhcn-system.md").read_text(encoding="utf-8")
    terms = legacy.load_authoritative_terms(Path(
        "localization/quality/authoritative-terms.json"))
    glossary = load_glossary(Path("localization/quality/glossary.json"))
    root = args.root / ("live" if args.live else "mock")
    root.mkdir(parents=True, exist_ok=True)
    sample_sha = hashlib.sha256(args.sample.read_bytes()).hexdigest()
    manifest = root / "manifest.json"
    signature = {"sample_sha256": sample_sha, "count": args.count,
                 "model": cfg.model if cfg else "mock",
                 "prompt_sha256": hashlib.sha256(system.encode()).hexdigest()}
    if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != signature:
        raise ValueError("sample/model/prompt changed; cannot resume this comparison")
    manifest.write_text(json.dumps(signature, indent=2) + "\n", encoding="utf-8")
    results = []
    for arm in args.arms:
        try:
            result = await run_arm(rows, arm, args, cfg, system, terms, glossary, root)
        except (ProviderUnavailable, legacy.APIRequestError,
                httpx.TransportError) as exc:
            # Persist an audit-safe provider blocker without leaking gateway body,
            # credentials, or treating this as a failed translation.
            blocker = {"arm": arm, "sample_sha256": sample_sha,
                       "status": "blocked_provider_http_error"
                         if isinstance(exc, legacy.APIRequestError)
                         else "blocked_provider_transport_error"
                         if isinstance(exc, httpx.TransportError)
                         else "blocked_provider_unavailable",
                       "http_status": exc.status_code
                         if isinstance(exc, legacy.APIRequestError) else None,
                       "retry_after": exc.retry_after
                         if isinstance(exc, legacy.APIRequestError) else "",
                       "error_type": type(exc).__name__}
            (root / "last-provider-blocker.json").write_text(
                json.dumps(blocker, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            print(json.dumps(blocker, ensure_ascii=False), flush=True)
            raise
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))
    if len(results) > 1:
        baseline = next((x for x in results if x["arm"] == "single"), None)
        comparison = {
            "kind": "live" if args.live else "mock",
            "sample_sha256": sample_sha,
            "results": results,
            "note": (
                "MOCK uses untranslated source-like placeholders to test "
                "infrastructure only. Do not interpret QA or tokens as translation "
                "quality or actual provider savings."
                if not args.live else
                "LIVE is unblinded same-source A/B. Inspect QA and human translation "
                "quality before enabling any production route."
            ),
        }
        if baseline and any(
            x.get("eligible_for_fair_AB_comparison") is False for x in results
        ):
            comparison["note"] += (
                " PROVIDER-REJECTED attempt(s) present: comparison NOT fair; "
                "do not report savings from this experiment.")
        if baseline:
            for result in results:
                if (baseline["requests_total"] and
                    all(x.get("eligible_for_fair_AB_comparison", True)
                        for x in results)):
                    result["request_reduction_vs_single_percent"] = round(
                        100 * (1 - result["requests_total"] /
                               baseline["requests_total"]), 2)
                if baseline["initial_input_chars_no_retry"]:
                    result["initial_input_char_reduction_percent"] = round(
                        100 * (1 - result["initial_input_chars_no_retry"] /
                               baseline["initial_input_chars_no_retry"]), 2)
        (root / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(async_main(parser().parse_args()))
