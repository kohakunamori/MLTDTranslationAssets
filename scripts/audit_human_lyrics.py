#!/usr/bin/env python3
"""Build an offline comparison queue from the existing lyric collector cache.

Never edits translations, calls a model, or treats wording differences as errors.
Reports contain third-party reference text: keep them outside the public repo.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.core.hashing import sha256_text
from pipelines.core.jsonl import read_jsonl
from pipelines.text.mltd_lyric_rules import is_english_bypass


def normalize(text: str) -> str:
    """Ignore spacing/width/punctuation only; never use fuzzy meaning matches."""
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def reference_index(cache: Path) -> tuple[dict, dict]:
    index = defaultdict(list)
    stats = Counter()
    for path in sorted(cache.glob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        stats["pages"] += 1
        stats["attribution_" + str(page.get("translator_status", "unknown"))] += 1
        eligible = False
        for block in page.get("lyrics", []):
            original, translated = block.get("original_lines", []), block.get("translated_lines", [])
            if (not original or not translated or len(original) != len(translated)
                    or not block.get("matched_line_count")):
                stats["excluded_unaligned_blocks"] += 1
                continue
            eligible = True
            # Game slots may join up to three full-version lines. Equal counts
            # are only a prerequisite; exact Japanese matching is still needed.
            for start in range(len(original)):
                for length in range(1, min(3, len(original) - start) + 1):
                    source = " ".join(original[start:start + length])
                    target = " ".join(translated[start:start + length])
                    key = normalize(source)
                    if len(key) < 8 or not target.strip():
                        continue
                    index[key].append({
                        "title": page.get("title"), "url": page.get("url"),
                        "revision_id": page.get("revision_id"), "retrieved_at": page.get("retrieved_at"),
                        "translator_status": page.get("translator_status", "unknown"),
                        "translators": page.get("translators", []),
                        "reference_translation": target, "reference_line_start": start + 1,
                        "reference_line_count": length,
                    })
        stats["eligible_pages"] += int(eligible)
    if not stats["pages"]:
        raise ValueError("no lyric collector cache pages found")
    return dict(index), dict(stats)


def audit(root: Path, cache: Path) -> dict:
    references, reference_stats = reference_index(cache)
    candidates = defaultdict(list)
    for path in sorted((root / "lyrics/songs").glob("*.jsonl")):
        for row in read_jsonl(path):
            source = str(row.get("ja", ""))
            if row.get("source_sha256") != sha256_text(source):
                raise ValueError(f"invalid lyric source hash: {path}")
            if row.get("status") != "accepted" or not row.get("zh") or is_english_bypass(source):
                continue
            candidates[row["source_sha256"]].append(row)
    stats = Counter()
    comparisons = []
    for sid, occurrences in sorted(candidates.items()):
        source = occurrences[0]["ja"]
        refs = references.get(normalize(source), [])
        variants = sorted({str(row["zh"]) for row in occurrences})
        stats["accepted_source_identities"] += 1
        stats["multiple_candidate_wordings"] += int(len({normalize(v) for v in variants}) > 1)
        if not refs:
            stats["no_exact_reference_match"] += 1
            continue
        unique = {}
        for ref in refs:
            unique[(ref["url"], ref["revision_id"], ref["reference_translation"])] = ref
        refs = list(unique.values())
        ambiguity = len({normalize(r["reference_translation"]) for r in refs}) > 1
        stats["matched_source_identities"] += 1
        stats["conflicting_reference_wordings"] += int(ambiguity)
        credited = any(r["translator_status"] == "credited" for r in refs)
        stats["matched_with_attribution"] += int(credited)
        comparisons.append({
            "source_sha256": sid, "source": source, "candidates": variants,
            "occurrences": [{"bundle": r["bundle"], "index": r["index"]} for r in occurrences],
            "references": refs,
            "review_reason": "conflicting_references" if ambiguity else "compare_meaning_and_style",
            "different_wording": any(normalize(v) != normalize(r["reference_translation"])
                                     for v in variants for r in refs),
            "gold_eligible": False,
        })
    return {"schema_version": 1, "reference_stats": reference_stats, "summary": dict(stats),
            "policy": "Source-aligned review evidence only; differences are not quality scores. "
                      "Credit and equal line counts do not establish correctness or publication rights.",
            "comparisons": comparisons}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--reference-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.root, args.reference_cache)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"reference_stats": report["reference_stats"], "summary": report["summary"]}))


if __name__ == "__main__":
    main()
