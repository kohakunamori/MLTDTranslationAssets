"""Decide whether ``generated/<asset_version>/`` at HEAD is already up to date.

The generated release is a pure function of repository inputs: the tracked
version manifest, the locale rows, the image/lyrics/schema inputs and the
scripts that write the bundles.  When the release committed at HEAD was built
from the same inputs, rebuilding it produces the same content-addressed objects
and burns a full runner slot for nothing, so ``assets-generated.yml`` asks this
script first and skips the pipeline when the answer is "already current".

The fingerprint is deliberately narrower than "the whole repository":

* ``RELEASE_INPUT_TREES`` and ``RELEASE_INPUT_FILES`` below are the inputs that
  actually change release bytes;
* the workflow procedure itself is **not** part of the fingerprint.  Editing how
  the build is invoked does not change the content it produces, and treating it
  as an input would force a full rebuild for every CI tweak.  Use the workflow's
  ``force`` dispatch input after changing the build procedure on purpose.

Anything that cannot be proven -- no release, an unreadable commit, a dirty
working tree (``promote_merged_locales.py`` rewrites locales before this runs),
a missing path in the built-from commit, or any git error -- answers
``build: true``.  The decision is fail-open; a wrong "skip" would ship a stale
release, a wrong "build" only costs time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Trees whose content ends up in the release bytes.
RELEASE_INPUT_TREES = (
    "locales",
    "manifests",
    "pipelines",
    "schema",
    "images",
    "lyrics",
)

# Scripts invoked by the build workflow that can change release bytes.
RELEASE_INPUT_FILES = (
    "scripts/build_generated_release.py",
    "scripts/assets_generated_index.py",
    "scripts/promote_merged_locales.py",
    "scripts/build_portal_resource_manifest.py",
)

# Files the build itself writes.  `portal-resource-manifest.json` carries a
# `generated_at` timestamp, so counting it as an input would make every release
# look out of date one build later: the gate would never skip anything.
DERIVED_OUTPUTS = (
    "manifests/portal-resource-manifest.json",
)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def digest(commit: str, path: str) -> str | None:
    """Content digest of ``path`` in ``commit``, ignoring derived outputs.

    Trees and single files are handled the same way: ``ls-tree`` lists the
    ``mode type object name`` line of every file below the path.  Returns None
    when the path cannot be read there (absent, empty after filtering, or an
    unreadable commit), which the caller treats as "cannot prove reuse".
    """
    result = git("ls-tree", "-r", "-z", commit, "--", path)
    if result.returncode != 0:
        return None
    entries = []
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        meta, _, name = entry.partition("\t")
        if name in DERIVED_OUTPUTS:
            continue
        entries.append(f"{meta}\t{name}")
    if not entries:
        return None
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def changed_paths(paths: tuple[str, ...]) -> list[str]:
    """Release inputs whose working-tree content differs from HEAD."""
    result = git("status", "--porcelain", "--", *paths)
    if result.returncode != 0:
        return ["<git status unavailable>"]
    names = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name = line[3:].strip().strip('"')
        if name in DERIVED_OUTPUTS:
            continue
        names.append(name)
    return names


def decide(force: bool = False, release_root: Path | None = None) -> dict:
    root = release_root or ROOT / "generated"
    decision: dict = {"build": True, "reason": "", "forced": bool(force)}

    version_manifest_path = ROOT / "manifests" / "asset-version.json"
    try:
        version_manifest = json.loads(version_manifest_path.read_text(encoding="utf-8"))
        asset_version = str(version_manifest["asset_version"])
    except (OSError, ValueError, KeyError) as error:
        decision["reason"] = f"cannot read the tracked version manifest: {error}"
        return decision
    decision["asset_version"] = asset_version

    manifest_path = root / asset_version / "manifest.json"
    decision["release_manifest"] = manifest_path.relative_to(ROOT).as_posix()
    if not manifest_path.is_file():
        decision["reason"] = "no generated release is committed for the tracked version"
        return decision
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        decision["reason"] = f"unreadable generated manifest: {error}"
        return decision

    if str(manifest.get("asset_version")) != asset_version:
        decision["reason"] = (
            f"release tracks {manifest.get('asset_version')} but the manifest tracks {asset_version}"
        )
        return decision
    if manifest.get("build_status") != "success":
        decision["reason"] = f"release build_status is {manifest.get('build_status')!r}"
        return decision

    built_from = str(manifest.get("translation_commit") or "")
    decision["built_from"] = built_from
    if not built_from or git("cat-file", "-e", f"{built_from}^{{commit}}").returncode != 0:
        decision["reason"] = "the commit the release was built from is not available"
        return decision

    if force:
        decision["reason"] = "forced by the caller"
        return decision

    dirty = changed_paths(RELEASE_INPUT_TREES + RELEASE_INPUT_FILES)
    decision["dirty_paths"] = dirty
    if dirty:
        decision["reason"] = "the working tree changed release inputs after checkout: " + ", ".join(dirty[:5])
        return decision

    differences: list[str] = []
    current = git("rev-parse", "HEAD").stdout.strip()
    decision["head"] = current
    for path in RELEASE_INPUT_TREES + RELEASE_INPUT_FILES:
        before = digest(built_from, path)
        after = digest("HEAD", path)
        if before is None or after is None or before != after:
            differences.append(path)
    decision["differences"] = differences
    if differences:
        decision["reason"] = "release inputs changed: " + ", ".join(differences[:5])
        return decision

    decision["build"] = False
    decision["reason"] = (
        f"generated/{asset_version}/ was built from the same release inputs as HEAD"
    )
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", default="false",
                        help="'true' to build regardless of the fingerprint")
    parser.add_argument("--release-root", type=Path, default=None)
    args = parser.parse_args()
    decision = decide(force=str(args.force).strip().lower() in {"1", "true", "yes", "on"},
                      release_root=args.release_root)
    print(json.dumps(decision, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
