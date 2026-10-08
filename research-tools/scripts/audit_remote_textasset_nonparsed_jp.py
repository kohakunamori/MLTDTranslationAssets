#!/usr/bin/env python3
"""Audit non-parsed remote CSV/TSV TextAssets for JP localization candidates.

The canonical Unity DB already preserves the exact TextAsset m_Script string in
unity_object.normalized_json. This audit reconstructs and SHA-verifies each
csv-like/tsv payload, splits it into bounded cells/lines, and reports Japanese
values that are not present in the confirmed GTX + non-GTX localization union.

It is discovery-only: candidates are not auto-promoted because many remote
structured files are animation/sway/resource metadata rather than visible UI.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import zlib
from collections import Counter
from pathlib import Path
from typing import Iterable

JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")
MAX_CANDIDATE_CHARS = 4096


def iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row


def known_ids(paths: list[Path]) -> set[str]:
    result: set[str] = set()
    for path in paths:
        for row in iter_jsonl(path) or ():
            source = str(row.get("source", ""))
            sid = str(row.get("source_sha256", "")).strip()
            if not sid and source:
                sid = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if sid:
                result.add(sid)
    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def reconstruct_raw_text(
    text: str, expected_size: int, expected_sha256: str
) -> tuple[bytes | None, str | None]:
    for label, data in (
        ("utf-8-no-bom", text.encode("utf-8")),
        ("utf-8-with-bom", text.encode("utf-8-sig")),
    ):
        if len(data) == expected_size and sha256_bytes(data) == expected_sha256:
            return data, label
    return None, None


def candidate_values(text: str, classification: str) -> Iterable[str]:
    delimiter = "\t" if classification == "tsv" else ","
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for row in reader:
            for cell in row:
                value = cell.strip().strip("\ufeff")
                if (
                    value
                    and len(value) <= MAX_CANDIDATE_CHARS
                    and JP_KANA_RE.search(value)
                ):
                    yield value
    except csv.Error:
        # Malformed structured files still get a conservative line-level scan.
        for line in text.splitlines():
            value = line.strip().strip("\ufeff")
            if (
                value
                and len(value) <= MAX_CANDIDATE_CHARS
                and JP_KANA_RE.search(value)
            ):
                yield value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", type=Path, default=Path("build/current-unity-assets.sqlite"))
    ap.add_argument(
        "--known",
        type=Path,
        action="append",
        default=[
            Path("build/localization-90200/translation-memory.jsonl"),
            Path("build/localization-90200/machine-translation-nongtx-queue.jsonl"),
        ],
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/remote-textasset-nonparsed-jp-audit.json"),
    )
    ap.add_argument("--progress-every", type=int, default=1000)
    args = ap.parse_args()

    known = known_ids(args.known)
    con = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True, timeout=120)
    con.execute("PRAGMA query_only=ON")

    query = """
        SELECT t.object_id,o.logical_name,o.object_name,t.classification,
               t.payload_size,t.payload_sha256,o.normalized_json
        FROM unity_textasset t
        JOIN unity_object o ON o.object_id=t.object_id
        WHERE t.classification IN ('csv-like','tsv') AND t.parsed_json IS NULL
        ORDER BY t.object_id
    """

    rows: dict[str, dict] = {}
    classifications = Counter()
    namespaces = Counter()
    scanned = decoded = sha_verified = candidate_occurrences = known_occurrences = 0
    payload_bytes = 0
    errors: list[dict] = []

    for object_id, logical, object_name, classification, size, expected_sha, blob in con.execute(query):
        scanned += 1
        classifications[str(classification)] += 1
        if blob is None:
            errors.append({"object_id": object_id, "logical_name": logical, "error": "normalized_json_missing"})
            continue
        try:
            tree = json.loads(zlib.decompress(blob).decode("utf-8"))
            text = tree.get("m_Script") if isinstance(tree, dict) else None
        except Exception as exc:
            errors.append({"object_id": object_id, "logical_name": logical, "error": f"decode:{type(exc).__name__}:{exc}"})
            continue
        if not isinstance(text, str):
            errors.append({"object_id": object_id, "logical_name": logical, "error": "missing_string_m_Script"})
            continue
        raw, reconstruction = reconstruct_raw_text(text, int(size or 0), str(expected_sha or "").upper())
        if raw is None:
            errors.append({"object_id": object_id, "logical_name": logical, "error": "payload_sha_mismatch"})
            continue
        sha_verified += 1
        decoded += 1
        payload_bytes += len(raw)
        namespace = str(logical).split("_", 1)[0] if "_" in str(logical) else str(logical)
        for value in candidate_values(text, str(classification)):
            candidate_occurrences += 1
            sid = hashlib.sha256(value.encode("utf-8")).hexdigest()
            if sid in known:
                known_occurrences += 1
                continue
            namespaces[namespace] += 1
            row = rows.setdefault(
                sid,
                {
                    "source_sha256": sid,
                    "source": value,
                    "occurrences": 0,
                    "examples": [],
                },
            )
            row["occurrences"] += 1
            if len(row["examples"]) < 3:
                row["examples"].append(
                    {
                        "logical_name": logical,
                        "object_name": object_name,
                        "classification": classification,
                    }
                )
        if args.progress_every and decoded % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "decoded": decoded,
                        "unknown_unique": len(rows),
                        "candidate_occurrences": candidate_occurrences,
                        "errors": len(errors),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    con.close()
    values = sorted(rows.values(), key=lambda row: (-row["occurrences"], row["source"]))
    result = {
        "schema_version": 1,
        "kind": "mltd-remote-textasset-nonparsed-jp-audit",
        "database": str(args.database),
        "known_source_ids": len(known),
        "scope": "all canonical csv-like/tsv TextAssets with exact reconstructed m_Script payloads",
        "objects_scanned": scanned,
        "decoded_payloads": decoded,
        "sha_verified_payloads": sha_verified,
        "decoded_payload_bytes": payload_bytes,
        "classification_histogram": dict(classifications),
        "jp_candidate_occurrences": candidate_occurrences,
        "known_occurrences": known_occurrences,
        "unknown_unique": len(values),
        "unknown_namespace_histogram": namespaces.most_common(50),
        "errors": errors,
        "rows": values,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
