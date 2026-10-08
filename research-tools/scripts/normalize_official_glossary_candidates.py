#!/usr/bin/env python3
"""Create review-only Simplified Chinese candidates from historical official terminology.

This intentionally performs script conversion only. It never promotes output into the
production glossary because Traditional->Simplified conversion does not resolve regional
wording, franchise naming policy, or title-localization choices.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

try:
    from opencc import OpenCC
except ImportError as exc:
    raise SystemExit(
        "opencc is required; run with: uv run --with opencc-python-reimplemented python "
        "scripts/normalize_official_glossary_candidates.py ..."
    ) from exc

KANA_RE = re.compile(r"[ぁ-ゖァ-ヺ]")
LATIN_RE = re.compile(r"[A-Za-z]")


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            rows.append(value)
    return rows


def normalize(rows: list[dict], config: str) -> tuple[list[dict], dict]:
    cc = OpenCC(config)
    output: list[dict] = []
    counts = Counter()
    for row in rows:
        source = str(row.get("source_term", ""))
        traditional = str(row.get("official_traditional", ""))
        status = str(row.get("status", ""))
        if status != "needs_zhcn_normalization" or not source or not traditional:
            converted = ""
            normalization_status = "blocked_source_candidate"
            counts["blocked"] += 1
        else:
            converted = cc.convert(traditional)
            normalization_status = "candidate_only_needs_review"
            counts["converted"] += 1
            counts["changed_by_opencc" if converted != traditional else "unchanged_by_opencc"] += 1
            if KANA_RE.search(converted):
                counts["contains_kana"] += 1
            if LATIN_RE.search(converted):
                counts["contains_latin"] += 1

        output.append({
            **row,
            "suggested_zh_cn": converted,
            "normalization_status": normalization_status,
            "opencc_config": config,
            "changed_by_opencc": bool(converted and converted != traditional),
            "contains_kana": bool(converted and KANA_RE.search(converted)),
            "contains_latin": bool(converted and LATIN_RE.search(converted)),
            "regional_lexicon_review_required": bool(converted),
            "franchise_naming_review_required": bool(converted),
            "review_status": "pending" if converted else "blocked",
            "approved_zh_cn": "",
            "review_notes": "",
            "safe_to_auto_promote": False,
        })

    summary = {
        "schema_version": 1,
        "input_rows": len(rows),
        **counts,
        "opencc_config": config,
        "safe_to_auto_promote": False,
        "policy": (
            "OpenCC output is review-only evidence. Regional wording, official franchise "
            "naming, and title choices must be reviewed before glossary promotion."
        ),
    }
    return output, summary


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--config", default="t2s")
    args = ap.parse_args()

    rows, summary = normalize(read_jsonl(args.input), args.config)
    write_jsonl(args.output, rows)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
