#!/usr/bin/env python3
"""Create a small deterministic usability sample from MLTD batch translations."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def rank(seed: str, sid: str) -> str:
    return hashlib.sha256(f"{seed}\0{sid}".encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--translations", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--sample-size", type=int, default=30)
    ap.add_argument("--seed", default="mltd-production-sample-v1")
    args = ap.parse_args()

    if args.sample_size <= 0:
        raise SystemExit("--sample-size must be > 0")

    queue = {str(r.get("source_sha256", "")): r for r in read_jsonl(args.queue)}
    translations = [r for r in read_jsonl(args.translations) if str(r.get("translation", "")).strip()]
    accepted = [r for r in translations if str(r.get("status", "")) == "machine_translated"]
    flagged = [r for r in translations if str(r.get("status", "")) != "machine_translated"]

    accepted.sort(key=lambda r: rank(args.seed, str(r.get("source_sha256", ""))))
    flagged.sort(key=lambda r: rank(args.seed + "-flagged", str(r.get("source_sha256", ""))))

    selected = accepted[: args.sample_size]
    selected += flagged[: min(5, len(flagged))]

    output: list[dict] = []
    for row in selected:
        sid = str(row.get("source_sha256", ""))
        q = queue.get(sid, {})
        output.append({
            "source_sha256": sid,
            "source": row.get("source", q.get("source", "")),
            "translation": row.get("translation", ""),
            "status": row.get("status", ""),
            "deterministic_qa_issues": row.get("deterministic_qa_issues", []),
            "qa_nonblocking": bool(row.get("qa_nonblocking", False)),
            "examples": q.get("examples", row.get("examples", [])),
            "context_examples": q.get("context_examples", []),
            "usage_profile": q.get("usage_profile", {}),
        })

    write_jsonl(args.output, output)
    result = {
        "schema_version": 1,
        "translations_available": len(translations),
        "accepted_available": len(accepted),
        "flagged_available": len(flagged),
        "sample_size_requested": args.sample_size,
        "accepted_sampled": min(args.sample_size, len(accepted)),
        "flagged_sampled": min(5, len(flagged)),
        "sample_rows": len(output),
        "status_counts": dict(Counter(str(r.get("status", "")) for r in output)),
        "seed": args.seed,
        "output": str(args.output),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
