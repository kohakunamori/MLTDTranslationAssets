#!/usr/bin/env python3
"""Independent semantic/style reviewer for MLTD translation candidates."""
from __future__ import annotations

import argparse
import json
import os
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

from mltd_localize_gtx import PROTECTED_TOKEN_RE, read_jsonl
from mltd_translation_quality import applicable_glossary, index_unique, load_glossary
from build_character_voice_evidence import (
    load_character_evidence,
    select_official_style_samples,
)
from translate_gtx_queue import strip_json_fence
from classify_translation_risk import load_risk_index
from mltd_character_profiles import load_character_profiles, select_character_profile

SCORE_NAMES = (
    "semantic_accuracy",
    "terminology",
    "fluency",
    "character_voice",
    "context_consistency",
)
VERDICTS = {"PASS", "REVIEW", "REJECT"}


def mask_for_review(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []
    def repl(match):
        tokens.append(match.group(0))
        return f"__MLTD_PROTECTED_{len(tokens)-1:03d}__"
    return PROTECTED_TOKEN_RE.sub(repl, text), tokens


def build_review_items(
    queue_rows: dict[str, dict],
    candidates: dict[str, dict],
    glossary: dict,
    speaker_evidence: dict[str, dict] | None = None,
    max_style_samples: int = 2,
    risk_index: dict[str, dict] | None = None,
    character_profiles: dict[str, dict] | None = None,
) -> list[dict]:
    items: list[dict] = []
    speaker_evidence = speaker_evidence or {}
    risk_index = risk_index or {}
    character_profiles = character_profiles or {}
    for sid, candidate in candidates.items():
        queue = queue_rows.get(sid)
        if queue is None:
            continue
        source = str(queue["source"])
        translation = str(candidate.get("translation", ""))
        source_masked, source_tokens = mask_for_review(source)
        translation_masked, translation_tokens = mask_for_review(translation)
        usage_profile = queue.get("usage_profile", {})
        items.append({
            "id": sid,
            "source": source_masked,
            "translation": translation_masked,
            "protected_source": source_tokens,
            "protected_translation": translation_tokens,
            "examples": queue.get("examples", []),
            "context_examples": queue.get("context_examples", []),
            "usage_profile": usage_profile,
            "occurrences": queue.get("occurrences"),
            "queue_reason": queue.get("queue_reason", ""),
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
        })
    return items


def normalize_reviews(value) -> dict[str, dict]:
    if isinstance(value, dict) and "reviews" in value:
        value = value["reviews"]
    if not isinstance(value, list):
        raise ValueError("review backend must return {'reviews':[...]} or a list")
    out: dict[str, dict] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("review response contains a non-object")
        sid = str(row.get("id", ""))
        if not sid or sid in out:
            raise ValueError(f"missing or duplicate review id: {sid!r}")
        verdict = str(row.get("verdict", "")).upper()
        if verdict not in VERDICTS:
            raise ValueError(f"{sid}: invalid verdict {verdict!r}")
        scores = row.get("scores")
        if not isinstance(scores, dict):
            raise ValueError(f"{sid}: scores must be an object")
        normalized_scores: dict[str, int] = {}
        for name in SCORE_NAMES:
            try:
                score = int(scores[name])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{sid}: missing/invalid score {name}") from exc
            if score < 1 or score > 5:
                raise ValueError(f"{sid}: score {name} out of range: {score}")
            normalized_scores[name] = score
        blocking = row.get("blocking_errors", [])
        if not isinstance(blocking, list):
            raise ValueError(f"{sid}: blocking_errors must be a list")
        out[sid] = {
            "verdict": verdict,
            "scores": normalized_scores,
            "blocking_errors": [str(x) for x in blocking],
            "notes": str(row.get("notes", "")),
        }
    return out


def command_review(command: str, items: list[dict], timeout: float, style_guide: str) -> dict[str, dict]:
    payload = json.dumps({
        "rubric": {
            "score_range": "1..5",
            "semantic_accuracy_5": "No detected semantic error, omission, addition, polarity/subject/number error.",
            "pass_rule": "PASS only when safe to release; uncertainty is REVIEW; material error is REJECT.",
        },
        "style_guide": style_guide,
        "items": items,
    }, ensure_ascii=False)
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
        env=env,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"review command exited {proc.returncode}: {proc.stderr[-2000:]}")
    return normalize_reviews(strip_json_fence(proc.stdout))


def openai_review(endpoint: str, model: str, api_key: str, items: list[dict], timeout: float, style_guide: str) -> dict[str, dict]:
    system = """You are an independent QA reviewer for Japanese -> Simplified Chinese MLTD localization.
Do NOT rewrite the translation. Evaluate it independently against the Japanese source and supplied context.
Return JSON only: {"reviews":[{"id":"same id","verdict":"PASS|REVIEW|REJECT","scores":{"semantic_accuracy":1-5,"terminology":1-5,"fluency":1-5,"character_voice":1-5,"context_consistency":1-5},"blocking_errors":[],"notes":"short"}]}.
PASS means safe to release. semantic_accuracy=5 means no detected semantic error, omission, addition, polarity/subject/number mistake. Unsupported singular/plural audience, gender, personal pronoun, or subject/object inference is an addition/semantic error. Explicit 我们/咱们 without a Japanese plural/inclusive cue is an unsupported subject addition. Treat お客さん/お客様 -> 观众 as semantic narrowing unless the source itself establishes live/stage/audience context. Explicit Japanese address/honorific semantics must not disappear silently; a protected player-name token followed by さん normally corresponds to that token plus 先生 unless supplied project evidence establishes an exception. Unsupported indefinite filler objects such as 什么 are semantic additions when the Japanese leaves the object implicit. Explicit sentence-final けど/けれど/ですが/だが must retain an adversative/concessive relation; a bare ellipsis without an equivalent but/however cue is at least REVIEW. Visible Arabic numeric literals must remain exact. Evaluate all supplied context occurrences and usage_profile. If one deduplicated translation is not valid across its listed speakers/categories, use REVIEW or REJECT rather than judging only the first occurrence. An item may contain an explicitly reviewed character_profile; use it to judge voice consistency, but do not excuse semantic deviation merely because the Chinese sounds in-character. Each item may include deterministic risk metadata: high/critical items require especially strict terminology, context-consistency and character-voice scrutiny; critical items are expected to receive a second independent review downstream. If context is insufficient or meaning is ambiguous, use REVIEW. Material mistranslation is REJECT. Protected markers represent runtime data and must correspond. Masked source/translation fields may contain __MLTD_PROTECTED_NNN__; compare the protected_source and protected_translation arrays before flagging a token mismatch. Distinguish emotive reaction words and mimetics from literal physical sound effects; if that distinction is uncertain from context, use REVIEW. Marked coquettish/protesting やぁ～ん/いや～ん must not be flattened to neutral 哎呀/哎哟. In ordinary spoken dialogue, generic 応援 -> 应援 without glossary/source-bound evidence is terminology/fluency uncertainty and must not PASS solely because it is understandable. Proper names, units, titles, ranks, and branded entities without glossary or source-bound official evidence are terminology uncertainty; do not award PASS solely because an invented Chinese form is readable. Check lexical-semantic near-misses strictly: 悪い意味ではない/じゃない is not equivalent to no other meaning; 今度 must follow temporal/discourse context and future intent must not become 这次; 話 must not be narrowed to 故事 unless narrative meaning is established. Preserve concrete lexical classes: バイク must not become bicycle wording; standalone 劇 must not be generalized to plain 演出 when play/drama is intended; タックル must not collapse to generic 冲撞 in tactical/sporting context. Fluency=5 requires native Chinese word order with no detectable Japanese calque; awkward constructions such as 留到很晚努力着 are at least REVIEW even when semantics are recoverable. Treat explanatory/playful のだ/のだよ/なのだよ as voice evidence, especially for high/critical rows. Preserve pragmatic strength: mild まあ、いいか/ま、いいか must not PASS if rendered as strongly defeatist/resigned Chinese such as bare 算了/就这样吧 without context supporting that stronger stance."""
    user = json.dumps({"style_guide": style_guide, "items": items}, ensure_ascii=False, separators=(",", ":"))
    body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"review HTTP {exc.code}: {exc.read(4000).decode('utf-8','replace')}") from exc
    except URLError as exc:
        raise RuntimeError(f"review endpoint error: {exc}") from exc
    payload = json.loads(raw.decode("utf-8"))
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("review response missing choices[]")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise ValueError("review response missing message.content")
    return normalize_reviews(strip_json_fence(content))


def load_completed(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        if not sid:
            raise ValueError("existing review row missing source_sha256")
        if sid in out:
            raise ValueError(f"existing review duplicate: {sid}")
        out[sid] = row
    return out


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
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path)
    ap.add_argument("--provider", choices=("command", "openai-compatible"), required=True)
    ap.add_argument("--command")
    ap.add_argument("--endpoint")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY")
    ap.add_argument("--glossary", type=Path)
    ap.add_argument("--style-guide", type=Path)
    ap.add_argument("--speaker-evidence", type=Path)
    ap.add_argument("--character-profiles", type=Path, help="reviewed character profile JSONL")
    ap.add_argument("--max-style-samples", type=int, default=2)
    ap.add_argument("--risk", type=Path, help="JSONL emitted by classify_translation_risk.py")
    ap.add_argument("--risk-level", action="append", choices=("low", "medium", "high", "critical"), help="only review selected risk level; repeatable")
    ap.add_argument("--reviewer-id", default="", help="stable identity for this independent review run")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-delay", type=float, default=2.0)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be > 0")
    if args.provider == "command" and not args.command:
        raise SystemExit("--provider command requires --command")
    if args.provider == "openai-compatible" and (not args.endpoint or not args.model):
        raise SystemExit("--provider openai-compatible requires --endpoint and --model")

    queue = index_unique(read_jsonl(args.queue), "queue")
    candidates = index_unique(read_jsonl(args.candidates), "candidates")
    missing_queue = sorted(set(candidates) - set(queue))
    if missing_queue:
        raise ValueError(
            "candidate rows are not source-bound to the current queue; "
            f"count={len(missing_queue)} first={missing_queue[0]}"
        )
    glossary = load_glossary(args.glossary)
    style = args.style_guide.read_text(encoding="utf-8") if args.style_guide else ""
    risk_index = load_risk_index(args.risk)
    selected_risk_levels = set(args.risk_level or [])
    if selected_risk_levels and not risk_index:
        raise SystemExit("--risk-level requires --risk")
    missing_risk = sorted(set(candidates) - set(risk_index)) if risk_index else []
    if missing_risk:
        raise ValueError(f"risk index missing {len(missing_risk)} candidate rows; first={missing_risk[0]}")
    speaker_evidence = load_character_evidence(args.speaker_evidence)
    character_profiles = load_character_profiles(args.character_profiles)
    if args.max_style_samples < 0:
        raise SystemExit("--max-style-samples must be >= 0")
    completed = load_completed(args.output)
    all_items = build_review_items(
        queue,
        candidates,
        glossary,
        speaker_evidence,
        args.max_style_samples,
        risk_index,
        character_profiles,
    )
    pending = [
        item for item in all_items
        if item["id"] not in completed
        and (
            not selected_risk_levels
            or str(item.get("risk", {}).get("risk_level", "")) in selected_risk_levels
        )
    ]
    if args.max_items:
        pending = pending[:args.max_items]
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    counts = {"candidates": len(candidates), "reviewable": len(all_items), "already_completed": len(completed), "selected_pending": len(pending), "reviewed": 0, "failed_batches": 0}
    errors: list[dict] = []

    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset:offset + args.batch_size]
        # Long SHA-256 IDs are frequently mistyped by review models. For the
        # OpenAI-compatible backend, bind short batch-local IDs in code and
        # keep the source SHA solely as the persisted review identity.
        if args.provider == "openai-compatible":
            request_batch = [{**item, "id": f"R{index}"} for index, item in enumerate(batch)]
        else:
            request_batch = batch
        response = None
        last_error: Exception | None = None
        expected = {item["id"] for item in request_batch}
        for attempt in range(max(1, args.retries)):
            try:
                response = command_review(args.command, request_batch, args.timeout, style) if args.provider == "command" else openai_review(args.endpoint, args.model, api_key, request_batch, args.timeout, style)
                if set(response) != expected:
                    raise ValueError(
                        "response id set mismatch: "
                        f"missing={sorted(expected-set(response))[:3]} "
                        f"unexpected={sorted(set(response)-expected)[:3]}"
                    )
                break
            except Exception as exc:
                response = None
                last_error = exc
                if attempt + 1 < max(1, args.retries):
                    time.sleep(args.retry_delay * (attempt + 1))
        if response is None:
            counts["failed_batches"] += 1
            errors.append({"offset": offset, "size": len(batch), "error": str(last_error)})
            break
        rows = []
        for item, request_item in zip(batch, request_batch, strict=True):
            sid = item["id"]
            candidate = candidates[sid]
            review = response[request_item["id"]]
            rows.append({
                "source_sha256": sid,
                "source": queue[sid]["source"],
                "translation": candidate.get("translation", ""),
                "verdict": review["verdict"],
                "scores": review["scores"],
                "blocking_errors": review["blocking_errors"],
                "notes": review["notes"],
                "reviewer_provenance": f"review:{args.provider}",
                "reviewer_model": args.model or args.command,
                "reviewer_id": args.reviewer_id or f"{args.provider}:{args.model or args.command}",
            })
        append_rows(args.output, rows)
        counts["reviewed"] += len(rows)

    result = {
        "schema_version": 1,
        "provider": args.provider,
        "model": args.model,
        "risk_rows_loaded": len(risk_index),
        "selected_risk_levels": sorted(selected_risk_levels),
        "reviewer_id": args.reviewer_id or f"{args.provider}:{args.model or args.command}",
        "speaker_evidence_rows_loaded": len(speaker_evidence),
        "approved_character_profiles_loaded": len(character_profiles),
        **counts,
        "total_output_rows": len(load_completed(args.output)),
        "complete_for_selected_range": counts["failed_batches"] == 0 and counts["reviewed"] == len(pending),
        "errors_first": errors[:20],
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
