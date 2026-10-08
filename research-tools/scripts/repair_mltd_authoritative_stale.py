#!/usr/bin/env python3
"""Recover stale MLTD rows whose only defect is an authoritative anniversary form.

This is deliberately narrow: it never re-translates a sentence.  It repairs only
an ordinal anniversary phrase already established by authoritative-terms.json,
then requires the full current deterministic QA and authoritative checks to pass
with no remaining issues before re-adding the row to active output.  Stale
historical evidence is never deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_translation_quality import evaluate_row, load_glossary
from scripts.revalidate_mltd_api_output import authoritative_issues
from scripts.translate_mltd_api_pool import load_authoritative_terms

ANNIVERSARY_SOURCE_RE = re.compile(
    r"(?P<ordinal>[1-9](?:st|nd|rd|th))(?P<spelling>アニバーサリー|あにばーさりー)"
)


def repair_anniversary_translation(
    source: str,
    translation: str,
    source_term: str,
    target: str,
) -> str | None:
    if source_term not in source or target in translation:
        return None
    match = ANNIVERSARY_SOURCE_RE.fullmatch(source_term)
    if match is None:
        return None
    ordinal = match.group("ordinal")
    number = ordinal[0]
    variants = re.compile(
        rf"(?:{re.escape(ordinal)}\s*(?:周年(?:纪念|庆)?|[Aa]nniversary)|"
        rf"{re.escape(number)}\s*周年(?:庆)?)"
    )
    found = variants.search(translation)
    if found is None:
        return None
    repaired = translation[: found.start()] + target + translation[found.end() :]
    return repaired if repaired != translation else None


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
    term_sha = hashlib.sha256(args.authoritative_terms.read_bytes()).hexdigest()

    active_rows = []
    active_ids: set[str] = set()
    with args.active.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{args.active}:{line_no}: expected object")
            active_rows.append(row)
            sid = str(row.get("source_sha256", ""))
            if sid:
                active_ids.add(sid)

    latest_stale: dict[str, dict] = {}
    with args.stale.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{args.stale}:{line_no}: expected object")
            sid = str(row.get("source_sha256", ""))
            if sid:
                latest_stale[sid] = row

    recovered: list[tuple[dict, str, str, str]] = []
    for sid, row in latest_stale.items():
        if sid in active_ids:
            continue
        reasons = row.get("stale_reasons", [])
        if not isinstance(reasons, list) or len(reasons) != 1:
            continue
        reason = reasons[0]
        if not isinstance(reason, dict) or reason.get("code") != "authoritative_term_missing":
            continue
        source_term = str(reason.get("source_term", ""))
        target = str(reason.get("target", ""))
        if not source_term or terms.get(source_term) != target:
            continue
        before = str(row.get("translation", ""))
        after = repair_anniversary_translation(
            str(row.get("source", "")), before, source_term, target
        )
        if after is None:
            continue
        candidate = dict(row)
        candidate["translation"] = after
        qa = evaluate_row(row, candidate, glossary)
        if qa.get("issues"):
            continue
        if authoritative_issues(str(row.get("source", "")), after, terms):
            continue
        recovered.append((row, before, after, source_term))

    print(
        json.dumps(
            {
                "active_rows": len(active_rows),
                "unresolved_stale_ids": sum(sid not in active_ids for sid in latest_stale),
                "eligible_repairs": len(recovered),
                "apply": args.apply,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    for row, before, after, source_term in recovered[: max(args.show, 0)]:
        print(
            json.dumps(
                {
                    "source_sha256": row.get("source_sha256"),
                    "source_term": source_term,
                    "before": before,
                    "after": after,
                },
                ensure_ascii=False,
            )
        )

    if not args.apply or not recovered:
        return 0

    args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
    with args.evidence_output.open("a", encoding="utf-8", newline="\n") as evidence:
        for row, before, after, source_term in recovered:
            evidence.write(
                json.dumps(
                    {
                        "source_sha256": row.get("source_sha256"),
                        "source": row.get("source"),
                        "before_translation": before,
                        "after_translation": after,
                        "repair": "deterministic:authoritative_anniversary_normalization",
                        "source_term": source_term,
                        "target": terms[source_term],
                        "original_provenance": row.get("provenance"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        evidence.flush()
        os.fsync(evidence.fileno())

    repaired_rows = []
    for row, _, after, source_term in recovered:
        value = dict(row)
        value["translation"] = after
        value.pop("stale_reasons", None)
        value.pop("qa", None)
        value.pop("qa_nonblocking", None)
        repairs = list(value.get("deterministic_repairs", []))
        repairs.append(
            {
                "type": "authoritative_anniversary_normalization",
                "source_term": source_term,
                "target": terms[source_term],
            }
        )
        value["deterministic_repairs"] = repairs
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
