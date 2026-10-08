#!/usr/bin/env python3
"""Audit locally cached high-risk non-GTX TextAssets for Japanese text.

This is read-only. It scans only candidate bundles already present in the local
asset cache, extracts the nominated TextAsset payload, decodes UTF-8/CP932 text,
and reports Japanese strings that are not already represented in known
localization source inventories.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import UnityPy

JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
TEXTISH_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9]")
MAX_STRING = 12000


def sid(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iter_jsonl_ids(path: Path) -> Iterable[str]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            source_id = str(row.get("source_sha256", "")).strip()
            source = str(row.get("source", ""))
            if source_id:
                yield source_id
            elif source:
                yield sid(source)


def decode_payload(raw: bytes) -> tuple[str | None, str | None]:
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if "\x00" in text and text.count("\x00") > max(4, len(text) // 20):
            continue
        return text, enc
    return None, None


def collect_json_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        s = value.strip()
        if s and len(s) <= MAX_STRING and JP_RE.search(s):
            out.append(s)
    elif isinstance(value, list):
        for item in value:
            collect_json_strings(item, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and JP_RE.search(k):
                out.append(k.strip())
            collect_json_strings(v, out)


def extract_strings(text: str) -> list[str]:
    out: list[str] = []
    stripped = text.strip()
    if not stripped:
        return out
    if stripped[:1] in "[{":
        try:
            value = json.loads(stripped)
        except Exception:
            value = None
        if value is not None:
            collect_json_strings(value, out)
            return out
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) > MAX_STRING or not JP_RE.search(s):
            continue
        # Exclude obvious binary-decoding garbage while keeping normal JSON-ish text.
        printable = sum(ch.isprintable() or ch in "\t" for ch in s)
        if printable / max(1, len(s)) < 0.9:
            continue
        out.append(s)
    return out


def read_candidate_payload(bundle: Path, path_id: int) -> bytes | None:
    env = UnityPy.load(str(bundle))
    for obj in env.objects:
        if obj.type.name != "TextAsset" or int(obj.path_id) != path_id:
            continue
        data = obj.read()
        value = getattr(data, "m_Script", b"")
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, memoryview):
            return value.tobytes()
        return bytes(value)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", type=Path, default=Path("build/localization-90200/nongtx-highrisk-textasset-candidates.json"))
    ap.add_argument("--asset-root", type=Path, default=Path("work/local-assets/jp-android"))
    ap.add_argument("--workspace", type=Path, default=Path("build/localization-90200"))
    ap.add_argument("--output", type=Path, default=Path("build/localization-90200/nongtx-local-textasset-jp-audit.json"))
    args = ap.parse_args()

    doc = json.loads(args.candidates.read_text(encoding="utf-8-sig"))
    rows = doc.get("rows", [])
    known = set()
    for name in (
        "translation-memory.jsonl",
        "fontrender-translation-queue.jsonl",
        "apk-bi-jp-translation-queue.jsonl",
        "event-unit-translation-queue.jsonl",
    ):
        known.update(iter_jsonl_ids(args.workspace / name) or ())

    counts = Counter()
    unique: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    prefixes = Counter()

    for row in rows:
        remote = str(row.get("remote", ""))
        logical = str(row.get("logical", ""))
        try:
            path_id = int(row.get("path_id"))
        except Exception:
            counts["bad_candidate"] += 1
            continue
        bundle = args.asset_root / remote
        if not bundle.is_file():
            counts["missing_bundle"] += 1
            continue
        counts["local_candidates"] += 1
        try:
            raw = read_candidate_payload(bundle, path_id)
        except Exception as exc:
            counts["load_error"] += 1
            if len(errors) < 100:
                errors.append({"logical": logical, "remote": remote, "path_id": path_id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if raw is None:
            counts["textasset_not_found"] += 1
            continue
        text, encoding = decode_payload(raw)
        if text is None:
            counts["nontext_payload"] += 1
            continue
        counts[f"encoding:{encoding}"] += 1
        strings = extract_strings(text)
        if not strings:
            counts["decoded_without_jp"] += 1
            continue
        counts["objects_with_jp"] += 1
        prefixes[str(row.get("prefix1", ""))] += 1
        for source in strings:
            source_id = sid(source)
            entry = unique.get(source_id)
            if entry is None:
                entry = {
                    "source_sha256": source_id,
                    "source": source,
                    "known_localization_source": source_id in known,
                    "occurrences": 0,
                    "examples": [],
                }
                unique[source_id] = entry
            entry["occurrences"] += 1
            if len(entry["examples"]) < 4:
                entry["examples"].append({
                    "logical": logical,
                    "remote": remote,
                    "path_id": path_id,
                    "prefix1": row.get("prefix1"),
                    "prefix2": row.get("prefix2"),
                    "encoding": encoding,
                })

    new_rows = [v for v in unique.values() if not v["known_localization_source"]]
    result = {
        "schema_version": 1,
        "kind": "mltd-local-cached-highrisk-textasset-jp-audit",
        "candidate_rows": len(rows),
        "asset_root": str(args.asset_root),
        "known_source_ids": len(known),
        "counts": dict(counts),
        "objects_with_jp_by_prefix1": dict(prefixes),
        "unique_jp_strings": len(unique),
        "known_unique": len(unique) - len(new_rows),
        "new_unique": len(new_rows),
        "errors": errors,
        "new_rows": sorted(new_rows, key=lambda x: (-int(x["occurrences"]), x["source"])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {"new_rows", "errors"}}, ensure_ascii=False, indent=2))
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
