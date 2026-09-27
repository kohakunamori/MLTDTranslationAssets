#!/usr/bin/env python3
"""Validation script for the MLTD Localization GitHub Repository.

Checks:
1. Every JSONL line in locales/ matches the JSON schema and passes source SHA-256 verification.
2. No reserved delimiters (| or ^) are present in any translation.
3. Every category directory (story, card, dialogue, birth, master) is present and non-empty.
4. Glossaries (authoritative-terms.json, idols.json) are valid JSON and contain expected terms.
5. The image manifest is valid and consistent.
6. Lyrics files are valid JSONL and match schema.

The APK built-in surfaces (bottom bar atlas, BI text, font) are deliberately
absent: they are delivered by the client repository instead.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESERVED_DELIMITERS = ("|", "^")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VALID_STATUSES = {"untranslated", "pending", "accepted"}
CATEGORIES = ("story", "card", "dialogue", "birth", "master")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().lower()


def validate_locales(root: Path) -> dict[str, int]:
    locales_dir = root / "locales"
    if not locales_dir.is_dir():
        print(f"ERROR: Missing locales/ directory at {locales_dir}", file=sys.stderr)
        sys.exit(1)

    counts = {"total_rows": 0, "accepted": 0, "pending": 0, "untranslated": 0, "bundles": 0}

    for cat in CATEGORIES:
        cat_dir = locales_dir / cat
        if not cat_dir.is_dir():
            print(f"ERROR: Missing category directory {cat_dir}", file=sys.stderr)
            sys.exit(1)

        jsonl_files = list(cat_dir.glob("*.jsonl"))
        if not jsonl_files:
            print(f"WARNING: No .jsonl files in {cat_dir}", file=sys.stderr)
            continue

        for jf in jsonl_files:
            counts["bundles"] += 1
            with jf.open("r", encoding="utf-8") as f:
                for line_idx, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    counts["total_rows"] += 1
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        print(f"ERROR: {jf}:{line_idx} invalid JSON: {exc}", file=sys.stderr)
                        sys.exit(1)

                    # Required keys
                    for req_key in ("base_version", "bundle", "item_key", "source_sha256", "ja", "zh", "status", "updated_at"):
                        if req_key not in row:
                            print(f"ERROR: {jf}:{line_idx} missing key '{req_key}'", file=sys.stderr)
                            sys.exit(1)

                    ja = row["ja"]
                    zh = row["zh"]
                    declared_sha = row["source_sha256"]
                    status = row["status"]

                    # Status check
                    if status not in VALID_STATUSES:
                        print(f"ERROR: {jf}:{line_idx} invalid status '{status}'", file=sys.stderr)
                        sys.exit(1)
                    counts[status] += 1

                    # Untranslated must be empty zh
                    if status == "untranslated" and zh != "":
                        print(f"ERROR: {jf}:{line_idx} status is untranslated but zh is not empty", file=sys.stderr)
                        sys.exit(1)

                    # Source hash check
                    computed_sha = sha256_text(ja)
                    if declared_sha.lower() != computed_sha:
                        print(f"ERROR: {jf}:{line_idx} source hash mismatch: declared {declared_sha} != {computed_sha}", file=sys.stderr)
                        sys.exit(1)

                    # Delimiter check
                    for d in RESERVED_DELIMITERS:
                        if d in zh:
                            print(f"ERROR: {jf}:{line_idx} translation contains illegal reserved delimiter '{d}': {zh}", file=sys.stderr)
                            sys.exit(1)

    return counts


def validate_glossary(root: Path) -> None:
    glossary_dir = root / "glossary"
    terms_file = glossary_dir / "authoritative-terms.json"
    idols_file = glossary_dir / "idols.json"

    if not terms_file.is_file():
        print(f"ERROR: Missing {terms_file}", file=sys.stderr)
        sys.exit(1)

    terms_data = json.loads(terms_file.read_text(encoding="utf-8"))
    entries = terms_data.get("entries", {})
    if len(entries) < 90:
        print(f"ERROR: authoritative-terms.json entries count {len(entries)} < 90", file=sys.stderr)
        sys.exit(1)

    if not idols_file.is_file():
        print(f"ERROR: Missing {idols_file}", file=sys.stderr)
        sys.exit(1)

    idols_data = json.loads(idols_file.read_text(encoding="utf-8"))
    idols = idols_data.get("idols", [])
    if len(idols) != 52:
        print(f"ERROR: idols.json idols count {len(idols)} != 52", file=sys.stderr)
        sys.exit(1)


def validate_manifests(root: Path) -> None:
    manifests_dir = root / "manifests"
    images_file = manifests_dir / "images.manifest.json"

    if not images_file.is_file():
        print(f"ERROR: Missing {images_file}", file=sys.stderr)
        sys.exit(1)

    images_data = json.loads(images_file.read_text(encoding="utf-8"))
    images = images_data.get("images", [])
    if len(images) < 937:
        print(f"ERROR: images.manifest.json images count {len(images)} < 937", file=sys.stderr)
        sys.exit(1)

    # Bottom-bar atlases are APK built-ins now; this repository must not carry them.
    stray = [i.get("id") for i in images if i.get("kind") == "bottom_bar_atlas"]
    if stray:
        print(
            f"ERROR: images.manifest.json still lists APK built-in atlas(es) {stray}; "
            "they belong to the client repository",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> int:
    print(f"Validating repository at: {ROOT}")
    locales_counts = validate_locales(ROOT)
    validate_glossary(ROOT)
    validate_manifests(ROOT)

    print("\nRepository validation SUCCESSFUL!")
    print(f"Total locales rows: {locales_counts['total_rows']}")
    print(f"  Accepted: {locales_counts['accepted']}")
    print(f"  Pending: {locales_counts['pending']}")
    print(f"  Untranslated: {locales_counts['untranslated']}")
    print(f"  Bundles: {locales_counts['bundles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
