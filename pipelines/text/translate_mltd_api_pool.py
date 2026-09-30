#!/usr/bin/env python3
"""MLTD translation pool with Batch V2 grouping and legacy single-item fallback."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mltd_localize_gtx import read_jsonl, validate_translation
from mltd_translation_quality import NUMBER_RE, evaluate_row, load_glossary
from mltd_translation_prompt import compile_prompt_bundle, snapshot_status
from translate_gtx_queue import (
    ACCEPTED_STATUS,
    BASIC_BLOCKING_QA_CODES,
    mask_tokens,
    restore_tokens,
    source_id,
)

TERM_MARKER_RE = re.compile(r"__MLTD_TERM_(\d{3})__")
NUMBER_MARKER_RE = re.compile(r"__MLTD_NUMBER_(\d{3})__")
EXISTING_MARKER_OR_NUMBER_RE = re.compile(
    r"__MLTD_(?:TERM|TOKEN)_\d{3}__|" + NUMBER_RE.pattern
)

CONFIG_ENVIRONMENT_KEYS = {
    "http_proxy": "HTTP_PROXY",
    "https_proxy": "HTTPS_PROXY",
    "all_proxy": "ALL_PROXY",
    "no_proxy": "NO_PROXY",
}
RUNTIME_CONFIG_NAME = "api-runtime.local.json"


@dataclass(frozen=True)
class ModelConfig:
    id: str
    model: str
    api_protocol: str
    endpoint: str
    api_key_env: str
    concurrency: int
    reasoning_effort: str
    temperature: float
    timeout: float
    retries: int
    request_params: dict[str, Any] = field(default_factory=dict)
    request_params_by_task: dict[str, dict[str, Any]] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    cache_anchor: str = "MLTD_TRANSLATION_CACHE_ANCHOR"
    cache_warmup: bool = True
    cache_warmup_attempts: int = 5
    cache_warmup_delay_seconds: float = 1.0
    cache_partition_by_reasoning: bool = True
    requests_per_minute: float = 0.0


@dataclass
class WorkItem:
    row: dict[str, Any]
    failed_models: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


class APIRequestError(RuntimeError):
    """HTTP transport failure with retry metadata preserved for backoff."""

    def __init__(self, status_code: int, retry_after: str, detail: str):
        self.status_code = status_code
        self.retry_after = retry_after
        self.detail = detail
        super().__init__(
            f"HTTP {status_code} retry_after={retry_after!r}: {detail}"
        )


class RequestAbortedBeforeSend(RuntimeError):
    """A queued worker was invalidated before it was allowed to send HTTP."""


class AsyncRequestRateLimiter:
    """Simple per-model start-rate limiter shared by all workers and retries."""

    def __init__(self, requests_per_minute: float):
        self.requests_per_minute = max(float(requests_per_minute), 0.0)
        self._interval = (
            60.0 / self.requests_per_minute if self.requests_per_minute > 0 else 0.0
        )
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next_start > now:
                await asyncio.sleep(self._next_start - now)
                now = time.monotonic()
            self._next_start = max(self._next_start, now) + self._interval


def api_error_is_quota_exhausted(exc: Exception) -> bool:
    if not isinstance(exc, APIRequestError) or exc.status_code != 429:
        return False
    detail = exc.detail.lower()
    return (
        "openrouter_free_tier_daily" in detail
        or "free-models-per-day" in detail
        or "daily limit" in detail
    )


def should_retry_transport_error(exc: Exception) -> bool:
    if not isinstance(exc, APIRequestError):
        return True
    if api_error_is_quota_exhausted(exc):
        return False
    if exc.status_code == 429 or exc.status_code in {408, 409, 425}:
        return True
    if 500 <= exc.status_code < 600:
        return True
    if 400 <= exc.status_code < 500:
        return False
    return True


def retry_delay_seconds(exc: Exception, attempt: int) -> float:
    """Choose a bounded transport retry delay without slowing deterministic QA retries."""
    if isinstance(exc, APIRequestError):
        try:
            explicit = float(exc.retry_after)
        except (TypeError, ValueError):
            explicit = 0.0
        if explicit > 0:
            return min(explicit, 60.0)
        if exc.status_code == 429:
            # Shared upstream pools frequently need longer than the generic 1/2s
            # retry used for transient network failures.  A 5/10/20s ladder also
            # avoids a concurrency cohort immediately re-hitting the same limit.
            return min(5.0 * (2 ** max(attempt - 1, 0)), 30.0)
        if 500 <= exc.status_code < 600:
            return min(2.0 * (2 ** max(attempt - 1, 0)), 16.0)
    return min(float(2 ** max(attempt - 1, 0)), 8.0)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_authoritative_terms(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    raw = doc.get("entries", {}) if isinstance(doc, dict) else {}
    if not isinstance(raw, dict):
        raise ValueError("authoritative terms file requires entries object")
    result: dict[str, str] = {}
    for source, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        source_text = str(source)
        target = str(spec.get("target", "")).strip()
        if source_text and target:
            result[source_text] = target
    return result


def mask_authoritative_terms(
    text: str,
    terms: dict[str, str],
) -> tuple[str, list[str], list[str]]:
    targets: list[str] = []
    applied: list[str] = []
    value = text
    for source in sorted(terms, key=len, reverse=True):
        if source not in value:
            continue
        target = terms[source]

        def repl(_: re.Match[str]) -> str:
            index = len(targets)
            if index > 999:
                raise ValueError("too many authoritative terms in one source")
            targets.append(target)
            applied.append(source)
            return f"__MLTD_TERM_{index:03d}__"

        value = re.sub(re.escape(source), repl, value)
    return value, targets, applied


def restore_authoritative_terms(text: str, targets: list[str]) -> str:
    value = text
    for index, target in enumerate(targets):
        marker = f"__MLTD_TERM_{index:03d}__"
        if value.count(marker) != 1:
            raise ValueError(
                f"authoritative term marker mismatch for {marker}: "
                f"count={value.count(marker)}"
            )
        value = value.replace(marker, target)
    if TERM_MARKER_RE.search(value):
        raise ValueError("unexpected authoritative term marker in output")
    return value


def mask_numeric_literals(text: str) -> tuple[str, list[str]]:
    """Mask QA-visible Arabic numeric literals while leaving existing markers intact.

    ``NUMBER_RE`` is the same expression used by deterministic QA, so every
    literal whose spelling QA requires is protected before the model sees the
    SOURCE.  TERM/TOKEN markers already contain index digits and must never be
    recursively masked.
    """
    targets: list[str] = []

    def repl(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.startswith("__MLTD_"):
            return value
        index = len(targets)
        if index > 999:
            raise ValueError("too many numeric literals in one source")
        targets.append(value)
        return f"__MLTD_NUMBER_{index:03d}__"

    return EXISTING_MARKER_OR_NUMBER_RE.sub(repl, text), targets


def restore_numeric_literals(text: str, targets: list[str]) -> str:
    value = text
    seen: list[int] = []

    def repl(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(targets):
            raise ValueError(f"unknown numeric marker {match.group(0)!r}")
        seen.append(index)
        return targets[index]

    value = NUMBER_MARKER_RE.sub(repl, value)
    expected = list(range(len(targets)))
    if sorted(seen) != expected or len(seen) != len(expected):
        raise ValueError(
            f"numeric markers changed: expected={expected!r} got={seen!r}"
        )
    if NUMBER_MARKER_RE.search(value):
        raise ValueError("unresolved numeric marker remains")
    return value


def adaptive_retry_params(
    base: dict[str, Any],
    default_reasoning_effort: str,
    last_error: str,
) -> dict[str, Any]:
    params = dict(base)
    if last_error != "empty_translation":
        return params
    effort = str(
        params.get("reasoning_effort", default_reasoning_effort)
    ).strip().lower()
    lowered = {
        "high": "low",
        "medium": "low",
        "low": "none",
        "minimal": "minimal",
        "none": "none",
    }.get(effort, "none")
    params["reasoning_effort"] = lowered
    limit_key = (
        "max_tokens"
        if "max_tokens" in params
        else "max_completion_tokens"
        if "max_completion_tokens" in params
        else "max_tokens"
    )
    try:
        current = int(params.get(limit_key, 0) or 0)
    except (TypeError, ValueError):
        current = 0
    params[limit_key] = min(max(current * 2, 2048), 8192)
    return params


def normalize_endpoint(spec: dict[str, Any], protocol: str) -> str:
    endpoint = str(spec.get("endpoint", "")).strip()
    if endpoint:
        return endpoint
    base = str(spec.get("base_url", "")).strip().rstrip("/")
    if not base:
        raise ValueError("model requires endpoint or base_url")

    # OpenRouter's public OpenAI-compatible root is /api/v1.  Accept the
    # common /v1 shorthand (and either fully-qualified protocol endpoint) but
    # normalize it before choosing Chat Completions vs Responses.  This keeps
    # a protocol switch from silently producing https://openrouter.ai/v1/... .
    if "openrouter.ai" in base.lower():
        for suffix in ("/chat/completions", "/responses"):
            if base.endswith(suffix):
                base = base[: -len(suffix)].rstrip("/")
                break
        if base.endswith("/v1") and not base.endswith("/api/v1"):
            base = base[: -len("/v1")] + "/api/v1"
        elif base.lower().rstrip("/") == "https://openrouter.ai":
            base += "/api/v1"
        return base + (
            "/responses" if protocol == "responses" else "/chat/completions"
        )

    if protocol == "responses":
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        if "api.deepseek.com" in base:
            if base.endswith("/v1"):
                base = base[:-3]
            return base.rstrip("/") + "/responses"
        if base.endswith("/v1"):
            return base + "/responses"
        return base + "/responses"
    if base.endswith("/chat/completions"):
        return base
    # Gemini's OpenAI-compatible root is /v1beta/openai, NOT /v1.
    # Do not append an OpenAI /v1 suffix to Google's documented endpoint.
    if base.lower().endswith("/v1beta/openai"):
        return base + "/chat/completions"
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


def _config_environment(doc: dict[str, Any]) -> dict[str, Any]:
    """Read optional process-environment values from an API config document.

    ``environment`` is the preferred location.  Flat keys are accepted as a
    small convenience for existing one-model config files.  Values are kept
    out of summaries and are applied only after the model rows have validated.
    """
    nested = doc.get("environment", {})
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        raise ValueError("API config environment must be an object")

    values: dict[str, Any] = {}
    supported = set(CONFIG_ENVIRONMENT_KEYS) | {"api_key"}
    for key, value in doc.items():
        normalized = str(key).strip().lower()
        if normalized in supported:
            values[normalized] = value
    for key, value in nested.items():
        normalized = str(key).strip().lower()
        if normalized in supported:
            values[normalized] = value

    for key, value in values.items():
        if key == "api_key" and isinstance(value, dict):
            for env_name, api_key in value.items():
                if not str(env_name).strip():
                    raise ValueError("API config environment api_key has an empty env name")
                if not isinstance(api_key, str):
                    raise ValueError(
                        f"API config environment api_key[{env_name!r}] must be a string"
                    )
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError(
                f"API config environment {key!r} must be a string"
            )
    return values


def _load_runtime_environment(config_path: Path, doc: dict[str, Any]) -> dict[str, Any]:
    """Merge optional ignored runtime secrets/proxy settings with config values.

    ``api-runtime.local.json`` is deliberately separate from model/provider
    configuration so the human-facing model file can stay shareable and easy to
    audit.  Inline ``environment`` remains supported for backwards compatibility
    and wins when both locations provide the same setting.
    """
    merged: dict[str, Any] = {}
    runtime_path = config_path.parent / RUNTIME_CONFIG_NAME
    if runtime_path.is_file():
        runtime_doc = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        if not isinstance(runtime_doc, dict):
            raise ValueError(f"runtime config {runtime_path} must be a JSON object")
        merged.update(_config_environment(runtime_doc))
    merged.update(_config_environment(doc))
    return merged


def _load_config_presets(config_path: Path, doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Load optional named model presets from a sibling JSON file.

    Schema 3 configs automatically use ``api-models.presets.json`` beside the
    local config when it exists, so the human-facing file does not need a
    repetitive ``preset_file`` field.  Older schemas keep their explicit-only
    behavior for backwards compatibility.
    """
    preset_file = str(doc.get("preset_file", "")).strip()
    if preset_file:
        path = Path(preset_file)
        if not path.is_absolute():
            path = config_path.parent / path
    elif int(doc.get("schema_version", 1) or 1) >= 3:
        local_default = config_path.parent / "api-models.presets.json"
        canonical_default = REPO / "localization" / "api-models.presets.json"
        if local_default.is_file():
            path = local_default
        elif canonical_default.is_file():
            path = canonical_default
        else:
            return {}
    else:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    presets = raw.get("presets", {}) if isinstance(raw, dict) else {}
    if not isinstance(presets, dict):
        raise ValueError(f"preset file {path} requires presets object")
    result: dict[str, dict[str, Any]] = {}
    for name, value in presets.items():
        if not isinstance(value, dict):
            raise ValueError(f"preset {name!r} in {path} must be an object")
        result[str(name)] = dict(value)
    return result


def _apply_model_preset(
    row: dict[str, Any],
    presets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = str(row.get("preset", "")).strip()
    if not name:
        return dict(row)
    if name not in presets:
        raise ValueError(f"unknown model preset: {name!r}")
    merged = dict(presets[name])
    merged.update(row)
    return merged


def _import_config_environment(
    values: dict[str, Any],
    models: list[ModelConfig],
) -> None:
    """Import proxy and API-key values into this process's environment.

    HTTPX reads proxy variables when ``AsyncClient`` is created.  This helper
    therefore runs before clients are constructed.  API keys never enter the
    printed config summary; request code continues to resolve them through
    each model's existing ``api_key_env`` field.
    """
    for config_key, env_name in CONFIG_ENVIRONMENT_KEYS.items():
        value = values.get(config_key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            os.environ[env_name] = value

    api_key = values.get("api_key")
    if api_key is None:
        return
    if isinstance(api_key, dict):
        for env_name, value in api_key.items():
            value = str(value).strip()
            if value:
                os.environ[str(env_name).strip()] = value
        return

    api_key = str(api_key).strip()
    if not api_key:
        return
    api_key_envs = {cfg.api_key_env.strip() for cfg in models if cfg.api_key_env.strip()}
    for env_name in api_key_envs:
        os.environ[env_name] = api_key


def _expand_model_rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand compact schema-3 model syntax into ordinary model rows.

    Supported forms:

    ``models: [{...}]``
        Legacy list form. Still fully supported.

    ``models: {"gemini": "gemini-3.8-flash-high"}``
        Compact form: object key is model id and string value is provider model.

    ``models: {"backup": {"model": "...", "concurrency": 4}}``
        Object form for per-model overrides. Top-level ``defaults`` are merged
        first; model-specific values win.
    """
    defaults = doc.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("API config defaults must be an object")

    models = doc.get("models", [])
    rows: list[dict[str, Any]] = []
    if isinstance(models, list):
        for index, value in enumerate(models):
            if not isinstance(value, dict):
                raise ValueError(f"API config models[{index}] must be an object")
            row = dict(defaults)
            row.update(value)
            rows.append(row)
        return rows

    if isinstance(models, dict):
        for ident, value in models.items():
            ident = str(ident).strip()
            if not ident:
                raise ValueError("API config models has an empty model id")
            if isinstance(value, str):
                override: dict[str, Any] = {"model": value}
            elif isinstance(value, dict):
                override = dict(value)
            else:
                raise ValueError(
                    f"API config models[{ident!r}] must be a model-name string or object"
                )
            explicit_id = str(override.get("id", ident)).strip()
            if explicit_id != ident:
                raise ValueError(
                    f"API config models[{ident!r}] must not override id as {explicit_id!r}"
                )
            row = dict(defaults)
            row.update(override)
            row["id"] = ident
            rows.append(row)
        return rows

    raise ValueError("API config requires models as an array or object")


def select_models(models: list[ModelConfig], requested_ids: list[str] | None) -> list[ModelConfig]:
    """Select configured models by id while preserving config order.

    Production normally uses the full enabled pool.  ``--model-id`` is intended
    for isolated smoke/A-B runs so they can reuse the exact same provider and
    preset config without duplicating config files.
    """
    requested = [str(value).strip() for value in (requested_ids or []) if str(value).strip()]
    if not requested:
        return list(models)
    wanted = set(requested)
    available = {cfg.id for cfg in models}
    unknown = sorted(wanted - available)
    if unknown:
        raise ValueError(
            f"unknown --model-id value(s): {', '.join(unknown)}; "
            f"available: {', '.join(cfg.id for cfg in models)}"
        )
    return [cfg for cfg in models if cfg.id in wanted]


def load_model_config(path: Path) -> list[ModelConfig]:
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError("API config must be a JSON object")
    environment = _load_runtime_environment(path, doc)
    presets = _load_config_presets(path, doc)
    rows = _expand_model_rows(doc)
    result: list[ModelConfig] = []
    seen: set[str] = set()
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            continue
        row = _apply_model_preset(raw_row, presets)
        if not row.get("enabled", True):
            continue
        model = str(row.get("model", "")).strip()
        ident = str(row.get("id", model or f"model-{index}")).strip()
        if not model:
            raise ValueError(f"{ident}: model is required")
        if not ident or ident in seen:
            raise ValueError(f"duplicate/empty model id: {ident!r}")
        seen.add(ident)
        concurrency = int(row.get("concurrency", 1))
        retries = int(row.get("retries", 2))
        if concurrency <= 0 or retries <= 0:
            raise ValueError(f"{ident}: concurrency/retries must be > 0")
        request_params = row.get("request_params", {})
        if not isinstance(request_params, dict):
            raise ValueError(f"{ident}: request_params must be an object")
        extra = row.get("extra_body", {})
        if not isinstance(extra, dict):
            raise ValueError(f"{ident}: extra_body must be an object")
        merged_params = dict(extra)
        merged_params.update(request_params)
        by_task_raw = row.get("request_params_by_task", {})
        if not isinstance(by_task_raw, dict):
            raise ValueError(f"{ident}: request_params_by_task must be an object")
        by_task: dict[str, dict[str, Any]] = {}
        for task_name, task_params in by_task_raw.items():
            if not isinstance(task_params, dict):
                raise ValueError(
                    f"{ident}: request_params_by_task[{task_name!r}] must be an object"
                )
            by_task[str(task_name).upper()] = dict(task_params)
        request_headers = row.get("request_headers", {})
        if not isinstance(request_headers, dict):
            raise ValueError(f"{ident}: request_headers must be an object")
        protocol = str(row.get("api_protocol", "chat_completions")).strip().lower()
        if protocol not in {"chat_completions", "responses"}:
            raise ValueError(f"{ident}: unsupported api_protocol={protocol!r}")
        cache_warmup_attempts = int(row.get("cache_warmup_attempts", 5))
        cache_warmup_delay_seconds = float(row.get("cache_warmup_delay_seconds", 1.0))
        requests_per_minute = float(row.get("requests_per_minute", 0.0) or 0.0)
        if cache_warmup_attempts < 1 or cache_warmup_delay_seconds < 0:
            raise ValueError(
                f"{ident}: cache_warmup_attempts must be >=1 and delay >=0"
            )
        if requests_per_minute < 0:
            raise ValueError(f"{ident}: requests_per_minute must be >= 0")
        result.append(ModelConfig(
            id=ident,
            model=model,
            api_protocol=protocol,
            endpoint=normalize_endpoint(row, protocol),
            api_key_env=str(row.get("api_key_env", "OPENAI_API_KEY")),
            concurrency=concurrency,
            reasoning_effort=str(row.get("reasoning_effort", "high")).strip(),
            temperature=float(row.get("temperature", 0.1)),
            timeout=float(row.get("timeout", 180.0)),
            retries=retries,
            request_params=merged_params,
            request_params_by_task=by_task,
            request_headers={str(k): str(v) for k, v in request_headers.items()},
            cache_anchor=str(
                row.get("cache_anchor", "MLTD_TRANSLATION_CACHE_ANCHOR")
            ).strip(),
            cache_warmup=bool(row.get("cache_warmup", True)),
            cache_warmup_attempts=cache_warmup_attempts,
            cache_warmup_delay_seconds=cache_warmup_delay_seconds,
            cache_partition_by_reasoning=bool(
                row.get("cache_partition_by_reasoning", True)
            ),
            requests_per_minute=requests_per_minute,
        ))
    if not result:
        raise ValueError("API config contains no enabled models")
    _import_config_environment(environment, result)
    return result


def completed_ids(paths: list[Path]) -> set[str]:
    done: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        for row in read_jsonl(path):
            sid = str(row.get("source_sha256", ""))
            translation = str(row.get("translation", ""))
            status = str(row.get("status", ""))
            if sid and translation and status not in {"pending", "needs_review", "skip"}:
                done.add(sid)
    return done


def speaker_info(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    usage = row.get("usage_profile", {})
    if not isinstance(usage, dict):
        return [], []
    codes = usage.get("speaker_codes", [])
    names = usage.get("speaker_names", [])
    return (
        [str(x) for x in codes] if isinstance(codes, list) else [],
        [str(x) for x in names] if isinstance(names, list) else [],
    )


def first_example_key(row: dict[str, Any]) -> str:
    examples = row.get("examples", [])
    if not isinstance(examples, list):
        return ""
    for example in examples:
        if isinstance(example, dict) and example.get("key"):
            return str(example["key"]).lower()
    return ""


def classify_task(row: dict[str, Any]) -> str:
    # Source-specific extractors may know the semantic surface better than the
    # generic GTX heuristics (for example event-unit dialogue has no GTX key).
    # Honor only the closed production task vocabulary; unknown hints fall back
    # to normal inference so arbitrary queue metadata cannot alter routing.
    task_hint = str(row.get("task_hint", "")).strip().upper()
    if task_hint in {"UI", "TITLE", "DESCRIPTION", "DIALOGUE", "SHARED", "SHARED_SIMPLE"}:
        return task_hint

    usage = row.get("usage_profile", {})
    source = str(row.get("source", ""))
    codes, _ = speaker_info(row)
    key = first_example_key(row)
    title_like = any(
        token in key for token in ("achievement_name", "_title", "_rank", "_name_")
    )
    shared = False
    if isinstance(usage, dict):
        shared = bool(
            usage.get("multi_speaker")
            or usage.get("multi_category")
            or usage.get("requires_cross_context_consistency")
        )
        if shared and len(source) <= 24:
            # Short reused chapter/event titles still need title semantics and
            # neighboring-title context. Only neutral microcopy should collapse
            # to the cheap SHARED_SIMPLE route.
            if not codes and title_like:
                return "TITLE"
            # Reused microcopy/interjections such as 確認, はい!, 受け取る, {0}個
            # do not warrant expensive high-reasoning cross-context analysis.
            return "SHARED_SIMPLE"
    if isinstance(usage, dict) and shared:
        if not codes:
            if title_like:
                return "TITLE"
            return "UI" if len(source) <= 40 else "DESCRIPTION"
        if len(codes) == 1:
            return "DIALOGUE"
        return "SHARED"
    if codes:
        return "DIALOGUE"
    if title_like:
        return "TITLE"
    if any(token in key for token in ("description", "_detail", "_explain", "_help")):
        return "DESCRIPTION"
    return "UI" if len(source) <= 40 else "DESCRIPTION"


def nearest_context(row: dict[str, Any]) -> tuple[str, str]:
    contexts = row.get("context_examples", [])
    if not isinstance(contexts, list):
        return "", ""
    source = str(row.get("source", ""))
    for occurrence in contexts:
        if not isinstance(occurrence, dict):
            continue
        window = occurrence.get("context", [])
        if not isinstance(window, list):
            continue
        if not any(
            isinstance(item, dict)
            and int(item.get("relative", 999)) == 0
            and str(item.get("source", "")) == source
            for item in window
        ):
            continue
        previous = next(
            (
                str(x.get("source", "")).strip()
                for x in window
                if isinstance(x, dict) and int(x.get("relative", 999)) == -1
            ),
            "",
        )
        following = next(
            (
                str(x.get("source", "")).strip()
                for x in window
                if isinstance(x, dict) and int(x.get("relative", 999)) == 1
            ),
            "",
        )
        return previous, following
    return "", ""


def task_request_params_for_row(
    cfg: ModelConfig,
    row: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Resolve task parameters with narrow context-sensitive quality boosts.

    Most TITLE rows are cheap labels and should keep the preset's no-reasoning
    route.  Chapter/event titles with actual PREVIOUS/NEXT evidence are a
    different class: short Japanese ellipsis/contrast is often impossible to
    disambiguate from the source string alone.  Give only those rows low
    reasoning and a little more completion headroom instead of globally making
    all titles expensive.
    """
    task = classify_task(row)
    params = dict(cfg.request_params_by_task.get(task, {}))
    if task == "TITLE":
        previous, following = nearest_context(row)
        effort = str(params.get("reasoning_effort", cfg.reasoning_effort) or "").strip().lower()
        if (previous or following) and effort in {"", "none"}:
            params["reasoning_effort"] = "low"
            try:
                current_max = int(params.get("max_tokens", 0) or 0)
            except (TypeError, ValueError):
                current_max = 0
            params["max_tokens"] = max(current_max, 512)
    return task, params


def build_user_prompt(row: dict[str, Any], masked_source: str, feedback: str = "") -> str:
    task = classify_task(row)
    codes, _ = speaker_info(row)
    lines = [f"TASK={task}"]
    if codes and task == "DIALOGUE":
        # usage_profile.speaker_names is useful evidence but is not a reliable
        # positional identity map: the enriched queue contains cross-speaker name
        # associations on some rows.  Send only stable speaker codes here; the
        # canonical System Prompt owns code -> identity for known speakers.
        lines.append("SPEAKER=" + ", ".join(codes))
    elif task == "SHARED":
        lines.append("SPEAKER=MULTIPLE")
    usage = row.get("usage_profile", {})
    if isinstance(usage, dict) and task == "SHARED":
        categories = usage.get("categories", [])
        if isinstance(categories, list) and categories:
            lines.append("USAGE_CATEGORIES=" + ", ".join(str(x) for x in categories))
        lines.append(
            "NOTE=This source is reused. Produce one translation valid across all listed usages."
        )
    lyric_context = isinstance(usage, dict) and usage.get("category") == "LIVE_LYRICS"
    if task in {"DIALOGUE", "TITLE"} or lyric_context:
        previous, following = nearest_context(row)
        if previous:
            lines.extend(["", "PREVIOUS:", previous])
        lines.extend(["", "SOURCE:", masked_source])
        if following:
            lines.extend(["", "NEXT:", following])
    else:
        lines.extend(["", "SOURCE:", masked_source])

    previous_translation = str(row.get("previous_translation", "")).strip()
    stale_reasons = row.get("stale_reasons", [])
    if previous_translation:
        lines.extend(["", "PREVIOUS_TRANSLATION_TO_REPAIR:", previous_translation])
        repair_details: list[str] = []
        if isinstance(stale_reasons, list):
            for issue in stale_reasons[:8]:
                if not isinstance(issue, dict):
                    continue
                code = str(issue.get("code", "")).strip()
                detail = str(issue.get("detail", "")).strip()
                if not detail:
                    source_term = str(issue.get("source_term", "")).strip()
                    target = str(issue.get("target", "")).strip()
                    if source_term or target:
                        detail = f"{source_term} -> {target}".strip()
                if code:
                    repair_details.append(f"{code}: {detail}" if detail else code)
        if repair_details:
            lines.extend(["REPAIR_ISSUES:", "\n".join(repair_details)])
        lines.append(
            "REPAIR_INSTRUCTION=Treat the previous translation as flawed negative evidence. "
            "Translate SOURCE from scratch and fix every listed issue; do not preserve bad wording for consistency."
        )
    if feedback:
        lines.extend(
            [
                "",
                "RETRY_CORRECTION:",
                feedback,
                "Return only a corrected final translation.",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def extract_chat_text(doc: dict[str, Any]) -> str:
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response missing choices[]")
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = [
            str(part.get("text"))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if chunks:
            return "".join(chunks).strip()
    raise ValueError("response missing message.content text")


def extract_response_text(doc: dict[str, Any]) -> str:
    top_level = doc.get("output_text")
    if isinstance(top_level, str):
        return top_level.strip()
    output = doc.get("output")
    if not isinstance(output, list):
        raise ValueError("response missing output[]")
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                chunks.append(str(part["text"]))
    if chunks:
        return "".join(chunks).strip()
    return ""


def normalize_response_usage(usage: Any) -> dict[str, Any]:
    """Normalize Responses and chat/completions usage into one accounting schema.

    Some OpenAI-compatible gateways expose chat-shaped usage even on /responses,
    while others expose Responses-shaped input/output fields.  Prefer explicit
    top-level compatibility fields when present, then fall back to nested token
    details.  The returned nested details are retained only for backwards
    compatibility; callers must account the top-level normalized values once.
    """
    if not isinstance(usage, dict):
        return {}

    prompt_tokens = int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    )
    completion_tokens = int(
        usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    )
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = usage.get("input_tokens_details", {})
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = usage.get("output_tokens_details", {})
    if not isinstance(completion_details, dict):
        completion_details = {}

    cached = int(
        usage.get(
            "cached_tokens",
            usage.get(
                "prompt_cache_hit_tokens",
                prompt_details.get("cached_tokens", 0),
            ),
        )
        or 0
    )
    cache_write = int(
        usage.get("cache_write_tokens", prompt_details.get("cache_write_tokens", 0))
        or 0
    )
    reasoning = int(
        usage.get("reasoning_tokens", completion_details.get("reasoning_tokens", 0))
        or 0
    )
    accepted_prediction = int(
        usage.get(
            "accepted_prediction_tokens",
            completion_details.get("accepted_prediction_tokens", 0),
        )
        or 0
    )
    cache_miss = int(
        usage.get("prompt_cache_miss_tokens", max(prompt_tokens - cached, 0)) or 0
    )
    total_tokens = int(
        usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached,
        "prompt_cache_hit_tokens": cached,
        "prompt_cache_miss_tokens": cache_miss,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "accepted_prediction_tokens": accepted_prediction,
        "prompt_tokens_details": {
            "cached_tokens": cached,
            "cache_write_tokens": cache_write,
        },
        "completion_tokens_details": {
            "reasoning_tokens": reasoning,
            "accepted_prediction_tokens": accepted_prediction,
        },
    }


def response_request_params(
    cfg: ModelConfig,
    request_params: dict[str, Any] | None,
) -> dict[str, Any]:
    # Match chat/completions semantics: ModelConfig.temperature is the default,
    # while explicit global/task request parameters may override it.
    merged: dict[str, Any] = {"temperature": cfg.temperature}
    merged.update(cfg.request_params)
    if request_params:
        merged.update(request_params)
    effort = str(
        merged.pop("reasoning_effort", cfg.reasoning_effort) or ""
    ).strip()
    max_output = None
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        if key in merged:
            max_output = merged.pop(key)
            break
    if effort:
        reasoning = merged.get("reasoning")
        if isinstance(reasoning, dict):
            reasoning = dict(reasoning)
            reasoning.setdefault("effort", effort)
            merged["reasoning"] = reasoning
        else:
            merged["reasoning"] = {"effort": effort}
    cache_key = str(merged.get("prompt_cache_key", "")).strip()
    if cache_key and cfg.cache_partition_by_reasoning and effort:
        suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", effort.lower())
        # Keep well below the strictest known prompt-cache-key length limit.
        merged["prompt_cache_key"] = f"{cache_key[:48]}:{suffix}"[:64]
    if max_output is not None:
        merged["max_output_tokens"] = max_output
    return merged


async def request_translation(
    client: httpx.AsyncClient,
    cfg: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    request_params: dict[str, Any] | None = None,
    rate_limiter: AsyncRequestRateLimiter | None = None,
    request_guard: Callable[[], bool] | None = None,
) -> tuple[str, dict[str, Any]]:
    api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(cfg.request_headers)
    if cfg.api_protocol == "responses":
        dynamic_input = user_prompt
        if cfg.cache_anchor:
            dynamic_input = f"{cfg.cache_anchor}\n{dynamic_input}"
        payload: dict[str, Any] = {
            "model": cfg.model,
            "instructions": system_prompt,
            "input": dynamic_input,
            "stream": False,
        }
        payload.update(response_request_params(cfg, request_params))
    else:
        anchored_user = user_prompt
        if cfg.cache_anchor:
            anchored_user = f"{cfg.cache_anchor}\n{anchored_user}"
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": anchored_user},
            ],
            "temperature": cfg.temperature,
            "stream": False,
        }
        if cfg.reasoning_effort:
            payload["reasoning_effort"] = cfg.reasoning_effort
        payload.update(cfg.request_params)
        if request_params:
            payload.update(request_params)
    if rate_limiter is not None:
        await rate_limiter.acquire()
    if request_guard is not None and not request_guard():
        raise RequestAbortedBeforeSend("request invalidated before HTTP send")
    started = time.perf_counter()
    response = await client.post(
        cfg.endpoint,
        headers=headers,
        json=payload,
        timeout=cfg.timeout,
    )
    cache_hint_fallback = False
    if (
        cfg.api_protocol == "responses"
        and response.status_code in {400, 422}
        and any(
            key in payload
            for key in (
                "prompt_cache_key",
                "prompt_cache_options",
                "prompt_cache_retention",
            )
        )
    ):
        fallback_payload = dict(payload)
        fallback_payload.pop("prompt_cache_key", None)
        fallback_payload.pop("prompt_cache_options", None)
        fallback_payload.pop("prompt_cache_retention", None)
        if rate_limiter is not None:
            await rate_limiter.acquire()
        response = await client.post(
            cfg.endpoint,
            headers=headers,
            json=fallback_payload,
            timeout=cfg.timeout,
        )
        cache_hint_fallback = True
    elapsed = time.perf_counter() - started
    if response.status_code >= 400:
        snippet = response.text[-2000:]
        retry_after = response.headers.get("Retry-After", "")
        raise APIRequestError(response.status_code, retry_after, snippet)
    doc = response.json()
    if cfg.api_protocol == "responses":
        status = str(doc.get("status", "completed"))
        if status == "failed":
            raise RuntimeError(f"Responses API failed: {doc.get('error')}")
        text = extract_response_text(doc)
        if status == "incomplete":
            # A partial translation is unsafe to accept. Returning empty text
            # intentionally routes this through the existing adaptive retry,
            # which lowers reasoning and expands the output budget.
            text = ""
        usage = normalize_response_usage(doc.get("usage", {}))
    else:
        text = extract_chat_text(doc)
        usage = normalize_response_usage(
            doc.get("usage", {}) if isinstance(doc, dict) else {}
        )
    return text, {
        "latency_seconds": elapsed,
        "usage": usage,
        "prompt_cache_diagnostics": (
            doc.get("prompt_cache_diagnostics", {})
            if isinstance(doc, dict)
            else {}
        ),
        "cache_hint_fallback": cache_hint_fallback,
    }


async def warm_cache_for_model(
    client: httpx.AsyncClient,
    cfg: ModelConfig,
    system_prompt: str,
    rate_limiter: AsyncRequestRateLimiter | None = None,
) -> dict[str, Any]:
    if not cfg.cache_warmup or not cfg.cache_anchor:
        return {"enabled": False, "warmed": False, "requests": 0}

    efforts = {
        str(cfg.reasoning_effort or "none").strip().lower() or "none"
    }
    for task_params in cfg.request_params_by_task.values():
        effort = str(task_params.get("reasoning_effort", "")).strip().lower()
        if effort:
            efforts.add(effort)

    profiles: dict[str, Any] = {}
    total_requests = 0
    total_prompt = 0
    total_cached = 0
    total_fallbacks = 0

    # Warm each reasoning/cache partition independently. Two completed requests
    # intentionally diverge immediately after the fixed cache anchor so the
    # reusable prefix is the system prompt + anchor, not a task-specific suffix.
    for effort in sorted(efforts):
        params = {"reasoning_effort": effort, "max_tokens": 16}
        profile_prompt = 0
        profile_cached = 0
        profile_requests = 0
        profile_fallbacks = 0
        last_cached = 0
        last_prompt = 0

        for prompt in ("A", "B"):
            _, meta = await request_translation(
                client, cfg, system_prompt, prompt, params, rate_limiter
            )
            usage = meta.get("usage", {})
            profile_prompt += int(usage.get("prompt_tokens", 0) or 0)
            profile_cached += int(usage.get("cached_tokens", 0) or 0)
            profile_requests += 1
            profile_fallbacks += int(bool(meta.get("cache_hint_fallback")))

        for attempt in range(cfg.cache_warmup_attempts):
            if cfg.cache_warmup_delay_seconds:
                await asyncio.sleep(cfg.cache_warmup_delay_seconds)
            _, meta = await request_translation(
                client, cfg, system_prompt, f"C{attempt}", params, rate_limiter
            )
            usage = meta.get("usage", {})
            last_prompt = int(usage.get("prompt_tokens", 0) or 0)
            last_cached = int(usage.get("cached_tokens", 0) or 0)
            profile_prompt += last_prompt
            profile_cached += last_cached
            profile_requests += 1
            profile_fallbacks += int(bool(meta.get("cache_hint_fallback")))
            if last_cached > 0:
                break

        profiles[effort] = {
            "warmed": last_cached > 0,
            "requests": profile_requests,
            "prompt_tokens": profile_prompt,
            "cached_tokens": profile_cached,
            "cache_hint_fallbacks": profile_fallbacks,
            "last_probe_prompt_tokens": last_prompt,
            "last_probe_cached_tokens": last_cached,
            "last_probe_cache_hit_ratio": (
                round(last_cached / last_prompt, 4) if last_prompt else 0.0
            ),
        }
        total_requests += profile_requests
        total_prompt += profile_prompt
        total_cached += profile_cached
        total_fallbacks += profile_fallbacks

    return {
        "enabled": True,
        "warmed": all(x["warmed"] for x in profiles.values()),
        "requests": total_requests,
        "prompt_tokens": total_prompt,
        "cached_tokens": total_cached,
        "cache_hint_fallbacks": total_fallbacks,
        "profiles": profiles,
    }


def validate_candidate(
    source_row: dict[str, Any],
    raw_text: str,
    tokens: list[str],
    term_targets: list[str],
    glossary: dict,
    numeric_targets: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    source = str(source_row.get("source", ""))
    if not raw_text:
        return None, "empty_translation"
    try:
        translated = restore_tokens(raw_text, tokens)
        translated = restore_authoritative_terms(translated, term_targets)
        translated = restore_numeric_literals(translated, numeric_targets or [])
        validate_translation(source, translated)
    except Exception as exc:
        return None, f"structure:{exc}"
    candidate = {
        "source_sha256": str(source_row.get("source_sha256", "")),
        "source": source,
        "translation": translated,
        "status": ACCEPTED_STATUS,
    }
    qa = evaluate_row(source_row, candidate, glossary)
    issues = qa.get("issues", []) if isinstance(qa, dict) else []
    blocking = [
        x
        for x in issues
        if isinstance(x, dict) and x.get("code") in BASIC_BLOCKING_QA_CODES
    ]
    if blocking:
        return None, "blocking_qa:" + ",".join(
            str(x.get("code", "")) for x in blocking
        )
    if issues:
        candidate["qa"] = qa
        candidate["qa_nonblocking"] = True
    return candidate, ""


async def translate_with_model(
    client: httpx.AsyncClient,
    cfg: ModelConfig,
    rate_limiter: AsyncRequestRateLimiter | None,
    system_prompt: str,
    row: dict[str, Any],
    glossary: dict,
    authoritative_terms: dict[str, str],
    stats: dict[str, dict[str, float]],
    request_guard: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    source = str(row.get("source", ""))
    sid = str(row.get("source_sha256", ""))
    if not source or sid != source_id(source):
        return None, "source_identity_mismatch"
    if source in authoritative_terms:
        target = authoritative_terms[source]
        candidate, qa_error = validate_candidate(
            row, target, [], [], glossary
        )
        if candidate is not None:
            candidate["provenance"] = {
                "provider": "deterministic:authoritative_terms",
                "model_id": None,
                "model": None,
                "task": classify_task(row),
                "reasoning_effort": "none",
                "request_params": {},
                "authoritative_terms": [source],
            }
            stats[cfg.id]["deterministic_term_hits"] += 1
            return candidate, ""
        return None, qa_error

    term_masked_source, term_targets, applied_terms = mask_authoritative_terms(
        source, authoritative_terms
    )
    token_masked_source, tokens = mask_tokens(term_masked_source)
    masked_source, numeric_targets = mask_numeric_literals(token_masked_source)
    task, task_request_params = task_request_params_for_row(cfg, row)
    feedback = ""
    last_error = ""
    successful_request_params = dict(task_request_params)
    for attempt in range(1, cfg.retries + 1):
        attempt_request_params = adaptive_retry_params(
            task_request_params,
            cfg.reasoning_effort,
            last_error,
        )
        if attempt > 1 and attempt_request_params != task_request_params:
            stats[cfg.id]["adaptive_retries"] += 1
            stats[cfg.id][f"task::{task}::adaptive_retries"] += 1
        try:
            raw, meta = await request_translation(
                client,
                cfg,
                system_prompt,
                build_user_prompt(row, masked_source, feedback),
                attempt_request_params,
                rate_limiter,
                request_guard,
            )
            stats[cfg.id]["requests"] += 1
            stats[cfg.id]["latency_seconds"] += float(meta["latency_seconds"])
            task_prefix = f"task::{task}::"
            stats[cfg.id][task_prefix + "requests"] += 1
            stats[cfg.id][task_prefix + "latency_seconds"] += float(
                meta["latency_seconds"]
            )
            if meta.get("cache_hint_fallback"):
                stats[cfg.id]["cache_hint_fallbacks"] += 1
                stats[cfg.id][task_prefix + "cache_hint_fallbacks"] += 1
            usage = meta.get("usage", {})
            if isinstance(usage, dict):
                # request_translation() normalizes both API protocols to this
                # top-level accounting schema.  Do not also sum nested details:
                # they are retained only for backwards-compatible diagnostics.
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cached_tokens",
                    "cache_write_tokens",
                    "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens",
                    "reasoning_tokens",
                    "accepted_prediction_tokens",
                ):
                    value = usage.get(key)
                    if isinstance(value, (int, float)):
                        stats[cfg.id][key] += float(value)
                        stats[cfg.id][task_prefix + key] += float(value)
            diagnostics = meta.get("prompt_cache_diagnostics", {})
            if isinstance(diagnostics, dict):
                reason = str(diagnostics.get("reason", "")).strip()
                if reason:
                    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", reason)
                    stats[cfg.id][f"cache_miss_reason::{safe_reason}"] += 1
                    stats[cfg.id][
                        f"task::{task}::cache_miss_reason::{safe_reason}"
                    ] += 1
            candidate, qa_error = validate_candidate(
                row, raw, tokens, term_targets, glossary, numeric_targets
            )
            if candidate is not None:
                successful_request_params = dict(attempt_request_params)
                candidate["provenance"] = {
                    "provider": cfg.endpoint,
                    "model_id": cfg.id,
                    "model": cfg.model,
                    "task": task,
                    "reasoning_effort": successful_request_params.get(
                        "reasoning_effort", cfg.reasoning_effort
                    ),
                    "request_params": {
                        **cfg.request_params,
                        **successful_request_params,
                    },
                    "authoritative_terms": applied_terms,
                }
                stats[cfg.id]["accepted"] += 1
                stats[cfg.id][task_prefix + "accepted"] += 1
                return candidate, ""
            last_error = qa_error
            stats[cfg.id]["qa_retries"] += 1
            stats[cfg.id][task_prefix + "qa_retries"] += 1
            if qa_error == "empty_translation":
                feedback = (
                    "The previous response used its completion budget without "
                    "returning final text. Return the final translation immediately "
                    "and keep internal reasoning minimal."
                )
            else:
                feedback = (
                    f"Previous output failed deterministic QA ({qa_error}). "
                    "Re-translate the same SOURCE and preserve all visible Arabic "
                    "numeric literals exactly."
                )
        except RequestAbortedBeforeSend:
            return None, "quota_exhausted:model_disabled_before_request"
        except Exception as exc:
            stats[cfg.id]["requests"] += 1
            stats[cfg.id]["request_errors"] += 1
            task_prefix = f"task::{task}::"
            stats[cfg.id][task_prefix + "requests"] += 1
            stats[cfg.id][task_prefix + "request_errors"] += 1
            if isinstance(exc, APIRequestError) and exc.status_code == 429:
                stats[cfg.id]["rate_limit_errors"] += 1
                stats[cfg.id][task_prefix + "rate_limit_errors"] += 1
                if api_error_is_quota_exhausted(exc):
                    stats[cfg.id]["quota_exhausted_errors"] += 1
                    stats[cfg.id][task_prefix + "quota_exhausted_errors"] += 1
            last_error = f"{type(exc).__name__}:{exc}"
            feedback = (
                "Previous request failed. Translate the same SOURCE and obey the output "
                "contract exactly."
            )
            retryable = should_retry_transport_error(exc)
            if not retryable:
                stats[cfg.id]["non_retryable_errors"] += 1
                stats[cfg.id][task_prefix + "non_retryable_errors"] += 1
                if api_error_is_quota_exhausted(exc):
                    return None, f"quota_exhausted:{last_error}"
                break
            if attempt < cfg.retries:
                stats[cfg.id]["retries"] += 1
                delay = retry_delay_seconds(exc, attempt)
                stats[cfg.id]["retry_sleep_seconds"] += delay
                stats[cfg.id][task_prefix + "retry_sleep_seconds"] += delay
                await asyncio.sleep(delay)
            continue
        if attempt < cfg.retries:
            stats[cfg.id]["retries"] += 1
            delay = min(float(2 ** (attempt - 1)), 8.0)
            stats[cfg.id]["retry_sleep_seconds"] += delay
            stats[cfg.id][task_prefix + "retry_sleep_seconds"] += delay
            await asyncio.sleep(delay)
    return None, last_error or "translation_failed"


async def producer(
    input_path: Path,
    queue: asyncio.Queue[WorkItem | None],
    done_ids: set[str],
    max_items: int,
    stats: dict[str, Any],
) -> None:
    selected = 0
    scanned = 0
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
            await queue.put(WorkItem(row=row))
            selected += 1
            if max_items and selected >= max_items:
                break
    stats["scanned_rows"] = scanned
    stats["selected_pending"] = selected


async def worker(
    cfg: ModelConfig,
    client: httpx.AsyncClient,
    rate_limiter: AsyncRequestRateLimiter | None,
    work_queue: asyncio.Queue[WorkItem | None],
    result_queue: asyncio.Queue[tuple[str, Any]],
    system_prompt: str,
    glossary: dict,
    authoritative_terms: dict[str, str],
    all_model_ids: set[str],
    disabled_model_ids: set[str],
    model_stats: dict[str, dict[str, float]],
) -> None:
    while True:
        item = await work_queue.get()
        if item is None:
            work_queue.task_done()
            return
        if cfg.id in disabled_model_ids:
            if disabled_model_ids >= all_model_ids:
                await result_queue.put(
                    (
                        "deferred",
                        {
                            "source_sha256": str(item.row.get("source_sha256", "")),
                            "reason": "all_models_unavailable",
                        },
                    )
                )
            else:
                await work_queue.put(item)
                await asyncio.sleep(0.02)
            work_queue.task_done()
            continue
        if cfg.id in item.failed_models:
            await work_queue.put(item)
            work_queue.task_done()
            await asyncio.sleep(0.02)
            continue
        result, error = await translate_with_model(
            client,
            cfg,
            rate_limiter,
            system_prompt,
            item.row,
            glossary,
            authoritative_terms,
            model_stats,
            lambda: cfg.id not in disabled_model_ids,
        )
        if result is not None:
            await result_queue.put(("accepted", result))
        else:
            if error.startswith("quota_exhausted:"):
                if cfg.id not in disabled_model_ids:
                    disabled_model_ids.add(cfg.id)
                    model_stats[cfg.id]["quota_circuit_breaker_trips"] += 1
                if disabled_model_ids >= all_model_ids:
                    await result_queue.put(
                        (
                            "deferred",
                            {
                                "source_sha256": str(item.row.get("source_sha256", "")),
                                "reason": "quota_exhausted",
                            },
                        )
                    )
                else:
                    await work_queue.put(item)
                work_queue.task_done()
                continue
            item.failed_models.add(cfg.id)
            item.errors.append(f"{cfg.id}:{error}")
            model_stats[cfg.id]["terminal_item_failures"] += 1
            if item.failed_models >= all_model_ids:
                await result_queue.put(
                    (
                        "failed",
                        {
                            "source_sha256": str(item.row.get("source_sha256", "")),
                            "source": str(item.row.get("source", "")),
                            "errors": item.errors,
                        },
                    )
                )
            else:
                await work_queue.put(item)
        work_queue.task_done()


async def writer(
    result_queue: asyncio.Queue[tuple[str, Any]],
    output: Path,
    failed_output: Path,
    prompt_version: str,
    prompt_sha256: str,
    authoritative_terms_sha256: str,
    fsync_every: int,
    write_stats: dict[str, int],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    failed_output.parent.mkdir(parents=True, exist_ok=True)
    accepted_handle = output.open("a", encoding="utf-8", newline="\n")
    failed_handle = failed_output.open("a", encoding="utf-8", newline="\n")
    since_sync = 0
    try:
        while True:
            kind, payload = await result_queue.get()
            if kind == "stop":
                result_queue.task_done()
                break
            if kind == "accepted":
                payload["prompt_version"] = prompt_version
                payload["prompt_sha256"] = prompt_sha256
                payload["authoritative_terms_sha256"] = authoritative_terms_sha256
                accepted_handle.write(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                accepted_handle.flush()
                write_stats["accepted"] += 1
                since_sync += 1
                if fsync_every > 0 and since_sync >= fsync_every:
                    os.fsync(accepted_handle.fileno())
                    since_sync = 0
            elif kind == "failed":
                failed_handle.write(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                failed_handle.flush()
                write_stats["failed"] += 1
            elif kind == "deferred":
                # Intentionally do not persist quota/provider-unavailable items as
                # translation failures.  They remain pending by source ID and are
                # naturally picked up by the next resume run.
                write_stats["deferred"] += 1
            result_queue.task_done()
    finally:
        accepted_handle.flush()
        failed_handle.flush()
        os.fsync(accepted_handle.fileno())
        os.fsync(failed_handle.fileno())
        accepted_handle.close()
        failed_handle.close()


def preview_pending(
    input_path: Path,
    done_ids: set[str],
    count: int,
    authoritative_terms: dict[str, str],
) -> list[dict[str, Any]]:
    result = []
    with input_path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            sid = str(row.get("source_sha256", ""))
            if sid in done_ids or not str(row.get("source", "")):
                continue
            source = str(row["source"])
            term_masked, _, applied_terms = mask_authoritative_terms(
                source, authoritative_terms
            )
            masked, _ = mask_tokens(term_masked)
            result.append(
                {
                    "source_sha256": sid,
                    "task": classify_task(row),
                    "deterministic_exact_term": (
                        authoritative_terms.get(source)
                        if source in authoritative_terms
                        else None
                    ),
                    "authoritative_terms": applied_terms,
                    "user_prompt": build_user_prompt(row, masked),
                }
            )
            if len(result) >= count:
                break
    return result


def resolve_system_prompt(args: argparse.Namespace) -> tuple[str, str, str, dict[str, Any]]:
    """Resolve the production System Prompt from one shared compiler authority.

    ``compiled`` is the production default: policy + glossary + speaker evidence
    are compiled in memory, so forgetting to run the snapshot builder cannot
    silently leave translation on an older prompt.  ``artifact`` preserves the
    old frozen-file behavior for explicit prompt A/B or forensic replay.
    """
    if args.prompt_source == "compiled":
        bundle = compile_prompt_bundle(
            args.glossary,
            args.character_evidence,
            output_path=args.system_prompt,
        )
        prompt = bundle.text
        manifest = bundle.manifest
        status = snapshot_status(bundle, args.system_prompt, args.prompt_manifest)
        return (
            prompt,
            str(manifest["prompt_version"]),
            str(manifest["prompt_sha256"]),
            status,
        )

    system_prompt = args.system_prompt.read_text(encoding="utf-8")
    prompt_sha256 = sha256_text(system_prompt)
    manifest = (
        json.loads(args.prompt_manifest.read_text(encoding="utf-8-sig"))
        if args.prompt_manifest.is_file()
        else {}
    )
    prompt_version = str(manifest.get("prompt_version", args.system_prompt.stem))
    manifest_sha = str(manifest.get("prompt_sha256", ""))
    if manifest_sha and manifest_sha != prompt_sha256:
        raise ValueError(
            f"system prompt SHA mismatch: manifest={manifest_sha} actual={prompt_sha256}"
        )
    return system_prompt, prompt_version, prompt_sha256, {
        "status": "artifact",
        "prompt_exists": args.system_prompt.is_file(),
        "manifest_exists": args.prompt_manifest.is_file(),
    }


async def async_main(args: argparse.Namespace) -> int:
    models = select_models(load_model_config(args.config), args.model_id)
    batch_mode = getattr(args, "batch_mode", "single")
    if batch_mode != "single":
        # Lazy import avoids changing the tested legacy single-item path.
        from scripts import mltd_batch_v2_production as batch_production
    system_prompt, prompt_version, prompt_sha256, prompt_snapshot = resolve_system_prompt(args)

    resume_paths = list(args.resume_from)
    if not args.no_default_resume:
        resume_paths.insert(
            0, Path("build/localization-90200/machine-translations-codex.jsonl")
        )
    done_ids = completed_ids(resume_paths + [args.output])
    glossary = load_glossary(args.glossary)
    authoritative_terms = load_authoritative_terms(args.authoritative_terms)
    authoritative_terms_sha256 = (
        hashlib.sha256(args.authoritative_terms.read_bytes()).hexdigest()
        if args.authoritative_terms.is_file()
        else ""
    )
    config_summary = {
        "prompt_source": args.prompt_source,
        "system_prompt": str(args.system_prompt),
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "prompt_chars": len(system_prompt),
        "prompt_snapshot": prompt_snapshot,
        "input": str(args.input),
        "output": str(args.output),
        "already_completed": len(done_ids),
        "authoritative_term_count": len(authoritative_terms),
        "authoritative_terms_sha256": authoritative_terms_sha256,
        "models": [
            {
                "id": cfg.id,
                "model": cfg.model,
                "api_protocol": cfg.api_protocol,
                "endpoint": cfg.endpoint,
                "concurrency": cfg.concurrency,
                "reasoning_effort": cfg.reasoning_effort,
                "retries": cfg.retries,
                "request_params": cfg.request_params,
                "request_params_by_task": cfg.request_params_by_task,
                "request_headers": sorted(cfg.request_headers),
                "cache_anchor": cfg.cache_anchor,
                "cache_warmup": cfg.cache_warmup,
                "cache_warmup_attempts": cfg.cache_warmup_attempts,
                "cache_warmup_delay_seconds": cfg.cache_warmup_delay_seconds,
                "requests_per_minute": cfg.requests_per_minute,
            }
            for cfg in models
        ],
        "total_concurrency": sum(cfg.concurrency for cfg in models),
        "batch_mode": batch_mode,
    }
    print(json.dumps(config_summary, ensure_ascii=False, indent=2))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "preview": preview_pending(
                        args.input,
                        done_ids,
                        args.preview_items,
                        authoritative_terms,
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    work_queue: asyncio.Queue[WorkItem | None] = asyncio.Queue(
        maxsize=max(32, sum(cfg.concurrency for cfg in models) * 8)
    )
    result_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    producer_stats: dict[str, Any] = {}
    write_stats = {"accepted": 0, "failed": 0, "deferred": 0}
    model_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    all_model_ids = {cfg.id for cfg in models}
    disabled_model_ids: set[str] = set()
    clients = {
        cfg.id: httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=max(2, cfg.concurrency + 2),
                max_keepalive_connections=max(1, cfg.concurrency),
            ),
            headers={"User-Agent": "mltd-current-localization-api-pool/1"},
            # The config loader imports HTTP_PROXY/HTTPS_PROXY before these
            # clients are created.  Keep this explicit so a future HTTPX
            # default change cannot silently bypass the configured proxy.
            trust_env=True,
        )
        for cfg in models
    }
    rate_limiters = {
        cfg.id: (
            AsyncRequestRateLimiter(cfg.requests_per_minute)
            if cfg.requests_per_minute > 0
            else None
        )
        for cfg in models
    }
    warmup_started = time.perf_counter()
    cache_warmup_results: dict[str, Any] = {}
    for cfg in models:
        try:
            cache_warmup_results[cfg.id] = await warm_cache_for_model(
                clients[cfg.id], cfg, system_prompt, rate_limiters[cfg.id]
            )
        except Exception as exc:
            cache_warmup_results[cfg.id] = {
                "enabled": cfg.cache_warmup,
                "warmed": False,
                "error": f"{type(exc).__name__}:{exc}",
            }
    cache_warmup_seconds = time.perf_counter() - warmup_started
    print(
        json.dumps(
            {
                "cache_warmup_seconds": round(cache_warmup_seconds, 3),
                "cache_warmup": cache_warmup_results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    writer_task = asyncio.create_task(
        writer(
            result_queue,
            args.output,
            args.failed_output,
            prompt_version,
            prompt_sha256,
            authoritative_terms_sha256,
            args.fsync_every,
            write_stats,
        )
    )
    workers = [
        asyncio.create_task(
            batch_production.batch_worker(
                cfg, clients[cfg.id], rate_limiters[cfg.id], work_queue,
                result_queue, system_prompt, glossary, authoritative_terms,
                all_model_ids, disabled_model_ids, model_stats, batch_mode,
            ) if batch_mode != "single" else worker(
                cfg, clients[cfg.id], rate_limiters[cfg.id], work_queue,
                result_queue, system_prompt, glossary, authoritative_terms,
                all_model_ids, disabled_model_ids, model_stats,
            )
        )
        for cfg in models
        for _ in range(cfg.concurrency)
    ]
    started = time.perf_counter()
    try:
        if batch_mode == "single":
            await producer(
                args.input, work_queue, done_ids, args.max_items, producer_stats
            )
        else:
            await batch_production.producer_batch(
                args.input, work_queue, done_ids, args.max_items,
                producer_stats, batch_mode, authoritative_terms,
            )
        await work_queue.join()
        for _ in workers:
            await work_queue.put(None)
        await work_queue.join()
        await asyncio.gather(*workers)
        await result_queue.join()
        await result_queue.put(("stop", None))
        await writer_task
    finally:
        for client in clients.values():
            await client.aclose()

    elapsed = time.perf_counter() - started
    model_summary = {}
    for mid, values in model_stats.items():
        item = {
            k: round(v, 3)
            for k, v in values.items()
            if not k.startswith("task::")
        }
        task_values: dict[str, dict[str, float]] = defaultdict(dict)
        for key, value in values.items():
            if not key.startswith("task::"):
                continue
            _, task_name, metric = key.split("::", 2)
            task_values[task_name][metric] = float(value)
        requests = float(values.get("requests", 0.0))
        latency = float(values.get("latency_seconds", 0.0))
        prompt_tokens = float(values.get("prompt_tokens", 0.0))
        cached_tokens = float(values.get("cached_tokens", 0.0))
        completion_tokens = float(values.get("completion_tokens", 0.0))
        reasoning_tokens = float(values.get("reasoning_tokens", 0.0))
        if requests > 0:
            item["average_latency_seconds"] = round(latency / requests, 3)
            item["input_tokens_per_request"] = round(prompt_tokens / requests, 3)
            item["cached_input_tokens_per_request"] = round(
                cached_tokens / requests, 3
            )
            item["uncached_input_tokens_per_request"] = round(
                max(prompt_tokens - cached_tokens, 0.0) / requests, 3
            )
            item["reasoning_tokens_per_request"] = round(
                reasoning_tokens / requests, 3
            )
        if prompt_tokens > 0:
            item["cache_hit_ratio"] = round(cached_tokens / prompt_tokens, 4)
        if completion_tokens > 0:
            item["reasoning_token_ratio"] = round(
                reasoning_tokens / completion_tokens, 4
            )
        by_task = {}
        for task_name, metrics in sorted(task_values.items()):
            task_item = {k: round(v, 3) for k, v in metrics.items()}
            task_requests = float(metrics.get("requests", 0.0))
            task_latency = float(metrics.get("latency_seconds", 0.0))
            task_prompt = float(metrics.get("prompt_tokens", 0.0))
            task_cached = float(metrics.get("cached_tokens", 0.0))
            task_completion = float(metrics.get("completion_tokens", 0.0))
            task_reasoning = float(metrics.get("reasoning_tokens", 0.0))
            if task_requests > 0:
                task_item["average_latency_seconds"] = round(
                    task_latency / task_requests, 3
                )
                task_item["input_tokens_per_request"] = round(
                    task_prompt / task_requests, 3
                )
                task_item["cached_input_tokens_per_request"] = round(
                    task_cached / task_requests, 3
                )
                task_item["uncached_input_tokens_per_request"] = round(
                    max(task_prompt - task_cached, 0.0) / task_requests, 3
                )
                task_item["reasoning_tokens_per_request"] = round(
                    task_reasoning / task_requests, 3
                )
            if task_prompt > 0:
                task_item["cache_hit_ratio"] = round(
                    task_cached / task_prompt, 4
                )
            if task_completion > 0:
                task_item["reasoning_token_ratio"] = round(
                    task_reasoning / task_completion, 4
                )
            by_task[task_name] = task_item
        if by_task:
            item["by_task"] = by_task
        model_summary[mid] = item

    summary = {
        "elapsed_seconds": round(elapsed, 3),
        "cache_warmup_seconds": round(cache_warmup_seconds, 3),
        "cache_warmup": cache_warmup_results,
        "batch_mode": batch_mode,
        **producer_stats,
        **write_stats,
        "items_per_minute": (
            round(write_stats["accepted"] * 60.0 / elapsed, 3)
            if elapsed > 0
            else 0.0
        ),
        "models": model_summary,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not write_stats["failed"] else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        default=Path(
            "build/localization-90200/machine-translation-queue-context.jsonl"
        ),
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.jsonl"),
    )
    ap.add_argument(
        "--failed-output",
        type=Path,
        default=Path(
            "build/localization-90200/machine-translations-api.failed.jsonl"
        ),
    )
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path(
            "build/localization-90200/machine-translations-api.summary.json"
        ),
    )
    ap.add_argument(
        "--resume-from",
        action="append",
        type=Path,
        default=[],
        help="additional translation JSONL whose accepted source IDs should be skipped",
    )
    ap.add_argument(
        "--no-default-resume",
        action="store_true",
        help="do not implicitly resume from machine-translations-codex.jsonl; useful for isolated benchmarks",
    )
    ap.add_argument(
        "--prompt-source",
        choices=("compiled", "artifact"),
        default="compiled",
        help=(
            "compiled (default) builds the System Prompt in memory from current policy/glossary/speaker evidence; "
            "artifact replays an explicit frozen --system-prompt/--prompt-manifest pair"
        ),
    )
    ap.add_argument(
        "--system-prompt",
        type=Path,
        default=Path("localization/prompts/mltd-zhcn-system.md"),
        help="canonical audit snapshot path, or the frozen prompt when --prompt-source=artifact",
    )
    ap.add_argument(
        "--prompt-manifest",
        type=Path,
        default=Path(
            "localization/prompts/mltd-zhcn-system.manifest.json"
        ),
    )
    ap.add_argument(
        "--character-evidence",
        type=Path,
        default=Path("build/localization-90200/character-voice-evidence.json"),
        help="speaker identity source used by the compiled System Prompt",
    )
    ap.add_argument(
        "--glossary",
        type=Path,
        default=Path("localization/quality/glossary.json"),
    )
    ap.add_argument(
        "--authoritative-terms",
        type=Path,
        default=Path("localization/quality/authoritative-terms.json"),
    )
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument(
        "--model-id",
        action="append",
        default=[],
        help=(
            "run only the named configured model id; repeat to select multiple. "
            "Omit for the full enabled multi-model pool."
        ),
    )
    ap.add_argument(
        "--batch-mode", choices=("single", "dynamic", "dynamic-mixed"),
        default="dynamic",
        help=(
            "dynamic (default): Batch V2 by task, DIALOGUE by speaker; "
            "single: original one-item-per-call fallback; dynamic-mixed: "
            "explicit experimental mixed-speaker grouping (human review required)"
        ),
    )
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--fsync-every", type=int, default=32)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preview-items", type=int, default=3)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.max_items < 0 or args.preview_items < 0 or args.fsync_every < 0:
        raise SystemExit("numeric options must be >= 0")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
