"""Publish verified outputs without rebasing generated manifests.

Queued builds can start from an older checkout, and translation jobs can advance
main while a build is running. Generated JSON is a snapshot, not mergeable source:
publish it on the fetched main only when its release inputs still match the build
checkout. Otherwise ask the workflow to build main again. A normal push provides
the final compare-and-swap; a rejected push repeats the input check.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from should_build_release import DERIVED_OUTPUTS, RELEASE_INPUT_FILES, RELEASE_INPUT_TREES

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATHS = ("generated", "locales", "manifests/portal-resource-manifest.json")
INPUT_PATHS = RELEASE_INPUT_TREES + RELEASE_INPUT_FILES + tuple(
    f":(exclude){path}" for path in DERIVED_OUTPUTS
)
MESSAGE = "build(assets): publish reviewed translations and generated release [skip ci]"


def git(root: Path, *args: str, env: dict | None = None,
        data: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args], env=env, input=data,
        capture_output=True, text=True, encoding="utf-8", errors="strict",
    )
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


def inputs_match(root: Path, before: str, after: str) -> bool:
    result = git(root, "diff", "--quiet", before, after, "--", *INPUT_PATHS, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Cannot compare release inputs: {result.stderr.strip()}")
    return result.returncode == 0


def release_current(root: Path, commit: str) -> bool:
    """A competing publisher may already have delivered these exact inputs."""
    try:
        version = json.loads(git(root, "show", f"{commit}:manifests/asset-version.json").stdout)
        asset_version = str(version["asset_version"])
        manifest = json.loads(git(root, "show", f"{commit}:generated/{asset_version}/manifest.json").stdout)
        built_from = manifest.get("translation_commit")
        if (str(manifest.get("asset_version")) != asset_version
                or manifest.get("build_status") != "success" or not built_from):
            return False
        return inputs_match(root, built_from, commit)
    except (RuntimeError, ValueError, KeyError):
        # Missing/unreadable evidence must never suppress this verified build.
        return False


def publish(root: Path = ROOT, *, force: bool = False, attempts: int = 3) -> dict:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    source = git(root, "rev-parse", "HEAD").stdout.strip()
    if git(root, "diff", "--cached", "--name-only").stdout.strip():
        raise RuntimeError("Refusing to publish with pre-existing staged changes")
    # With R2 configured, payloads must never enter a Git release, even if a
    # caller accidentally forced one into its own index in an earlier commit.
    output_paths = OUTPUT_PATHS
    if (root / 'generated/r2-storage.json').exists():
        output_paths += (':(exclude)generated/objects', ':(exclude)generated/build-inputs')
    # Use an isolated index: the caller's checkout and index are never rewritten,
    # and unrelated local edits/untracked files cannot enter the release commit.
    with tempfile.TemporaryDirectory(prefix="assets-publish-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        git(root, "read-tree", source, env=env)
        git(root, "add", "--", *output_paths, env=env)
        snapshot = git(root, "write-tree", env=env).stdout.strip()
        unchanged = snapshot == git(root, "rev-parse", f"{source}^{{tree}}").stdout.strip()

        # Record exact blob identities (including removals), never a textual merge
        # of two release manifests. --no-abbrev is required for update-index.
        raw = git(root, "diff", "--raw", "--no-abbrev", "--no-renames", "-z",
                  source, snapshot, "--", *output_paths).stdout.split("\0")
        records = []
        for position in range(0, len(raw) - 1, 2):
            metadata, path = raw[position:position + 2]
            _, mode, _, blob, _ = metadata.split()
            records.append(f"{mode} {blob}\t{path}\0")

        for attempt in range(1, attempts + 1):
            git(root, "fetch", "origin", "main")
            latest = git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            ancestor = git(root, "merge-base", "--is-ancestor", source, latest, check=False)
            if ancestor.returncode:
                raise RuntimeError("Build checkout is not an ancestor of origin/main")
            if not inputs_match(root, source, latest):
                return {"pushed": False, "rebuild": True, "reason": "main has newer release inputs",
                        "source": source, "latest": latest}
            if unchanged:
                return {"pushed": False, "rebuild": False, "reason": "no generated changes"}
            if (not force and latest != source and inputs_match(root, snapshot, latest)
                    and release_current(root, latest)):
                return {"pushed": False, "rebuild": False, "reason": "main already published these inputs",
                        "source": source, "latest": latest}

            git(root, "read-tree", latest, env=env)
            git(root, "update-index", "-z", "--index-info", data="".join(records), env=env)
            tree = git(root, "write-tree", env=env).stdout.strip()
            if tree == git(root, "rev-parse", f"{latest}^{{tree}}").stdout.strip():
                return {"pushed": False, "rebuild": False, "reason": "main already contains these outputs"}
            commit = git(root, "commit-tree", tree, "-p", latest, "-m", MESSAGE).stdout.strip()
            result = git(root, "push", "origin", f"{commit}:refs/heads/main", check=False)
            if result.returncode == 0:
                return {"pushed": True, "rebuild": False, "reason": "published verified outputs",
                        "commit": commit, "source": source, "attempt": attempt}
        raise RuntimeError(f"Generated publish failed after {attempts} normal pushes: {result.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", default="false")
    args = parser.parse_args()
    result = publish(ROOT, force=args.force.lower() in {"true", "1", "yes", "on"})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            for key in ("pushed", "rebuild"):
                handle.write(f"{key}={str(result[key]).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
