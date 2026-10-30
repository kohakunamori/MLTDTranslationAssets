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
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.core.hashing import sha256_file, sha256_text  # noqa: E402
from pipelines.core.jsonl import read_jsonl  # noqa: E402
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


def build_queue(root: Path, terms: dict[str, str], stage: str = "all") -> dict:
    if stage not in {"all", "llm_translated", "unmarked"}:
        raise ValueError("stage must be all, llm_translated, or unmarked")
    rows: list[dict] = []
    counts = Counter()
    term_counts = Counter()
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
            for source_term, target in terms.items():
                if source_term not in source or target in translation:
                    continue
                item = {
                    "kind": kind,
                    "file": path.relative_to(root).as_posix(),
                    "item_key": row.get("item_key"),
                    "index": row.get("index"),
                    "source_sha256": str(row.get("source_sha256", "")),
                    "source": source,
                    "translation": translation,
                    "translation_stage": provenance or None,
                    "source_term": source_term,
                    "target": target,
                    "review_status": "unreviewed",
                }
                if item["source_sha256"] != sha256_text(source):
                    raise ValueError(f"source identity mismatch: {path}")
                rows.append(item)
                counts[(kind, provenance or "unmarked")] += 1
                term_counts[(source_term, target)] += 1
    rows.sort(key=lambda row: (
        0 if row["translation_stage"] == "llm_translated" else 1,
        row["source_term"], row["file"], str(row.get("item_key") or ""),
        str(row.get("index") or ""), row["source_sha256"],
    ))
    return {
        "schema_version": 1,
        "kind": "mltd-authoritative-term-review-queue",
        "stage_filter": stage,
        "terms_count": len(terms),
        "rows": rows,
        "summary": {
            "rows": len(rows),
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
    report = build_queue(ROOT, terms, args.stage)
    report["terms_sha256"] = sha256_file(terms_path) if terms_path.is_file() else ""
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
