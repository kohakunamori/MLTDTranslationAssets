#!/usr/bin/env python3
"""Recover stale MLTD translations using only evidence-backed deterministic edits.

This stage sits between revalidation and LLM repair.  It never invents a new
translation: it normalizes known historical bad renderings, then requires the
full production blocking QA and current authoritative-term contract to pass.
Stale evidence is append-preserved; recovered rows are re-added to active output
with explicit deterministic repair provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_translation_quality import (
    TRADITIONAL_CHAR_MAP,
    evaluate_row,
    introduced_traditional_chars,
    load_glossary,
)
from scripts.revalidate_mltd_api_output import authoritative_issues
from scripts.translate_gtx_queue import BASIC_BLOCKING_QA_CODES
from scripts.translate_mltd_api_pool import load_authoritative_terms
from scripts.repair_mltd_authoritative_stale import repair_anniversary_translation

# Only source-scoped variants with strong project evidence belong here.  The
# final QA/authoritative pass is mandatory, so a partial or unsafe replacement
# is never promoted merely because a string matched.
AUTHORITATIVE_VARIANTS: dict[str, tuple[str, ...]] = {
    "アナザー衣装": (
        "另一套服装",
        "Another服装",
        "另色服装",
        "另一款服装",
        "异色风格服装",
        "异色款服装",
        "异色版服装",
        "另类服装",
        "另类款服装",
        "另类颜色服装",
        "替换服装",
        "另一版服装",
        "另选服装",
        "另型服装",
        "另款服装",
        "另一个服装",
        "Anothers服装",
    ),
    "アナザー2衣装": (
        "另一套2服装",
        "Another2服装",
        "异色款2服装",
        "异色风格2服装",
        "另类2服装",
        "异色版2服装",
        "另一款2服装",
        "第2套异色服装",
        "另色2服装",
        "另版2服装",
        "另款2服装",
        "另一个2服装",
        "其他2服装",
        "第2套替换服装",
        "替换款2服装",
        "替换版2服装",
        "另类款2服装",
        "异色款式2服装",
        "另一套2号服装",
        "异色服装2",
        "另一套服装2",
    ),
    "茜ちゃん": ("茜酱", "茜茜"),
    "キミさけ": ("君叫", "你与酒", "KimiSake"),
}


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            rows.append(row)
    return rows


def current_stale_reasons(
    row: dict[str, Any],
    glossary: dict[str, Any],
    terms: dict[str, str],
) -> list[dict[str, Any]]:
    """Recompute stale reasons under the current policy instead of trusting history."""
    qa = evaluate_row(row, row, glossary)
    blocking = [
        issue
        for issue in qa.get("issues", [])
        if isinstance(issue, dict) and issue.get("code") in BASIC_BLOCKING_QA_CODES
    ]
    return [
        *blocking,
        *authoritative_issues(
            str(row.get("source", "")),
            str(row.get("translation", "")),
            terms,
        ),
    ]


def authoritative_normalize(
    source: str,
    translation: str,
    reason: dict[str, Any],
    terms: dict[str, str],
) -> tuple[str, str] | None:
    source_term = str(reason.get("source_term", ""))
    target = str(reason.get("target", ""))
    if not source_term or not target or terms.get(source_term) != target:
        return None

    anniversary = repair_anniversary_translation(source, translation, source_term, target)
    if anniversary is not None:
        return anniversary, f"authoritative:{source_term}"

    value = translation
    changed = False
    for variant in AUTHORITATIVE_VARIANTS.get(source_term, ()):
        if variant in value:
            value = value.replace(variant, target)
            changed = True
    if changed:
        return value, f"authoritative:{source_term}"
    return None


def deterministic_normalize(
    row: dict[str, Any],
    terms: dict[str, str],
) -> tuple[str, list[str]]:
    source = str(row.get("source", ""))
    value = str(row.get("translation", ""))
    reasons = row.get("stale_reasons", [])
    if not isinstance(reasons, list):
        reasons = []
    repairs: list[str] = []

    # Apply authoritative replacements first because e.g. 茜酱 should become
    # source-backed 小茜 rather than merely stripping the suffix to 茜.
    for reason in reasons:
        if not isinstance(reason, dict) or reason.get("code") != "authoritative_term_missing":
            continue
        proposal = authoritative_normalize(source, value, reason, terms)
        if proposal is not None:
            value, label = proposal
            repairs.append(label)

    codes = {
        str(reason.get("code", ""))
        for reason in reasons
        if isinstance(reason, dict)
    }
    if "honorific_chan_jiang_calque" in codes and "酱" in value:
        value = value.replace("酱", "")
        repairs.append("style:remove_chan_jiang_calque")
    if "forbidden_term_present" in codes and "ウフフ" in source and "呜呼呼" in value:
        value = value.replace("呜呼呼", "呵呵")
        repairs.append("style:ufufu_to_heheda")
    if "introduced_traditional_chinese" in codes:
        introduced = introduced_traditional_chars(source, value)
        changed_chars: list[str] = []
        for char in introduced:
            target = TRADITIONAL_CHAR_MAP.get(char)
            if target and target != char and char in value:
                value = value.replace(char, target)
                changed_chars.append(f"{char}->{target}")
        if changed_chars:
            repairs.append("script:t2s_introduced:" + ",".join(changed_chars))

    return value, repairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--active",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.jsonl"),
    )
    ap.add_argument(
        "--stale",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.stale.jsonl"),
    )
    ap.add_argument(
        "--evidence-output",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.deterministic-repairs.jsonl"),
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
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    glossary = load_glossary(args.glossary)
    terms = load_authoritative_terms(args.authoritative_terms)
    term_sha = hashlib.sha256(args.authoritative_terms.read_bytes()).hexdigest()
    active_rows = read_rows(args.active)
    active_ids = {str(row.get("source_sha256", "")) for row in active_rows}

    latest_stale: dict[str, dict[str, Any]] = {}
    for row in read_rows(args.stale):
        sid = str(row.get("source_sha256", ""))
        if sid:
            latest_stale[sid] = row

    recovered: list[tuple[dict[str, Any], str, list[str]]] = []
    rejected_after_edit = Counter()
    for sid, row in latest_stale.items():
        if sid in active_ids:
            continue
        current_reasons = current_stale_reasons(row, glossary, terms)
        if not current_reasons:
            continue
        repair_input = dict(row)
        repair_input["stale_reasons"] = current_reasons
        before = str(row.get("translation", ""))
        after, repairs = deterministic_normalize(repair_input, terms)
        if not repairs or after == before:
            continue
        candidate = dict(row)
        candidate["translation"] = after
        qa = evaluate_row(row, candidate, glossary)
        blocking = [
            issue
            for issue in qa.get("issues", [])
            if isinstance(issue, dict) and issue.get("code") in BASIC_BLOCKING_QA_CODES
        ]
        term_issues = authoritative_issues(str(row.get("source", "")), after, terms)
        remaining = [*blocking, *term_issues]
        if remaining:
            for issue in remaining:
                rejected_after_edit[str(issue.get("code", "unknown"))] += 1
            continue
        recovered.append((row, after, repairs))

    summary = {
        "active_rows": len(active_rows),
        "unresolved_stale_ids": sum(sid not in active_ids for sid in latest_stale),
        "eligible_repairs": len(recovered),
        "rejected_after_edit": dict(rejected_after_edit),
        "apply": args.apply,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row, after, repairs in recovered[: max(args.show, 0)]:
        print(
            json.dumps(
                {
                    "source_sha256": row.get("source_sha256"),
                    "repairs": repairs,
                    "before": row.get("translation"),
                    "after": after,
                },
                ensure_ascii=False,
            )
        )

    if not args.apply or not recovered:
        return 0

    args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
    with args.evidence_output.open("a", encoding="utf-8", newline="\n") as evidence:
        for row, after, repairs in recovered:
            evidence.write(
                json.dumps(
                    {
                        "source_sha256": row.get("source_sha256"),
                        "source": row.get("source"),
                        "before_translation": row.get("translation"),
                        "after_translation": after,
                        "repair": "deterministic:stale_normalization",
                        "repair_steps": repairs,
                        "original_provenance": row.get("provenance"),
                        "authoritative_terms_sha256": term_sha,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        evidence.flush()
        os.fsync(evidence.fileno())

    repaired_rows: list[dict[str, Any]] = []
    for row, after, repairs in recovered:
        value = dict(row)
        value["translation"] = after
        value.pop("stale_reasons", None)
        value.pop("qa", None)
        value.pop("qa_nonblocking", None)
        prior = list(value.get("deterministic_repairs", []))
        prior.append({"type": "stale_normalization", "steps": repairs})
        value["deterministic_repairs"] = prior
        value["repair_authoritative_terms_sha256"] = term_sha
        repaired_rows.append(value)

    tmp = args.active.with_suffix(args.active.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in [*active_rows, *repaired_rows]:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, args.active)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
