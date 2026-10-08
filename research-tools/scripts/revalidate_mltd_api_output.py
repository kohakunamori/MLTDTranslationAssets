#!/usr/bin/env python3
"""Revalidate API translations against current deterministic production rules."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_translation_quality import evaluate_row, load_glossary
from scripts.translate_gtx_queue import BASIC_BLOCKING_QA_CODES
from scripts.translate_mltd_api_pool import load_authoritative_terms, mask_authoritative_terms


def authoritative_issues(
    source: str,
    translation: str,
    terms: dict[str, str],
) -> list[dict]:
    """Validate exactly the authoritative terms the translator would mask.

    Source terms can overlap (for example a full character name and a nickname
    suffix inside the same phrase).  Revalidation must follow the same
    longest-first, non-overlapping selection as production masking rather than
    independently requiring every textual substring.
    """
    _, _, applied = mask_authoritative_terms(source, terms)
    expected_terms = Counter(applied)
    issues = []
    for source_term, expected in expected_terms.items():
        target = terms[source_term]
        actual = translation.count(target)
        if actual < expected:
            issues.append(
                {
                    "code": "authoritative_term_missing",
                    "source_term": source_term,
                    "target": target,
                    "expected_count": expected,
                    "actual_count": actual,
                }
            )
    return issues


def stale_reason_signature(reasons: Any) -> str:
    """Canonicalize a stale reason set for append-only version comparison."""
    if not isinstance(reasons, list):
        reasons = []
    return json.dumps(reasons, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_existing_stale(path: Path) -> dict[str, str]:
    """Return the latest recorded reason signature for every stale source ID.

    Stale evidence is append-only.  A source may become invalid for new reasons
    after QA policy evolves, so source-ID-only deduplication is insufficient.
    Keeping the latest signature lets revalidation append a new evidence version
    when the current reason set changed without duplicating identical records.
    """
    latest: dict[str, str] = {}
    if not path.is_file():
        return latest
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            sid = str(row.get("source_sha256", ""))
            if sid:
                latest[sid] = stale_reason_signature(row.get("stale_reasons", []))
    return latest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.jsonl"),
    )
    ap.add_argument(
        "--stale-output",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.stale.jsonl"),
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
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    glossary = load_glossary(args.glossary)
    terms = load_authoritative_terms(args.authoritative_terms)
    rows = []
    with args.input.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{args.input}:{line_no}: expected object")
            rows.append(row)

    kept = []
    stale = []
    reason_counts: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source", ""))
        translation = str(row.get("translation", ""))
        qa = evaluate_row(row, row, glossary)
        blocking = [
            issue
            for issue in qa.get("issues", [])
            if isinstance(issue, dict)
            and issue.get("code") in BASIC_BLOCKING_QA_CODES
        ]
        term_issues = authoritative_issues(source, translation, terms)
        reasons = [*blocking, *term_issues]
        if reasons:
            stale_row = dict(row)
            stale_row["stale_reasons"] = reasons
            stale.append(stale_row)
            for issue in reasons:
                code = str(issue.get("code", "unknown"))
                reason_counts[code] = reason_counts.get(code, 0) + 1
        else:
            kept.append(row)

    summary = {
        "input_rows": len(rows),
        "kept_rows": len(kept),
        "stale_rows": len(stale),
        "reason_counts": dict(sorted(reason_counts.items())),
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.dry_run:
        for row in stale[:50]:
            print(
                json.dumps(
                    {
                        "source_sha256": row.get("source_sha256"),
                        "source": row.get("source"),
                        "translation": row.get("translation"),
                        "stale_reasons": row.get("stale_reasons"),
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    stale_seen = load_existing_stale(args.stale_output)
    args.stale_output.parent.mkdir(parents=True, exist_ok=True)
    with args.stale_output.open("a", encoding="utf-8", newline="\n") as handle:
        for row in stale:
            sid = str(row.get("source_sha256", ""))
            signature = stale_reason_signature(row.get("stale_reasons", []))
            if sid and stale_seen.get(sid) == signature:
                continue
            handle.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            if sid:
                stale_seen[sid] = signature
        handle.flush()
        os.fsync(handle.fileno())

    tmp = args.input.with_suffix(args.input.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in kept:
            handle.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, args.input)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
