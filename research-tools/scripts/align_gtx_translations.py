#!/usr/bin/env python3
"""Align source-hash translations to a GTX bundle/key catalogue.

New asset versions frequently keep the same Japanese strings while changing
bundle identities and keys.  The production translation memory is keyed by
``source_sha256``; :mod:`scripts.mltd_localize_gtx` expects rows keyed by
``(bundle, key)``.  This adapter joins the two identities without guessing:
the source hash is SHA-256 of the exact UTF-8 source string, and the source
text must match byte-for-byte before a translation is reused.

The output is a candidate only.  Rows without an exact, non-empty, accepted
translation are emitted with an empty translation and ``pending`` status so
the GTX builder leaves the original Japanese fallback in place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

# Direct ``python scripts/align_gtx_translations.py`` execution puts only the
# scripts directory on sys.path; add the repository root so the shared GTX
# helpers resolve the same way as module execution and test collection.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.mltd_localize_gtx import (
    is_source_text,
    read_jsonl,
    translation_status_is_accepted,
    validate_translation,
    write_jsonl,
)


class AlignmentError(ValueError):
    """Raised when source identity or translation joins are ambiguous."""


def source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _require_sha(row: dict, *, label: str, line_no: int) -> str:
    source = str(row.get("source", ""))
    sid = str(row.get("source_sha256", ""))
    if not sid:
        raise AlignmentError(f"{label}:{line_no}: missing source_sha256")
    expected = source_sha256(source)
    if sid.lower() != expected:
        raise AlignmentError(
            f"{label}:{line_no}: source_sha256 mismatch; expected {expected}, got {sid}"
        )
    return expected


def _translation_index(rows: Iterable[dict], label: str) -> tuple[dict[str, dict], Counter]:
    index: dict[str, dict] = {}
    counts = Counter()
    for line_no, row in enumerate(rows, 1):
        sid = _require_sha(row, label=label, line_no=line_no)
        translation = str(row.get("translation", ""))
        # Empty rows are useful queue evidence but are not reusable values.
        if not translation:
            counts["empty_translation_rows"] += 1
            continue
        source = str(row.get("source", ""))
        status = str(row.get("status", "pending"))
        existing = index.get(sid)
        if existing is not None:
            if str(existing.get("source", "")) != source:
                raise AlignmentError(f"{label}:{line_no}: conflicting source for {sid}")
            if str(existing.get("translation", "")) != translation:
                raise AlignmentError(f"{label}:{line_no}: conflicting translation for {sid}")
            continue
        index[sid] = row
        counts["nonempty_translation_rows"] += 1
        if translation_status_is_accepted(status):
            counts["accepted_source_hashes"] += 1
        else:
            counts["nonaccepted_source_hashes"] += 1
    return index, counts


def align_catalogue(catalogue_rows: list[dict], translation_rows: list[dict], *, source_label: str) -> tuple[list[dict], list[dict], dict]:
    """Return ``(candidate_rows, unresolved_rows, report)`` for one catalogue."""
    translations, source_counts = _translation_index(translation_rows, source_label)
    candidate: list[dict] = []
    unresolved: list[dict] = []
    seen: set[tuple[str, str]] = set()
    counts = Counter(source_counts)
    for line_no, row in enumerate(catalogue_rows, 1):
        bundle = str(row.get("bundle", ""))
        key = str(row.get("key", ""))
        identity = (bundle.casefold(), key)
        if identity in seen:
            raise AlignmentError(f"catalogue:{line_no}: duplicate bundle/key {bundle!r}/{key!r}")
        seen.add(identity)
        source = str(row.get("source", ""))
        if not is_source_text(source):
            counts["non_source_rows"] += 1
            continue
        sid = source_sha256(source)
        stored = str(row.get("source_sha256", ""))
        if stored and stored.lower() != sid:
            raise AlignmentError(f"catalogue:{line_no}: source_sha256 mismatch; expected {sid}, got {stored}")
        match = translations.get(sid)
        reason = "unmatched"
        output = {
            "bundle": bundle,
            "key": key,
            "source": source,
            "source_sha256": sid,
            "translation": "",
            "status": "pending",
        }
        if match is not None and str(match.get("source", "")) == source:
            translation = str(match.get("translation", ""))
            status = str(match.get("status", "pending"))
            if not translation_status_is_accepted(status):
                reason = "nonaccepted_status"
            elif translation == source:
                reason = "same_as_source"
            else:
                validate_translation(source, translation)
                output.update(
                    {
                        "translation": translation,
                        "status": status,
                        "match_method": "source_sha256_exact",
                        "source_translation_provenance": match.get("provenance", {}),
                    }
                )
                counts["reused_exact"] += 1
        if not output.get("translation"):
            output["match_method"] = f"source_sha256_{reason}"
            unresolved.append(output)
            counts[reason] += 1
        candidate.append(output)
    counts["source_candidates"] = len(candidate) + 0
    counts["unresolved"] = len(unresolved)
    counts["translated"] = counts["reused_exact"]
    report = {
        "schema_version": 1,
        "identity": "source_sha256_utf8_exact",
        "same_as_source_policy": "fallback_pending",
        "counts": dict(counts),
        "coverage": (counts["translated"] / counts["source_candidates"] if counts["source_candidates"] else 0.0),
    }
    return candidate, unresolved, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="bundle/key candidate JSONL")
    parser.add_argument("--unresolved-output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidate, unresolved, report = align_catalogue(
        read_jsonl(args.catalogue), read_jsonl(args.translations), source_label=str(args.translations)
    )
    report.update(
        {
            "catalogue": str(args.catalogue.resolve()),
            "translation_source": str(args.translations.resolve()),
            "candidate": str(args.output.resolve()),
            "unresolved_queue": str(args.unresolved_output.resolve()),
        }
    )
    write_jsonl(args.output, candidate)
    write_jsonl(args.unresolved_output, unresolved)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
