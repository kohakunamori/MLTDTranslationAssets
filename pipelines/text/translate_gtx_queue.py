#!/usr/bin/env python3
"""Translate deduplicated MLTD GTX source values with resumable batch backends.

The input is the JSONL queue emitted by mltd_localization_pipeline.py make-queue.
Output rows are source-bound translation-memory seeds that can be passed directly
to mltd_localization_pipeline.py audit/build-overlay.

Safety properties:
* exact source_sha256 identity is preserved;
* GTX protected tokens are masked before model calls and restored byte-for-byte;
* malformed, missing, duplicate, or token-damaging responses are rejected;
* output is append-only/resumable and fsynced after every successful batch;
* no translation is accepted when the returned text still equals the JP source.

Backends:
* openai-compatible: POST a Chat Completions-shaped request to an explicitly
  supplied endpoint; API key is read from an environment variable.
* command: run an explicit local command once per batch. JSON is written to
  stdin and JSON is read from stdout. This is the preferred adapter for local
  translation models because it does not couple this repository to a model SDK.
* nllb: optional local Hugging Face NLLB backend. Dependencies are imported
  lazily so the repository's normal Python environment remains dependency-free.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mltd_localize_gtx import (
    PROTECTED_TOKEN_RE,
    read_jsonl,
    validate_translation,
)
from mltd_translation_quality import applicable_glossary, evaluate_row, load_glossary
from build_character_voice_evidence import (
    load_character_evidence,
    select_official_style_samples,
)
from classify_translation_risk import load_risk_index
from mltd_character_profiles import load_character_profiles, select_character_profile

MARKER_RE = re.compile(r"__MLTD_TOKEN_(\d{3})__")
ACCEPTED_STATUS = "machine_translated"

# Production-basic QA blocks runtime/source corruption, empty/meta output,
# visible Arabic-numeric literal changes, and explicit project-glossary violations.
# Untranslated-looking proper names, identical cross-language unit strings and
# other semantic/style heuristics remain attached as non-blocking diagnostics.
BASIC_BLOCKING_QA_CODES = {
    "queue_source_missing",
    "source_identity_mismatch",
    "empty_translation",
    "protected_token_mismatch",
    "numeric_literal_mismatch",
    "introduced_traditional_chinese",
    "japanese_honorific_residual",
    "honorific_chan_jiang_calque",
    "japanese_grammar_residual",
    "sentence_final_de_shuo_calque",
    "translator_meta_text",
    "preferred_term_missing",
    "forbidden_term_present",
}


def source_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mask_tokens(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []

    def repl(match: re.Match[str]) -> str:
        index = len(tokens)
        if index > 999:
            raise ValueError("too many protected tokens in one source value")
        tokens.append(match.group(0))
        return f"__MLTD_TOKEN_{index:03d}__"

    return PROTECTED_TOKEN_RE.sub(repl, text), tokens


def restore_tokens(text: str, tokens: list[str]) -> str:
    seen: list[int] = []

    def repl(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(tokens):
            raise ValueError(f"unknown protected-token marker {match.group(0)!r}")
        seen.append(index)
        return tokens[index]

    restored = MARKER_RE.sub(repl, text)
    expected = list(range(len(tokens)))
    if sorted(seen) != expected or len(seen) != len(expected):
        raise ValueError(
            f"protected-token markers changed: expected={expected!r} got={seen!r}"
        )
    if MARKER_RE.search(restored):
        raise ValueError("unresolved protected-token marker remains")
    return restored


def request_items(
    rows: list[dict],
    glossary: dict | None = None,
    speaker_evidence: dict[str, dict] | None = None,
    max_style_samples: int = 2,
    risk_index: dict[str, dict] | None = None,
    character_profiles: dict[str, dict] | None = None,
    max_examples: int = 0,
    max_context_examples: int = 0,
) -> tuple[list[dict], dict[str, tuple[str, list[str]]]]:
    payload: list[dict] = []
    masked: dict[str, tuple[str, list[str]]] = {}
    glossary = glossary or {"entries": {}, "kana_allowlist": []}
    speaker_evidence = speaker_evidence or {}
    risk_index = risk_index or {}
    character_profiles = character_profiles or {}
    for row in rows:
        source = str(row.get("source", ""))
        sid = str(row.get("source_sha256", "")) or source_id(source)
        if not source or sid != source_id(source):
            raise ValueError(f"queue source identity mismatch: {sid!r}")
        masked_text, tokens = mask_tokens(source)
        usage_profile = row.get("usage_profile", {})
        payload.append(
            {
                "id": sid,
                "text": masked_text,
                "examples": (
                    (row.get("examples", []) or [])[:max_examples]
                    if max_examples > 0
                    else (row.get("examples", []) or [])
                ),
                "context_examples": (
                    (row.get("context_examples", []) or [])[:max_context_examples]
                    if max_context_examples > 0
                    else (row.get("context_examples", []) or [])
                ),
                "usage_profile": usage_profile,
                "occurrences": row.get("occurrences"),
                "queue_reason": row.get("queue_reason", ""),
                "glossary": applicable_glossary(source, glossary),
                "official_voice_examples": select_official_style_samples(
                    source,
                    usage_profile if isinstance(usage_profile, dict) else {},
                    speaker_evidence,
                    max_style_samples,
                ),
                "risk": risk_index.get(sid, {}),
                "character_profile": select_character_profile(
                    usage_profile if isinstance(usage_profile, dict) else {},
                    character_profiles,
                ),
                "previous_translation": str(row.get("previous_translation", "")),
                "review_feedback": row.get("review_feedback", {}),
            }
        )
        masked[sid] = (source, tokens)
    return payload, masked


def strip_json_fence(text: str):
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        starts = [i for i in (value.find("{"), value.find("[")) if i >= 0]
        if not starts:
            raise
        start = min(starts)
        for end in range(len(value), start, -1):
            try:
                return json.loads(value[start:end])
            except json.JSONDecodeError:
                continue
        raise


def normalize_backend_response(value) -> dict[str, str]:
    if isinstance(value, dict) and "translations" in value:
        value = value["translations"]
    if isinstance(value, dict):
        # Permit a direct id -> translation mapping for simple local commands.
        return {str(key): str(item) for key, item in value.items()}
    if not isinstance(value, list):
        raise ValueError("translation backend must return a list or object")
    result: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("translation response list contains a non-object")
        sid = str(row.get("id", ""))
        text = row.get("text", row.get("translation"))
        if not sid or text is None:
            raise ValueError("translation response row requires id and text")
        if sid in result:
            raise ValueError(f"duplicate translation response id: {sid}")
        result[sid] = str(text)
    return result


def translate_command(
    command: str,
    items: list[dict],
    timeout: float,
    style_guide: str = "",
) -> dict[str, str]:
    argv: str | list[str]
    if os.name == "nt":
        # CreateProcess receives a native Windows command line.  Passing a
        # pre-split shlex list here breaks quoted executable/script paths.
        argv = command
    else:
        argv = shlex.split(command)
    child_env = os.environ.copy()
    # The command-backend wire format is UTF-8 JSON.  Windows Python otherwise
    # inherits a locale code page (for example GBK) for redirected stdio and
    # silently corrupts Japanese input before a model adapter sees it.
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(
        argv,
        input=json.dumps({"style_guide": style_guide, "items": items}, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="strict",
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"translation command exited {proc.returncode}: {proc.stderr[-2000:]}"
        )
    return normalize_backend_response(strip_json_fence(proc.stdout))


def translate_openai_compatible(
    endpoint: str,
    model: str,
    api_key: str,
    items: list[dict],
    timeout: float,
    temperature: float,
    style_guide: str = "",
) -> dict[str, str]:
    system = (
        "You translate Japanese text from THE IDOLM@STER Million Live! Theater Days "
        "into natural Simplified Chinese. Return JSON only. Preserve every marker "
        "such as __MLTD_TOKEN_000__ exactly once and unchanged. Preserve character "
        "names, game terminology, punctuation intent and speaker tone. Use every supplied "
        "context occurrence as evidence. The same source may be reused by multiple speakers or "
        "scene categories; when usage_profile.requires_cross_context_consistency is true, choose "
        "one translation that remains valid across those uses instead of overfitting one speaker. "
        "official_voice_examples are historical official Chinese style evidence and may be Traditional "
        "Chinese or region-specific; use them only as evidence for meaning/voice/terminology, never as "
        "a reason to output Traditional Chinese or copy wording blindly. An item may also contain an explicitly "
        "reviewed character_profile; when present, use it as a voice constraint, but never let style override the "
        "literal Japanese meaning. Preserve visible Arabic numeric literals exactly. Do not infer or add "
        "singular/plural audience, gender, subject/object, or pronouns absent from the Japanese. Preserve "
        "address semantics; in particular a protected player-name token followed by さん should normally "
        "remain that token plus 先生 unless supplied project evidence establishes an exception. Each item may include a deterministic risk object. For high/critical risk, inspect every supplied context occurrence carefully and avoid a "
        "speaker-specific rendering when the same source is reused across speakers/categories. If the source is "
        "genuinely ambiguous, choose the most natural context-safe rendering and avoid inventing unsupported detail. "
        "Do not add notes or explanations. Output schema: "
        '{"translations":[{"id":"<same id>","text":"<Chinese>"}]}.'
    )
    user = json.dumps(
        {"style_guide": style_guide, "items": items},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read(4000).decode("utf-8", "replace")
        raise RuntimeError(f"translation HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"translation endpoint error: {exc}") from exc
    payload = json.loads(raw.decode("utf-8"))
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat-completions response missing choices[]")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("chat-completions response missing message.content")
    return normalize_backend_response(strip_json_fence(content))


def build_nllb_backend(
    model_name: str,
    device: str,
    source_lang: str,
    target_lang: str,
    max_source_tokens: int,
    max_new_tokens: int,
):
    try:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "provider=nllb requires torch, transformers, and sentencepiece in "
            "the Python environment"
        ) from exc

    if device == "auto":
        resolved_device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        resolved_device = device
    if resolved_device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested for NLLB but torch.cuda.is_available() is false")

    tokenizer = AutoTokenizer.from_pretrained(model_name, src_lang=source_lang)
    dtype = torch.float16 if resolved_device.startswith("cuda") else torch.float32
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name, torch_dtype=dtype)
    model.to(resolved_device)
    model.eval()
    forced_bos = tokenizer.convert_tokens_to_ids(target_lang)
    if forced_bos is None or int(forced_bos) < 0:
        raise RuntimeError(f"NLLB target language token not found: {target_lang}")

    def translate(items: list[dict]) -> dict[str, str]:
        texts = [str(item["text"]) for item in items]
        encoded = tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=False,
        )
        lengths = encoded["attention_mask"].sum(dim=1).tolist()
        too_long = [int(value) for value in lengths if int(value) > max_source_tokens]
        if too_long:
            raise ValueError(
                f"NLLB source exceeds --max-source-tokens={max_source_tokens}: "
                f"max={max(too_long)}"
            )
        encoded = {key: value.to(resolved_device) for key, value in encoded.items()}
        with torch.inference_mode():
            generated = model.generate(
                **encoded,
                forced_bos_token_id=int(forced_bos),
                max_new_tokens=max_new_tokens,
            )
        decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
        if len(decoded) != len(items):
            raise RuntimeError(
                f"NLLB returned {len(decoded)} rows for {len(items)} inputs"
            )
        return {
            str(item["id"]): text
            for item, text in zip(items, decoded, strict=True)
        }

    translate.runtime = {  # type: ignore[attr-defined]
        "model": model_name,
        "device": resolved_device,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "dtype": str(dtype),
    }
    return translate


def load_completed(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    completed: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        source = str(row.get("source", ""))
        if not sid or sid != source_id(source):
            raise ValueError(f"existing output has invalid source identity: {sid!r}")
        if sid in completed:
            prior = completed[sid]
            if prior.get("translation") != row.get("translation"):
                raise ValueError(f"existing output has conflicting duplicate: {sid}")
            continue
        completed[sid] = row
    return completed


def append_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path)
    ap.add_argument(
        "--provider",
        choices=("command", "openai-compatible", "nllb"),
        required=True,
    )
    ap.add_argument("--command", help="local command for provider=command")
    ap.add_argument("--endpoint", help="full chat-completions endpoint URL")
    ap.add_argument("--model", default="")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--source-lang", default="jpn_Jpan")
    ap.add_argument("--target-lang", default="zho_Hans")
    ap.add_argument("--max-source-tokens", type=int, default=1024)
    ap.add_argument("--max-new-tokens", type=int, default=512)
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY")
    ap.add_argument("--batch-size", type=int, default=24)
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-delay", type=float, default=2.0)
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--status", default=ACCEPTED_STATUS)
    ap.add_argument("--glossary", type=Path)
    ap.add_argument("--style-guide", type=Path)
    ap.add_argument("--speaker-evidence", type=Path)
    ap.add_argument("--character-profiles", type=Path, help="reviewed character profile JSONL")
    ap.add_argument("--risk", type=Path, help="JSONL emitted by classify_translation_risk.py")
    ap.add_argument("--risk-level", action="append", choices=("low", "medium", "high", "critical"), help="only translate selected risk level; repeatable")
    ap.add_argument("--max-style-samples", type=int, default=2)
    ap.add_argument("--max-examples", type=int, default=0, help="limit per-item occurrence examples; 0 keeps all")
    ap.add_argument("--max-context-examples", type=int, default=0, help="limit per-item adjacent context examples; 0 keeps all")
    ap.add_argument(
        "--qa-mode",
        choices=("full", "basic"),
        default="full",
        help="full blocks every deterministic REVIEW/REJECT; basic blocks only structural/glossary failures and records other findings as warnings",
    )
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be > 0")
    if args.provider == "command" and not args.command:
        raise SystemExit("--provider command requires --command")
    if args.provider == "openai-compatible":
        if not args.endpoint or not args.model:
            raise SystemExit("--provider openai-compatible requires --endpoint and --model")
    if args.provider == "nllb" and not args.model:
        raise SystemExit("--provider nllb requires --model")

    queue = read_jsonl(args.queue)
    completed = load_completed(args.output)
    risk_index = load_risk_index(args.risk)
    selected_risk_levels = set(args.risk_level or [])
    if selected_risk_levels and not risk_index:
        raise SystemExit("--risk-level requires --risk")
    if risk_index:
        missing_risk = [
            str(row.get("source_sha256", ""))
            for row in queue
            if str(row.get("source_sha256", "")) not in risk_index
        ]
        if missing_risk:
            raise ValueError(f"risk index missing {len(missing_risk)} queue rows; first={missing_risk[0]}")
    pending = [
        row
        for row in queue
        if str(row.get("source_sha256", "")) not in completed
        and (
            not selected_risk_levels
            or str(risk_index[str(row.get("source_sha256", ""))].get("risk_level", "")) in selected_risk_levels
        )
    ]
    if args.max_items:
        pending = pending[: args.max_items]

    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    glossary = load_glossary(args.glossary)
    speaker_evidence = load_character_evidence(args.speaker_evidence)
    character_profiles = load_character_profiles(args.character_profiles)
    if args.max_style_samples < 0:
        raise SystemExit("--max-style-samples must be >= 0")
    if args.max_examples < 0 or args.max_context_examples < 0:
        raise SystemExit("--max-examples/--max-context-examples must be >= 0")
    style_guide = (
        args.style_guide.read_text(encoding="utf-8")
        if args.style_guide
        else ""
    )
    nllb_backend = None
    if args.provider == "nllb":
        nllb_backend = build_nllb_backend(
            args.model,
            args.device,
            args.source_lang,
            args.target_lang,
            args.max_source_tokens,
            args.max_new_tokens,
        )
    counts = {
        "queue_rows": len(queue),
        "already_completed": len(completed),
        "selected_pending": len(pending),
        "translated": 0,
        "needs_review": 0,
        "failed_batches": 0,
    }
    errors: list[dict] = []

    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset : offset + args.batch_size]
        batch_by_id = {
            str(row.get("source_sha256")): row
            for row in batch
        }
        items, masked = request_items(
            batch,
            glossary,
            speaker_evidence,
            args.max_style_samples,
            risk_index,
            character_profiles,
            args.max_examples,
            args.max_context_examples,
        )

        def call(call_items: list[dict]):
            if args.provider == "command":
                return translate_command(
                    args.command,
                    call_items,
                    args.timeout,
                    style_guide,
                )
            if args.provider == "nllb":
                assert nllb_backend is not None
                return nllb_backend(call_items)
            return translate_openai_compatible(
                args.endpoint,
                args.model,
                api_key,
                call_items,
                args.timeout,
                args.temperature,
                style_guide,
            )

        # Salvage valid rows from partial/mismatched backend responses and retry
        # only the missing IDs. This prevents one substituted Codex ID from
        # discarding the other valid translations in the same batch.
        response: dict[str, str] = {}
        remaining_items = list(items)
        last_error: Exception | None = None
        unexpected_ids: set[str] = set()
        for attempt in range(max(1, args.retries)):
            if not remaining_items:
                break
            requested_ids = {item["id"] for item in remaining_items}
            try:
                partial = call(remaining_items)
                unexpected_ids.update(set(partial) - requested_ids)
                for sid, translated in partial.items():
                    if sid in requested_ids:
                        response[sid] = translated
                remaining_items = [
                    item for item in remaining_items
                    if item["id"] not in response
                ]
                if not remaining_items:
                    break
                last_error = ValueError(
                    f"partial response missing {len(remaining_items)} ids"
                )
            except Exception as exc:
                last_error = exc
            if attempt + 1 < max(1, args.retries):
                time.sleep(args.retry_delay * (attempt + 1))

        # If a bulk retry still misses a few IDs, repair those IDs one-by-one.
        # A single-row request is much less likely to suffer an ID substitution.
        if response and remaining_items and len(remaining_items) <= 4:
            for missing_item in list(remaining_items):
                sid = missing_item["id"]
                for repair_attempt in range(max(1, args.retries)):
                    try:
                        partial = call([missing_item])
                        if sid in partial:
                            response[sid] = partial[sid]
                            break
                    except Exception as exc:
                        last_error = exc
                    if repair_attempt + 1 < max(1, args.retries):
                        time.sleep(args.retry_delay * (repair_attempt + 1))

        expected_ids = {item["id"] for item in items}
        actual_ids = set(response)
        items_to_write = items
        if expected_ids != actual_ids:
            counts["failed_batches"] += 1
            errors.append(
                {
                    "offset": offset,
                    "size": len(batch),
                    "error": str(last_error or "response id set mismatch"),
                    "salvaged": len(actual_ids & expected_ids),
                    "missing": sorted(expected_ids - actual_ids)[:20],
                    "unexpected": sorted(unexpected_ids)[:20],
                }
            )
            # Persist every valid row we did recover. The unresolved IDs remain
            # absent from the output and will be selected automatically on the
            # next resume pass.
            items_to_write = [
                item for item in items
                if item["id"] in response
            ]
            if not items_to_write:
                continue

        output_rows: list[dict] = []
        for item in items_to_write:
            sid = item["id"]
            source, tokens = masked[sid]
            translated_raw = response[sid].strip()
            status = args.status
            error = ""
            try:
                translated = restore_tokens(translated_raw, tokens)
                validate_translation(source, translated)
                if not translated:
                    status = "needs_review"
                    error = "empty_translation"
                elif translated == source and args.qa_mode == "full":
                    status = "needs_review"
                    error = "unchanged_translation"
            except Exception as exc:
                translated = translated_raw
                status = "needs_review"
                error = str(exc)

            source_row = batch_by_id[sid]
            row = {
                "source_sha256": sid,
                "source": source,
                "translation": translated,
                "status": status,
                "provenance": f"machine:{args.provider}",
                "model": args.model or args.command,
                "occurrences": source_row.get("occurrences"),
                "examples": source_row.get("examples", []),
            }
            if not error:
                qa = evaluate_row(source_row, row, glossary)
                qa_issues = qa["issues"]
                if args.qa_mode == "full":
                    blocking_issues = qa_issues
                else:
                    blocking_issues = [
                        issue
                        for issue in qa_issues
                        if issue.get("code") in BASIC_BLOCKING_QA_CODES
                    ]
                if qa_issues:
                    row["deterministic_qa_verdict"] = qa["qa_verdict"]
                    row["deterministic_qa_issues"] = qa_issues
                    if args.qa_mode == "basic" and not blocking_issues:
                        row["qa_nonblocking"] = True
                if blocking_issues:
                    status = "needs_review"
                    row["status"] = status
                    error = "deterministic_qa:" + ",".join(
                        str(issue.get("code", ""))
                        for issue in blocking_issues
                    )
            if error:
                row["review_reason"] = error
                counts["needs_review"] += 1
            else:
                counts["translated"] += 1
            output_rows.append(row)

        append_rows(args.output, output_rows)
        completed.update({row["source_sha256"]: row for row in output_rows})
        done = min(offset + len(batch), len(pending))
        print(
            f"progress {done}/{len(pending)} translated={counts['translated']} "
            f"needs_review={counts['needs_review']}",
            file=sys.stderr,
            flush=True,
        )

    result = {
        "schema_version": 1,
        "queue": str(args.queue),
        "output": str(args.output),
        "provider": args.provider,
        "model": args.model,
        "risk_rows_loaded": len(risk_index),
        "selected_risk_levels": sorted(selected_risk_levels),
        "qa_mode": args.qa_mode,
        "approved_character_profiles_loaded": len(character_profiles),
        "device": (
            getattr(nllb_backend, "runtime", {}).get("device")
            if nllb_backend is not None
            else None
        ),
        **counts,
        "total_output_rows": len(load_completed(args.output)),
        "complete_for_selected_range": counts["failed_batches"] == 0
        and counts["translated"] + counts["needs_review"] == len(pending),
        "errors_first": errors[:20],
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
