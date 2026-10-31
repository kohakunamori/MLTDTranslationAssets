#!/usr/bin/env python3
"""Build an external, read-only review queue for authoritative term gaps.

The queue is evidence for human review. It never edits locale or lyric files and
never treats a missing ``translation_stage`` as machine output. Full source and
translation text is deliberately written outside the public checkout.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.core.hashing import sha256_file, sha256_text  # noqa: E402
from pipelines.core.jsonl import read_jsonl  # noqa: E402
from pipelines.text.authoritative_term_matching import find_term_matches  # noqa: E402
from pipelines.text.translate_mltd_api_pool import load_authoritative_terms  # noqa: E402


def outside_repo(path: Path, root: Path = ROOT) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(root.resolve()):
        raise ValueError("review queue output must be outside the source checkout")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def iter_catalogue(root: Path):
    for path in sorted((root / "locales").rglob("*.jsonl")):
        yield "locale", path
    for path in sorted((root / "lyrics" / "songs").glob("*.jsonl")):
        yield "lyrics", path


def load_authoritative_name_terms(path: Path) -> dict[str, str]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = document.get("entries", {}) if isinstance(document, dict) else {}
    return {
        str(source): str(spec.get("target", ""))
        for source, spec in entries.items()
        if isinstance(spec, dict)
        and str(spec.get("category", "")) in {"character_name", "character_nickname"}
        and str(spec.get("target", ""))
    }


def strip_whitespace(text: str) -> str:
    """Drop whitespace so newline/space layout alone never changes the evidence."""
    return re.sub(r"\s+", "", text)


def whitespace_variants(text: str) -> set[str]:
    """The text itself plus its whitespace-stripped twin, when they differ."""
    collapsed = strip_whitespace(text)
    return {text} if collapsed == text else {text, collapsed}


def source_name_hits(source: str, name_terms: dict[str, str]) -> dict[str, str]:
    """Character names the row's source really uses, keyed by catalogue source term."""
    hits: dict[str, str] = {}
    for variant in whitespace_variants(source):
        for match in find_term_matches(variant, name_terms):
            hits[match.term] = name_terms[match.term]
    return hits


def conflict_targets(
    translation: str, name_terms: dict[str, str], expected_targets: set[str]
) -> list[str]:
    """Name targets used in ``translation`` for a character the source never names."""
    variants = whitespace_variants(translation)
    return sorted({
        target
        for target in name_terms.values()
        if target
        and target not in expected_targets
        and any(target in variant for variant in variants)
    })


def matched_source_terms(
    source: str, terms: dict[str, str], name_terms: dict[str, str]
) -> list[str]:
    """Terms the source really uses, in deterministic order.

    Terms are matched through the shared helper, which rejects an all-katakana
    term sitting inside a longer katakana run (``ジュリア`` inside ``ジュリアン``,
    ``ロコ`` inside ``ウロコ``) while leaving kanji terms alone. Character names
    additionally tolerate a source whose whitespace layout differs from the
    catalogue string.
    """
    matched = {match.term for match in find_term_matches(source, terms)}
    collapsed = strip_whitespace(source)
    if collapsed != source:
        matched.update(
            match.term
            for match in find_term_matches(collapsed, terms)
            if match.term in name_terms
        )
    return sorted(term for term in matched if term in terms)


def catalogue_row_key(row: dict) -> tuple[str, str, str]:
    """Identity of the catalogue row an issue came from, for deduplication."""
    item_key = row.get("item_key")
    if item_key not in (None, ""):
        return (str(row["file"]), "item_key", str(item_key))
    index = row.get("index")
    if index is not None:
        return (str(row["file"]), "index", str(index))
    return (str(row["file"]), "source_sha256", str(row.get("source_sha256", "")))


def build_queue(
    root: Path,
    terms: dict[str, str],
    stage: str = "all",
    name_terms: dict[str, str] | None = None,
) -> dict:
    if stage not in {"all", "llm_translated", "unmarked"}:
        raise ValueError("stage must be all, llm_translated, or unmarked")
    rows: list[dict] = []
    counts = Counter()
    term_counts = Counter()
    name_terms = name_terms or {}
    for kind, path in iter_catalogue(root):
        for row in read_jsonl(path):
            if row.get("status") != "accepted" or not str(row.get("zh", "")):
                continue
            provenance = str(row.get("translation_stage") or "")
            if stage == "llm_translated" and provenance != "llm_translated":
                continue
            if stage == "unmarked" and provenance:
                continue
            source = str(row.get("ja", ""))
            translation = str(row.get("zh", ""))
            normalized_translation = strip_whitespace(translation)
            source_names = source_name_hits(source, name_terms)
            # A character is only "expected" when the source actually names them;
            # otherwise the issue stays unreviewed instead of being invented.
            conflicts = conflict_targets(
                translation, name_terms, set(source_names.values())
            ) if source_names else []
            base = {
                "kind": kind,
                "file": path.relative_to(root).as_posix(),
                "item_key": row.get("item_key"),
                "index": row.get("index"),
                "source_sha256": str(row.get("source_sha256", "")),
                "source": source,
                "translation": translation,
                "translation_stage": provenance or None,
                "review_status": "unreviewed",
                "character_name_conflicts": conflicts,
                "priority": "high" if conflicts else "normal",
            }
            if base["source_sha256"] != sha256_text(source):
                raise ValueError(f"source identity mismatch: {path}")
            emitted = False
            for source_term in matched_source_terms(source, terms, name_terms):
                target = terms[source_term]
                if target in translation:
                    continue
                if source_term in name_terms and target in normalized_translation:
                    continue
                rows.append({
                    **base,
                    "source_term": source_term,
                    "target": target,
                    "review_reason": "authoritative_term_missing",
                })
                counts[(kind, provenance or "unmarked")] += 1
                term_counts[(source_term, target)] += 1
                emitted = True
            if conflicts and not emitted:
                # A name conflict with no other term gap still deserves a row.
                rows.append({
                    **base,
                    "source_term": None,
                    "target": None,
                    "review_reason": "character_name_conflict",
                })
                counts[(kind, provenance or "unmarked")] += 1
    rows.sort(key=lambda row: (
        0 if row["priority"] == "high" else 1,
        0 if row["translation_stage"] == "llm_translated" else 1,
        str(row.get("source_term") or ""),
        row["file"],
        str(row.get("item_key") or ""),
        str(row.get("index") or ""),
        row["source_sha256"],
    ))
    return {
        "schema_version": 1,
        "kind": "mltd-authoritative-term-review-queue",
        "stage_filter": stage,
        "terms_count": len(terms),
        "rows": rows,
        "summary": {
            "rows": len(rows),
            "issue_records": len(rows),
            "unique_catalogue_rows": len({catalogue_row_key(row) for row in rows}),
            "high_priority_name_conflicts": sum(
                1 for row in rows if row["priority"] == "high"
            ),
            "by_kind_and_stage": {"|".join(key): value for key, value in sorted(counts.items())},
            "by_term": {"|".join(key): value for key, value in sorted(term_counts.items())},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("all", "llm_translated", "unmarked"), default="all")
    parser.add_argument("--terms", type=Path, default=Path("glossary/authoritative-terms.json"))
    args = parser.parse_args()
    output = outside_repo(args.output)
    terms_path = args.terms.resolve()
    terms = load_authoritative_terms(terms_path)
    report = build_queue(ROOT, terms, args.stage, load_authoritative_name_terms(terms_path))
    report["terms_sha256"] = sha256_file(terms_path) if terms_path.is_file() else ""
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
