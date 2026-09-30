#!/usr/bin/env python3
"""Promote reviewed locale rows before the generated Assets build.

The Portal edits the translation text in a GitHub PR.  The PR merge is the
review decision; the source row may still carry ``pending`` or
``untranslated`` because it was not safe to mark it accepted before review.
This helper promotes only rows that changed in the supplied Git range and have
non-empty translations.  It is used in two places:

* pull-request CI runs it in the checkout as a preflight, so the exact merged
  result is validated without committing anything to the contributor branch;
* the post-merge Assets workflow runs it on ``main`` and commits the status
  transition together with ``generated/``.

Rows outside the diff are never touched.  Source hashes and the final
``validate_repo.py`` gate remain authoritative.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


PROMOTABLE = {"pending", "untranslated"}


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="strict",
    )
    return result.stdout


def canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("bundle", "")),
        str(row.get("item_key", "")),
        str(row.get("source_sha256", "")).lower(),
    )


def parse_rows(text: str, label: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label}:{line_no}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label}:{line_no}: expected an object")
        key = identity(row)
        if not all(key):
            raise ValueError(f"{label}:{line_no}: row has no complete bundle/item/source identity")
        if key in rows:
            raise ValueError(f"{label}:{line_no}: duplicate row identity {key!r}")
        rows[key] = row
    return rows


def changed_locale_files(root: Path, before: str, after: str) -> list[str]:
    output = git("diff", "--name-only", "--diff-filter=AM", before, after, "--", "locales", cwd=root)
    return sorted(
        path.strip()
        for path in output.splitlines()
        if path.strip().startswith("locales/") and path.strip().endswith(".jsonl")
    )


def source_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(str(row.get("ja", "")).encode("utf-8")).hexdigest()


def promote_file(root: Path, relative: str, before: str, dry_run: bool) -> int:
    path = root / relative
    if not path.is_file():
        return 0
    before_text = ""
    try:
        before_text = git("show", f"{before}:{relative}", cwd=root)
    except subprocess.CalledProcessError:
        # An added locale file has no parent version; every row is new.
        before_text = ""
    previous = parse_rows(before_text, f"{before}:{relative}") if before_text else {}

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    promoted = 0
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{relative}:{index + 1}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{relative}:{index + 1}: expected an object")
        key = identity(row)
        old = previous.get(key)
        if old is not None and canonical(old) == canonical(row):
            continue
        status = row.get("status")
        if status not in PROMOTABLE or not str(row.get("zh", "")):
            continue
        declared = str(row.get("source_sha256", "")).lower()
        actual = source_hash(row)
        if declared != actual:
            raise ValueError(
                f"{relative}:{index + 1}: source_sha256 does not match ja; refusing promotion"
            )
        row["status"] = "accepted"
        if "translation_stage" in row:
            row["translation_stage"] = "human_translated"
        newline = "\n" if raw.endswith("\n") else ""
        lines[index] = json.dumps(row, ensure_ascii=False) + newline
        promoted += 1

    if promoted and not dry_run:
        path.write_text("".join(lines), encoding="utf-8", newline="")
    return promoted


def promote(root: Path, before: str, after: str, dry_run: bool = False) -> dict[str, Any]:
    if not before or before == "0" * 40:
        return {"files": [], "promoted": 0, "dry_run": dry_run}
    files = changed_locale_files(root, before, after)
    counts = {relative: promote_file(root, relative, before, dry_run) for relative in files}
    return {
        "files": [path for path, count in counts.items() if count],
        "promoted": sum(counts.values()),
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--before", required=True, help="parent commit before the reviewed push")
    parser.add_argument("--after", required=True, help="commit being validated or published")
    parser.add_argument("--dry-run", action="store_true", help="report rows without editing files")
    args = parser.parse_args()
    result = promote(args.root.resolve(), args.before, args.after, args.dry_run)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
