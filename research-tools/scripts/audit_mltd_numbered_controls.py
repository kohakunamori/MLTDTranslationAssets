#!/usr/bin/env python3
"""Safely quarantine old MLTD numbered-control-code corruption.

Audits the latest accepted value for each source SHA in GTX main output, saves
source-bound evidence and a context-preserving retranslation queue. Optional
--apply-main atomically removes ONLY currently-bad rows from the accepted main
output. Never compete with a running GTX writer or change companion output.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.translate_gtx_queue import source_id

CODE_RE = re.compile(r"\\[0-9]{2}\\")
MARKER = b'"source_sha256":"'
BAD_NAME = "numbered-control-code-current.jsonl"
SUMMARY_NAME = "numbered-control-code-current.summary.json"
REPAIR_NAME = "machine-translation-control-code-repair-queue.jsonl"


def _codes(value: str) -> Counter[str]:
    return Counter(CODE_RE.findall(value))


def _jsonlines(path: Path):
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            yield number, raw, json.loads(raw)


def scan_main(path: Path, seen_ids: set[str] | None = None) -> tuple[dict[str, dict], dict[str, int]]:
    stats = Counter()
    current: dict[str, dict] = {}
    for number, _raw, row in _jsonlines(path):
        stats["rows"] += 1
        sid = str(row.get("source_sha256", ""))
        original = str(row.get("source", ""))
        translated = str(row.get("translation", ""))
        if not sid or not original or not translated:
            continue
        if sid != source_id(original):
            raise ValueError(f"{path}:{number}: source SHA mismatch; refuse mutation")
        if seen_ids is not None:
            seen_ids.add(sid)
        before = _codes(original)
        after = _codes(translated)
        if before:
            stats["rows_with_codes"] += 1
        if before == after:
            current.pop(sid, None)
            continue
        stats["mismatching_rows"] += 1
        current[sid] = {
            "source_sha256": sid,
            "source": original,
            "translation": translated,
            "status": row.get("status", ""),
            "source_codes": dict(before),
            "translation_codes": dict(after),
            "original_line": number,
            "provenance": row.get("provenance", {}),
            "stale_reasons": [{
                "code": "protected_token_mismatch",
                "detail": "MLTD numbered control sequences differ",
            }],
        }
    stats["unique_unresolved"] = len(current)
    return current, dict(stats)


def _writer_is_active(output: Path) -> bool:
    try:
        import psutil
    except ImportError:
        # Fail closed rather than mutating a live output with unknown writers.
        return True
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if not any(str(c).replace("\\", "/").endswith("translate_mltd_api_pool.py") for c in cmd):
                continue
            specified = None
            if "--output" in cmd:
                idx = cmd.index("--output")
                if idx + 1 < len(cmd):
                    specified = Path(cmd[idx + 1]).resolve()
            else:
                specified = Path("build/localization-90200/machine-translations-api.jsonl").resolve()
            if specified == output.resolve():
                return True
        except (psutil.Error, OSError, ValueError):
            return True
    return False


def repair_rows(queue: Path, invalid: dict[str, dict]) -> list[dict]:
    needed = {sid.encode("ascii"): sid for sid in invalid}
    found = {}
    with queue.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            if not needed:
                break
            pos = raw.find(MARKER)
            if pos < 0:
                continue
            sid_raw = raw[pos + len(MARKER): pos + len(MARKER) + 64]
            sid = needed.get(sid_raw)
            if sid is None:
                continue
            row = json.loads(raw)
            if row.get("source_sha256") != sid or row.get("source") != invalid[sid]["source"]:
                raise ValueError(f"{queue}:{number}: mismatched repair queue source")
            proposal = dict(row)
            proposal["previous_translation"] = invalid[sid]["translation"]
            proposal["stale_reasons"] = invalid[sid]["stale_reasons"]
            proposal["queue_reason"] = "numbered_control_code_repair"
            found[sid] = proposal
            needed.pop(sid_raw)
    if needed:
        raise ValueError(f"missing {len(needed)} source SHA in context queue; refuse quarantine")
    return [found[sid] for sid in sorted(found)]


def atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def audit(
    output: Path, context: Path, workspace: Path, *,
    apply_main: bool = False,
) -> dict:
    if apply_main and _writer_is_active(output):
        raise RuntimeError("GTX output may have a running writer; refusing quarantine")
    baseline = output.stat()
    accepted_now: set[str] = set()
    invalid, counts = scan_main(output, accepted_now)
    fresh_invalid = set(invalid)
    destination = workspace / "audits"
    destination.mkdir(parents=True, exist_ok=True)
    bad_path = destination / BAD_NAME
    queue_path = workspace / REPAIR_NAME
    summary_path = destination / SUMMARY_NAME
    previously_quarantined: dict[str, dict] = {}
    if summary_path.is_file() and bad_path.is_file():
        previous = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        if previous.get("main_output_modified"):
            for _number, _raw, archived in _jsonlines(bad_path):
                sid = str(archived.get("source_sha256", ""))
                if sid and sid not in accepted_now:
                    previously_quarantined[sid] = archived
    # Previously quarantined entries no longer occur in the main output.
    # Preserve their sole repair evidence across subsequent audit runs.
    invalid = {**previously_quarantined, **invalid}
    counts["already_quarantined_unique"] = len(previously_quarantined)
    counts["unique_unresolved"] = len(invalid)
    repair = repair_rows(context, invalid) if invalid else []
    if output.stat().st_size != baseline.st_size or output.stat().st_mtime_ns != baseline.st_mtime_ns:
        raise RuntimeError("GTX output changed during audit; refusing stale snapshot")
    atomic_jsonl(bad_path, [invalid[sid] for sid in sorted(invalid)])
    atomic_jsonl(queue_path, repair)

    if apply_main and fresh_invalid:
        # Stage the clean copy while the original stays readable. Keep only
        # corrupt rows excluded; no changes to all other JSONL bytes or order.
        if _writer_is_active(output):
            raise RuntimeError("GTX writer started during audit; refusing mutation")
        tmp = output.with_name(output.name + ".control-code-clean.tmp")
        removed = 0
        with tmp.open("wb") as dst:
            for _line, raw, row in _jsonlines(output):
                sid = str(row.get("source_sha256", ""))
                if sid in fresh_invalid:
                    removed += 1
                    continue
                dst.write(raw)
            dst.flush()
            os.fsync(dst.fileno())
        if removed < len(fresh_invalid):
            tmp.unlink(missing_ok=True)
            raise RuntimeError("did not locate all corrupt source IDs; refusing mutation")
        if _writer_is_active(output) or output.stat().st_size != baseline.st_size or output.stat().st_mtime_ns != baseline.st_mtime_ns:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("GTX output changed before atomic replacement; refusing mutation")
        os.replace(tmp, output)
        counts["quarantined_rows"] = removed
    else:
        counts["quarantined_rows"] = 0

    report = {
        "schema_version": 1,
        "kind": "numbered-control-code-production-audit",
        "snapshot_bytes": baseline.st_size,
        "counts": counts,
        "repair_source_ids": sorted(invalid),
        "repair_queue": str(queue_path),
        "quarantine": str(bad_path),
        "main_output_modified": bool((apply_main and fresh_invalid) or previously_quarantined),
        "note": "Only source-SHA-bound corrupt numbered control sequences; review/retranslate repair queue without parallel GTX writer",
    }
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/localization-90200/machine-translations-api.jsonl"))
    parser.add_argument("--context", type=Path, default=Path("build/localization-90200/machine-translation-queue-context.jsonl"))
    parser.add_argument("--workspace", type=Path, default=Path("build/localization-90200"))
    parser.add_argument("--apply-main", action="store_true",
                        help="atomic quarantine of corrupt accepted GTX rows (only when no GTX writer)")
    args = parser.parse_args()
    print(json.dumps(audit(args.output, args.context, args.workspace, apply_main=args.apply_main),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
