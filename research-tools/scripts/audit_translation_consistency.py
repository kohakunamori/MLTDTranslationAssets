#!/usr/bin/env python3
"""Audit cross-file translation-memory and approved-glossary consistency."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import read_jsonl, translation_status_is_accepted
from scripts.mltd_translation_quality import load_glossary


def load_official_candidates(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    out: dict[str, str] = {}
    for row in read_jsonl(path):
        source = str(row.get("source_term", ""))
        candidate = str(row.get("suggested_zh_cn", ""))
        if not source or not candidate:
            continue
        if source in out and out[source] != candidate:
            raise ValueError(f"official candidate conflict for {source!r}")
        out[source] = candidate
    return out


def _conflicts(by_source: dict[str, dict[str, list[dict]]]) -> list[dict]:
    result: list[dict] = []
    for source, variants in by_source.items():
        if len(variants) <= 1:
            continue
        result.append({
            "source": source,
            "translation_count": len(variants),
            "translations": [
                {"translation": translation, "evidence": evidence[:8], "occurrences": len(evidence)}
                for translation, evidence in sorted(variants.items())
            ],
        })
    result.sort(key=lambda x: (-x["translation_count"], x["source"]))
    return result


def audit(
    paths: list[Path],
    glossary: dict,
    official_candidates: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    official_candidates = official_candidates or {}
    release_by_source: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    review_by_source: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    all_sources: set[str] = set()
    rows_scanned = 0
    release_rows = 0
    review_rows = 0
    glossary_errors: list[dict] = []
    candidate_glossary_findings: list[dict] = []
    official_advisories: list[dict] = []

    for path in paths:
        for row in read_jsonl(path):
            source = str(row.get("source", ""))
            translation = str(row.get("translation", ""))
            if not source or not translation:
                continue
            rows_scanned += 1
            all_sources.add(source)
            status = str(row.get("status", ""))
            release_eligible = translation_status_is_accepted(status)
            evidence = {
                "file": str(path),
                "source_sha256": str(row.get("source_sha256", "")),
                "status": status,
                "provenance": str(row.get("provenance", "")),
                "release_eligible": release_eligible,
            }
            target = release_by_source if release_eligible else review_by_source
            target[source][translation].append(evidence)
            if release_eligible:
                release_rows += 1
            else:
                review_rows += 1

            for jp, spec in glossary.get("entries", {}).items():
                if jp not in source:
                    continue
                if isinstance(spec, str):
                    spec = {"preferred": spec}
                if not isinstance(spec, dict):
                    continue
                preferred = str(spec.get("preferred", ""))
                forbidden = [str(x) for x in spec.get("forbidden", [])]
                findings_target = glossary_errors if release_eligible else candidate_glossary_findings
                if preferred and preferred not in translation:
                    findings_target.append({
                        "code": "approved_preferred_term_missing",
                        "source_term": jp,
                        "preferred": preferred,
                        "source": source,
                        "translation": translation,
                        **evidence,
                    })
                for term in forbidden:
                    if term and term in translation:
                        findings_target.append({
                            "code": "approved_forbidden_term_present",
                            "source_term": jp,
                            "forbidden": term,
                            "source": source,
                            "translation": translation,
                            **evidence,
                        })

            expected = official_candidates.get(source)
            if expected and translation != expected:
                official_advisories.append({
                    "code": "exact_official_term_candidate_differs",
                    "source": source,
                    "candidate_zh_cn": expected,
                    "translation": translation,
                    **evidence,
                })

    tm_conflicts = _conflicts(release_by_source)
    candidate_tm_conflicts = _conflicts(review_by_source)
    summary = {
        "schema_version": 1,
        "files": [str(path) for path in paths],
        "rows_scanned": rows_scanned,
        "release_eligible_rows": release_rows,
        "review_only_rows": review_rows,
        "unique_sources": len(all_sources),
        "translation_memory_conflicts": len(tm_conflicts),
        "candidate_translation_memory_conflicts": len(candidate_tm_conflicts),
        "approved_glossary_errors": len(glossary_errors),
        "candidate_glossary_findings": len(candidate_glossary_findings),
        "official_candidate_advisories": len(official_advisories),
        "hard_error_count": len(tm_conflicts) + len(glossary_errors),
        "candidate_findings_are_non_blocking": True,
        "official_candidate_advisories_are_non_blocking": True,
    }
    report = {
        **summary,
        "translation_memory_conflicts_first": tm_conflicts[:200],
        "candidate_translation_memory_conflicts_first": candidate_tm_conflicts[:200],
        "approved_glossary_errors_first": glossary_errors[:500],
        "candidate_glossary_findings_first": candidate_glossary_findings[:500],
        "official_candidate_advisories_first": official_advisories[:500],
    }
    return report, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--translations", type=Path, action="append", required=True)
    ap.add_argument("--glossary", type=Path)
    ap.add_argument("--official-candidates", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--fail-on-hard-errors", action="store_true")
    args = ap.parse_args()

    glossary = load_glossary(args.glossary)
    report, summary = audit(
        args.translations,
        glossary,
        load_official_candidates(args.official_candidates),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.fail_on_hard_errors and summary["hard_error_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
