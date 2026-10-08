#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSON command-backend adapter for Codex GPT-5.6 Luna.

Reads one JSON request from stdin and writes one JSON response to stdout.
Designed for translate_gtx_queue.py, review_gtx_translations.py, and
evaluate_translation_benchmark.py provider=command.

The model is intentionally hard-pinned to gpt-5.6-luna.  No fallback model is
permitted.  Codex diagnostics stay on stderr; only the validated structured
answer is emitted to stdout.

On the current Windows Codex build, MCP shutdown can linger after the final
answer is already available.  We therefore request both output-schema and
--output-last-message, poll the latter for a complete JSON object, validate it,
then terminate the disposable Codex process tree.  This avoids turning a good
model answer into a command-backend timeout.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MODEL = "gpt-5.6-luna"


def mode_for(payload: dict[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("request requires non-empty items[]")
    first = items[0]
    if not isinstance(first, dict):
        raise ValueError("items[] must contain objects")
    if "official_reference" in first and "candidate" in first:
        return "benchmark"
    if "translation" in first and "source" in first:
        return "review"
    if "text" in first:
        return "translate"
    raise ValueError("cannot infer request mode")


def schema_for(mode: str) -> dict[str, Any]:
    score_1_5 = {"type": "integer", "minimum": 1, "maximum": 5}
    if mode == "translate":
        return {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["translations"],
            "additionalProperties": False,
        }
    if mode == "review":
        return {
            "type": "object",
            "properties": {
                "reviews": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "verdict": {"type": "string", "enum": ["PASS", "REVIEW", "REJECT"]},
                            "scores": {
                                "type": "object",
                                "properties": {
                                    "semantic_accuracy": score_1_5,
                                    "terminology": score_1_5,
                                    "fluency": score_1_5,
                                    "character_voice": score_1_5,
                                    "context_consistency": score_1_5,
                                },
                                "required": [
                                    "semantic_accuracy",
                                    "terminology",
                                    "fluency",
                                    "character_voice",
                                    "context_consistency",
                                ],
                                "additionalProperties": False,
                            },
                            "blocking_errors": {"type": "array", "items": {"type": "string"}},
                            "notes": {"type": "string"},
                        },
                        "required": ["id", "verdict", "scores", "blocking_errors", "notes"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["reviews"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["PASS", "REVIEW", "REJECT"]},
                        "scores": {
                            "type": "object",
                            "properties": {
                                "semantic_accuracy": score_1_5,
                                "reference_consistency": score_1_5,
                                "terminology": score_1_5,
                                "fluency": score_1_5,
                                "character_voice": score_1_5,
                            },
                            "required": [
                                "semantic_accuracy",
                                "reference_consistency",
                                "terminology",
                                "fluency",
                                "character_voice",
                            ],
                            "additionalProperties": False,
                        },
                        "error_classes": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
                    },
                    "required": ["id", "verdict", "scores", "error_classes", "notes"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["evaluations"],
        "additionalProperties": False,
    }


def instruction_for(mode: str) -> str:
    if mode == "translate":
        return (
            "You are the Translator role for THE IDOLM@STER Million Live! Theater Days. "
            "Translate every supplied Japanese item into natural Simplified Chinese. "
            "Use all supplied context, glossary, risk, approved character profile, and "
            "historical official voice examples as evidence. Historical examples may be "
            "Traditional Chinese; never output Traditional Chinese merely because an example "
            "uses it. Preserve every __MLTD_TOKEN_NNN__ marker exactly once and unchanged. "
            "Preserve visible Arabic numeric literals exactly (for example 1 must remain 1, not 一). "
            "Do not infer or add singular/plural audience, gender, subject/object, pronouns, or indefinite filler objects such as 什么 when "
            "the Japanese leaves them unspecified; use neutral Chinese wording when the source is neutral. Do not add explicit 我们/咱们 unless the Japanese supplies a plural/inclusive cue such as 私たち, みんなで, or 一緒に. Do not narrow お客さん/お客様 to 观众 unless the source itself establishes live/stage/audience context. "
            "Preserve explicit adversative/concessive semantics: sentence-final けど/けれど/ですが/だが with an unfinished pause must retain a Chinese but/however relation rather than collapsing to a bare ellipsis. "
            "Do not silently drop Japanese address semantics: when a protected player-name token is "
            "immediately followed by さん, normally render the equivalent respectful Chinese address "
            "as that same protected token followed by 先生 unless supplied project evidence says otherwise. "
            "Distinguish emotive reaction words and mimetics from literal physical sound effects; a shock or dismay reaction must not become a crash/impact sound unless context establishes an actual sound event. Preserve marked social/emotional exclamations: coquettish/protesting やぁ～ん/いや～ん must not be flattened to neutral 哎呀/哎哟. In ordinary spoken dialogue, generic 応援 must not default to the fandom loanword 应援 unless glossary/source-bound evidence establishes that term; choose natural 加油/打气/支持 according to context. "
            "Do not invent permanent semantic localizations for franchise proper names, units, titles, ranks, or branded entities without glossary or source-bound official evidence. "
            "Preserve precise semantic contrasts: 悪い意味ではない/悪い意味じゃない means not a bad or negative meaning, not merely no other meaning. Resolve 今度 from tense/discourse context; in a future promise or intention do not translate it as 这次. Treat 話 as context-sensitive (speech/topic/matter/story) and do not default to 故事 without narrative evidence. Preserve concrete lexical classes: バイク is a motorbike/motorcycle, not a bicycle; standalone 劇 retains a play/drama sense rather than generic 演出; タックル retains tackle/action specificity in tactical or sporting context. Prefer native Chinese word order and avoid detectable Japanese calques such as 留到很晚努力着; rewrite naturally while preserving meaning. Preserve explanatory/playful のだ/のだよ/なのだよ voice when natural, especially on high/critical-risk dialogue. Preserve acceptance/resignation strength: mild まあ、いいか/ま、いいか should not be strengthened into strongly defeatist Chinese such as bare 算了/就这样吧 unless context clearly supports that tone. "
            "If an item includes previous_translation and review_feedback, this is a repair pass: use the "
            "feedback to fix every identified issue, especially awkward Chinese, while re-checking the Japanese "
            "source from scratch. Do not preserve flawed wording merely for consistency with the previous candidate. "
            "Do not omit, add, explain, annotate, romanize, or return Japanese unchanged. "
            "For shared text, choose wording valid across every occurrence. Return only the "
            "schema-conforming JSON object, with exactly one translation for every input id."
        )
    if mode == "review":
        return (
            "You are an independent QA Reviewer. Do not rewrite translations. Compare every "
            "candidate against its Japanese source, all contexts, terminology constraints, "
            "protected markers, risk metadata, approved character profile and official voice "
            "evidence. PASS only when safe to release. semantic_accuracy=5 means no detected "
            "mistranslation, omission, addition, polarity, subject/object, entity, temporal or "
            "numeric error. Treat unsupported singular/plural, gender, pronoun, subject/object, or indefinite filler objects such as 什么 "
            "as an addition/semantic error. Explicit 我们/咱们 without a Japanese plural/inclusive cue is an unsupported subject addition. Treat お客さん/お客様 -> 观众 as semantic narrowing unless the source itself establishes live/stage/audience context. Explicit sentence-final けど/けれど/ですが/だが must preserve its adversative/concessive relation; a bare ellipsis without an equivalent but/however cue is at least REVIEW. Treat loss of an explicit address/honorific "
            "as at least REVIEW unless project evidence establishes the localized convention. "
            "Items intentionally mask runtime tokens inside source/translation as __MLTD_PROTECTED_NNN__; compare protected_source and protected_translation arrays and do not reject a row merely because the raw token is absent from the masked field when the arrays correspond. "
            "Distinguish emotive reaction words/mimetics from literal physical sound effects; if a reaction could have been mistaken for an impact/crash sound, require context evidence or use REVIEW/REJECT. Marked coquettish/protesting やぁ～ん/いや～ん must not be flattened to neutral 哎呀/哎哟. In ordinary spoken dialogue, generic 応援 -> 应援 without glossary/source-bound evidence is terminology/fluency uncertainty and must not PASS solely because it is understandable. "
            "For franchise proper names, unit names, titles, ranks, and branded entities, lack of glossary or source-bound official evidence is uncertainty: do not approve an invented semantic localization merely because it is readable Chinese. "
            "For puns/wordplay and pragmatically ambiguous wording, literal recoverability alone is insufficient; if the Chinese sounds contrived or shifts what is wrong/allowed/appropriate, use REVIEW. "
            "Check lexical-semantic near-misses strictly: 悪い意味ではない/じゃない is not equivalent to no other meaning; 今度 must follow temporal/discourse context and future intent must not become 这次; 話 must not be narrowed to 故事 unless narrative meaning is established. Preserve concrete lexical classes: バイク must not become bicycle wording; standalone 劇 must not be generalized to plain 演出 when play/drama is intended; タックル must not collapse to generic 冲撞 in tactical/sporting context. Fluency=5 requires native Chinese word order with no detectable Japanese calque; awkward constructions such as 留到很晚努力着 are at least REVIEW even when semantics are recoverable. Treat explanatory/playful のだ/のだよ/なのだよ as voice evidence, especially for high/critical rows. Preserve pragmatic strength: mild まあ、いいか/ま、いいか must not PASS if rendered as strongly defeatist/resigned Chinese such as bare 算了/就这样吧 without context supporting that stronger stance. "
            "Uncertainty is REVIEW; material error is REJECT. High/critical "
            "items require strict context, terminology, fluency and character-voice scrutiny. "
            "Return only schema-conforming JSON with exactly one review for every input id."
        )
    return (
        "You are the hidden-reference benchmark evaluator for Japanese -> Simplified Chinese "
        "MLTD localization. Judge source meaning first. The official reference is strong "
        "semantic/terminology evidence but may be Traditional Chinese or Taiwan wording, so do "
        "not require script or wording equality. PASS means release-quality. REVIEW means "
        "uncertain/minor issue; REJECT means material error. semantic_accuracy=5 means no "
        "detected mistranslation, omission, addition, polarity, subject/object, entity or "
        "number error. Allowed error classes are mistranslation, omission, addition, polarity, "
        "subject_or_object, entity, number, terminology, tone, context. Return only the "
        "schema-conforming JSON object with exactly one evaluation for every input id."
    )


def output_rows(value: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    key = {"translate": "translations", "review": "reviews", "benchmark": "evaluations"}[mode]
    rows = value.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"response missing {key}[]")
    return rows


def validate_ids(value: dict[str, Any], payload: dict[str, Any], mode: str) -> None:
    """Keep only usable expected IDs so the caller can retry missing rows.

    Codex occasionally returns the correct number of rows while substituting one
    or more IDs. Treating that as an all-or-nothing batch failure wastes valid
    translations. We fail closed on duplicate expected IDs, drop unexpected
    rows, and return the valid subset. The outer translator is responsible for
    retrying only the missing IDs.
    """
    expected = [str(x.get("id", "")) for x in payload["items"]]
    if not all(expected) or len(set(expected)) != len(expected):
        raise ValueError("request contains missing/duplicate ids")
    expected_set = set(expected)
    rows = output_rows(value, mode)
    key = {"translate": "translations", "review": "reviews", "benchmark": "evaluations"}[mode]
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id", ""))
        if sid not in expected_set:
            continue
        if sid in seen:
            raise ValueError(f"response contains duplicate expected id: {sid}")
        kept.append(row)
        seen.add(sid)
    if not kept:
        raise ValueError(
            f"response contains no usable expected ids: expected={len(expected)}"
        )
    value[key] = kept


def kill_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def read_json_candidate(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        # Codex stdout is normally only the final structured answer.  Keep a
        # defensive extractor in case a future client adds a short prefix.
        start = text.find("{")
        if start < 0:
            return None
        for end in range(len(text), start, -1):
            try:
                value = json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
            return value if isinstance(value, dict) else None
    return None


def read_codex_stderr_candidate(path: Path) -> dict[str, Any] | None:
    """Extract the live final-answer JSON from Codex's stderr transcript."""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    marker = "\ncodex\n"
    pos = text.rfind(marker)
    if pos >= 0:
        tail = text[pos + len(marker) :].lstrip()
    elif text.startswith("codex\n"):
        tail = text[len("codex\n") :].lstrip()
    else:
        return None
    start = tail.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(tail[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=float, default=150.0)
    ap.add_argument("--poll", type=float, default=0.25)
    ap.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="",
        help="optional Codex model_reasoning_effort override; model remains hard-pinned to gpt-5.6-luna",
    )
    ap.add_argument("--input", type=Path, help="read request JSON from a file instead of stdin")
    ap.add_argument("--output", type=Path, help="write response JSON to a file instead of stdout")
    args = ap.parse_args()
    if args.timeout <= 0 or args.poll <= 0:
        raise SystemExit("timeout/poll must be positive")

    if args.input:
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
    else:
        payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise SystemExit("stdin must be a JSON object")
    mode = mode_for(payload)

    codex = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    if not codex:
        raise SystemExit("codex executable not found")

    with tempfile.TemporaryDirectory(prefix="mltd-codex-luna-") as td:
        temp = Path(td)
        schema = temp / "schema.json"
        result = temp / "result.json"
        stdout_capture = temp / "stdout.json"
        stderr_log = temp / "stderr.log"
        schema.write_text(
            json.dumps(schema_for(mode), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        prompt = (
            instruction_for(mode)
            + "\n\nREQUEST_JSON:\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        cmd = [
            codex,
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
            "-c",
            "mcp_servers.notion.enabled=false",
            "-m",
            MODEL,
        ]
        if args.reasoning_effort:
            cmd += ["-c", f'model_reasoning_effort="{args.reasoning_effort}"']
        cmd += [
            "-s",
            "read-only",
            "-a",
            "never",
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(result),
            "--color",
            "never",
            "-",
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        stdout_handle = stdout_capture.open("w", encoding="utf-8", errors="replace")
        stderr_handle = stderr_log.open("w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=Path.cwd(),
            creationflags=creationflags,
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()

        deadline = time.monotonic() + args.timeout
        parsed: dict[str, Any] | None = None
        last_parse_error = ""
        try:
            while time.monotonic() < deadline:
                for candidate_path in (result, stdout_capture):
                    try:
                        candidate = read_json_candidate(candidate_path)
                        if candidate is None:
                            continue
                        validate_ids(candidate, payload, mode)
                        parsed = candidate
                        break
                    except Exception as exc:
                        last_parse_error = (
                            f"{candidate_path.name}: {type(exc).__name__}: {exc}"
                        )
                if parsed is not None:
                    break
                try:
                    candidate = read_codex_stderr_candidate(stderr_log)
                    if candidate is not None:
                        validate_ids(candidate, payload, mode)
                        parsed = candidate
                        break
                except Exception as exc:
                    last_parse_error = f"stderr.log: {type(exc).__name__}: {exc}"
                if proc.poll() is not None:
                    break
                time.sleep(args.poll)

            if parsed is None:
                for candidate_path in (result, stdout_capture):
                    candidate = read_json_candidate(candidate_path)
                    if candidate is None:
                        continue
                    validate_ids(candidate, payload, mode)
                    parsed = candidate
                    break
            if parsed is None:
                candidate = read_codex_stderr_candidate(stderr_log)
                if candidate is not None:
                    validate_ids(candidate, payload, mode)
                    parsed = candidate

            if parsed is None:
                stderr_handle.flush()
                stderr = (
                    stderr_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                    if stderr_log.is_file()
                    else ""
                )
                raise RuntimeError(
                    f"Codex Luna produced no valid structured result; "
                    f"exit={proc.poll()} parse={last_parse_error!r} stderr={stderr!r}"
                )
        finally:
            kill_tree(proc)
            stdout_handle.close()
            stderr_handle.close()

        rendered = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            tmp = args.output.with_suffix(args.output.suffix + ".tmp")
            tmp.write_text(rendered, encoding="utf-8")
            tmp.replace(args.output)
        else:
            sys.stdout.write(rendered)
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
