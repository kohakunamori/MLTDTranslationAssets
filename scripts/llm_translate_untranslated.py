#!/usr/bin/env python3
"""Prepare and apply resumable LLM translation drafts.

This adapter deliberately keeps the existing translation pool as the provider
implementation.  It only converts the public Assets JSONL shape to the pool's
queue shape and writes machine output back as ``pending`` rows.  It never
changes an accepted row and never treats an LLM result as human-reviewed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipelines.text.mltd_localize_gtx import validate_translation


ROOT = Path(__file__).resolve().parents[1]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rows(root: Path):
    for path in sorted((root / "locales").rglob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                yield path, line_no, value


def collect(args: argparse.Namespace) -> int:
    wanted_version = str(args.asset_version or "").strip()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    seen: set[str] = set()
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for path, line_no, row in rows(ROOT):
            if row.get("status") != "untranslated" or str(row.get("zh", "")):
                continue
            if wanted_version and str(row.get("asset_version")) != wanted_version:
                continue
            source = str(row.get("ja", ""))
            sid = str(row.get("source_sha256", ""))
            if not source or sid != sha256_text(source):
                raise SystemExit(f"invalid source identity at {path}:{line_no}")
            if sid in seen:
                continue
            seen.add(sid)
            stream.write(json.dumps({
                "source": source,
                "source_sha256": sid,
                "bundle": row.get("bundle", ""),
                "key": row.get("item_key", ""),
                "task": "ASSETS_TEXT",
                "asset_version": row.get("asset_version"),
                "source_client_version": row.get("source_client_version"),
                "translation": "",
                "status": "pending",
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    print(json.dumps({"queue": str(output), "items": count,
                      "asset_version": wanted_version or None}, ensure_ascii=False))
    return 0


def apply(args: argparse.Namespace) -> int:
    translations: dict[str, str] = {}
    for line in args.draft.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        source = str(row.get("source", ""))
        sid = str(row.get("source_sha256", ""))
        translation = str(row.get("translation", ""))
        if not source or sid != sha256_text(source) or not translation:
            continue
        validate_translation(source, translation)
        if "|" in translation or "^" in translation:
            raise SystemExit(f"LLM output contains reserved delimiter for {sid}")
        prior = translations.get(sid)
        if prior is not None and prior != translation:
            raise SystemExit(f"conflicting LLM output for source {sid}")
        translations[sid] = translation

    if not translations:
        print(json.dumps({"updated": 0, "drafts": 0}, ensure_ascii=False))
        return 0

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    updated = 0
    for path in sorted((ROOT / "locales").rglob("*.jsonl")):
        original = path.read_text(encoding="utf-8")
        changed = False
        lines = []
        for line in original.splitlines():
            if not line.strip():
                lines.append(line)
                continue
            row: dict[str, Any] = json.loads(line)
            sid = str(row.get("source_sha256", ""))
            translation = translations.get(sid)
            if row.get("status") == "untranslated" and translation is not None:
                if str(row.get("ja", "")) != "":
                    validate_translation(str(row["ja"]), translation)
                row["zh"] = translation
                row["status"] = "pending"
                row["translation_stage"] = "llm_translated"
                row["updated_at"] = timestamp
                changed = True
                updated += 1
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"updated": updated, "drafts": len(translations),
                      "stage": "llm_translated"}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--output", type=Path, required=True)
    collect_parser.add_argument("--asset-version", default="")
    collect_parser.set_defaults(func=collect)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--draft", type=Path, required=True)
    apply_parser.set_defaults(func=apply)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
