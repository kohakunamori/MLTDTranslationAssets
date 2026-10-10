#!/usr/bin/env python3
"""Build the path -> object index that the Cloudflare gateway serves from.

The gateway keeps no copy of the release.  It carries a *table*: for every path
a client can ask for, which content-addressed object in the GitHub repository
holds the translated bytes.  A request that is in the table is fetched from the
repository (commit-pinned, so the bytes can never change underneath a cache) and
cached at Cloudflare's edge; a request that is not in the table is forwarded to
the official CDN.

That makes publishing a gateway release a *build + deploy* of one small file
rather than a copy of the release:

    python asset-server/cloudflare/build_index.py            # plan only
    python asset-server/cloudflare/build_index.py --apply    # write src/index.json
    cd asset-server/cloudflare && npx wrangler deploy

The planner is deliberately as strict as the old R2 publisher was: it refuses to
index a release whose manifest does not describe it, whose keys collide, or
whose objects are missing from (or do not hash to their name in) the local pool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
INDEX_PATH = HERE / "src" / "index.json"
ASSET_PREFIX = "production/2018/Android/"
DEFAULT_REPOSITORY = "kohakunamori/MLTDTranslationAssets"
DEFAULT_OBJECT_ROOT = "generated/objects/sha256"
VERSION_RE = re.compile(r"[0-9]{1,32}$")
CHUNK = 1 << 20


class PlanError(RuntimeError):
    """The repository cannot produce the index this plan claims."""


@dataclass
class Index:
    version: str
    source_commit: str
    repository: str
    object_root: str
    #: request path -> artifact sha256
    objects: dict[str, str] = field(default_factory=dict)
    #: request path -> byte size
    sizes: dict[str, int] = field(default_factory=dict)
    #: artifact sha256 -> byte size (one entry per stored object, aliases share)
    objects_bytes: dict[str, int] = field(default_factory=dict)
    entry_count: int = 0
    logical_keys: int = 0
    generated_at_utc: str | None = None

    @property
    def runtime_keys(self) -> int:
        return self.entry_count

    @property
    def total_bytes(self) -> int:
        """Bytes a client would actually transfer: aliases are not a second copy."""
        return sum(self.objects_bytes.values())

    def payload(self, *, built_at: str) -> dict:
        return {
            "schema_version": 1,
            "asset_version": self.version,
            "repository": self.repository,
            "source_commit": self.source_commit,
            "object_root": self.object_root,
            "entry_count": self.entry_count,
            "runtime_keys": self.runtime_keys,
            "logical_keys": self.logical_keys,
            "total_bytes": self.total_bytes,
            "generated_at_utc": self.generated_at_utc,
            "built_at": built_at,
            "objects": dict(sorted(self.objects.items())),
            # Digest -> byte size, so a HEAD can be answered from the table
            # without touching the repository at all.
            "sizes": dict(sorted(self.objects_bytes.items())),
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_version(root: Path, requested: str | None) -> str:
    if requested:
        version = str(requested).strip()
    else:
        marker = root / "manifests" / "asset-version.json"
        try:
            version = str(json.loads(marker.read_text(encoding="utf-8"))["asset_version"])
        except (OSError, ValueError, KeyError) as error:
            raise PlanError(f"cannot read the tracked asset version from {marker}: {error}")
    if not VERSION_RE.fullmatch(version):
        raise PlanError(f"refusing a version that is not purely numeric: {version!r}")
    return version


def detect_commit(root: Path, manifest_path: Path, requested: str | None) -> str:
    """The commit to pin raw.githubusercontent.com at.

    It is the commit that last touched this release's manifest -- the commit the
    objects were committed together with -- and not the manifest's own
    ``generated_commit`` field, which records the translation input rather than
    where the bytes landed.
    """
    if requested:
        commit = requested.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise PlanError(f"--commit must be a full 40-character sha: {commit!r}")
        return commit
    relative = manifest_path.relative_to(root).as_posix()
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H", "--", relative],
            capture_output=True, text=True,
        )
    except OSError as error:
        raise PlanError(f"cannot run git to resolve the pin commit: {error}")
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PlanError(
            f"cannot resolve the commit that last touched {relative} "
            f"(git said: {completed.stderr.strip() or completed.stdout.strip()!r})"
        )
    return commit


def build_index(
    root: Path,
    version: str,
    *,
    repository: str = DEFAULT_REPOSITORY,
    object_root: str = DEFAULT_OBJECT_ROOT,
    commit: str | None = None,
    include_logical_alias: bool = True,
    verify_hashes: bool = True,
) -> Index:
    """Read ``generated/<version>/manifest.json`` and prove it against the pool."""
    version_dir = root / "generated" / version
    manifest_path = version_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlanError(f"cannot read {manifest_path}: {error}")
    if str(manifest.get("asset_version")) != version:
        raise PlanError(
            f"{manifest_path} declares asset_version={manifest.get('asset_version')!r}, not {version!r}"
        )
    if manifest.get("build_status") != "success":
        raise PlanError(f"{manifest_path} build_status={manifest.get('build_status')!r}; refusing to publish")

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PlanError(f"{manifest_path} has no entries")

    pool = root / object_root
    index = Index(
        version=version,
        source_commit=detect_commit(root, manifest_path, commit),
        repository=repository,
        object_root=object_root,
        entry_count=len(entries),
        generated_at_utc=manifest.get("generated_at_utc"),
    )

    seen: dict[str, str] = {}
    for position, entry in enumerate(entries):
        digest = str(entry.get("artifact_sha256") or "")
        runtime_path = str(entry.get("runtime_path") or "")
        logical_path = str(entry.get("logical_path") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PlanError(f"entry {position} has a malformed artifact_sha256: {digest!r}")
        if not runtime_path:
            raise PlanError(f"entry {position} has no runtime_path")

        names = [runtime_path]
        if include_logical_alias and logical_path and logical_path != runtime_path:
            names.append(logical_path)

        for name in names:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                raise PlanError(f"entry {position} carries an unusable key: {name!r}")
            if not relative.as_posix().startswith(ASSET_PREFIX):
                # The gateway only rewrites paths under this prefix; anything
                # else would silently never match a request.
                raise PlanError(f"key outside {ASSET_PREFIX}: {name!r}")
            key = relative.as_posix()
            previous = seen.get(key)
            if previous is not None and previous != digest:
                raise PlanError(f"two different artifacts both claim the key {key!r}")
            seen[key] = digest
            index.objects[key] = digest

        source = pool / digest
        if not source.is_file():
            raise PlanError(f"entry {position} ({runtime_path}) is missing from the pool: {source}")
        size = source.stat().st_size
        if verify_hashes and sha256_file(source) != digest:
            raise PlanError(f"{source} does not hash to its own name")
        index.objects_bytes[digest] = size
        for name in names:
            index.sizes[PurePosixPath(name).as_posix()] = size

    index.logical_keys = len(index.objects) - index.entry_count
    return index


def write_index(index: Index, path: Path, *, built_at: str) -> int:
    payload = index.payload(built_at=built_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")
    return len(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--asset-version", default=None, help="default: manifests/asset-version.json")
    parser.add_argument("--commit", default=None, help="pin commit (default: last commit touching the manifest)")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY, help="owner/name on GitHub")
    parser.add_argument("--object-root", default=DEFAULT_OBJECT_ROOT)
    parser.add_argument("--out", type=Path, default=INDEX_PATH)
    parser.add_argument("--no-logical-alias", action="store_true", help="index runtime names only")
    parser.add_argument("--no-verify-hashes", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write the index (default: plan only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    try:
        version = detect_version(root, args.asset_version)
        index = build_index(
            root,
            version,
            repository=args.repository,
            object_root=args.object_root,
            commit=args.commit,
            include_logical_alias=not args.no_logical_alias,
            verify_hashes=not args.no_verify_hashes,
        )
    except PlanError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    payload = index.payload(built_at="")
    raw_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    plan = {
        "asset_version": index.version,
        "source_commit": index.source_commit,
        "repository": index.repository,
        "keys": len(index.objects),
        "runtime_keys": index.runtime_keys,
        "logical_keys": index.logical_keys,
        "total_bytes": index.total_bytes,
        "index_bytes_raw": raw_bytes,
        "out": str(args.out),
        "apply": bool(args.apply),
    }
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"release  asset_version={index.version} pinned_commit={index.source_commit}")
        print(f"keys     {len(index.objects)} request paths "
              f"({index.runtime_keys} runtime + {index.logical_keys} logical alias)")
        print(f"bytes    {index.total_bytes:,} of object payload described")
        print(f"index    {raw_bytes / 1024**2:.2f} MiB raw -> {args.out}")

    if not args.apply:
        print("[plan] dry run: nothing written; pass --apply to write the index")
        return 0

    built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    written = write_index(index, args.out, built_at=built_at)
    print(f"wrote    {args.out} ({written / 1024**2:.2f} MiB)")
    print("next:    (cd asset-server/cloudflare && npx wrangler deploy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
