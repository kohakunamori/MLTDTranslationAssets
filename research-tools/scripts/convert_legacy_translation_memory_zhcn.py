#!/usr/bin/env python3
"""Convert already source-bound historical official translation memory to zh-CN candidates.

This operates on JSONL translation memory, not Unity bundles. It therefore preserves the
previously verified JP source binding and avoids introducing UnityPy-version variance.
The output remains review-only and is deliberately not auto-promoted.
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
        "scripts/convert_legacy_translation_memory_zhcn.py ..."
    ) from exc

PROTECTED_TOKEN_RE = re.compile(
    r"\{[^{}]+\}"
    r"|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]"
    r"|<[^<>]+>"
    r"|\\[nrt]"
)


def protected_tokens(value: str) -> Counter[str]:
    return Counter(PROTECTED_TOKEN_RE.findall(value))


def validate_tokens(source: str, translated: str) -> None:
    before = protected_tokens(source)
    after = protected_tokens(translated)
    if before != after:
        raise ValueError(
            f"protected token mismatch: source={dict(before)!r} translation={dict(after)!r}"
        )


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            yield row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--config", default="t2s")
    ap.add_argument("--sample", type=int, default=20)
    args = ap.parse_args()

    cc = OpenCC(args.config)
    counts = Counter()
    errors: list[dict] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8", newline="\n") as out:
        for row in read_jsonl(args.input):
            counts["input_rows"] += 1
            source = str(row.get("source", ""))
            original = str(row.get("translation", ""))
            converted = cc.convert(original)
            status = str(row.get("status", ""))
            token_ok = True
            error = ""
            try:
                validate_tokens(source, converted)
            except ValueError as exc:
                token_ok = False
                error = str(exc)
                if status.startswith("needs_review"):
                    counts["inherited_token_mismatch"] += 1
                else:
                    counts["new_token_mismatch"] += 1

            if converted != original:
                counts["changed_by_opencc"] += 1
            else:
                counts["unchanged_by_opencc"] += 1

            # Historical official wording is valuable evidence, but OpenCC alone does
            # not certify Mainland terminology/style. Keep every row review-only.
            converted_row = {
                **row,
                "translation": converted,
                "status": "needs_review_zhcn_normalization",
                "source_status": status,
                "provenance": "official-legacy-zh+opencc-t2s",
                "normalization": {
                    "opencc_config": args.config,
                    "token_validation_pass": token_ok,
                    "regional_lexicon_review_required": True,
                    "franchise_terminology_review_required": True,
                    "safe_to_auto_promote": False,
                },
            }
            if error:
                converted_row["normalization"]["error"] = error
                if len(errors) < args.sample:
                    errors.append({
                        "source": source,
                        "translation": converted,
                        "error": error,
                    })
            out.write(json.dumps(converted_row, ensure_ascii=False, separators=(",", ":")) + "\n")
            counts["output_rows"] += 1

    tmp.replace(args.output)
    summary = {
        "schema_version": 1,
        **counts,
        "opencc_config": args.config,
        "safe_to_auto_promote": False,
        "output": str(args.output),
        "errors_first": errors,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not counts["new_token_mismatch"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
