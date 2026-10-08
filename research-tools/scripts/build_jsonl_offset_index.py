#!/usr/bin/env python3
"""Build a compact byte-offset index for large UTF-8 JSONL files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_index(source: Path, stride: int) -> dict:
    if stride <= 0:
        raise ValueError("stride must be > 0")
    offsets: list[list[int]] = []
    rows = 0
    with source.open("rb") as handle:
        while True:
            offset = handle.tell()
            line = handle.readline()
            if not line:
                break
            if rows % stride == 0:
                offsets.append([rows, offset])
            rows += 1
    return {
        "schema_version": 1,
        "source": str(source),
        "size": source.stat().st_size,
        "stride": stride,
        "rows": rows,
        "offsets": offsets,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--stride", type=int, default=1024)
    args = ap.parse_args()
    output = args.output or args.input.with_suffix(args.input.suffix + ".offset-index.json")
    doc = build_index(args.input, args.stride)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tmp.replace(output)
    print(
        json.dumps(
            {
                "source": doc["source"],
                "size": doc["size"],
                "rows": doc["rows"],
                "stride": doc["stride"],
                "checkpoints": len(doc["offsets"]),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
