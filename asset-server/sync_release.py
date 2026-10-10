#!/usr/bin/env python3
"""Keep exactly one asset release in step with the repository.

The deployment serves the newest release the pipeline has built, and nothing older:
clients follow the official version, so holding eight versions on disk only created
ways for the distributor to answer from the wrong one.  That makes this loop small,
and it makes the dangerous cases explicit:

* a download that stops half way, or a digest that does not match, must leave the
  release that is already on disk serving -- never a mixture;
* the manifest and its checksums are replaced together, and only after every object
  they name is present and verified;
* objects the new release does not reference are removed *after* the swap, so a
  failure at any point leaves the previous release intact.

Nothing here talks to the official archive; that stays the reader's job.

The loop reads the release over raw file URLs and treats the GitHub API as an
optional shortcut for finding the newest commit.  That is not a style choice:
unauthenticated API calls are limited to 60 an hour for the whole machine, and a
sync that can be locked out by its own retries is a sync that stops mirroring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time

try:  # the lock is a Linux deployment concern; the test suite also runs on Windows
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_REPOSITORY = "kohakunamori/MLTDTranslationAssets"
DEFAULT_BRANCH = "main"
API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "checksums.txt"
VERSION_NAME = "version.json"
OBJECT_DIRNAME = "objects/sha256"
SCHEMA_VERSION = 1

#: Statuses worth another try: the far side is having a bad minute, not refusing us.
RETRY_STATUSES = frozenset({500, 502, 503, 504})
#: The contents API answers this for files larger than its inline limit; the commit
#: pinned raw URL named in the payload is the documented way to read them.
INLINE_LIMIT_ENCODING = "none"


class SyncError(RuntimeError):
    """The release on disk must not change because of this failure."""


class HttpError(SyncError):
    def __init__(self, message: str, *, url: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


@dataclass(frozen=True)
class FetchResponse:
    status: int
    body: bytes
    headers: dict[str, str]


def http_get(url: str, *, accept: str = "application/vnd.github+json", timeout: float = 60.0,
             attempts: int = 3) -> FetchResponse:
    """One GET with bounded retries, no implicit caching, optional token.

    A token is read from the environment and never written to disk or to the report:
    unauthenticated GitHub allows 60 API calls an hour, which one loop of this shape
    can exhaust by being restarted a few times.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("MLTD_ASSETS_GITHUB_TOKEN") or ""
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", "mltd-asset-sync")
        request.add_header("Accept", accept)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        # The deployment may sit behind a caching proxy: a stale cached ref would
        # make it miss a release that has already been published.
        request.add_header("Cache-Control", "no-cache, no-store, max-age=0")
        request.add_header("Pragma", "no-cache")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return FetchResponse(status=response.status, body=response.read(),
                                     headers={k.lower(): v for k, v in response.headers.items()})
        except urllib.error.HTTPError as exc:
            detail = exc.headers.get("x-ratelimit-reset") if exc.headers else None
            error = HttpError(
                f"HTTP {exc.code} for {url}" + (f" (rate limit resets at {detail})" if detail else ""),
                url=url, status=exc.code)
            if exc.code not in RETRY_STATUSES or attempt == attempts:
                raise error from exc
            last = error
        except urllib.error.URLError as exc:
            last = HttpError(f"network failure for {url}: {exc.reason}", url=url)
            if attempt == attempts:
                raise last from exc
        time.sleep(min(2 ** attempt, 8))
    raise last if last else SyncError(f"unreachable {url}")


class GitHubReleaseSource:
    """Read the newest built release out of the localization repository."""

    def __init__(self, repository: str = DEFAULT_REPOSITORY, branch: str = DEFAULT_BRANCH,
                 fetch: Callable[..., FetchResponse] = http_get) -> None:
        self.repository = repository
        self.branch = branch
        self.fetch = fetch

    def head_commit(self) -> str:
        """A commit to pin reads to, or the branch itself when the API will not say.

        Falling back to the branch name is not a failure: those reads are still
        exact enough for this loop (the manifest names its own commit) and they are
        never refused.  Losing a whole sync because a rate limit ran out would be
        worse than reading a branch.
        """
        try:
            payload = json.loads(self._api_json(
                f"{API_BASE}/repos/{self.repository}/commits/{self.branch}"))
        except SyncError:
            return self.branch
        commit = str(payload.get("sha") or "")
        return commit if len(commit) == 40 else self.branch

    def newest_version(self, commit: str) -> str:
        """The newest release the pipeline has actually built.

        The directory listing is the source of truth for that; ``manifests/asset-version.json``
        is the version being *tracked* and is updated by a pull request, so it can
        name a version that has not been built yet.  The listing therefore comes
        first, and the tracked version is the fallback for when the API is
        unavailable.
        """
        ref = commit or self.branch
        try:
            entries = json.loads(self._api_json(
                f"{API_BASE}/repos/{self.repository}/contents/generated?ref={urllib.parse.quote(ref, safe='')}"))
        except SyncError:
            entries = None
        if isinstance(entries, list):
            versions = [str(item.get("name")) for item in entries
                        if isinstance(item, dict) and item.get("type") == "dir"
                        and str(item.get("name", "")).isdigit()]
            if versions:
                return max(versions, key=lambda value: (int(value), value))
            raise SyncError("the repository has no generated release directory")

        tracked = json.loads(self._raw_text("manifests/asset-version.json", ref))
        version = str((tracked or {}).get("asset_version") or "")
        if not version.isdigit():
            raise SyncError("the tracked asset version is missing or not a number")
        return version

    def release_path(self, version: str, name: str) -> str:
        return f"generated/{version}/{name}"

    def fetch_text(self, path: str, commit: str) -> str:
        """Read a repository file over its raw URL: no API quota, no size limit."""
        return self._raw_text(path, commit)

    def _raw_text(self, path: str, ref: str) -> str:
        if path.startswith("/") or ".." in Path(path).parts:
            raise SyncError(f"refusing to read outside the repository: {path}")
        url = f"{RAW_BASE}/{self.repository}/{urllib.parse.quote(ref, safe='')}/{path}"
        if len(ref) != 40:
            # A branch name can be cached anywhere along the way; a unique query makes
            # the read fresh without spending an API call to pin a commit.
            url += f"?sync={int(time.time())}"
        try:
            return self.fetch(url, accept="text/plain").body.decode("utf-8")
        except HttpError as exc:
            raise SyncError(f"cannot read {path}: {exc}") from exc

    def fetch_object(self, digest: str, commit: str) -> bytes:
        """Read one content-addressed object and verify it before returning."""
        url = (f"{RAW_BASE}/{self.repository}/{commit}/generated/{OBJECT_DIRNAME}/{digest}")
        body = self.fetch(url).body
        if hashlib.sha256(body).hexdigest() != digest:
            raise SyncError(f"object {digest} did not match its own name")
        return body

    def _api_json(self, url: str) -> str:
        return self.fetch(url).body.decode("utf-8")


@dataclass
class Release:
    asset_version: str
    generated_commit: str
    manifest: dict[str, Any]

    @property
    def objects(self) -> dict[str, str]:
        """object digest -> the manifest entry that needs it."""
        wanted: dict[str, str] = {}
        for entry in self.manifest.get("entries") or []:
            if not isinstance(entry, dict):
                raise SyncError("the manifest has a non-object entry")
            digest = str(entry.get("artifact_sha256") or "")
            object_path = str(entry.get("object_path") or "")
            if len(digest) != 64 or object_path.split("/")[-1] != digest:
                raise SyncError("a manifest entry does not describe its own object")
            wanted[digest] = str(entry.get("logical_path") or "")
        return wanted


def parse_release(source: GitHubReleaseSource, version: str, commit: str) -> Release:
    """Fetch and vet the release description before any object is downloaded."""
    manifest_text = source.fetch_text(source.release_path(version, MANIFEST_NAME), commit)
    checksums_text = source.fetch_text(source.release_path(version, CHECKSUMS_NAME), commit)
    try:
        manifest = json.loads(manifest_text)
    except ValueError as exc:
        raise SyncError(f"the manifest is not JSON: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("asset_version") != version:
        raise SyncError(f"the manifest does not describe asset version {version}")
    release = Release(asset_version=version,
                      generated_commit=str(manifest.get("generated_commit") or commit),
                      manifest=manifest)
    listed = parse_checksums(checksums_text)
    wanted = release.objects
    for digest in wanted:
        if listed.get(f"{OBJECT_DIRNAME}/{digest}") != digest:
            raise SyncError(f"the checksums file does not list object {digest}")
    return release


def parse_checksums(text: str) -> dict[str, str]:
    """Read ``<digest>  <object path>`` lines into a path -> digest map."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise SyncError(f"unusable checksums line: {line[:80]}")
        digest, path = parts
        if len(digest) != 64:
            raise SyncError(f"checksums line without a sha256 digest: {line[:80]}")
        result[path.lstrip("*")] = digest
    return result


def _write_atomic(path: Path, body: bytes) -> None:
    """Write beside the target and rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as handle:
        handle.write(body)
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def verify_release(root: Path, release: Release) -> dict[str, Any]:
    """Every object the release names is on disk and hashes to its name."""
    pool = root / OBJECT_DIRNAME
    missing, mismatched = [], []
    for digest in sorted(release.objects):
        candidate = pool / digest
        try:
            with candidate.open("rb") as stream:
                actual = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    actual.update(chunk)
        except OSError:
            missing.append(digest)
            continue
        if actual.hexdigest() != digest:
            mismatched.append(digest)
    return {"ok": not missing and not mismatched, "checked": len(release.objects),
            "missing": missing, "mismatched": mismatched}


def sync_once(root: Path, *, repository: str = DEFAULT_REPOSITORY, branch: str = DEFAULT_BRANCH,
              source: GitHubReleaseSource | None = None, workers: int = 8,
              commit: str | None = None) -> dict[str, Any]:
    """Bring ``root`` to the newest release, or leave what is there untouched."""
    root = Path(root)
    source = source or GitHubReleaseSource(repository=repository, branch=branch)
    resolved = commit or source.head_commit()
    version = source.newest_version(resolved)
    release = parse_release(source, version, resolved)
    wanted = release.objects

    pool = root / OBJECT_DIRNAME
    pool.mkdir(parents=True, exist_ok=True)
    missing = sorted(digest for digest in wanted if not (pool / digest).is_file())

    failures: list[str] = []
    lock = threading.Lock()

    def download(digest: str) -> None:
        try:
            _write_atomic(pool / digest, source.fetch_object(digest, resolved))
        except (SyncError, OSError) as exc:
            with lock:
                failures.append(f"{digest}: {exc}")

    if missing:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool_executor:
            list(pool_executor.map(download, missing))
    if failures:
        raise SyncError(f"{len(failures)} object(s) failed to download; kept the previous release: {failures[0]}")

    verification = verify_release(root, release)
    if not verification["ok"]:
        raise SyncError("the release did not verify; kept the previous release")

    previous = None
    try:
        previous = json.loads((root / VERSION_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None

    manifest_text = json.dumps(release.manifest, ensure_ascii=False, indent=1) + "\n"
    _write_atomic(root / MANIFEST_NAME, manifest_text.encode("utf-8"))
    _write_atomic(root / CHECKSUMS_NAME, "".join(
        f"{digest}  {OBJECT_DIRNAME}/{digest}\n" for digest in sorted(wanted)).encode("utf-8"))
    _write_atomic(root / VERSION_NAME, (json.dumps({
        "schema_version": SCHEMA_VERSION,
        "asset_version": version,
        "generated_commit": release.generated_commit,
        "source_commit": resolved,
        "entries": len(wanted),
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))

    # Only now, with the new release serving, drop what it does not reference.
    removed: list[str] = []
    for candidate in pool.iterdir():
        if candidate.is_file() and candidate.name not in wanted:
            candidate.unlink()
            removed.append(candidate.name)

    return {
        "sync_status": "success",
        "asset_version": version,
        "generated_commit": release.generated_commit,
        "source_commit": resolved,
        "entries": len(wanted),
        "downloaded": len(missing),
        "removed": len(removed),
        "unchanged": previous is not None and previous.get("asset_version") == version
        and previous.get("source_commit") == resolved,
        "verification": verification,
    }


def single_writer_lock(root: Path):
    """Hold a non-blocking lock on the mirror, or report who already has it.

    Two loops on one mirror is not a data race that ends well: the slower one can
    finish a tick holding an older release and swap it back over the newer one.  The
    deployment runs one loop, but a person can always start a second by hand, and the
    probe in the deployment script does exactly that against its own root.
    """
    if fcntl is None:
        return None
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / ".generated-assets-sync.lock", "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("MLTD_MIRROR_ROOT", "/data")))
    parser.add_argument("--repository", default=os.environ.get("MLTD_ASSETS_REPOSITORY", DEFAULT_REPOSITORY))
    parser.add_argument("--branch", default=os.environ.get("MLTD_ASSETS_BRANCH", DEFAULT_BRANCH))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("MLTD_MIRROR_OBJECT_WORKERS", "8")))
    parser.add_argument("--once", action="store_true", help="sync once and exit (default: keep looping)")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("MLTD_SYNC_INTERVAL", "21600")))
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    lock = single_writer_lock(args.root)
    if lock is False:
        print(json.dumps({"sync_status": "skipped",
                          "error": "another generated-assets sync is already running against this mirror"},
                         ensure_ascii=False), flush=True)
        return 0

    def tick() -> None:
        try:
            print(json.dumps(sync_once(args.root, repository=args.repository, branch=args.branch,
                                       workers=args.workers), ensure_ascii=False), flush=True)
        except (SyncError, OSError) as exc:
            # A failed tick is not a reason to stop: the previous release keeps serving.
            print(json.dumps({"sync_status": "refused", "error": str(exc)}, ensure_ascii=False), flush=True)

    try:
        if args.once:
            tick()
            return 0
        if args.interval <= 0:
            parser.error("--interval must be positive")
        while True:
            tick()
            time.sleep(args.interval)
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
