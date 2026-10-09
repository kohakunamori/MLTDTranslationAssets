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
import sys
from pathlib import Path
from typing import Any

# The repository's own gate is the authority on what may be published, so the
# promotion reuses its protected-token check instead of re-implementing it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_repo import validate_translation_tokens  # noqa: E402


PROMOTABLE = {"pending", "untranslated"}
# `translation_stage` values written by the LLM pipeline.  A sweep must not turn
# those into `accepted`: the merge of a machine draft is not a human review.
MACHINE_STAGES = {"llm_translated", "machine_translated", "llm_draft"}


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


def _promotable(row: dict[str, Any], *, allow_machine: bool) -> bool:
    if row.get("status") not in PROMOTABLE or not str(row.get("zh", "")):
        return False
    # Machine drafts are not a review decision: `--sweep` leaves them pending so a
    # human still has to look at them, and only an explicit --allow-machine-drafts
    # publishes them.
    if not allow_machine and str(row.get("translation_stage") or "") in MACHINE_STAGES:
        return False
    return True


def _tokens_agree(row: dict[str, Any]) -> bool:
    """Whether `validate_repo.py` would accept this row's translation.

    Promotion is publication: a row whose translation drops a protected token
    (`{$P$}`, `<size=..>`, `%s`, `\\01\\`) fails the repository gate, so it must
    stay pending for a human instead of being swept into the build.
    """
    try:
        validate_translation_tokens(str(row.get("ja", "")), str(row.get("zh", "")))
    except ValueError:
        return False
    return True


def promote_file(
    root: Path,
    relative: str,
    before: str | None,
    dry_run: bool,
    *,
    allow_machine: bool = False,
) -> tuple[int, int]:
    """Return (promoted, skipped_for_tokens) for one locale file."""
    path = root / relative
    if not path.is_file():
        return 0, 0
    if before is None:
        # Sweep mode: every row is a candidate, not just the ones this push touched.
        previous: dict[tuple[str, str, str], dict[str, Any]] = {}
    else:
        try:
            before_text = git("show", f"{before}:{relative}", cwd=root)
        except subprocess.CalledProcessError:
            # An added locale file has no parent version; every row is new.
            before_text = ""
        previous = parse_rows(before_text, f"{before}:{relative}") if before_text else {}

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    promoted = 0
    skipped_tokens = 0
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
        if not _promotable(row, allow_machine=allow_machine):
            continue
        if not _tokens_agree(row):
            skipped_tokens += 1
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
    return promoted, skipped_tokens


def promote(root: Path, before: str, after: str, dry_run: bool = False) -> dict[str, Any]:
    if not before or before == "0" * 40:
        return {
            "files": [],
            "promoted": 0,
            "skipped_protected_tokens": 0,
            "dry_run": dry_run,
        }
    files = changed_locale_files(root, before, after)
    results = {relative: promote_file(root, relative, before, dry_run) for relative in files}
    return {
        "files": [path for path, (count, _) in results.items() if count],
        "promoted": sum(count for count, _ in results.values()),
        "skipped_protected_tokens": sum(skipped for _, skipped in results.values()),
        "dry_run": dry_run,
    }


def sweep(
    root: Path, dry_run: bool = False, *, allow_machine: bool = False
) -> dict[str, Any]:
    """Promote every eligible row in the repository, not only a push's diff.

    Rows that were merged before this helper existed never pass through the diff
    path, so they stay `pending` forever even though they carry reviewed text.
    Machine drafts, and rows whose translation would fail the repository's own
    protected-token gate, stay pending for a human.
    """
    files = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "locales").rglob("*.jsonl")
        if path.is_file()
    )
    results = {
        relative: promote_file(
            root, relative, None, dry_run, allow_machine=allow_machine
        )
        for relative in files
    }
    machine_pending = token_pending = 0
    for relative in files:
        for line in (root / relative).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            if row.get("status") not in PROMOTABLE or not str(row.get("zh", "")):
                continue
            if not _promotable(row, allow_machine=allow_machine):
                machine_pending += 1
            elif not _tokens_agree(row):
                token_pending += 1
    return {
        "files": [path for path, (count, _) in results.items() if count],
        "promoted": sum(count for count, _ in results.values()),
        "machine_drafts_left_pending": machine_pending,
        "protected_token_mismatch_left_pending": token_pending,
        "scanned_files": len(files),
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--before", help="parent commit before the reviewed push")
    parser.add_argument("--after", help="commit being validated or published")
    parser.add_argument("--dry-run", action="store_true", help="report rows without editing files")
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="promote every eligible row in the repository instead of a git range",
    )
    parser.add_argument(
        "--allow-machine-drafts",
        action="store_true",
        help="with --sweep, also promote rows whose translation_stage is machine output",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if args.sweep:
        result = sweep(root, args.dry_run, allow_machine=args.allow_machine_drafts)
        result["mode"] = "sweep"
    else:
        if not args.before or not args.after:
            parser.error("--before and --after are required unless --sweep is used")
        result = promote(root, args.before, args.after, args.dry_run)
        result["mode"] = "range"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
