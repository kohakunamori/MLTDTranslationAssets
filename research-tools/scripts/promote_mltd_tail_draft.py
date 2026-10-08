#!/usr/bin/env python3
"""Explicit, source-bound promotion of a tiny verified MLTD localization tail.

Draft rows are agent-authored proposals, never represented as API-provider or
human-reviewed translations. Production outputs are strictly append-only.
No external translation requests or concurrent writers are launched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import translation_status_is_accepted
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.revalidate_mltd_api_output import authoritative_issues
from scripts.translate_gtx_queue import BASIC_BLOCKING_QA_CODES
from scripts.translate_mltd_api_pool import load_authoritative_terms


DEFAULT_WORKSPACE = Path("build/localization-90200")
DRAFT_NAME = "audits/final-nine-source-bound-draft.jsonl"
AUDIT_NAME = "audits/final-nine-source-bound-validation.json"
PROPOSALS_NAME = "audits/final-nine-source-bound-approved.jsonl"


def read_jsonl(path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            yield row


def write_atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(staged, path)


def write_atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    with staged.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staged, path)


def translator_active() -> bool:
    """Fail closed when process enumeration is unavailable or ambiguous."""
    try:
        import psutil
    except ImportError:
        return True
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = process.info.get("cmdline") or []
            if any(
                str(part).replace("\\", "/").endswith("translate_mltd_api_pool.py")
                for part in cmd
            ):
                return True
        except (psutil.Error, OSError, TypeError):
            return True
    return False


def load_failure_sources(workspace: Path) -> dict[str, tuple[str, str]]:
    result = {}
    for stage, name in (
        ("gtx", "machine-translations-api.failed.jsonl"),
        ("companion", "machine-translations-nongtx-api.failed.jsonl"),
    ):
        for row in read_jsonl(workspace / name):
            sid = str(row.get("source_sha256", ""))
            source = str(row.get("source", ""))
            if not source or sid != source_id(source):
                raise ValueError(f"{name}: invalid failed-row source SHA {sid}")
            if sid in result and result[sid][0] != source:
                raise ValueError(f"{name}: source SHA conflict {sid}")
            result[sid] = (source, stage)
    return result


def read_completed(path: Path) -> set[str]:
    completed = set()
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        if (
            sid
            and str(row.get("translation", "")).strip()
            and translation_status_is_accepted(str(row.get("status", "")))
        ):
            completed.add(sid)
    return completed


def plan(
    workspace: Path,
    draft: Path,
    outputs: dict[str, Path],
    glossary_path: Path,
    terms_path: Path,
) -> tuple[list[dict], dict]:
    glossary = load_glossary(glossary_path)
    terms = load_authoritative_terms(terms_path)
    sources = load_failure_sources(workspace)
    finished = {stage: read_completed(path) for stage, path in outputs.items()}
    selected = set()
    approved = []
    reviews = []
    counts = Counter()
    for row in read_jsonl(draft):
        sid = str(row.get("source_sha256", ""))
        text = str(row.get("translation", ""))
        if not sid or sid in selected or sid not in sources:
            raise ValueError(f"unexpected/duplicate source SHA in draft: {sid}")
        selected.add(sid)
        original, stage = sources[sid]
        if source_id(original) != sid:
            raise ValueError(f"{sid}: failed source no longer source-bound")
        if sid in finished[stage]:
            raise ValueError(f"{sid}: already accepted; refuse duplicate promotion")
        if not text.strip() or text.strip() == original:
            raise ValueError(f"{sid}: empty/unchanged draft")
        candidate = {
            "source_sha256": sid,
            "source": original,
            "translation": text,
            "status": "agent_translated",
            "provenance": {
                "provider": "local:assistant_source_bound_tail",
                "model_id": None,
                "model": "GPT-5.6 Sol",
                "draft": str(draft),
                "approval_scope": "source_integrity_and_deterministic_qa_only",
                "not_human_verified": True,
            },
            "prompt_version": "agent-tail-repair-20260921",
            "prompt_sha256": None,
            "authoritative_terms_sha256": None,
        }
        qa = evaluate_row(
            {"source_sha256": sid, "source": original}, candidate, glossary
        )
        blocks = [
            issue for issue in qa["issues"]
            if issue.get("code") in BASIC_BLOCKING_QA_CODES
        ]
        term_issues = authoritative_issues(original, text, terms)
        if blocks or term_issues:
            raise ValueError(
                f"{sid}: blocking QA: "
                + json.dumps([*blocks, *term_issues], ensure_ascii=True)
            )
        if qa["issues"]:
            candidate["qa"] = qa
            candidate["qa_nonblocking"] = True
        candidate["_output_stage"] = stage
        approved.append(candidate)
        counts[stage] += 1
        counts[qa["qa_verdict"]] += 1
        reviews.append(
            {
                "source_sha256": sid,
                "stage": stage,
                "qa_verdict": qa["qa_verdict"],
                "issues": qa["issues"],
                "authoritative_issues": term_issues,
            }
        )
    summary = {
        "schema_version": 1,
        "kind": "source-bound-agent-tail-draft-validation",
        "source_count": len(approved),
        "by_stage_and_qa": dict(sorted(counts.items())),
        "review": reviews,
        "draft": str(draft),
        "outputs": {stage: str(path) for stage, path in outputs.items()},
        "human_reviewed": False,
        "all_blocking_qa_passed": True,
    }
    return approved, summary


def promote(
    workspace: Path,
    draft: Path,
    outputs: dict[str, Path],
    glossary_path: Path,
    terms_path: Path,
    *,
    apply: bool = False,
) -> dict:
    if apply and translator_active():
        raise RuntimeError("translator is running or cannot be checked; no output modified")
    snapshots = {stage: (path.stat().st_size, path.stat().st_mtime_ns)
                 for stage, path in outputs.items()}
    rows, report = plan(workspace, draft, outputs, glossary_path, terms_path)
    audit_path = workspace / AUDIT_NAME
    approved_path = workspace / PROPOSALS_NAME
    write_atomic_jsonl(
        approved_path,
        [{k: v for k, v in row.items() if k != "_output_stage"} for row in rows],
    )
    report["applied"] = False
    report["approved_artifact"] = str(approved_path)
    if apply:
        if translator_active():
            raise RuntimeError("translator started while validating; no output modified")
        for stage, path in outputs.items():
            if snapshots[stage] != (path.stat().st_size, path.stat().st_mtime_ns):
                raise RuntimeError(f"{stage} output changed during validation; no output modified")
        # All sources and both output snapshots checked BEFORE either append.
        # Files are opened only after every row passes integrity and production QA.
        for stage, path in outputs.items():
            stage_rows = [row for row in rows if row["_output_stage"] == stage]
            payload = "".join(
                json.dumps(
                    {k: v for k, v in row.items() if k != "_output_stage"},
                    ensure_ascii=False, separators=(",", ":"),
                ) + "\n"
                for row in stage_rows
            ).encode("utf-8")
            if not payload:
                continue
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                with os.fdopen(descriptor, "ab", closefd=False) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
        report["applied"] = True
    write_atomic_json(audit_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--glossary", type=Path, default=Path("localization/quality/glossary.json"))
    parser.add_argument("--terms", type=Path, default=Path("localization/quality/authoritative-terms.json"))
    parser.add_argument("--apply", action="store_true", help="append QA-validated agent drafts to production outputs")
    args = parser.parse_args()
    draft = args.draft or args.workspace / DRAFT_NAME
    outputs = {
        "gtx": args.workspace / "machine-translations-api.jsonl",
        "companion": args.workspace / "machine-translations-nongtx-api.jsonl",
    }
    report = promote(
        args.workspace, draft, outputs, args.glossary, args.terms,
        apply=args.apply,
    )
    print(json.dumps(
        {key: value for key, value in report.items() if key != "review"},
        ensure_ascii=True, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
