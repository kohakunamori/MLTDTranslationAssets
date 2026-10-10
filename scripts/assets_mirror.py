#!/usr/bin/env python3
"""NAS-side mirror for the MLTD localization ``generated/`` object pool.

Boundary (owner ruling, see ``docs/server-nas-assets-mirror.md``):
the NAS ``mltd-asset`` deployment is **only an Assets distributor**.  It receives an
``asset_version`` chosen by the server, reads the newest commit of the
``MLTDTranslationAssets`` default branch, finds ``generated/<asset_version>/manifest.json``,
verifies ``build_status`` / ``source_commit`` / ``generated_commit`` / checksums and that the
release's provenance commits descend from the commit it was actually read at, mirrors
the content-addressed objects and serves them **by original logical resource path**.

It does **not** translate, edit images, build Unity3D bundles, decide cross-version reuse,
generate manifests, or coordinate with the Portal.

镜像只同步显式给出的数字 ``asset_version``：一次刷新绝不激活本地默认版本、绝不删除任何东西。
``activate``（写 ``current.json``）与 ``prune``（清理）是彼此独立的显式动作，``prune`` 在被
``--apply`` 明确要求之前一直只是 dry-run。

Two hard invariants:

1. The official archive's ``current`` pointer / symlink is never read or written.  This
   mirror owns a separate ``current.json`` default and still accepts explicit versions.
2. Every check is fail-closed: a manifest that does not validate writes **zero bytes**; an
   object whose bytes do not hash to its declared digest is rejected before it touches disk.

Mapping implemented here (the server keeps using plain resource paths and must never learn
about content addressing)::

    request   /assets/current/<logical_path> or /assets/<asset_version>/<logical_path>
    manifest  generated/<asset_version>/manifest.json   entry.logical_path == <logical_path>
    object    entry.object_path = objects/sha256/<aa>/<artifact_sha256>       (upstream)
    bytes     <root>/objects/sha256/<artifact_sha256>                       (local flat)

The upstream manifest uses the two-hex fan-out.  The local pool writes a flat canonical
name to avoid duplicating objects across versions, while still reading a pre-existing
local shard.  The reader resolves both layouts and the writer never rewrites an existing
object, so an old ``asset_version`` remains downloadable without a second copy.

Object downloads use ``raw.githubusercontent.com`` rather than the GitHub contents API:
the contents API wraps every object in base64 JSON (33% overhead plus a JSON parse of the
whole body before the digest can be checked), while the raw host streams the exact bytes
over plain HTTP with no separate API rate-limit budget.  The commit is always pinned in the
raw URL, so the object is immutable for a given ``(asset_version, commit)`` pair.

Network access always goes through an injectable ``fetchImpl`` so the whole module is
testable offline; the default implementation is stdlib ``urllib``.

CLI（每个写入子命令都只包含它自己的下载动作：``sync --apply`` 与 ``watch`` 只写对象与版本元数据，
从不写 ``current.json``、从不删除；``activate`` 是唯一改变本地默认版本的命令，
``prune --apply`` 是唯一删除版本/孤儿对象的命令）::

    sync     --root <dir> --asset-version <v> [--commit <sha>] [--dry-run|--apply]
    verify   --root <dir> --asset-version <v>
    resolve  --root <dir> --asset-version <v> --logical-path <p>
    list     --root <dir>
    activate --root <dir> --asset-version <v>      # write current.json (explicit)
    prune    --root <dir> [--dry-run|--apply]      # cleanup plan is the default
    watch    --root <dir> --asset-version <v> [--asset-version <v> ...] [--once]

Exit codes: 0 success; 1 failure (validation / sync / verify / not resolvable);
75 refused (rate limited, or another writer holds the lock).
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import binascii
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The provenance gate is the producer's, imported rather than re-implemented:
# two compare implementations would be two chances to disagree about what "an
# ancestor" means, and the mirror is the side that must not drift.
#
# 本仓把同一模块作为顶层 `assets_generated_index` 发布（CI 入口
# `python scripts/assets_generated_index.py …`，scripts/ 无 `__init__.py`）；
# 以包布局消费本仓（`scripts.*` 命名空间包）的调用方则解析包名。
# 兼容适配：优先顶层导入，回退包导入，绝不静默降级为第二实现——
# 两个候选模块都缺失时直接 ImportError 失败。
try:  # pragma: no cover - 由产品仓测试覆盖真实分支
    from assets_generated_index import (
        DEFAULT_PROVENANCE_REPOSITORY as _PRODUCER_DEFAULT_REPOSITORY,
        ReleaseCommitError,
        check_release_commits,
    )
except ImportError:  # pragma: no cover - 供主仓布局 / 已导入包名的调用方
    from scripts.assets_generated_index import (  # type: ignore[no-redef]
        DEFAULT_PROVENANCE_REPOSITORY as _PRODUCER_DEFAULT_REPOSITORY,
        ReleaseCommitError,
        check_release_commits,
    )

SCHEMA_VERSION = 1
STATE_SCHEMA = "mltd-assets-mirror-state/v1"

DEFAULT_REPOSITORY = "kohakunamori/MLTDTranslationAssets"
DEFAULT_BRANCH = "main"
API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

#: ``kind`` every ``generated/<asset_version>/manifest.json`` must declare.
#:
#: One spelling for one contract.  The producer
#: (``scripts/assets_generated_index.py::MANIFEST_KIND``), the JSON schema
#: (``configs/schemas/assets-generated-manifest.schema.json``) and
#: ``docs/ASSETS_GENERATED_CI.md`` all use this value; this module previously
#: spelled it ``mltd-generated-assets-manifest``, which no producer emitted, so
#: a real ``sync`` refused the producer's own manifest on rule 1 of §5.1.
MANIFEST_KIND = "mltd-assets-generated-manifest"

OBJECT_POOL_PREFIX = ("objects", "sha256")
PUBLISHED_DIRNAME = "published"
CURRENT_FILENAME = "current.json"
RETAINED_FILENAME = "retained.json"

#: The retired fan-out form (``objects/sha256/<aa>/<digest>``).  Accepted on read so a
#: release written before the flat switch keeps serving; never produced by a new sync.
LEGACY_FANOUT_RE = re.compile(r"^objects/sha256/[0-9a-f]{2}/[0-9a-f]{64}$")
#: Verify-policy key for a manifest fetched at a commit later than the one it was
#: generated at (a bot commit or a documentation commit landed on top).
SNAPSHOT_POLICY = "snapshot-ahead-of-generated"

#: Asset-axis version string: digits only.  A value such as ``current`` is not a version.
ASSET_VERSION_RE = re.compile(r"^[0-9]+$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

#: Version strings carrying a combined client+assets identity are rejected outright
#: (spec: independent version axes; ``9.0.200+1077100`` / ``client-9.0.200-assets-1077100``).
COMBINED_VERSION_MARKERS = ("+", "-assets-")

#: Entry whitelists (spec §6.1 / §6.2).  Anything else means the entry must not be published.
ALLOWED_REUSE_STATUS = frozenset({"exact", "verified-compatible"})
ALLOWED_TRANSLATION_STATUS = frozenset({"accepted", "modified", "reused"})
# What a payload is. Kept identical to the producer's set
# (``scripts/assets_generated_index.py::ALL_RESOURCE_KINDS``): one spelling for
# one contract, so a producer's manifest cannot be refused for a value the two
# sides merely named differently.
ALLOWED_RESOURCE_KINDS = frozenset({"bundle", "texture", "audio", "other"})

# A CI run identity: digits, or GitHub's `<run_id>.<run_attempt>` pair. `None`
# is accepted by the caller's special case -- this pattern only sees a present
# value.
RUN_ID_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 75


class AssetsMirrorError(Exception):
    """Base class for every expected failure (message is operator-facing)."""


class HttpError(AssetsMirrorError):
    def __init__(self, message: str, *, url: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class RateLimitError(HttpError):
    """403/429 from GitHub.  Never retried silently; the caller decides."""

    def __init__(self, message: str, *, url: str = "", status: int = 0,
                 retry_after: str | None = None, reset_at: str | None = None) -> None:
        super().__init__(message, url=url, status=status)
        self.retry_after = retry_after
        self.reset_at = reset_at


class ManifestValidationError(AssetsMirrorError):
    def __init__(self, problems: Iterable[str], *, asset_version: str = "") -> None:
        self.problems = list(problems)
        head = f"manifest for asset_version={asset_version!r} rejected ({len(self.problems)} problem(s))"
        super().__init__("\n".join([head] + [f"  - {p}" for p in self.problems]))


class ObjectDigestMismatch(AssetsMirrorError):
    def __init__(self, digest: str, actual: str, *, origin: str = "") -> None:
        self.digest = digest
        self.actual = actual
        where = f" from {origin}" if origin else ""
        super().__init__(
            f"object digest mismatch{where}: declared {digest}, received {actual}; "
            "refusing to write these bytes"
        )


class SyncError(AssetsMirrorError):
    pass


# --------------------------------------------------------------------------------------
# fetch abstraction
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Fetcher = Callable[..., FetchResponse]


def default_fetch(url: str, *, accept: str | None = None, timeout: float = 60.0) -> FetchResponse:
    """Stdlib HTTP GET.  A token is read from the environment, never from code or config."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", "mltd-assets-mirror")
    # The NAS may sit behind a caching HTTP proxy.  A stale cached branch ref
    # would make the distributor miss a newly published generated version.
    request.add_header("Cache-Control", "no-cache, no-store, max-age=0")
    request.add_header("Pragma", "no-cache")
    request.add_header("Accept", accept or "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("MLTD_ASSETS_GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return FetchResponse(status=response.status, body=response.read(), headers=headers)
    except urllib.error.HTTPError as exc:  # pragma: no cover - exercised through injection
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        raise _http_error_from(url, exc.code, headers) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network failure path
        raise HttpError(f"network failure for {url}: {exc.reason}", url=url) from exc


def _http_error_from(url: str, status: int, headers: dict[str, str]) -> HttpError:
    if status in (403, 429):
        retry_after = headers.get("retry-after")
        reset_at = headers.get("x-ratelimit-reset")
        detail = []
        if retry_after:
            detail.append(f"Retry-After={retry_after}s")
        if reset_at:
            detail.append(f"X-RateLimit-Reset={reset_at}")
        if headers.get("x-ratelimit-remaining") == "0":
            detail.append("rate limit exhausted")
        suffix = f" ({', '.join(detail)})" if detail else ""
        return RateLimitError(
            f"GitHub refused {url} with HTTP {status}{suffix}; not retrying automatically",
            url=url, status=status, retry_after=retry_after, reset_at=reset_at,
        )
    return HttpError(f"HTTP {status} for {url}", url=url, status=status)


def _raise_for_status(response: FetchResponse, url: str) -> FetchResponse:
    if 200 <= response.status < 300:
        return response
    raise _http_error_from(url, response.status, {k.lower(): v for k, v in response.headers.items()})


# --------------------------------------------------------------------------------------
# GitHub source
# --------------------------------------------------------------------------------------


class GitHubAssetsSource:
    """Read-only reader for ``generated/`` in the public localization assets repository."""

    def __init__(
        self,
        repo: str = DEFAULT_REPOSITORY,
        branch: str = DEFAULT_BRANCH,
        fetchImpl: Fetcher | None = None,
        *,
        api_base: str = API_BASE,
        raw_base: str = RAW_BASE,
    ) -> None:
        self.repo = repo
        self.branch = branch
        self.fetch = fetchImpl or default_fetch
        self.api_base = api_base.rstrip("/")
        self.raw_base = raw_base.rstrip("/")

    # -- URLs -------------------------------------------------------------------------

    def _ref_path(self) -> str:
        return "/".join(urllib.parse.quote(part, safe="") for part in self.branch.split("/"))

    def _contents_url(self, asset_version: str, name: str, commit: str) -> str:
        quoted_version = urllib.parse.quote(asset_version, safe="")
        return (
            f"{self.api_base}/repos/{self.repo}/contents/generated/{quoted_version}/{name}"
            f"?ref={urllib.parse.quote(commit, safe='')}"
        )

    def object_path_for(self, digest: str) -> str:
        """The in-repo object path this source serves: sharded, then legacy flat."""
        return object_path_for(digest)

    def legacy_object_url(self, digest: str, commit: str) -> str:
        """The historical flat URL, tried only when the canonical shard is absent."""
        return (
            f"{self.raw_base}/{self.repo}/{urllib.parse.quote(commit, safe='')}"
            f"/generated/{legacy_object_path_for(digest)}"
        )

    def object_url(self, digest: str, commit: str) -> str:
        return (
            f"{self.raw_base}/{self.repo}/{urllib.parse.quote(commit, safe='')}"
            f"/generated/{object_path_for(digest)}"
        )

    # -- primitives -------------------------------------------------------------------

    def _get(self, url: str) -> FetchResponse:
        """One GET, retried a few times for a *transient* failure.

        The deployed mirror reaches GitHub through a LAN proxy, and that proxy
        drops connections now and then: measured on the NAS as an isolated
        ``SSLEOFError`` under concurrent load, and as a burst of ``Connection
        refused`` that failed a whole tick (every version refused, nothing
        published) while the proxy restarted.  A dropped connection is not a
        verdict on the content, so it is retried with a growing pause; the longest
        pause is deliberate, because the failure mode being covered is "the proxy
        is briefly away", not "the object is slow".

        What is *not* retried matters just as much.  A 4xx is an answer -- 404
        above all, because ``fetch_object`` uses it to select the legacy sharded
        path, and 403/429, where the token or the rate limit is the problem and
        hammering it would only make that worse.  A digest mismatch is a verdict
        on the bytes, and the same wrong bytes would come back.
        """
        attempts = len(OBJECT_FETCH_BACKOFFS) + 1
        last: HttpError | None = None
        for attempt in range(attempts):
            try:
                return _raise_for_status(self.fetch(url), url)
            except RateLimitError:
                raise
            except HttpError as exc:
                # Status 0 is the fetcher's own "the transport failed" marker; 5xx
                # is the server saying "later".  Any other status is a verdict.
                if exc.status != 0 and exc.status < 500:
                    raise
                last = exc
            except AssetsMirrorError:
                raise
            except Exception as exc:  # pragma: no cover - unexpected transport error
                last = HttpError(f"transport error for {url}: {exc}", url=url)
            if attempt < attempts - 1:
                time.sleep(OBJECT_FETCH_BACKOFFS[attempt])
        assert last is not None
        raise last

    def _get_json(self, url: str) -> Any:
        response = self._get(url)
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"invalid JSON from {url}: {exc}", url=url, status=response.status) from exc

    def _get_contents_text(self, url: str) -> str:
        payload = self._get_json(url)
        if not isinstance(payload, dict) or "content" not in payload:
            raise HttpError(f"unexpected contents API payload from {url}", url=url)
        encoding = payload.get("encoding")
        if encoding == "none":
            # GitHub returns encoding=none and an empty content field for files
            # larger than the Contents API's inline limit.  Manifests/checksums
            # are intentionally allowed to be large, so use the commit-pinned
            # raw URL supplied by the API instead of treating this as corruption.
            download_url = payload.get("download_url")
            if not isinstance(download_url, str) or not download_url:
                raise HttpError(
                    f"large contents payload from {url} has no download_url",
                    url=url,
                )
            response = self._get(download_url)
            try:
                return response.body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HttpError(
                    f"cannot decode raw contents payload from {download_url}: {exc}",
                    url=download_url,
                ) from exc
        if encoding not in (None, "base64"):
            raise HttpError(f"unsupported contents encoding {payload.get('encoding')!r} from {url}", url=url)
        try:
            return base64.b64decode(payload["content"] or "", validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise HttpError(f"cannot base64-decode contents payload from {url}: {exc}", url=url) from exc

    # -- public API -------------------------------------------------------------------

    def head_commit(self) -> str:
        """Newest commit of the configured branch (40-hex, validated)."""
        # A NAS-side proxy may cache a branch ref despite no-cache headers.  Bust
        # only this mutable lookup; all subsequent requests are commit-pinned.
        url = (
            f"{self.api_base}/repos/{self.repo}/git/ref/heads/{self._ref_path()}"
            f"?mltd_cache_bust={time.time_ns()}"
        )
        payload = self._get_json(url)
        obj = payload.get("object") if isinstance(payload, dict) else None
        sha = (obj or {}).get("sha", "")
        if not isinstance(sha, str) or not HEX40_RE.match(sha):
            raise HttpError(f"branch {self.branch!r} did not resolve to a 40-hex commit (got {sha!r})", url=url)
        return sha

    def generated_versions(self, commit: str) -> list[str]:
        """Return generated release versions present in one immutable tree.

        The tree is read once per refresh, so automatic latest-version discovery
        cannot mix manifests from different branch heads.  A truncated tree is
        refused: silently choosing from a partial listing could activate an old
        release while a newer one exists.
        """
        _require_commit(commit, "commit")
        url = f"{self.api_base}/repos/{self.repo}/git/trees/{commit}?recursive=1"
        payload = self._get_json(url)
        if not isinstance(payload, dict) or payload.get("truncated") is True:
            raise HttpError(f"generated tree for {commit} is truncated or malformed", url=url)
        tree = payload.get("tree")
        if not isinstance(tree, list):
            raise HttpError(f"generated tree for {commit} has no tree list", url=url)
        versions: set[str] = set()
        for item in tree:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = item.get("path")
            match = re.fullmatch(r"generated/([0-9]+)/manifest\.json", str(path or ""))
            if match:
                versions.add(match.group(1))
        return sorted(versions, key=lambda value: (int(value), value), reverse=True)

    def fetch_manifest(self, asset_version: str, commit: str) -> dict:
        _require_asset_version(asset_version)
        _require_commit(commit, "commit")
        url = self._contents_url(asset_version, "manifest.json", commit)
        text = self._get_contents_text(url)
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HttpError(f"manifest.json for {asset_version} is not valid JSON: {exc}", url=url) from exc
        if not isinstance(manifest, dict):
            raise HttpError(f"manifest.json for {asset_version} is not a JSON object", url=url)
        return manifest

    def fetch_checksums(self, asset_version: str, commit: str) -> str:
        _require_asset_version(asset_version)
        _require_commit(commit, "commit")
        url = self._contents_url(asset_version, "checksums.txt", commit)
        return self._get_contents_text(url)

    def fetch_object(self, digest: str, commit: str) -> bytes:
        """Download one CAS object and verify its SHA-256 before returning it.

        Sharded first; the historical flat URL is tried only when the sharded path is
        gone upstream (404), which is the situation of a release mirrored while
        the repository still used the old layout.  A digest mismatch is never
        retried against the other path: the bytes were wrong, and the second
        path would only serve the same wrong bytes under a second name.
        """
        _require_digest(digest, "digest")
        _require_commit(commit, "commit")
        url = self.object_url(digest, commit)
        try:
            response = self._get(url)
        except HttpError as exc:
            if exc.status != 404:
                raise
            legacy_url = self.legacy_object_url(digest, commit)
            response = self._get(legacy_url)
            url = legacy_url
        actual = hashlib.sha256(response.body).hexdigest()
        if actual != digest:
            raise ObjectDigestMismatch(digest, actual, origin=url)
        return response.body


def _require_asset_version(value: str) -> str:
    if not isinstance(value, str) or not ASSET_VERSION_RE.match(value):
        raise AssetsMirrorError(
            f"asset_version must be digits only (got {value!r}); combined versions and pointer "
            f"names such as 'current' are not asset versions"
        )
    return value


def _require_commit(value: str, label: str) -> str:
    if not isinstance(value, str) or not HEX40_RE.match(value):
        raise AssetsMirrorError(f"{label} must be a 40-hex sha (got {value!r})")
    return value


def _require_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.match(value):
        raise AssetsMirrorError(f"{label} must be a 64-hex sha256 (got {value!r})")
    return value


# --------------------------------------------------------------------------------------
# manifest validation
# --------------------------------------------------------------------------------------


def object_path_for(digest: str) -> str:
    """Canonical (two-hex sharded) in-repo object path for a digest."""
    _require_digest(digest, "digest")
    return f"objects/sha256/{digest[:2]}/{digest}"


def legacy_object_path_for(digest: str) -> str:
    """The historical flat object path, accepted for read-only compatibility."""
    _require_digest(digest, "digest")
    return f"objects/sha256/{digest}"


def _check_plain_version(value: Any, label: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not value:
        problems.append(f"{label} must be a non-empty string (got {value!r})")
        return
    for marker in COMBINED_VERSION_MARKERS:
        if marker in value:
            problems.append(
                f"{label}={value!r} contains {marker!r}: client and assets version axes must never be "
                "combined (spec §2.1)"
            )


def _check_plain_run_id(value: Any, label: str, problems: list[str]) -> None:
    """``None`` is a legitimate run identity (no run context); a malformed one is not."""
    if value is None:
        return
    if not isinstance(value, str) or not RUN_ID_RE.match(value):
        problems.append(f"{label} must be null or '<digits>'/'<digits>.<digits>' (got {value!r})")


def _check_logical_path(value: Any, label: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{label}: logical_path must be a non-empty string")
        return
    if value.startswith("/") or value.startswith("\\") or re.match(r"^[A-Za-z]:[\\/]", value):
        problems.append(f"{label}: logical_path {value!r} must be relative to the asset root")
        return
    parts = re.split(r"[\\/]+", value)
    if ".." in parts:
        problems.append(f"{label}: logical_path {value!r} contains '..' (path traversal)")
    if any(part in ("", ".") for part in parts):
        problems.append(f"{label}: logical_path {value!r} has empty or '.' segments")
    if "\x00" in value:
        problems.append(f"{label}: logical_path contains a NUL byte")


def validate_manifest(manifest: Any, *, asset_version: str,
                      expected_head_commit: str | None) -> list[str]:
    """Return the list of problems; an empty list means the manifest may be mirrored.

    Fail-closed by design: one unacceptable entry rejects the whole version, so a partially
    published ``asset_version`` can never appear on the NAS.

    ``expected_head_commit`` is the commit the manifest was fetched from.  It is **no longer**
    required to equal ``generated_commit``: the pipeline generates a release, commits it, and
    later commits (a bot commit, documentation) land on top, so the two legitimately differ and
    the old equality made every such manifest unmirrorable.  What is checked instead is that a
    manifest's own ``snapshot_commit`` -- when it declares one -- is exactly the commit being
    read.  The ancestry of the provenance tuple needs a history query and is therefore NOT
    this function's job: ``sync`` runs
    :func:`validate_release_provenance` (the producer's ``check_release_commits``) right after
    this, before anything is downloaded or written.
    """
    problems: list[str] = []

    if not isinstance(manifest, dict):
        return [f"manifest must be a JSON object (got {type(manifest).__name__})"]

    if manifest.get("kind") != MANIFEST_KIND:
        problems.append(f"kind must be {MANIFEST_KIND!r} (got {manifest.get('kind')!r})")

    declared_version = manifest.get("asset_version")
    if declared_version != asset_version:
        problems.append(f"asset_version mismatch: manifest={declared_version!r}, requested={asset_version!r}")
    _check_plain_version(declared_version, "asset_version", problems)

    if "client_version" not in manifest:
        problems.append("client_version must be present and explicitly null on the assets axis")
    elif manifest.get("client_version") is not None:
        problems.append(
            f"client_version must be null on the assets axis (got {manifest.get('client_version')!r}); "
            "the assets manifest must not carry a client version"
        )

    if "source_client_version" in manifest:
        _check_plain_version(manifest.get("source_client_version"), "source_client_version", problems)

    # The run identity is provenance: null is a legitimate value (the producer had
    # no run context), but a *missing* key is not -- absence cannot be told apart
    # from a lost value, which is exactly what a provenance field exists to stop.
    if "ci_run_id" not in manifest:
        problems.append("ci_run_id must be present (null is allowed; absent is not)")
    else:
        _check_plain_run_id(manifest.get("ci_run_id"), "ci_run_id", problems)

    if manifest.get("build_status") != "success":
        problems.append(f"build_status must be 'success' (got {manifest.get('build_status')!r})")

    for field_name in ("source_commit", "generated_commit"):
        value = manifest.get(field_name)
        if not isinstance(value, str) or not HEX40_RE.match(value):
            problems.append(f"{field_name} must be a 40-hex sha (got {value!r})")

    snapshot_commit = manifest.get("snapshot_commit")
    if snapshot_commit is not None:
        if not isinstance(snapshot_commit, str) or not HEX40_RE.match(snapshot_commit):
            problems.append(f"snapshot_commit must be a 40-hex sha (got {snapshot_commit!r})")
        elif expected_head_commit is not None and snapshot_commit != expected_head_commit:
            problems.append(
                f"snapshot_commit={snapshot_commit!r} disagrees with the pinned commit "
                f"{expected_head_commit!r}; a manifest fetched at a commit must record that "
                f"commit as its snapshot (re-run with --commit {snapshot_commit})"
            )

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        problems.append(f"entries must be a list (got {type(entries).__name__})")
        return problems
    if not entries:
        problems.append("entries must not be empty")
        return problems

    seen_paths: dict[str, int] = {}
    for index, entry in enumerate(entries):
        label = f"entries[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{label}: entry must be a JSON object")
            continue

        logical_path = entry.get("logical_path")
        _check_logical_path(logical_path, label, problems)
        if isinstance(logical_path, str):
            if logical_path in seen_paths:
                problems.append(
                    f"{label}: logical_path {logical_path!r} duplicates entries[{seen_paths[logical_path]}]"
                )
            else:
                seen_paths[logical_path] = index

        if entry.get("resource_kind") not in ALLOWED_RESOURCE_KINDS:
            problems.append(
                f"{label} ({logical_path!r}): resource_kind={entry.get('resource_kind')!r} is not "
                f"one of {sorted(ALLOWED_RESOURCE_KINDS)}; every entry declares what it is"
            )

        reuse_status = entry.get("reuse_status")
        if reuse_status not in ALLOWED_REUSE_STATUS:
            problems.append(
                f"{label} ({logical_path!r}): reuse_status={reuse_status!r} is not publishable; "
                f"allowed: {sorted(ALLOWED_REUSE_STATUS)} ('suggested'/'blocked' never enter generated/)"
            )

        translation_status = entry.get("translation_status")
        if translation_status not in ALLOWED_TRANSLATION_STATUS:
            problems.append(
                f"{label} ({logical_path!r}): translation_status={translation_status!r} is not publishable; "
                f"allowed: {sorted(ALLOWED_TRANSLATION_STATUS)}"
            )

        digest = entry.get("artifact_sha256")
        if not isinstance(digest, str) or not HEX64_RE.match(digest):
            problems.append(f"{label} ({logical_path!r}): artifact_sha256 must be a 64-hex sha256 (got {digest!r})")
            continue

        object_path = entry.get("object_path")
        expected_object_path = object_path_for(digest)
        if object_path not in (expected_object_path, legacy_object_path_for(digest)):
            problems.append(
                f"{label} ({logical_path!r}): object_path={object_path!r} is not the canonical "
                f"content-addressed path for artifact_sha256={digest!r} (expected {expected_object_path!r})"
            )

    return problems


def parse_checksums(text: str) -> dict[str, str]:
    """Parse a ``sha256sum``-style checksums file into ``{path: digest}``."""
    table: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, remainder = line.partition(" ")
        digest = digest.strip().lower()
        path = remainder.strip()
        if path.startswith("*"):
            path = path[1:]
        path = path.strip()
        if not HEX64_RE.match(digest) or not path:
            continue
        table[path] = digest
    return table


# --------------------------------------------------------------------------------------
# local object pool
# --------------------------------------------------------------------------------------


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ObjectPool:
    """Content-addressed store at ``<root>/objects/sha256/<sha256>`` (flat).

    Layout matches the in-repo ``generated/objects`` tree, which is deliberate: a pool
    directory can be seeded from a checkout of the repository without any translation step.
    The retired fan-out path (``.../<aa>/<sha256>``) is still *read* so a pool seeded before
    the flat switch keeps serving, and is never written.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    @property
    def objects_dir(self) -> Path:
        return self.root.joinpath(*OBJECT_POOL_PREFIX)

    def path_for(self, digest: str) -> Path:
        """Canonical (flat) on-disk path of an object."""
        _require_digest(digest, "digest")
        return self.objects_dir / digest

    def legacy_path_for(self, digest: str) -> Path:
        _require_digest(digest, "digest")
        return self.objects_dir / digest[:2] / digest

    def existing_path(self, digest: str) -> Path | None:
        """Where the bytes are -- flat first, then the retired shard -- or ``None``."""
        flat = self.path_for(digest)
        if flat.is_file():
            return flat
        legacy = self.legacy_path_for(digest)
        return legacy if legacy.is_file() else None

    def has(self, digest: str) -> bool:
        return self.existing_path(digest) is not None

    def put_bytes(self, digest: str, data: bytes) -> bool:
        """Verify then atomically store ``data``.  Returns True when a new object was written.

        The digest is checked **before** anything reaches the pool, and an existing object
        with the same digest is left untouched (idempotent re-runs).
        """
        _require_digest(digest, "digest")
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise ObjectDigestMismatch(digest, actual)
        target = self.path_for(digest)
        if target.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".incoming-", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as temp:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return True

    def remove(self, digest: str) -> bool:
        """Remove both canonical and legacy names for one digest."""
        _require_digest(digest, "digest")
        removed = False
        for target in (self.path_for(digest), self.legacy_path_for(digest)):
            try:
                target.unlink()
                removed = True
            except FileNotFoundError:
                continue
            except OSError:
                raise
        legacy_parent = self.legacy_path_for(digest).parent
        try:
            legacy_parent.rmdir()
        except OSError:
            pass
        return removed

    def iter_digests(self) -> Iterator[str]:
        """Every digest present, flat and (legacy) sharded alike, deduplicated."""
        objects_dir = self.objects_dir
        if not objects_dir.is_dir():
            return
        seen: set[str] = set()
        for candidate in sorted(objects_dir.iterdir()):
            if candidate.is_file() and HEX64_RE.match(candidate.name):
                if candidate.name not in seen:
                    seen.add(candidate.name)
                    yield candidate.name
            elif candidate.is_dir() and len(candidate.name) == 2:
                for shard in sorted(candidate.iterdir()):
                    if shard.is_file() and HEX64_RE.match(shard.name) \
                            and shard.name not in seen:
                        seen.add(shard.name)
                        yield shard.name


@dataclass
class PoolVerification:
    ok: bool
    missing: list[str] = field(default_factory=list)
    mismatched: list[dict[str, str]] = field(default_factory=list)
    checksum_mismatches: list[dict[str, str]] = field(default_factory=list)
    unlisted_objects: list[str] = field(default_factory=list)
    checked: int = 0
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "missing": self.missing,
            "mismatched": self.mismatched,
            "checksum_mismatches": self.checksum_mismatches,
            "unlisted_objects": self.unlisted_objects,
            "problems": self.problems,
        }


def verify_manifest_objects(
    manifest: dict,
    pool: ObjectPool,
    *,
    checksums_text: str | None = None,
) -> PoolVerification:
    """Check the manifest against the local pool and the upstream checksums file.

    For every entry: the declared ``object_path`` must appear in ``checksums.txt`` with the
    same digest, the object must exist in the pool, and the pool bytes must hash back to
    ``artifact_sha256``.
    """
    result = PoolVerification(ok=True)
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        result.ok = False
        result.problems.append("manifest has no entries list")
        return result

    checksum_table: dict[str, str] = {}
    if checksums_text:
        checksum_table = parse_checksums(checksums_text)
    if not checksum_table:
        result.ok = False
        result.problems.append("checksums.txt is missing or empty; refusing to verify/accept the manifest")


    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            result.ok = False
            result.problems.append(f"entries[{index}] is not an object")
            continue
        digest = entry.get("artifact_sha256")
        object_path = entry.get("object_path")
        logical_path = entry.get("logical_path")
        if not isinstance(digest, str) or not HEX64_RE.match(digest):
            result.ok = False
            result.problems.append(f"entries[{index}]: artifact_sha256 is not a 64-hex sha256")
            continue

        if checksum_table:
            # Exact path, not "the digest appears somewhere": the manifest names
            # where the bytes are and the checksums file must vouch for *that*
            # entry.  An old release is consistent in both files (both name the
            # shard) and a new one names the flat path in both, so this costs a
            # well-formed release nothing and catches a half-migrated pair.
            listed = checksum_table.get(str(object_path))
            if listed is None:
                result.ok = False
                result.checksum_mismatches.append(
                    {"object_path": str(object_path), "declared": digest, "checksums_txt": None}
                )
            elif listed != digest:
                result.ok = False
                result.checksum_mismatches.append(
                    {"object_path": str(object_path), "declared": digest, "checksums_txt": listed}
                )

        target = pool.existing_path(digest)
        if target is None:
            result.ok = False
            result.missing.append(str(logical_path or object_path))
            continue
        result.checked += 1
        actual = sha256_file(target)
        if actual != digest:
            result.ok = False
            result.mismatched.append(
                {"logical_path": str(logical_path), "object_path": str(object_path),
                 "expected": digest, "actual": actual}
            )

    return result


# --------------------------------------------------------------------------------------
# mirror
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedObject:
    logical_path: str
    object_path: str
    artifact_sha256: str
    pool_path: str
    size: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "object_path": self.object_path,
            "artifact_sha256": self.artifact_sha256,
            "pool_path": self.pool_path,
            "size": self.size,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_url(repo: str, base_sha: str, candidate_sha: str,
                 api_base: str = API_BASE) -> str:
    """The read-only compare the provenance gate calls.

    ``candidate_sha`` is the manifest's own input commit and ``base_sha`` the
    commit the manifest was actually fetched at; the endpoint answers whether
    the candidate is in the base's history (``ahead``) or the same object
    (``identical``).  This is a GET of a public endpoint -- the only request the
    gate makes, and it never carries a token from here.
    """
    return (f"{api_base.rstrip('/')}/repos/{repo}/compare/{candidate_sha}...{base_sha}")


def validate_release_provenance(
    manifest: dict,
    *,
    snapshot_commit: str,
    repo: str,
    fetchImpl: Fetcher | None = None,
    api_base: str = API_BASE,
) -> list[str]:
    """Check the manifest's input commits against the commit it was read at.

    The manifest records the commits the build *consumed*; the commit the mirror
    *resolved* is the mirror's own fact and is passed in here -- never taken
    from the manifest, which cannot know it.  The ancestry is decided by the
    producer's :func:`check_release_commits` through the injected fetch, so a
    mirror never grows its own second opinion about ancestor semantics.

    ``fetchImpl`` takes a URL and returns ``(status, decoded_json)``.  The
    mirror's own :class:`GitHubAssetsSource` is not that shape, so the caller
    wraps it (see :meth:`AssetVersionMirror._compare_fetch`).
    """
    try:
        return check_release_commits(
            manifest,
            snapshot_commit=snapshot_commit,
            repository=repo,
            api_base=api_base,
            fetchImpl=fetchImpl,
        )
    except ReleaseCommitError as exc:
        # An unanswerable comparison is a refusal, not a pass: it reaches the
        # caller as the same kind of problem list the other gates produce.
        return list(exc.problems)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"cannot read {path}: {exc}") from exc


#: Pauses before retrying one GET whose transport failed: four attempts in all.
#: The last pause is long on purpose -- the case being covered is a LAN proxy
#: that has gone away and come back, not a slow object.
OBJECT_FETCH_BACKOFFS = (2.0, 8.0, 30.0)


#: Objects fetched at once by default: enough to hide the per-request round trip
#: without opening a connection burst at the CDN.  The deployed loop overrides it
#: through ``MLTD_MIRROR_OBJECT_WORKERS``.
DEFAULT_OBJECT_WORKERS = 8


def _require_object_workers(value: int) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError) as exc:
        raise AssetsMirrorError(f"object_workers must be an integer (got {value!r})") from exc
    if workers < 1:
        raise AssetsMirrorError(f"object_workers must be at least 1 (got {workers})")
    return workers


def _object_workers_from_env() -> int:
    raw = (os.environ.get("MLTD_MIRROR_OBJECT_WORKERS") or "").strip()
    if not raw:
        return DEFAULT_OBJECT_WORKERS
    try:
        return _require_object_workers(int(raw))
    except AssetsMirrorError as exc:
        raise AssetsMirrorError(f"MLTD_MIRROR_OBJECT_WORKERS: {exc}") from exc


class AssetVersionMirror:
    """Mirror one or more ``asset_version`` trees into ``<root>/published/<version>/``.

    The localization mirror has its own ``current.json`` pointer.  It never
    consults or changes the official archive's ``current`` symlink.
    """

    def __init__(self, source: GitHubAssetsSource, pool: ObjectPool, root: str | os.PathLike[str],
                 *, compare_fetch: Fetcher | None = None,
                 compare_repository: str | None = None,
                 object_workers: int | None = None) -> None:
        self.source = source
        self.pool = pool
        self.root = Path(root)
        # How many objects are fetched at once.  One object is one HTTP request
        # and a release that adds lyrics or images adds hundreds of them, so this
        # is the difference between a tick that takes a minute and a tick that
        # takes an hour on the same link.  ``None`` reads the environment so the
        # deployed loop can be tuned without editing the loop itself.
        self.object_workers = (
            _object_workers_from_env() if object_workers is None
            else _require_object_workers(object_workers)
        )
        # The provenance gate's transport is injectable here rather than inside
        # the producer, so the whole sync path (including the failure cases) is
        # testable offline and the mirror never needs the network to be proven
        # correct.
        self._compare_fetch_impl = compare_fetch
        self._compare_repository = compare_repository

    # -- provenance ------------------------------------------------------------------- #
    @property
    def compare_repository(self) -> str:
        """Repository the ancestry is checked against (default: the producer's)."""
        return self._compare_repository or self.source.repo or _PRODUCER_DEFAULT_REPOSITORY

    def _compare_fetch(self, url: str) -> tuple[int, Any]:
        """The ``(status, json)`` fetch the producer's gate expects.

        The default transport is the API host through the source's own
        transport, so there is exactly one place in this module that performs
        network I/O and one callable a test has to replace.  The producer builds
        the URL, and the ``api_base`` handed to it is the API base -- the
        compare endpoint lives on ``api.github.com``, not on the raw host.
        """
        if self._compare_fetch_impl is not None:
            return self._compare_fetch_impl(url)
        try:
            response = self.source._get(url)  # noqa: SLF001 - one transport, same module
        except AssetsMirrorError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never a pass
            raise ReleaseCommitError(
                [f"the compare request {url} could not be made: {exc}"]) from exc
        try:
            return response.status, json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseCommitError(
                [f"the compare response from {url} is not JSON: {exc}"]) from exc

    def _validate_provenance(self, manifest: dict, resolved_commit: str) -> None:
        """Refuse a manifest whose input commits are not in the snapshot's history.

        Runs on the real ``sync`` path (dry runs included), before anything is
        downloaded or written.  The snapshot is the commit this mirror resolved;
        the manifest cannot know it and its own word is never used for it.
        """
        problems = validate_release_provenance(
            manifest,
            snapshot_commit=resolved_commit,
            repo=self.compare_repository,
            fetchImpl=self._compare_fetch,
            api_base=API_BASE,
        )
        if problems:
            raise ManifestValidationError(
                [f"provenance: {problem}" for problem in problems],
                asset_version=str(manifest.get("asset_version") or ""),
            )

    # -- layout -----------------------------------------------------------------------

    @property
    def published_dir(self) -> Path:
        return self.root / PUBLISHED_DIRNAME

    def version_dir(self, asset_version: str) -> Path:
        return self.published_dir / _require_asset_version(asset_version)

    def state_path(self, asset_version: str) -> Path:
        return self.version_dir(asset_version) / "state.json"

    def manifest_path(self, asset_version: str) -> Path:
        return self.version_dir(asset_version) / "manifest.json"

    def checksums_path(self, asset_version: str) -> Path:
        return self.version_dir(asset_version) / "checksums.txt"

    @property
    def current_path(self) -> Path:
        return self.root / CURRENT_FILENAME

    @property
    def retained_path(self) -> Path:
        return self.root / RETAINED_FILENAME

    def retained_versions(self) -> set[str]:
        payload = _read_json(self.retained_path)
        if payload is None:
            return set()
        values = payload.get("asset_versions", [])
        if not isinstance(values, list):
            raise SyncError(f"{self.retained_path} has no asset_versions list")
        return {_require_asset_version(str(value)) for value in values}

    def set_retained_versions(self, versions: Iterable[str]) -> list[str]:
        normalized = sorted({_require_asset_version(str(value)) for value in versions},
                            key=lambda value: (int(value), value))
        _write_json(self.retained_path, {
            "schema_version": SCHEMA_VERSION,
            "asset_versions": normalized,
            "updated_at": _utc_now(),
        })
        return normalized

    def current_version(self) -> str:
        """Resolve the localization default without reading the official pointer."""
        payload = _read_json(self.current_path)
        if payload is not None:
            version = payload.get("asset_version")
            if isinstance(version, str) and ASSET_VERSION_RE.fullmatch(version):
                state = _read_json(self.state_path(version))
                if state and state.get("sync_status") == "success":
                    return version
        candidates = [
            item["asset_version"] for item in self.list_versions()
            if item.get("sync_status") == "success"
        ]
        if not candidates:
            raise SyncError("no successfully published localization asset version")
        return max(candidates, key=lambda value: (int(value), value))

    def activate_current(self, asset_version: str) -> dict[str, Any]:
        asset_version = _require_asset_version(asset_version)
        state = _read_json(self.state_path(asset_version))
        if state is None or state.get("sync_status") != "success":
            raise SyncError(f"cannot activate unpublished asset_version={asset_version}")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "asset_version": asset_version,
            "snapshot_commit": state.get("snapshot_commit"),
            "generated_commit": state.get("generated_commit"),
            "updated_at": _utc_now(),
        }
        _write_json(self.current_path, payload)
        return payload

    def _referenced_digests(self, versions: Iterable[str]) -> set[str]:
        referenced: set[str] = set()
        for version in versions:
            manifest = _read_json(self.manifest_path(version)) or {}
            for entry in manifest.get("entries", []):
                if isinstance(entry, dict) and HEX64_RE.fullmatch(str(entry.get("artifact_sha256", ""))):
                    referenced.add(str(entry["artifact_sha256"]))
        return referenced

    def prune(self, *, keep_versions: Iterable[str] = (), dry_run: bool = True) -> dict[str, Any]:
        """Remove unretained publications and orphaned CAS objects.

        The default is a plan.  Applied cleanup only removes version metadata and
        objects not referenced by any retained manifest; it never touches the
        official archive tree.
        """
        requested = {_require_asset_version(str(value)) for value in keep_versions}
        requested.update(self.retained_versions())
        try:
            requested.add(self.current_version())
        except SyncError:
            pass
        versions = self.list_versions()
        existing = {item["asset_version"] for item in versions}
        keep = requested & existing
        remove_versions = sorted(existing - keep, key=lambda value: (int(value), value))
        retained_digests = self._referenced_digests(keep)
        remove_objects = sorted(set(self.pool.iter_digests()) - retained_digests)
        if not dry_run:
            for version in remove_versions:
                shutil.rmtree(self.version_dir(version), ignore_errors=False)
            for digest in remove_objects:
                self.pool.remove(digest)
        return {
            "mode": "dry-run" if dry_run else "apply",
            "kept_versions": sorted(keep, key=lambda value: (int(value), value)),
            "removed_versions": remove_versions,
            "removed_objects": remove_objects,
            "removed_object_count": len(remove_objects),
        }

    def latest_generated_version(self, *, commit: str | None = None) -> tuple[str, str]:
        """Find the newest successful, schema-valid generated release."""
        resolved_commit = _require_commit(commit, "commit") if commit else self.source.head_commit()
        versions = self.source.generated_versions(resolved_commit)
        for asset_version in versions:
            try:
                manifest = self.source.fetch_manifest(asset_version, resolved_commit)
                if manifest.get("build_status") != "success":
                    continue
                problems = validate_manifest(
                    manifest, asset_version=asset_version, expected_head_commit=resolved_commit
                )
                if not problems:
                    return asset_version, resolved_commit
            except (AssetsMirrorError, ManifestValidationError):
                continue
        raise SyncError(f"no successful generated release found at commit {resolved_commit}")

    def sync_latest(self, *, commit: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """兼容入口：发现最新成功版本，然后**只同步**它，别的什么都不做。

        名字说明它解析的是什么（最新成功 generated 版本）；保留它只是为了让既有调用方与
        运维手册不断链。它被刻意限定为“发现 + 同步”：镜像不得移动本地默认版本、不得删除
        版本（那是显式 :meth:`activate_current` 与 :meth:`prune` 的职责）。调用方按报告里的
        ``asset_version`` / ``snapshot_commit`` 自行决定后续动作。

        ``keep_versions`` / ``persist_keep`` 随隐式清理一起移除：再传这两个参数会得到
        ``TypeError``——宁可硬失败，也不静默丢弃运维的保留请求。
        """
        asset_version, resolved_commit = self.latest_generated_version(commit=commit)
        report = self.sync(asset_version, commit=resolved_commit, dry_run=dry_run)
        report["selected_as_latest"] = asset_version
        return report

    # -- sync -------------------------------------------------------------------------

    def sync(self, asset_version: str, *, commit: str | None = None, dry_run: bool = True) -> dict:
        """Mirror ``asset_version``.  ``dry_run`` (the default) writes no bytes at all."""
        _require_asset_version(asset_version)
        started_at = _utc_now()

        resolved_commit = _require_commit(commit, "commit") if commit else self.source.head_commit()
        manifest = self.source.fetch_manifest(asset_version, resolved_commit)
        checksums_text = self.source.fetch_checksums(asset_version, resolved_commit)

        problems = validate_manifest(
            manifest, asset_version=asset_version, expected_head_commit=resolved_commit
        )
        if problems:
            # Fail-closed: nothing has been written and nothing will be.
            raise ManifestValidationError(problems, asset_version=asset_version)

        # The provenance gate runs before anything is enumerated, downloaded or
        # written -- and on a dry run too, because a plan built on a manifest
        # whose commits do not descend from the snapshot it was read at is a
        # plan to publish something unverifiable.  The snapshot is the commit
        # this mirror *actually resolved*, never a value the manifest reports.
        self._validate_provenance(manifest, resolved_commit)

        entries = manifest["entries"]
        digests: list[str] = []
        for entry in entries:
            digest = entry["artifact_sha256"]
            if digest not in digests:
                digests.append(digest)

        missing = [digest for digest in digests if not self.pool.has(digest)]
        skipped_existing = len(entries) - sum(1 for e in entries if e["artifact_sha256"] in missing)

        base_report: dict[str, Any] = {
            "asset_version": asset_version,
            "source_repository": self.source.repo,
            "source_ref": self.source.branch,
            # What the manifest says the release was built from, and what this
            # mirror actually read it at: two different facts, two fields.  A
            # receipt that named one field "source_commit" for both would make
            # the reader guess which of the two it was looking at.
            "source_commit": manifest.get("source_commit"),
            "snapshot_commit": resolved_commit,
            "generated_commit": manifest.get("generated_commit"),
            "entry_count": len(entries),
            "object_count": len(digests),
            "skipped_existing": skipped_existing,
            "bytes_to_download": None,
        }

        if dry_run:
            return {
                **base_report,
                "schema_version": SCHEMA_VERSION,
                "mode": "dry-run",
                "sync_status": "planned",
                "would_write": False,
                "to_download": [
                    {
                        "object_path": e["object_path"],
                        "artifact_sha256": e["artifact_sha256"],
                        "logical_paths": [x["logical_path"] for x in entries
                                          if x["artifact_sha256"] == e["artifact_sha256"]],
                    }
                    for e in entries if e["artifact_sha256"] in missing
                ],
                "started_at": started_at,
            }

        downloaded = 0
        downloaded_bytes = 0
        pending = [digest for digest in digests if not self.pool.has(digest)]
        if pending:
            # Fetch concurrently, write in this thread.  Every object is an
            # independent content-addressed GET, so the only shared state is the
            # pool, and keeping ``put_bytes`` here leaves its atomic rename and
            # its "already present" answer exactly as they were.  The first
            # failure still aborts the whole version before anything is
            # published: the remaining requests are cancelled and the exception
            # propagates out of ``sync``.
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.object_workers) as executor:
                futures = {
                    executor.submit(self.source.fetch_object, digest, resolved_commit): digest
                    for digest in pending
                }
                try:
                    for future in concurrent.futures.as_completed(futures):
                        digest = futures[future]
                        data = future.result()
                        if self.pool.put_bytes(digest, data):
                            downloaded += 1
                            downloaded_bytes += len(data)
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise

        verification = verify_manifest_objects(manifest, self.pool, checksums_text=checksums_text)
        completed_at = _utc_now()
        state_base = {
            "schema_version": SCHEMA_VERSION,
            "state_schema": STATE_SCHEMA,
            "asset_version": asset_version,
            "source_repository": self.source.repo,
            "source_ref": self.source.branch,
            "source_commit": manifest.get("source_commit"),
            "snapshot_commit": resolved_commit,
            "generated_commit": manifest.get("generated_commit"),
            "sync_started_at": started_at,
            "sync_completed_at": completed_at,
            "entry_count": len(entries),
            "object_count": len(digests),
            "skipped_existing": skipped_existing,
            "downloaded": downloaded,
            "downloaded_bytes": downloaded_bytes,
        }

        if not verification.ok:
            # Objects already written are deliberately left in place (they are content-addressed
            # and therefore harmless); the version is marked failed and never published.
            _write_json(self.state_path(asset_version), {**state_base, "sync_status": "failed",
                                                         "verification": verification.to_dict()})
            raise SyncError(
                f"post-write verification failed for asset_version={asset_version}; "
                f"{len(verification.missing)} missing, {len(verification.mismatched)} mismatched, "
                f"{len(verification.checksum_mismatches)} checksum mismatches "
                f"(state marked failed, objects kept, no rollback)"
            )

        self.manifest_path(asset_version).parent.mkdir(parents=True, exist_ok=True)
        _write_json(self.manifest_path(asset_version), manifest)
        self.checksums_path(asset_version).write_text(checksums_text, encoding="utf-8")
        _write_json(self.state_path(asset_version), {**state_base, "sync_status": "success",
                                                     "verification": verification.to_dict()})

        return {
            **base_report,
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "sync_status": "success",
            "would_write": True,
            "downloaded": downloaded,
            "downloaded_bytes": downloaded_bytes,
            "started_at": started_at,
            "completed_at": completed_at,
            "verification": verification.to_dict(),
        }

    # -- verify -----------------------------------------------------------------------

    def verify(self, asset_version: str) -> dict:
        """Re-verify an already-published version against the local pool."""
        manifest = _read_json(self.manifest_path(asset_version))
        if manifest is None:
            raise SyncError(
                f"asset_version={asset_version} is not published under {self.published_dir} "
                f"(no manifest.json); run sync --apply first"
            )
        checksums_text = None
        try:
            checksums_text = self.checksums_path(asset_version).read_text(encoding="utf-8")
        except FileNotFoundError:
            checksums_text = None
        result = verify_manifest_objects(manifest, self.pool, checksums_text=checksums_text)
        state = _read_json(self.state_path(asset_version)) or {}
        return {
            "asset_version": asset_version,
            "sync_status": state.get("sync_status"),
            "entry_count": len(manifest.get("entries", [])),
            "verification": result.to_dict(),
            "ok": result.ok,
        }

    # -- resolve ----------------------------------------------------------------------

    def resolve(self, asset_version: str, logical_path: str, *, require_object: bool = True) -> ResolvedObject | None:
        """Map an original logical resource path to the mirrored object (exact match only)."""
        _require_asset_version(asset_version)
        state = _read_json(self.state_path(asset_version))
        if state is None or state.get("sync_status") != "success":
            return None
        manifest = _read_json(self.manifest_path(asset_version))
        if manifest is None or manifest.get("asset_version") != asset_version:
            return None
        for entry in manifest.get("entries", []):
            if not isinstance(entry, dict) or entry.get("logical_path") != logical_path:
                continue
            digest = entry.get("artifact_sha256")
            if not isinstance(digest, str) or not HEX64_RE.match(digest):
                return None
            target = self.pool.existing_path(digest)
            if require_object and target is None:
                return None
            size = entry.get("size")
            if not isinstance(size, int):
                size = target.stat().st_size if target is not None else None
            return ResolvedObject(
                logical_path=logical_path,
                object_path=str(entry.get("object_path")),
                artifact_sha256=digest,
                pool_path=str(target) if target is not None else "",
                size=size,
            )
        return None

    # -- list -------------------------------------------------------------------------

    def list_versions(self) -> list[dict]:
        """Every published version and its sync status (versions coexist)."""
        versions: list[dict] = []
        if not self.published_dir.is_dir():
            return versions
        for candidate in sorted(self.published_dir.iterdir()):
            if not candidate.is_dir() or not ASSET_VERSION_RE.match(candidate.name):
                continue
            state = _read_json(candidate / "state.json") or {}
            manifest = _read_json(candidate / "manifest.json") or {}
            versions.append({
                "asset_version": candidate.name,
                "sync_status": state.get("sync_status", "unknown"),
                "entry_count": state.get("entry_count", len(manifest.get("entries", []))),
                "object_count": state.get("object_count"),
                "source_commit": state.get("source_commit"),
                "generated_commit": state.get("generated_commit"),
                # What the manifest was READ from.  It may be later than
                # generated_commit (a bot commit or documentation commit landed
                # on top), which is exactly why the two are separate fields.
                "snapshot_commit": state.get("snapshot_commit",
                                             manifest.get("snapshot_commit")),
                "sync_completed_at": state.get("sync_completed_at"),
                "manifest_present": (candidate / "manifest.json").is_file(),
            })
        return versions


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _add_common_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    """``--root`` / ``--repo`` / ``--branch`` are accepted before *and* after the subcommand.

    The copies attached to subcommands use ``SUPPRESS`` defaults so an omitted flag never
    overwrites the value parsed from the parent parser.
    """
    if suppress_defaults:
        parser.add_argument("--root", default=argparse.SUPPRESS,
                            help="mirror root holding objects/sha256/ and published/")
        parser.add_argument("--repo", default=argparse.SUPPRESS, help="source repository")
        parser.add_argument("--branch", default=argparse.SUPPRESS, help="source branch")
        parser.add_argument("--object-workers", type=int, default=argparse.SUPPRESS,
                            help="objects to fetch at once")
        return
    parser.add_argument("--root", default=os.environ.get("MLTD_MIRROR_ROOT", "."),
                        help="mirror root holding objects/sha256/ and published/ (env MLTD_MIRROR_ROOT)")
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY, help="source repository")
    parser.add_argument("--branch", default=DEFAULT_BRANCH,
                        help="source branch (default branch of the assets repo)")
    parser.add_argument("--object-workers", type=int, default=None,
                        help="objects to fetch at once (default "
                             f"{DEFAULT_OBJECT_WORKERS}, or MLTD_MIRROR_OBJECT_WORKERS)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assets_mirror",
        description="Mirror explicit generated asset_versions; activate and prune are separate, explicit actions.",
    )
    _add_common_options(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="mirror one asset_version (dry-run unless --apply)")
    sync.add_argument("--asset-version", required=True)
    sync.add_argument("--commit", default=None, help="pin the source commit (40-hex); default: branch HEAD")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                      help="plan only, write nothing (default)")
    mode.add_argument("--apply", dest="dry_run", action="store_false", help="actually write objects")
    _add_common_options(sync, suppress_defaults=True)

    sync_latest = sub.add_parser(
        "sync-latest",
        help="mirror the newest successful generated version only (no activate, no cleanup)",
    )
    sync_latest.add_argument("--commit", default=None, help="pin the source commit (40-hex; default: branch HEAD)")
    mode = sync_latest.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                      help="plan only, write nothing (default)")
    mode.add_argument("--apply", dest="dry_run", action="store_false",
                      help="write objects and version metadata; no activation, no cleanup")
    _add_common_options(sync_latest, suppress_defaults=True)

    watch = sub.add_parser(
        "watch",
        help="periodically sync the explicit --asset-version values (repeatable; required)",
    )
    watch.add_argument("--asset-version", action="append", default=[],
                       help="numeric asset_version to sync each tick (repeatable; "
                            "comma-separated values are accepted for compose-style callers)")
    watch.add_argument("--interval", type=float, default=21600,
                       help="seconds between refreshes (default: 21600)")
    watch.add_argument("--once", action="store_true", help="run one refresh tick and exit")
    _add_common_options(watch, suppress_defaults=True)

    activate = sub.add_parser(
        "activate",
        help="point the localization current.json at an already published version (explicit)",
    )
    activate.add_argument("--asset-version", required=True)
    _add_common_options(activate, suppress_defaults=True)

    retain = sub.add_parser("retain", help="pin an already published version for automatic cleanup")
    retain.add_argument("--asset-version", required=True)
    _add_common_options(retain, suppress_defaults=True)

    unretain = sub.add_parser("unretain", help="remove a version pin; the next explicit prune --apply may then remove it")
    unretain.add_argument("--asset-version", required=True)
    _add_common_options(unretain, suppress_defaults=True)

    current = sub.add_parser("current", help="show the localization mirror's current version")
    _add_common_options(current, suppress_defaults=True)

    prune = sub.add_parser("prune", help="plan or apply cleanup of unretained versions and orphan objects")
    mode = prune.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                      help="plan only, write nothing (default)")
    mode.add_argument("--apply", dest="dry_run", action="store_false", help="remove unretained data")
    _add_common_options(prune, suppress_defaults=True)

    verify = sub.add_parser("verify", help="re-verify a published asset_version against the local pool")
    verify.add_argument("--asset-version", required=True)
    _add_common_options(verify, suppress_defaults=True)

    resolve = sub.add_parser("resolve", help="map an original logical path to the mirrored object")
    resolve.add_argument("--asset-version", required=True)
    resolve.add_argument("--logical-path", required=True)
    _add_common_options(resolve, suppress_defaults=True)

    list_versions = sub.add_parser("list", help="list published asset_versions")
    _add_common_options(list_versions, suppress_defaults=True)
    return parser


def _normalized_argv(argv: list[str] | None) -> list[str] | None:
    """Translate ``--asset_version`` / ``--logical_path`` to their dashed forms."""
    if argv is None:
        return None
    return [arg.replace("--asset_version", "--asset-version").replace("--logical_path", "--logical-path")
            for arg in argv]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(_normalized_argv(argv))
    root = Path(args.root)
    source = GitHubAssetsSource(repo=args.repo, branch=args.branch)
    mirror = AssetVersionMirror(source, ObjectPool(root), root,
                                object_workers=getattr(args, "object_workers", None))

    def emit(payload: dict) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    try:
        if args.command == "sync":
            try:
                report = mirror.sync(args.asset_version, commit=args.commit,
                                     dry_run=args.dry_run)
            except ManifestValidationError as exc:
                # Section 5.1's rejections are a refusal, not a crash: the exit
                # code says which, and the reasons are already on the exception.
                print(f"refused: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            emit(report)
            if report["mode"] == "dry-run" and report["to_download"]:
                print(
                    f"[dry-run] {len(report['to_download'])} object(s) would be downloaded; "
                    "re-run with --apply to write them",
                    file=sys.stderr,
                )
            return EXIT_OK
        if args.command == "sync-latest":
            try:
                report = mirror.sync_latest(commit=args.commit, dry_run=args.dry_run)
            except ManifestValidationError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            emit(report)
            return EXIT_OK
        if args.command == "watch":
            # 一次刷新 tick 只镜像运维点名的版本：不发现 “latest”（无人值守部署若跟随最新
            # 版本并移动服务指针，就等于出现了第二个发布者）、不激活、不删除。
            # 需要激活或清理时显式运行 `activate` / `prune`。
            watched = list(dict.fromkeys(
                item.strip()
                for value in args.asset_version
                for item in value.split(",")
                if item.strip()
            ))
            if not watched:
                raise AssetsMirrorError(
                    "watch requires at least one explicit --asset-version "
                    "(the refresh never follows 'latest')"
                )
            for version in watched:
                _require_asset_version(version)
            if args.interval <= 0:
                raise AssetsMirrorError("watch interval must be greater than zero")
            while True:
                failed = False
                for version in watched:
                    try:
                        emit(mirror.sync(version, dry_run=False))
                    except RateLimitError:
                        raise
                    except (AssetsMirrorError, OSError) as exc:
                        failed = True
                        print(f"watch refresh failed for asset_version={version}: {exc}",
                              file=sys.stderr)
                if args.once:
                    return EXIT_FAIL if failed else EXIT_OK
                import time
                time.sleep(args.interval)
        if args.command == "activate":
            emit(mirror.activate_current(args.asset_version))
            return EXIT_OK
        if args.command == "retain":
            version = _require_asset_version(args.asset_version)
            versions = mirror.retained_versions()
            versions.add(version)
            emit({"retained": mirror.set_retained_versions(versions)})
            return EXIT_OK
        if args.command == "unretain":
            version = _require_asset_version(args.asset_version)
            versions = mirror.retained_versions()
            versions.discard(version)
            emit({"retained": mirror.set_retained_versions(versions)})
            return EXIT_OK
        if args.command == "current":
            emit({"asset_version": mirror.current_version()})
            return EXIT_OK
        if args.command == "prune":
            emit(mirror.prune(dry_run=args.dry_run))
            return EXIT_OK
        if args.command == "verify":
            report = mirror.verify(args.asset_version)
            emit(report)
            if not report["ok"]:
                print(f"verify failed for asset_version={args.asset_version}", file=sys.stderr)
                return EXIT_FAIL
            return EXIT_OK
        if args.command == "resolve":
            resolved = mirror.resolve(args.asset_version, args.logical_path)
            if resolved is None:
                # Distinguish 'version not published' from 'path unknown' for operators.
                state = _read_json(mirror.state_path(args.asset_version)) or {}
                if state.get("sync_status") != "success":
                    reason = (f"asset_version={args.asset_version} is not published as success "
                              f"(sync_status={state.get('sync_status', 'absent')})")
                else:
                    reason = (f"logical_path={args.logical_path!r} is not in the published manifest of "
                              f"asset_version={args.asset_version} (or its object is missing)")
                print(reason, file=sys.stderr)
                return EXIT_FAIL
            emit(resolved.to_dict())
            return EXIT_OK
        if args.command == "list":
            emit({"root": str(root), "versions": mirror.list_versions()})
            return EXIT_OK
    except RateLimitError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AssetsMirrorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_FAIL  # pragma: no cover - argparse guarantees a known command


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
