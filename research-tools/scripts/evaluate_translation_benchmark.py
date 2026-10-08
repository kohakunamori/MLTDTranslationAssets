#!/usr/bin/env python3
"""Reference-aware evaluator for the fixed MLTD hidden official-translation benchmark."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
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

from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import index_unique
from scripts.review_gtx_translations import mask_for_review
from scripts.translate_gtx_queue import strip_json_fence

SCORES = (
    "semantic_accuracy",
    "reference_consistency",
    "terminology",
    "fluency",
    "character_voice",
)
ERROR_CLASSES = {
    "mistranslation",
    "omission",
    "addition",
    "polarity",
    "subject_or_object",
    "entity",
    "number",
    "terminology",
    "tone",
    "context",
}
VERDICTS = {"PASS", "REVIEW", "REJECT"}


def load_reference(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        reference = str(row.get("reference_translation", ""))
        if not sid or not reference:
            raise ValueError("hidden reference row requires source_sha256 and reference_translation")
        if sid in out:
            raise ValueError(f"duplicate hidden reference id: {sid}")
        out[sid] = row
    return out


def build_items(
    source_rows: dict[str, dict],
    references: dict[str, dict],
    candidates: dict[str, dict],
) -> list[dict]:
    expected = set(source_rows)
    if set(references) != expected:
        raise ValueError(
            "benchmark source/reference identity mismatch: "
            f"source={len(expected)} reference={len(references)}"
        )
    unexpected = sorted(set(candidates) - expected)
    if unexpected:
        raise ValueError(f"candidate contains non-benchmark id: {unexpected[0]}")
    items: list[dict] = []
    for sid in sorted(set(candidates) & expected):
        source = str(source_rows[sid]["source"])
        candidate = str(candidates[sid].get("translation", ""))
        reference = str(references[sid]["reference_translation"])
        source_masked, _ = mask_for_review(source)
        candidate_masked, _ = mask_for_review(candidate)
        reference_masked, _ = mask_for_review(reference)
        items.append({
            "id": sid,
            "category": source_rows[sid].get("category", "unknown"),
            "source": source_masked,
            "candidate": candidate_masked,
            "official_reference": reference_masked,
        })
    return items


def normalize(value) -> dict[str, dict]:
    if isinstance(value, dict) and "evaluations" in value:
        value = value["evaluations"]
    if not isinstance(value, list):
        raise ValueError("benchmark evaluator must return an evaluations list")
    out: dict[str, dict] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("evaluation contains a non-object")
        sid = str(row.get("id", ""))
        if not sid or sid in out:
            raise ValueError(f"missing or duplicate evaluation id: {sid!r}")
        verdict = str(row.get("verdict", "")).upper()
        if verdict not in VERDICTS:
            raise ValueError(f"{sid}: invalid verdict {verdict!r}")
        raw_scores = row.get("scores")
        if not isinstance(raw_scores, dict):
            raise ValueError(f"{sid}: missing scores")
        scores: dict[str, int] = {}
        for name in SCORES:
            try:
                score = int(raw_scores[name])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{sid}: invalid score {name}") from exc
            if not 1 <= score <= 5:
                raise ValueError(f"{sid}: score {name} out of range")
            scores[name] = score
        raw_errors = row.get("error_classes", [])
        if not isinstance(raw_errors, list):
            raise ValueError(f"{sid}: error_classes must be an array")
        errors = [str(x) for x in raw_errors]
        unknown = sorted(set(errors) - ERROR_CLASSES)
        if unknown:
            raise ValueError(f"{sid}: unknown error class {unknown[0]!r}")
        out[sid] = {
            "verdict": verdict,
            "scores": scores,
            "error_classes": errors,
            "notes": str(row.get("notes", "")),
        }
    return out


def system_prompt() -> str:
    return """You evaluate Japanese -> Simplified Chinese MLTD translations against a hidden historical official Chinese reference.
The historical reference may be Traditional Chinese or use Taiwan regional wording, so DO NOT require exact wording or script equality. Judge source meaning first, and use the official reference as strong semantic/terminology evidence.
Return JSON only:
{"evaluations":[{"id":"same id","verdict":"PASS|REVIEW|REJECT","scores":{"semantic_accuracy":1-5,"reference_consistency":1-5,"terminology":1-5,"fluency":1-5,"character_voice":1-5},"error_classes":[],"notes":"short"}]}.
semantic_accuracy=5 means no detected mistranslation, omission, addition, polarity, subject/object, entity or number error.
Allowed error_classes: mistranslation, omission, addition, polarity, subject_or_object, entity, number, terminology, tone, context.
PASS means release-quality relative to the available evidence. REVIEW means uncertain/minor issue. REJECT means a material translation error."""


def command_call(command: str, items: list[dict], timeout: float) -> dict[str, dict]:
    payload = json.dumps({"rubric": system_prompt(), "items": items}, ensure_ascii=False)
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(
        command if os.name == "nt" else __import__("shlex").split(command),
        input=payload,
        text=True,
        encoding="utf-8",
        errors="strict",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"benchmark command exited {proc.returncode}: {proc.stderr[-2000:]}")
    return normalize(strip_json_fence(proc.stdout))


def openai_call(
    endpoint: str,
    model: str,
    api_key: str,
    items: list[dict],
    timeout: float,
) -> dict[str, dict]:
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"))},
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
        raise RuntimeError(f"benchmark HTTP {exc.code}: {exc.read(4000).decode('utf-8','replace')}") from exc
    except URLError as exc:
        raise RuntimeError(f"benchmark endpoint error: {exc}") from exc
    payload = json.loads(raw.decode("utf-8"))
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("benchmark response missing choices[]")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise ValueError("benchmark response missing message.content")
    return normalize(strip_json_fence(content))


def append_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_completed(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        if not sid or sid in out:
            raise ValueError(f"invalid/duplicate saved evaluation id: {sid!r}")
        out[sid] = row
    return out


def aggregate(rows: list[dict], total_expected: int) -> dict:
    verdicts = Counter()
    errors = Counter()
    category_rows: dict[str, list[dict]] = defaultdict(list)
    score_sums = Counter()
    semantic_five = 0
    for row in rows:
        verdicts[str(row["verdict"]).upper()] += 1
        for error in row.get("error_classes", []):
            errors[str(error)] += 1
        scores = row["scores"]
        for name in SCORES:
            score_sums[name] += int(scores[name])
        semantic_five += int(scores["semantic_accuracy"]) == 5
        category_rows[str(row.get("category", "unknown"))].append(row)

    def metrics(group: list[dict]) -> dict:
        n = len(group)
        if not n:
            return {"evaluated": 0}
        counts = Counter(str(x["verdict"]).upper() for x in group)
        return {
            "evaluated": n,
            "pass": counts["PASS"],
            "review": counts["REVIEW"],
            "reject": counts["REJECT"],
            "pass_rate": counts["PASS"] / n,
            "semantic_5_rate": sum(int(x["scores"]["semantic_accuracy"]) == 5 for x in group) / n,
        }

    evaluated = len(rows)
    return {
        "expected": total_expected,
        "evaluated": evaluated,
        "coverage": evaluated / total_expected if total_expected else 0.0,
        "verdicts": dict(verdicts),
        "semantic_5_rate": semantic_five / evaluated if evaluated else 0.0,
        "mean_scores": {
            name: score_sums[name] / evaluated if evaluated else 0.0
            for name in SCORES
        },
        "error_classes": dict(errors),
        "categories": {
            category: metrics(group)
            for category, group in sorted(category_rows.items())
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmark-source", type=Path, required=True)
    ap.add_argument("--hidden-reference", type=Path, required=True)
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--provider", choices=("command", "openai-compatible"), required=True)
    ap.add_argument("--command")
    ap.add_argument("--endpoint")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-delay", type=float, default=2.0)
    args = ap.parse_args()

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be >0")
    if args.provider == "command" and not args.command:
        raise SystemExit("--provider command requires --command")
    if args.provider == "openai-compatible" and (not args.endpoint or not args.model):
        raise SystemExit("--provider openai-compatible requires --endpoint and --model")

    sources = index_unique(read_jsonl(args.benchmark_source), "benchmark-source")
    references = load_reference(args.hidden_reference)
    candidates = index_unique(read_jsonl(args.candidates), "benchmark-candidates")
    items = build_items(sources, references, candidates)
    completed = load_completed(args.output)
    pending = [x for x in items if x["id"] not in completed]
    if args.max_items:
        pending = pending[:args.max_items]
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    failed_batches = 0
    errors: list[dict] = []

    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset:offset + args.batch_size]
        response = None
        last_error: Exception | None = None
        for attempt in range(max(1, args.retries)):
            try:
                response = (
                    command_call(args.command, batch, args.timeout)
                    if args.provider == "command"
                    else openai_call(args.endpoint, args.model, api_key, batch, args.timeout)
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 < max(1, args.retries):
                    time.sleep(args.retry_delay * (attempt + 1))
        if response is None:
            failed_batches += 1
            errors.append({"offset": offset, "size": len(batch), "error": str(last_error)})
            break
        expected = {x["id"] for x in batch}
        if set(response) != expected:
            failed_batches += 1
            errors.append({
                "offset": offset,
                "error": "response id set mismatch",
                "missing": sorted(expected - set(response))[:20],
                "unexpected": sorted(set(response) - expected)[:20],
            })
            break
        rows = []
        for item in batch:
            result = response[item["id"]]
            rows.append({
                "source_sha256": item["id"],
                "category": item["category"],
                **result,
                "evaluator_provenance": f"benchmark:{args.provider}",
                "evaluator_model": args.model or args.command,
            })
        append_rows(args.output, rows)

    all_rows = list(load_completed(args.output).values())
    summary = {
        "schema_version": 1,
        "provider": args.provider,
        "model": args.model,
        "benchmark_reference_hidden_from_translator": True,
        "candidate_rows": len(candidates),
        "failed_batches": failed_batches,
        "errors_first": errors[:20],
        **aggregate(all_rows, len(sources)),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
