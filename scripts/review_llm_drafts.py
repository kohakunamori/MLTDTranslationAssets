#!/usr/bin/env python3
"""Gated independent semantic/style review and bounded repair for high-risk LLM drafts."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipelines.core.jsonl import read_jsonl, write_jsonl  # noqa: E402
from pipelines.text.build_character_voice_evidence import load_character_evidence  # noqa: E402
from pipelines.text.mltd_character_profiles import load_character_profiles  # noqa: E402
from pipelines.text.mltd_translation_quality import index_unique, load_glossary  # noqa: E402
from pipelines.text.review_gtx_translations import (  # noqa: E402
    compute_missing_risk,
    load_completed,
    review_rows,
)
from pipelines.text.translate_mltd_api_pool import (  # noqa: E402
    load_model_config,
    normalize_endpoint,
    select_models,
)

DEFAULT_RISK_LEVELS = ("high", "critical")


def _chat_endpoint_for_model(cfg) -> str:
    endpoint = str(getattr(cfg, "endpoint", "") or "").strip()
    if endpoint.endswith("/chat/completions"):
        return endpoint
    if endpoint.endswith("/responses"):
        return normalize_endpoint({"base_url": endpoint[: -len("/responses")]}, "chat_completions")
    if endpoint:
        return normalize_endpoint({"base_url": endpoint}, "chat_completions")
    return ""


def resolve_review_backend(
    *,
    config_path: Path | None = None,
    provider: str | None = None,
    command: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    api_key_env: str = "OPENAI_API_KEY",
) -> dict[str, str] | None:
    """Resolve reviewer backend settings without failing on unconfigured offline runs."""
    if provider == "command":
        if not command:
            raise ValueError("--provider command requires --command")
        return {
            "provider": "command",
            "command": command,
            "endpoint": "",
            "model": model or "",
            "api_key": "",
        }
    if provider == "openai-compatible" or (endpoint and model):
        resolved_endpoint = str(endpoint or "").strip()
        resolved_model = str(model or "").strip()
        if not resolved_endpoint or not resolved_model:
            raise ValueError("openai-compatible review requires endpoint and model")
        return {
            "provider": "openai-compatible",
            "command": "",
            "endpoint": resolved_endpoint,
            "model": resolved_model,
            "api_key": os.environ.get(api_key_env, "") if api_key_env else "",
        }
    if config_path is not None and Path(config_path).is_file():
        models = select_models(load_model_config(Path(config_path)), None)
        ordered = sorted(models, key=lambda m: 0 if m.api_protocol == "chat_completions" else 1)
        for cfg in ordered:
            api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
            chat_endpoint = _chat_endpoint_for_model(cfg)
            if api_key and chat_endpoint:
                return {
                    "provider": "openai-compatible",
                    "command": "",
                    "endpoint": chat_endpoint,
                    "model": cfg.model,
                    "api_key": api_key,
                }
    return None


def _build_repair_stale_reasons(review_row: dict[str, Any]) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    for err in review_row.get("blocking_errors", []) or []:
        text = str(err).strip()
        if text:
            reasons.append({"code": "independent_review_reject", "detail": text})
    notes = str(review_row.get("notes", "") or "").strip()
    if notes:
        reasons.append({"code": "independent_review_notes", "detail": notes})
    if not reasons:
        reasons.append({"code": "independent_review_reject", "detail": "rejected by independent QA reviewer"})
    return reasons


def _default_pool_repair(
    repair_queue: Path,
    repair_draft: Path,
    *,
    config_path: Path,
    glossary: Path | None,
    character_evidence: Path | None,
    character_profiles: Path | None,
) -> int:
    cmd = [
        sys.executable,
        str(REPO / "pipelines" / "text" / "translate_mltd_api_pool.py"),
        "--config",
        str(config_path),
        "--input",
        str(repair_queue),
        "--output",
        str(repair_draft),
        "--no-default-resume",
        "--batch-mode",
        "single",
        "--prompt-source",
        "compiled",
        "--system-prompt",
        str(repair_draft.with_name(repair_draft.stem + ".system-prompt.md")),
    ]
    prompt_manifest = REPO / "pipelines" / "text" / "configs" / "mltd-zhcn-system.manifest.json"
    if prompt_manifest.is_file():
        cmd.extend(["--prompt-manifest", str(prompt_manifest)])
    if glossary and glossary.is_file():
        cmd.extend(["--glossary", str(glossary), "--authoritative-terms", str(glossary)])
    if character_evidence and character_evidence.is_file():
        cmd.extend(["--character-evidence", str(character_evidence)])
    if character_profiles and character_profiles.is_file():
        cmd.extend(["--character-profiles", str(character_profiles)])
    proc = subprocess.run(cmd, cwd=str(REPO), check=False)
    return proc.returncode


def run_gated_review(
    *,
    queue_path: Path,
    draft_path: Path,
    review_output: Path,
    rejected_output: Path,
    summary_path: Path | None = None,
    repair_queue_path: Path | None = None,
    repair_draft_path: Path | None = None,
    config_path: Path | None = None,
    provider: str | None = None,
    command: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    api_key_env: str = "OPENAI_API_KEY",
    glossary_path: Path | None = Path("glossary/authoritative-terms.json"),
    speaker_evidence_path: Path | None = None,
    character_profiles_path: Path | None = None,
    style_guide_path: Path | None = None,
    risk_levels: set[str] | None = None,
    batch_size: int = 16,
    max_items: int = 0,
    timeout: float = 120.0,
    retries: int = 3,
    retry_delay: float = 2.0,
    quarantine_rejected: bool = True,
    repair_enabled: bool = True,
    repair_runner: Callable[[Path, Path], int] | None = None,
    require_provider: bool = False,
) -> dict[str, Any]:
    """Review high/critical drafts, optionally repair REJECT rows once, and quarantine persistent failures."""
    selected_levels = set(risk_levels or DEFAULT_RISK_LEVELS)

    def _finish(payload: dict[str, Any]) -> dict[str, Any]:
        if summary_path is not None:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload

    if not draft_path.is_file() or not queue_path.is_file():
        return _finish({
            "schema_version": 1,
            "status": "skipped",
            "reason": "missing_queue_or_draft",
            "selected_risk_levels": sorted(selected_levels),
            "high_risk_candidates": 0,
        })

    draft_rows = read_jsonl(draft_path)
    queue_rows = read_jsonl(queue_path)
    if not draft_rows:
        return _finish({
            "schema_version": 1,
            "status": "skipped",
            "reason": "empty_draft",
            "selected_risk_levels": sorted(selected_levels),
            "high_risk_candidates": 0,
        })

    queue = index_unique(queue_rows, "queue")
    candidates = index_unique(draft_rows, "candidates")
    missing_queue = sorted(set(candidates) - set(queue))
    if missing_queue:
        raise ValueError(
            "candidate rows are not source-bound to the current queue; "
            f"count={len(missing_queue)} first={missing_queue[0]}"
        )
    risk_index = compute_missing_risk(queue, candidates)
    high_risk_sids = [
        sid for sid in candidates
        if str(risk_index.get(sid, {}).get("risk_level", "")) in selected_levels
    ]
    if not high_risk_sids:
        return _finish({
            "schema_version": 1,
            "status": "skipped",
            "reason": "no_high_risk_candidates",
            "selected_risk_levels": sorted(selected_levels),
            "total_drafts": len(candidates),
            "high_risk_candidates": 0,
        })

    backend = resolve_review_backend(
        config_path=config_path,
        provider=provider,
        command=command,
        endpoint=endpoint,
        model=model,
        api_key_env=api_key_env,
    )
    if backend is None:
        if require_provider:
            raise RuntimeError("no independent review provider configured")
        return _finish({
            "schema_version": 1,
            "status": "skipped",
            "reason": "no_review_provider",
            "selected_risk_levels": sorted(selected_levels),
            "total_drafts": len(candidates),
            "high_risk_candidates": len(high_risk_sids),
        })

    glossary = load_glossary(glossary_path) if glossary_path and glossary_path.is_file() else load_glossary(None)
    style_guide = style_guide_path.read_text(encoding="utf-8") if style_guide_path and style_guide_path.is_file() else ""
    speaker_evidence = (
        load_character_evidence(speaker_evidence_path)
        if speaker_evidence_path and speaker_evidence_path.is_file()
        else {}
    )
    character_profiles = (
        load_character_profiles(character_profiles_path)
        if character_profiles_path and character_profiles_path.is_file()
        else {}
    )

    initial_summary, initial_errors = review_rows(
        queue,
        candidates,
        glossary=glossary,
        style_guide=style_guide,
        speaker_evidence=speaker_evidence,
        character_profiles=character_profiles,
        risk_index=risk_index,
        selected_risk_levels=selected_levels,
        provider=backend["provider"],
        command=backend["command"] or None,
        endpoint=backend["endpoint"] or None,
        model=backend["model"],
        api_key=backend["api_key"],
        output=review_output,
        batch_size=batch_size,
        max_items=max_items,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
    )

    reviews = load_completed(review_output)
    rejected_reviews = {
        sid: rev for sid, rev in reviews.items()
        if sid in candidates and rev.get("verdict") == "REJECT"
    }

    repaired_sids: list[str] = []
    still_rejected_sids: set[str] = set(rejected_reviews)
    quarantined_candidates: dict[str, dict[str, Any]] = {}
    repair_attempted = False

    if rejected_reviews and repair_enabled and (
        repair_runner is not None or (config_path is not None and config_path.is_file())
    ):
        repair_attempted = True
        r_queue = repair_queue_path or draft_path.with_name(draft_path.stem + ".repair-queue.jsonl")
        r_draft = repair_draft_path or draft_path.with_name(draft_path.stem + ".repair-draft.jsonl")
        if r_draft.exists():
            r_draft.unlink()
        repair_items = [
            {
                **queue[sid],
                "previous_translation": str(candidates[sid].get("translation", "")),
                "stale_reasons": _build_repair_stale_reasons(rev),
            }
            for sid, rev in rejected_reviews.items()
        ]
        write_jsonl(r_queue, repair_items)
        if repair_runner is not None:
            repair_runner(r_queue, r_draft)
        else:
            _default_pool_repair(
                r_queue,
                r_draft,
                config_path=config_path,  # type: ignore[arg-type]
                glossary=glossary_path,
                character_evidence=speaker_evidence_path,
                character_profiles=character_profiles_path,
            )
        repaired_candidates = index_unique(read_jsonl(r_draft), "repair_candidates") if r_draft.is_file() else {}
        if repaired_candidates:
            r_review_out = r_draft.with_name(draft_path.stem + ".repair-review.jsonl")
            if r_review_out.exists():
                r_review_out.unlink()
            review_rows(
                queue,
                repaired_candidates,
                glossary=glossary,
                style_guide=style_guide,
                speaker_evidence=speaker_evidence,
                character_profiles=character_profiles,
                risk_index=risk_index,
                selected_risk_levels=selected_levels,
                provider=backend["provider"],
                command=backend["command"] or None,
                endpoint=backend["endpoint"] or None,
                model=backend["model"],
                api_key=backend["api_key"],
                reviewer_id=f"{backend['provider']}:{backend['model'] or backend['command']}:repair-verify",
                output=r_review_out,
                batch_size=batch_size,
                max_items=0,
                timeout=timeout,
                retries=retries,
                retry_delay=retry_delay,
            )
            re_reviews = load_completed(r_review_out)
            for sid, rep in repaired_candidates.items():
                verdict = re_reviews.get(sid, {}).get("verdict")
                if verdict in {"PASS", "REVIEW"}:
                    updated_row = dict(rep)
                    prov = dict(updated_row.get("provenance") or {})
                    prov["review_repaired"] = True
                    prov["repair_review_verdict"] = verdict
                    updated_row["provenance"] = prov
                    candidates[sid] = updated_row
                    repaired_sids.append(sid)
                    still_rejected_sids.discard(sid)
                elif verdict == "REJECT":
                    initial_rev = rejected_reviews.get(sid, {})
                    quarantined_candidates[sid] = {
                        **rep,
                        "initial_translation": str(candidates[sid].get("translation", "")),
                        "initial_review_blocking_errors": list(initial_rev.get("blocking_errors", []) or []),
                    }
                    rejected_reviews[sid] = re_reviews[sid]

    quarantined_rows: list[dict[str, Any]] = []
    kept_rows: list[dict[str, Any]] = []
    seen_draft: set[str] = set()
    for row in draft_rows:
        sid = str(row.get("source_sha256", ""))
        if sid in seen_draft:
            continue
        seen_draft.add(sid)
        if quarantine_rejected and sid in still_rejected_sids:
            rev = rejected_reviews.get(sid, {})
            quarantined_cand = quarantined_candidates.get(sid, candidates[sid])
            quarantined_rows.append({
                **quarantined_cand,
                "review_verdict": "REJECT",
                "review_blocking_errors": rev.get("blocking_errors", []),
                "review_notes": rev.get("notes", ""),
            })
        else:
            kept_rows.append(candidates[sid])

    if quarantined_rows or repaired_sids:
        write_jsonl(draft_path, kept_rows)
    if quarantined_rows:
        write_jsonl(rejected_output, quarantined_rows)

    verdict_counts: dict[str, int] = {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    for sid in high_risk_sids:
        verdict = reviews.get(sid, {}).get("verdict")
        if verdict in verdict_counts:
            verdict_counts[verdict] += 1

    return _finish({
        "schema_version": 1,
        "status": "completed" if not initial_errors else "partial_error",
        "selected_risk_levels": sorted(selected_levels),
        "total_drafts": len(draft_rows),
        "high_risk_candidates": len(high_risk_sids),
        "initial_review": initial_summary,
        "initial_verdict_counts": verdict_counts,
        "repair_attempted": repair_attempted,
        "repaired": len(repaired_sids),
        "quarantined": len(quarantined_rows),
        "kept_drafts": len(kept_rows),
        "errors": initial_errors[:20],
    })


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", type=Path, default=Path(".llm-queue.jsonl"))
    ap.add_argument("--draft", type=Path, default=Path(".llm-draft.jsonl"))
    ap.add_argument("--review-output", type=Path, default=Path(".llm-review.jsonl"))
    ap.add_argument("--rejected-output", type=Path, default=Path(".llm-review-rejected.jsonl"))
    ap.add_argument("--summary", type=Path, default=Path(".llm-review-summary.json"))
    ap.add_argument("--repair-queue", type=Path)
    ap.add_argument("--repair-draft", type=Path)
    ap.add_argument("--config", type=Path, help="optional api-models config JSON used for chat-completions review and repair")
    ap.add_argument("--provider", choices=("command", "openai-compatible"))
    ap.add_argument("--command")
    ap.add_argument("--endpoint")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY")
    ap.add_argument("--glossary", type=Path, default=Path("glossary/authoritative-terms.json"))
    ap.add_argument("--speaker-evidence", type=Path)
    ap.add_argument("--character-profiles", type=Path)
    ap.add_argument("--style-guide", type=Path)
    ap.add_argument(
        "--risk-level",
        action="append",
        choices=("low", "medium", "high", "critical"),
        help="risk tiers to review (defaults to high + critical)",
    )
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-delay", type=float, default=2.0)
    ap.add_argument("--no-quarantine", action="store_true")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--require-provider", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    summary = run_gated_review(
        queue_path=args.queue,
        draft_path=args.draft,
        review_output=args.review_output,
        rejected_output=args.rejected_output,
        summary_path=args.summary,
        repair_queue_path=args.repair_queue,
        repair_draft_path=args.repair_draft,
        config_path=args.config,
        provider=args.provider,
        command=args.command,
        endpoint=args.endpoint,
        model=args.model,
        api_key_env=args.api_key_env,
        glossary_path=args.glossary,
        speaker_evidence_path=args.speaker_evidence,
        character_profiles_path=args.character_profiles,
        style_guide_path=args.style_guide,
        risk_levels=set(args.risk_level or DEFAULT_RISK_LEVELS),
        batch_size=args.batch_size,
        max_items=args.max_items,
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
        quarantine_rejected=not args.no_quarantine,
        repair_enabled=not args.no_repair,
        require_provider=args.require_provider,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
