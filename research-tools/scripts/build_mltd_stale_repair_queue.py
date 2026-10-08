#!/usr/bin/env python3
"""Build a context-preserving repair queue for unresolved stale API translations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.is_file():
        return ids
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            sid = str(row.get("source_sha256", ""))
            if sid:
                ids.add(sid)
    return ids


def read_stale(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            sid = str(row.get("source_sha256", ""))
            if not sid:
                raise ValueError(f"{path}:{line_no}: stale row missing source_sha256")
            rows[sid] = row
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--queue",
        type=Path,
        default=Path("build/localization-90200/machine-translation-queue-context.jsonl"),
    )
    ap.add_argument(
        "--active",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.jsonl"),
    )
    ap.add_argument(
        "--stale",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.stale.jsonl"),
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/machine-translation-stale-repair-queue.jsonl"),
    )
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path("build/localization-90200/machine-translation-stale-repair-summary.json"),
    )
    args = ap.parse_args()

    active_ids = read_ids(args.active)
    stale = read_stale(args.stale)
    unresolved = set(stale) - active_ids
    matched: set[str] = set()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    # The context queue is hundreds of MB.  Parsing every non-target row with
    # json.loads dominated repair-queue rebuild time even when only a few dozen
    # source IDs were stale.  Scan raw JSONL bytes for the fixed source_sha256
    # field first and deserialize only matching rows.  This preserves exact
    # behavior while making the common small-repair case mostly I/O-bound.
    marker = b'"source_sha256":"'
    unresolved_bytes = {sid.encode("ascii"): sid for sid in unresolved}
    with args.queue.open("rb") as source_handle, tmp.open(
        "w", encoding="utf-8", newline="\n"
    ) as output_handle:
        for line_no, raw_line in enumerate(source_handle, 1):
            if not raw_line.strip():
                continue
            pos = raw_line.find(marker)
            if pos < 0:
                continue
            start = pos + len(marker)
            sid_bytes = raw_line[start : start + 64]
            sid = unresolved_bytes.get(sid_bytes)
            if sid is None:
                continue
            row = json.loads(raw_line.decode("utf-8-sig"))
            if not isinstance(row, dict):
                raise ValueError(f"{args.queue}:{line_no}: expected object")
            if str(row.get("source_sha256", "")) != sid:
                raise ValueError(f"{args.queue}:{line_no}: source_sha256 scan mismatch")
            previous = stale[sid]
            repair = dict(row)
            repair["previous_translation"] = str(previous.get("translation", ""))
            repair["stale_reasons"] = previous.get("stale_reasons", [])
            repair["queue_reason"] = "stale_api_repair"
            output_handle.write(
                json.dumps(repair, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            matched.add(sid)
            unresolved_bytes.pop(sid_bytes, None)
            if not unresolved_bytes:
                break
    tmp.replace(args.output)

    missing = sorted(unresolved - matched)
    summary = {
        "schema_version": 1,
        "kind": "mltd-stale-api-repair-queue",
        "active_ids": len(active_ids),
        "stale_ids": len(stale),
        "unresolved_stale_ids": len(unresolved),
        "repair_rows": len(matched),
        "missing_from_context_queue": len(missing),
        "missing_source_sha256": missing[:20],
        "output": str(args.output),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if missing:
        raise SystemExit("some unresolved stale rows were not found in the context queue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
