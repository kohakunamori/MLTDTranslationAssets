#!/usr/bin/env python3
"""Deterministically normalize MLTD ranking labels/titles in accepted translations.

This repair is intentionally narrow.  It only touches source-scoped ranking
labels/rank titles with evidence-backed Chinese renderings.  Song/title text,
numeric literals, and all unrelated wording are left untouched.  Every proposed
repair must pass current deterministic QA and authoritative-term validation
before it is eligible for write-back.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_translation_quality import evaluate_row, load_glossary
from scripts.revalidate_mltd_api_output import authoritative_issues
from scripts.translate_gtx_queue import BASIC_BLOCKING_QA_CODES
from scripts.translate_mltd_api_pool import load_authoritative_terms


RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "ハイスコア ランキング",
        "最高分排行榜",
        re.compile(
            r"(?:(?:荣登|获得)\s*)?(?:"
            r"(?:高分|最高得分|最佳得分|最佳成绩|最好成绩)\s*(?:获得\s*)?"
            r"(?:排行榜|排名|排行|榜单|榜)|高分排位(?:获得)?)"
            r"(?:\s*排名)?"
        ),
    ),
    (
        "イベント ランキング",
        "活动排行榜",
        re.compile(
            r"活动\s*(?:(?:排行榜|排名|排行)(?:荣获)?|获得\s*(?:排行榜|排名)?)"
        ),
    ),
    (
        "ラウンジ ランキング",
        "社交厅排行榜",
        re.compile(
            r"(?:(?:休息室|休息厅|大厅|公会|社团|社交厅|Lounge)\s*)?"
            r"(?:获得\s*)?(?:排行榜|排名|排行)"
        ),
    ),
    (
        "ゴールドランカー",
        "黄金排名",
        re.compile(r"黄金排名者"),
    ),
)


def repair_translation(source: str, translation: str) -> tuple[str, str] | None:
    """Return (repaired, source_label) or None when no safe label variant matches.

    When SOURCE ends in the standard ``<label><start>位～<end>位入賞`` template,
    normalize the entire translated ranking suffix.  This removes noisy variants
    such as ``获奖/入赏`` while keeping the already-translated title prefix intact.
    Numeric bounds always come from current SOURCE, never historical thresholds.
    """
    for source_label, target, pattern in RULES:
        if source_label not in source:
            continue
        source_match = re.search(
            re.escape(source_label)
            + r"\s*(?P<start>\d+)位～(?P<end>\d+)位入賞(?:</[^>]+>)?\s*$",
            source,
        )
        label_match = pattern.search(translation)
        if label_match is None:
            # Already-canonical labels need no deterministic repair.
            if target in translation:
                continue
            return None
        if source_match is not None:
            trailing = re.search(r"(?P<tags>(?:</[^>]+>\s*)+)$", translation)
            tags = trailing.group("tags") if trailing else ""
            prefix = translation[: label_match.start()].rstrip()
            spacer = " " if prefix else ""
            repaired = (
                f"{prefix}{spacer}{target}第{source_match.group('start')}～"
                f"{source_match.group('end')}名{tags}"
            )
        else:
            repaired = translation[: label_match.start()] + target + translation[label_match.end() :]
        if repaired != translation:
            return repaired, source_label
    return None


def repair_candidate_rule(source: str, translation: str) -> tuple[str, str] | None:
    """Return the source/target rule only when the current text needs repair."""
    for source_label, target, pattern in RULES:
        if source_label not in source:
            continue
        if target not in translation:
            return source_label, target
        # The literal-agent mistranslation contains the preferred target as a
        # substring (黄金排名者 contains 黄金排名), so substring presence alone is
        # insufficient to prove canonical rendering.
        if source_label == "ゴールドランカー" and pattern.search(translation):
            return source_label, target
        # Standard rank-placement templates should end at the numeric rank.  A
        # trailing 获奖/奖项/入赏 is redundant machine-translation noise even when
        # the label itself is already canonical.
        if "ランキング" in source_label and re.search(
            re.escape(source_label)
            + r"\s*\d+位～\d+位入賞(?:</[^>]+>)?\s*$",
            source,
        ) and re.search(r"(?:获奖|奖项|入赏)", translation):
            return source_label, target
    return None


def blocking_issues(row: dict, translation: str, glossary: dict, terms: dict[str, str]) -> list[dict]:
    candidate = dict(row)
    candidate["translation"] = translation
    qa = evaluate_row(row, candidate, glossary)
    issues = [
        issue
        for issue in qa.get("issues", [])
        if isinstance(issue, dict) and issue.get("code") in BASIC_BLOCKING_QA_CODES
    ]
    issues.extend(authoritative_issues(str(row.get("source", "")), translation, terms))
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.jsonl"),
    )
    ap.add_argument(
        "--evidence-output",
        type=Path,
        default=Path(
            "build/localization-90200/machine-translations-api.deterministic-repairs.jsonl"
        ),
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
    ap.add_argument("--show", type=int, default=12)
    args = ap.parse_args()

    glossary = load_glossary(args.glossary)
    terms = load_authoritative_terms(args.authoritative_terms)
    rows: list[dict] = []
    with args.input.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{args.input}:{line_no}: expected object")
            rows.append(row)

    changed: list[tuple[int, dict, str, str, str]] = []
    unmatched = Counter()
    rejected = Counter()
    eligible = Counter()
    for index, row in enumerate(rows):
        source = str(row.get("source", ""))
        translation = str(row.get("translation", ""))
        applicable = repair_candidate_rule(source, translation)
        if applicable is None:
            continue
        proposal = repair_translation(source, translation)
        if proposal is None:
            unmatched[applicable[0]] += 1
            continue
        repaired, source_label = proposal
        issues = blocking_issues(row, repaired, glossary, terms)
        if issues:
            rejected[source_label] += 1
            continue
        eligible[source_label] += 1
        changed.append((index, row, translation, repaired, source_label))

    summary = {
        "input_rows": len(rows),
        "eligible_repairs": len(changed),
        "eligible_by_label": dict(eligible),
        "unmatched_by_label": dict(unmatched),
        "rejected_by_label": dict(rejected),
        "apply": args.apply,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for _, row, before, after, source_label in changed[: max(args.show, 0)]:
        print(
            json.dumps(
                {
                    "source_sha256": row.get("source_sha256"),
                    "source_label": source_label,
                    "source": row.get("source"),
                    "before": before,
                    "after": after,
                },
                ensure_ascii=False,
            )
        )

    if not args.apply or not changed:
        return 0

    # Preserve the exact pre-repair evidence before atomically replacing active output.
    args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
    with args.evidence_output.open("a", encoding="utf-8", newline="\n") as evidence:
        for _, row, before, after, source_label in changed:
            record = {
                "source_sha256": row.get("source_sha256"),
                "source": row.get("source"),
                "before_translation": before,
                "after_translation": after,
                "repair": "deterministic:ranking_label_normalization",
                "source_label": source_label,
                "original_provenance": row.get("provenance"),
            }
            evidence.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        evidence.flush()
        os.fsync(evidence.fileno())

    by_index = {index: (after, source_label) for index, _, _, after, source_label in changed}
    tmp = args.input.with_suffix(args.input.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for index, row in enumerate(rows):
            update = by_index.get(index)
            if update is not None:
                after, source_label = update
                row = dict(row)
                row["translation"] = after
                repairs = list(row.get("deterministic_repairs", []))
                repairs.append(
                    {
                        "type": "ranking_label_normalization",
                        "source_label": source_label,
                    }
                )
                row["deterministic_repairs"] = repairs
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, args.input)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
