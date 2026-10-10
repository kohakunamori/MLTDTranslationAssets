#!/usr/bin/env python3
"""Commit the translation rows a run produced, and publish them to main.

This is the one place that writes LLM output into the repository, called at two
moments for two different reasons:

* **right after** ``llm_translate_untranslated.py apply`` validated -- the applied
  rows are ``pending``, and no release reads a ``pending`` row until ``publish``
  promotes it.  Committing them here is what makes a night's work survivable: run
  38058410546 applied 2,937 rows and lost every one of them because the failure
  came later, in the validate step that follows; only its diagnostics artifact
  saved the batch, and recovering it took a human.
* **after** ``publish`` -- the same rows, now ``accepted``, with the provenance
  ``translation_stage=llm_translated`` preserved.

The commit message is derived from the run's own ``.llm-publish*.json`` reports,
so the early call cannot claim a promotion that has not happened yet, and the
late call records what the promotion actually did.  Running with nothing staged
is not an error: it prints ``No changes to publish.`` and exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

#: Tracked paths this step owns.  ``locales/`` and ``lyrics/`` are the
#: translation libraries; the manifests carry what the discovery pass learned.
BASE_PATHS = ("locales", "lyrics", "manifests/asset-version.json")
OPTIONAL_PATHS = (
    # The discovery baseline: only changes when the game introduces a new
    # resource family, and that is exactly the event worth committing.
    "manifests/official-bundle-index.json",
    "manifests/official-asset-inventory.json",
)

#: A push can lose a race with the generated-assets workflow writing to main.
#: Never force-push: rebase the local commit and try again.
PUSH_ATTEMPTS = 3


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)


def _record_published() -> None:
    """Tell the job that main moved, so the build can be dispatched.

    A push made with ``GITHUB_TOKEN`` does not start another workflow, so
    ``llm-translate-assets.yml`` dispatches the generated-assets build itself and
    needs to know whether this run actually published anything.
    """
    handle = os.environ.get("GITHUB_OUTPUT")
    if handle:
        with open(handle, "a", encoding="utf-8") as stream:
            stream.write("published=true\n")


def stage_paths(root: Path = ROOT) -> list[str]:
    return [path for path in BASE_PATHS + OPTIONAL_PATHS if (root / path).exists()]


def _promoted(report: Path) -> int:
    if not report.is_file():
        return 0
    try:
        return int(json.loads(report.read_text(encoding="utf-8")).get("promoted", 0))
    except (ValueError, OSError):
        return 0


def commit_messages(root: Path = ROOT) -> tuple[str, str | None]:
    """The subject and body for the state this run is actually in."""
    version = json.loads((root / "manifests/asset-version.json").read_text(encoding="utf-8"))["asset_version"]
    promoted = _promoted(root / ".llm-publish.json")
    lyric_promoted = _promoted(root / ".llm-publish-lyrics.json")
    if promoted > 0 or lyric_promoted > 0:
        return (
            f"ci(assets): apply and publish LLM drafts for asset {version}",
            "Machine drafts admitted to the build: "
            f"{promoted} text row(s); song lyrics: {lyric_promoted} row(s). "
            "translation_stage stays llm_translated.",
        )
    return f"ci(assets): apply LLM translations for asset {version}", None


def publish(root: Path = ROOT, *, dry_run: bool = False) -> int:
    paths = stage_paths(root)
    if not paths:
        print("No translation paths exist; nothing to publish.")
        return 0
    status = _git(root, "status", "--porcelain", "--", *paths)
    if not status.stdout.strip():
        print("No changes to publish.")
        return 0
    subject, body = commit_messages(root)
    if dry_run:
        print(json.dumps({"paths": paths, "subject": subject, "body": body}, ensure_ascii=False))
        return 0
    _git(root, "config", "user.name", BOT_NAME)
    _git(root, "config", "user.email", BOT_EMAIL)
    _git(root, "add", *paths)
    commit = ["commit", "-m", subject] + (["-m", body] if body else [])
    result = _git(root, *commit)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        return result.returncode
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        if _git(root, "push", "origin", "HEAD:main").returncode == 0:
            print(f"Published LLM translations to main on attempt {attempt}.")
            _record_published()
            return 0
        if attempt == PUSH_ATTEMPTS:
            sys.stderr.write("main advanced during LLM publish; refusing to force-push.\n")
            return 1
        _git(root, "fetch", "origin", "main")
        rebase = _git(root, "rebase", "origin/main")
        if rebase.returncode != 0:
            sys.stderr.write(rebase.stdout + rebase.stderr)
            return rebase.returncode
        validation = subprocess.run(
            [sys.executable, str(root / "scripts" / "validate_repo.py")], cwd=root
        )
        if validation.returncode != 0:
            return validation.returncode
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report the staged paths and the message without committing")
    args = parser.parse_args(argv)
    return publish(ROOT, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
