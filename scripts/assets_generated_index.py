#!/usr/bin/env python3
"""Content-addressed store for generated MLTD asset releases (channel: ``assets``).

Why this exists
---------------
The localization GitHub repo (``MLTDTranslationAssets``) carries *text* in git
and hosts *binaries* elsewhere.  Generated Unity3D bundles are the exception:
they are the artifacts the assets server actually delivers, and several asset
versions must stay downloadable at the same time while the identical bundle is
shared between them.  A plain "one directory per build" layout duplicates those
bytes on every run.

Layout (``generated/`` is the store root) -- **flat objects**, one file per
digest, no fan-out directory (owner decision, 2026-09-30)::

    generated/
    ├── objects/
    │   └── sha256/
    │       ├── <sha256-object>
    │       └── <sha256-object>
    ├── 1077100/
    │   ├── manifest.json
    │   └── checksums.txt
    └── 1077500/
        ├── manifest.json
        └── checksums.txt

The two-hex fan-out (``objects/sha256/<aa>/<digest>``) is the **legacy** layout:
manifests committed before the flat switch still point at those paths, and the
NAS may still be serving them, so every read path here accepts both forms
(:func:`cas_object_path` is the canonical flat form, :func:`legacy_cas_object_path`
the tolerated one) and an object that exists only as a shard is copied -- never
moved -- to its flat path when a new manifest needs it.  New objects and new
manifests are always flat; ``checksums.txt`` of a new build lists flat paths.

Rules implemented here
----------------------
* One release directory per ``asset_version``; a second successful build of the
  same version **overwrites** the manifest (only the newest build of a version
  is retained).  Different ``asset_version`` directories coexist.
* Identical bytes are stored exactly once (content addressing).  Objects are
  written to a temporary file next to the destination and then ``os.replace``d
  into place, so a reader never observes a half-written object.
* ``manifest.json`` and ``checksums.txt`` are one logical pair: both are staged
  and replaced by :meth:`GeneratedStore.build_release`, the previous bytes are
  restored if the second replace fails, and the pair is read back and compared
  before the build is reported as written.  A release is never reported on the
  strength of having written only one half of the pair.
* A whole candidate release can be staged and promoted in one step with
  :meth:`GeneratedStore.transaction` (``with store.transaction(prune=True) as
  staged_store:``): the live store root is untouched until the staging tree has
  been fully validated, and a failure while promoting -- or an exception inside
  the block -- rolls the root back to its previous bytes.
* A manifest entry carries ``logical_path`` (the original game asset path) and,
  for bundles whose client-side catalog maps them to a hashed remote name,
  ``runtime_path`` (the path the game actually requests).  The server keeps
  accepting the logical path for diagnostics and old consumers, while the
  runtime path is the first-class lookup key for a client-compatible mirror.
  Both names resolve to the same content-addressed ``object_path``.
* ``client_version`` (client axis) and ``asset_version`` (assets axis) are
  INDEPENDENT.  Combined identities such as ``9.0.200+1077100`` or
  ``client-9.0.200-assets-1077100`` are rejected outright.
* The manifest carries the full provenance tuple the architecture fixes:
  ``asset_version``, ``client_version`` (always ``null`` here),
  ``source_client_version``, ``source_commit``, ``translation_commit``,
  ``generated_commit``, ``ci_run_id`` and, per entry, ``artifact_sha256``.
  ``ci_run_id`` is captured from the runner's own environment when the caller
  does not pass one, and stays ``null`` when there is none -- it is never
  fabricated.  Every entry also carries ``resource_kind``, because the logical
  path alone does not say whether a payload is a bundle, a texture or audio.
* A build whose ``build_status`` is not ``success`` touches nothing at all --
  not even the store root directory (explicit early return, asserted by tests).
* Objects are only ever deleted by the explicit :meth:`GeneratedStore.prune_orphans`
  sweep (or by a promoted :meth:`GeneratedStore.transaction` with ``prune=True``),
  never as a side effect of a build.  Pruning never removes an object that
  *any* retained manifest references -- a shared object keeps its bytes when one
  of the two versions that reference it is dropped -- and it never touches a
  legacy shard that a retained manifest still points at.  Git history is
  untouched either way.

Two orthogonal state dimensions
-------------------------------
``reuse_status`` (is the *official source asset* still compatible?):

===============  ======================================================
exact            ``source_sha256`` unchanged -> automatic reuse
verified-compatible  source changed, but a recorded verification authorises it
suggested        only path/name/text/visual similarity -> NEVER auto-published
blocked          no compatibility evidence -> reuse forbidden
===============  ======================================================

``translation_status`` (did the *translated content* change?):
``untranslated`` / ``pending`` / ``accepted`` / ``modified`` / ``reused``.

An entry may enter ``generated/<asset_version>/`` only when
``reuse_status in {exact, verified-compatible}`` AND
``translation_status in {accepted, modified, reused}``.  A changed translation
on an unchanged source is ``exact`` + ``modified`` -- it must NOT be dressed up
as ``verified-compatible``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

MANIFEST_KIND = "mltd-assets-generated-manifest"
MANIFEST_SCHEMA_VERSION = 1
OBJECTS_DIRNAME = "objects"
HASH_ALGO = "sha256"
CHECKSUMS_NAME = "checksums.txt"
MANIFEST_NAME = "manifest.json"

#: SHA-256 of an empty file.  An empty object is still an object; what it is
#: *not* is the same thing as an object that is missing from the store.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
ASSET_VERSION_RE = re.compile(r"^[0-9]+$")
CLIENT_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
# A workflow run id, optionally the GitHub ``<run_id>.<run_attempt>`` pair.
CI_RUN_ID_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")

#: Repository whose history the provenance commits are checked against.
DEFAULT_PROVENANCE_REPOSITORY = "kohakunamori/MLTDTranslationAssets"
GITHUB_API_BASE = "https://api.github.com"

#: Git object names accepted by the read-only compare endpoint.  The
#: ``<base>...<head>`` form reports ``head`` relative to ``base``: "ahead" means
#: ``head`` contains ``base``, "identical" that they are the same object.
COMPARE_STATUS_CONTAINS_BASE = ("ahead", "identical")
# Markers that betray a combined "client+assets" identity.
COMBINED_VERSION_MARKERS = ("+", "-assets-")

# Environment variables a CI runner sets for the run currently executing.  The
# first non-empty one is recorded as ``ci_run_id`` when the caller does not pass
# one explicitly, so a runner that exports the standard variable cannot forget
# to carry it into the manifest.  Nothing is invented when none is set: the
# field stays ``null`` and the caller may fill it in.
CI_RUN_ID_ENV_VARS = ("MLTD_CI_RUN_ID", "GITHUB_RUN_ID")

# ``resource_kind`` tells a consumer what it is about to store, because the
# logical path alone does not: the official host serves bundles and loose
# payloads from the same namespace, and the image and text surfaces both end in
# ``.unity3d``/``.gtx`` names that carry no such signal.
RESOURCE_KIND_BUNDLE = "bundle"
RESOURCE_KIND_TEXTURE = "texture"
RESOURCE_KIND_AUDIO = "audio"
RESOURCE_KIND_OTHER = "other"
ALL_RESOURCE_KINDS = (RESOURCE_KIND_BUNDLE, RESOURCE_KIND_TEXTURE,
                      RESOURCE_KIND_AUDIO, RESOURCE_KIND_OTHER)

ALL_REUSE_STATUSES = ("exact", "verified-compatible", "suggested", "blocked")
ALL_TRANSLATION_STATUSES = ("untranslated", "pending", "accepted", "modified", "reused")
ADMISSIBLE_REUSE_STATUSES = ("exact", "verified-compatible")
ADMISSIBLE_TRANSLATION_STATUSES = ("accepted", "modified", "reused")

REJECT_HINT = {
    "suggested": ("path/name similarity is not compatibility evidence; manual review only "
                  "(仅供人工参考) -- record a verification before reusing"),
    "blocked": "no compatibility evidence for the new source; reuse is forbidden",
    "untranslated": "no translation exists for this entry yet",
    "pending": "translation is still a draft awaiting review; never auto-published",
}

_ENTRY_REQUIRED_FIELDS = (
    "channel",
    "asset_version",
    "client_version",
    "source_client_version",
    "source_commit",
    "translation_commit",
    "generated_commit",
    "ci_run_id",
    "logical_key",
    "logical_path",
    "resource_kind",
    "source_sha256",
    "translated_sha256",
    "object_path",
    "artifact_sha256",
    "reuse_status",
    "translation_status",
)


class GeneratedStoreError(RuntimeError):
    """A hard failure: nothing was written, or the store is inconsistent."""


class VersionIdentityError(GeneratedStoreError):
    """A combined/ambiguous version identity was supplied."""


class ReleaseCommitError(GeneratedStoreError):
    """A release's provenance commits are not what the manifest claims.

    Raised both for a manifest that cannot be checked (the comparison could not
    be made) and for one that is checkably wrong, because in both cases the
    correct action is the same: do not publish it.
    """

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(
            [f"release provenance rejected ({len(self.problems)} problem(s))"]
            + [f"  - {problem}" for problem in self.problems]))


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def sha256_bytes(payload: bytes) -> str:
    """Lowercase hex SHA-256 of ``payload``."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """Lowercase hex SHA-256 of the file at ``path`` (streamed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cas_object_path(digest: str) -> PurePosixPath:
    """Canonical store-relative object path: ``objects/sha256/<digest>``.

    Flat by design (owner decision, 2026-09-30): one directory per digest level
    is unnecessary for a pool whose size is bounded by the asset cohort, and a
    flat path makes ``checksums.txt`` and the NAS side mapping a direct
    ``<objects>/sha256/<digest>`` lookup.
    """
    if not isinstance(digest, str) or not HEX64_RE.fullmatch(digest):
        raise ValueError(f"not a lowercase sha256 hex digest: {digest!r}")
    return PurePosixPath(OBJECTS_DIRNAME, HASH_ALGO, digest)


def legacy_cas_object_path(digest: str) -> PurePosixPath:
    """The retired two-hex fan-out path: ``objects/sha256/<aa>/<digest>``.

    Read-only compatibility.  Manifests committed before the flat switch embed
    this form in ``object_path``, and a NAS pool may still hold the bytes only
    under it; :meth:`GeneratedStore.object_file` therefore falls back to this
    path and a build copies such an object to the flat location rather than
    pretending it is missing.  It is never written by a new build.
    """
    if not isinstance(digest, str) or not HEX64_RE.fullmatch(digest):
        raise ValueError(f"not a lowercase sha256 hex digest: {digest!r}")
    return PurePosixPath(OBJECTS_DIRNAME, HASH_ALGO, digest[:2], digest)


def validate_asset_version(value: Any) -> str:
    """Return the asset version, or refuse a combined/ambiguous identity."""
    text = str(value)
    for marker in COMBINED_VERSION_MARKERS:
        if marker in text:
            raise VersionIdentityError(
                f"asset_version {value!r} contains {marker!r}: client and assets versions are "
                "independent axes; never combine them (e.g. '9.0.200+1077100' is invalid)"
            )
    if not ASSET_VERSION_RE.fullmatch(text):
        raise VersionIdentityError(f"asset_version {value!r} must be digits only (e.g. '1077100')")
    return text


def validate_source_client_version(value: Any) -> str:
    """Return the *source* client version, or refuse a combined identity."""
    text = str(value)
    for marker in COMBINED_VERSION_MARKERS:
        if marker in text:
            raise VersionIdentityError(
                f"source_client_version {value!r} contains {marker!r}: record the client version "
                "and the asset version in separate fields"
            )
    if not CLIENT_VERSION_RE.fullmatch(text):
        raise VersionIdentityError(
            f"source_client_version {value!r} must look like '9.0.200' (digits and dots)"
        )
    return text


def validate_commit(value: Any, field_name: str) -> str:
    text = str(value).strip().lower()
    if not HEX40_RE.fullmatch(text):
        raise GeneratedStoreError(f"{field_name} must be a 40-char hex commit sha; got {value!r}")
    return text


def validate_ci_run_id(value: Any) -> str | None:
    """Return the CI run identity, or ``None`` when the caller has none.

    This is provenance, not identity: a run id that is absent stays ``None``
    rather than becoming an empty string, and nothing is fabricated from a
    timestamp or a hostname.  What is refused is an id that is neither a run
    number nor a composite ``run``/``run_attempt`` pair, or one carrying a
    combined version marker -- the same fail-closed rule the version axes use.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for marker in COMBINED_VERSION_MARKERS:
        if marker in text:
            raise GeneratedStoreError(
                f"ci_run_id {value!r} contains {marker!r}: a run identity is never a version string"
            )
    if not CI_RUN_ID_RE.fullmatch(text):
        raise GeneratedStoreError(
            f"ci_run_id {value!r} must be digits, optionally as '<run>.<attempt>'"
        )
    return text


def validate_resource_kind(value: Any) -> str:
    """Return the resource kind recorded on an entry."""
    text = str(value).strip().lower()
    if text not in ALL_RESOURCE_KINDS:
        raise GeneratedStoreError(
            f"resource_kind {value!r} is unknown; expected one of "
            f"{', '.join(ALL_RESOURCE_KINDS)}"
        )
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_compare_fetch(url: str, *, timeout: float = 30.0) -> tuple[int, Any]:
    """Read-only GitHub compare, stdlib only: ``(status, decoded JSON)``.

    This is the one place the module talks to the network, and it is only ever a
    ``GET`` of ``/repos/<repo>/compare/<base>...<head>`` -- an endpoint GitHub
    exposes publicly for public repositories.  The token, when the environment
    has one, is read here and never stored, logged or written to a manifest.
    Callers that must stay offline inject their own ``fetchImpl`` instead.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", "mltd-assets-generated-index")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("MLTD_ASSETS_GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        body = exc.read() if hasattr(exc, "read") else b""
        status = exc.code
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise ReleaseCommitError(
            [f"cannot reach the GitHub compare endpoint for {url}: {exc.reason}"]
        ) from exc
    try:
        return int(status), json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise ReleaseCommitError([f"compare endpoint returned invalid JSON for {url}: {exc}"]) from exc


def compare_commit_status(
    base: str,
    head: str,
    *,
    repository: str = DEFAULT_PROVENANCE_REPOSITORY,
    api_base: str = GITHUB_API_BASE,
    fetchImpl: Any = None,
) -> str:
    """The GitHub compare status of ``head`` relative to ``base``.

    Returns ``identical`` / ``ahead`` / ``behind`` / ``diverged``, or raises
    :class:`ReleaseCommitError` when the comparison cannot be made at all (an
    unanswerable question is not a passing answer).  ``base``/``head`` are
    expected to be full 40-hex commit shas; a branch name would also resolve,
    which is why they are validated by the caller.
    """
    base_sha = validate_commit(base, "base")
    head_sha = validate_commit(head, "head")
    url = (f"{api_base.rstrip('/')}/repos/{repository}/compare/"
           f"{base_sha}...{head_sha}")
    fetcher = fetchImpl or default_compare_fetch
    try:
        status, payload = fetcher(url)
    except ReleaseCommitError:
        raise
    except Exception as exc:  # noqa: BLE001 - the caller diagnoses the cause
        raise ReleaseCommitError(
            [f"compare {base_sha[:12]}...{head_sha[:12]} could not be made: {exc}"]) from exc
    if int(status) != 200:
        message = ""
        if isinstance(payload, Mapping):
            message = str(payload.get("message") or "")
        raise ReleaseCommitError([
            f"compare {base_sha[:12]}...{head_sha[:12]} returned HTTP {int(status)}"
            + (f" ({message})" if message else "")
            + "; the provenance cannot be verified, so the manifest is not publishable"
        ])
    if not isinstance(payload, Mapping):
        raise ReleaseCommitError(
            [f"compare {base_sha[:12]}...{head_sha[:12]} returned {type(payload).__name__}, "
             "not a JSON object"])
    declared = payload.get("status")
    total_commits = payload.get("total_commits")
    # The endpoint's own two fields must agree: ``total_commits`` counts the
    # commits ``head`` has that ``base`` does not, so 'identical' can only mean
    # zero and 'ahead' can only mean at least one.  A response that contradicts
    # itself is not evidence that the commit is an ancestor.
    if isinstance(total_commits, int):
        if total_commits == 0 and declared == "ahead":
            raise ReleaseCommitError([
                f"compare {base_sha[:12]}...{head_sha[:12]} says ahead with total_commits=0"
            ])
        if total_commits > 0 and declared == "identical":
            raise ReleaseCommitError([
                f"compare {base_sha[:12]}...{head_sha[:12]} says identical with "
                f"total_commits={total_commits}"
            ])
    if declared not in COMPARE_STATUS_CONTAINS_BASE:
        raise ReleaseCommitError([
            f"commit {head_sha[:12]} does not contain {base_sha[:12]} "
            f"(compare status={declared!r}); a manifest may only record commits from the "
            "history of the snapshot it was built from"
        ])
    return str(declared)


def releases_from_document(document: Any) -> list[dict[str, Any]]:
    """Every release manifest in a document, feeding the commit checker.

    Accepts the shapes the repo actually produces: one manifest, a list of
    manifests, or ``{"releases": [...]}`` / ``{"manifests": [...]}``.
    """
    if isinstance(document, Mapping):
        if document.get("kind") == MANIFEST_KIND or "entries" in document:
            return [dict(document)]
        for key in ("releases", "manifests"):
            value = document.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
        raise GeneratedStoreError(
            "release document must be a manifest, a list of manifests, or an object with "
            "'releases'/'manifests'")
    if isinstance(document, list):
        return [dict(item) for item in document if isinstance(item, Mapping)]
    raise GeneratedStoreError("release document must be a JSON object or list")


def _manifest_commit_fields(manifest: Mapping[str, Any]) -> dict[str, str]:
    """The three commits a manifest declares, validated as 40-hex."""
    fields: dict[str, str] = {}
    for name in ("source_commit", "translation_commit", "generated_commit"):
        value = manifest.get(name)
        if not isinstance(value, str) or not HEX40_RE.fullmatch(value):
            raise ReleaseCommitError(
                [f"manifest is missing a usable {name} (got {value!r}); the provenance "
                 "tuple cannot be checked without it"])
        fields[name] = value
    return fields


def check_release_commits(
    manifest: Mapping[str, Any],
    *,
    snapshot_commit: str | None,
    repository: str = DEFAULT_PROVENANCE_REPOSITORY,
    api_base: str = GITHUB_API_BASE,
    fetchImpl: Any = None,
) -> list[str]:
    """Check the provenance tuple of one manifest against the snapshot commit.

    The rule ``generated_commit == HEAD`` is **withdrawn** (owner decision,
    2026-09-30).  A CI that generates a release, commits it and then lands a
    documentation commit on top leaves the manifest's ``generated_commit``
    permanently behind the branch head, so the equality check refused exactly
    the manifests the pipeline is built to produce -- and it never checked the
    thing that actually matters.  What replaces it:

    * ``generated_commit`` is the commit that **already contained the generator
      inputs** the build consumed.  It is by construction an ancestor of (or
      identical to) the snapshot the manifest was fetched at.
    * ``snapshot_commit`` is the commit the release was **resolved at** by the
      consumer doing the reading -- the branch SHA a mirror actually got.  It is
      the consumer's fact and lives in the consumer's state, not in the
      manifest, which is why it is an argument here rather than a manifest
      field.  It may be *after* ``generated_commit`` (a bot commit or a later
      documentation commit landed on top) -- that is the whole point.
    * ``source_commit``, ``translation_commit`` and ``generated_commit`` must
      each be an ancestor of, or identical to, ``snapshot_commit``.  Nothing
      may cite a commit that did not exist in the history it descends from.

    The ancestry question is answered by the read-only GitHub compare endpoint
    (``/compare/<a>...<b>``, status in ``{ahead, identical}``), reached through
    an injectable ``fetchImpl`` so the whole check is testable offline.

    Returns the list of problems; an empty list means the manifest may be
    published.  Raises :class:`ReleaseCommitError` if ``snapshot_commit`` is not
    a usable commit -- an unanswerable question is not a passing answer.
    """
    snapshot = validate_commit(snapshot_commit, "snapshot_commit")
    fields = _manifest_commit_fields(manifest)
    problems: list[str] = []
    for name, value in fields.items():
        if value == snapshot:
            continue  # an object trivially contains itself; no request needed
        try:
            compare_commit_status(value, snapshot, repository=repository,
                                  api_base=api_base, fetchImpl=fetchImpl)
        except ReleaseCommitError as exc:
            for problem in exc.problems:
                problems.append(f"{name}={value[:12]}...: {problem}")
    return problems


def _env_ci_run_id() -> str | None:
    """The CI run id the platform already put in the environment, if any.

    An empty value is ``None``: a step that exports ``GITHUB_RUN_ID=""`` has not
    identified a run, and recording the empty string as an identity would make
    two different runs look the same.
    """
    for name in CI_RUN_ID_ENV_VARS:
        value = os.environ.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _resolve_logical_path(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise GeneratedStoreError("logical_path must not be empty")
    if "\\" in text or text.startswith("/") or ".." in text.split("/"):
        raise GeneratedStoreError(f"logical_path {value!r} must be a clean relative posix path")
    return text


def _resolve_runtime_path(value: Any) -> str:
    """Validate the optional client-facing path in a generated entry."""
    return _resolve_logical_path(value)


# --------------------------------------------------------------------------- #
# value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CasObject:
    """A stored object: its digest, its store-relative path and its bytes on disk."""

    digest: str
    rel_path: str
    path: Path
    size: int
    deduped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "object_path": self.rel_path,
            "size": self.size,
            "deduped": self.deduped,
        }


@dataclass
class BuildResult:
    """Outcome of :meth:`GeneratedStore.build_release`."""

    asset_version: str
    status: str
    written: bool
    manifest_path: Path | None = None
    checksums_path: Path | None = None
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    objects_written: int = 0
    objects_deduped: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_version": self.asset_version,
            "build_status": self.status,
            "written": self.written,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "checksums_path": str(self.checksums_path) if self.checksums_path else None,
            "accepted": len(self.accepted),
            "rejected": self.rejected,
            "counts": self.counts,
            "objects_written": self.objects_written,
            "objects_deduped": self.objects_deduped,
            "note": self.note,
        }


@dataclass
class VerifyReport:
    """Result of :meth:`GeneratedStore.verify_release`."""

    asset_version: str
    ok: bool
    checked_objects: int = 0
    failures: list[str] = field(default_factory=list)
    #: Things that are accepted but not what a current build would write -- a
    #: manifest still naming the retired fan-out layout, for instance.  Kept
    #: apart from ``failures`` because a release that predates the layout change
    #: is readable, not broken.
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_version": self.asset_version,
            "ok": self.ok,
            "checked_objects": self.checked_objects,
            "failures": self.failures,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
class GeneratedStore:
    """Content-addressed store rooted at ``generated/``."""

    def __init__(self, root: Path, *, snapshot_commit: str | None = None,
                 provenance_repository: str = DEFAULT_PROVENANCE_REPOSITORY,
                 provenance_fetch: Any = None):
        """``snapshot_commit`` is the commit this store's manifests were READ at.

        It is optional and stays ``None`` for the plain on-disk store: a store
        does not know which commit it was checked out at, so inventing one (say,
        from ``generated_commit``) would silently assert an ancestry nobody
        verified.  A consumer that resolved the branch passes the SHA it
        actually got -- that is the receipt-side fact -- and the comparison runs
        against the read-only compare endpoint through an injectable fetch, so
        it stays testable offline.
        """
        self.root = Path(root)
        self.snapshot_commit = snapshot_commit
        self.provenance_repository = provenance_repository
        self.provenance_fetch = provenance_fetch

    # -- paths -------------------------------------------------------------- #
    @property
    def objects_dir(self) -> Path:
        return self.root / OBJECTS_DIRNAME / HASH_ALGO

    def object_path(self, digest: str) -> Path:
        """Canonical (flat) on-disk path of an object."""
        return self.root / cas_object_path(digest)

    def legacy_object_path(self, digest: str) -> Path:
        """The retired fan-out path of an object (read-only compatibility)."""
        return self.root / legacy_cas_object_path(digest)

    def object_file(self, digest: str) -> Path | None:
        """Where the bytes of ``digest`` actually are, flat first then shard.

        Returns ``None`` -- not a path -- when the object is absent in both
        forms, so a caller cannot mistake "the canonical path I would have
        written" for "the bytes that exist".
        """
        flat = self.object_path(digest)
        if flat.is_file():
            return flat
        legacy = self.legacy_object_path(digest)
        if legacy.is_file():
            return legacy
        return None

    def object_rel_path(self, digest: str) -> str:
        """The store-relative path of the bytes that exist, or the flat path."""
        found = self.object_file(digest)
        return (found.relative_to(self.root).as_posix() if found is not None
                else cas_object_path(digest).as_posix())

    def _adopt_legacy_object(self, digest: str) -> Path | None:
        """Give a shard-only object a flat home.

        The bytes are **copied**, never moved: a manifest committed earlier
        still points at ``objects/sha256/<aa>/<digest>``, and it must keep
        resolving after this release is built.  Both paths then hold identical
        bytes, which is the price of the layout change -- and the reason
        :meth:`prune_orphans` only sweeps a shard no retained manifest uses.
        """
        legacy = self.legacy_object_path(digest)
        if not legacy.is_file():
            return None
        flat = self.object_path(digest)
        if flat.is_file():
            return flat
        flat.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(prefix=f".{digest[:12]}.", suffix=".tmp",
                                            dir=str(flat.parent))
        try:
            with os.fdopen(handle, "wb") as out, open(legacy, "rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, flat)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return flat

    def release_dir(self, asset_version: str) -> Path:
        return self.root / validate_asset_version(asset_version)

    def manifest_path(self, asset_version: str) -> Path:
        return self.release_dir(asset_version) / MANIFEST_NAME

    def checksums_path(self, asset_version: str) -> Path:
        return self.release_dir(asset_version) / CHECKSUMS_NAME

    # -- object writes ------------------------------------------------------ #
    def put_object(self, src: Path) -> CasObject:
        """Store the bytes of ``src`` under their SHA-256 and return the reference.

        Re-putting identical bytes is a no-op (the existing object is kept, the
        file is not rewritten).  An existing object whose bytes do NOT match its
        name is a corrupted store and raises instead of being silently healed.
        """
        src = Path(src)
        if not src.is_file():
            raise GeneratedStoreError(f"source file does not exist: {src}")
        if src.stat().st_size == 0:
            raise GeneratedStoreError(
                f"refusing to store an empty file: {src}. An empty object carries no bytes, "
                "so it is indistinguishable from a failed download or a lost artifact; a "
                "surface that produced nothing must be an error, not a zero-byte payload."
            )
        digest = sha256_file(src)
        rel_path = cas_object_path(digest).as_posix()
        dest = self.object_path(digest)
        size = src.stat().st_size

        if dest.is_file():
            existing = sha256_file(dest)
            if existing == digest:
                return CasObject(digest, rel_path, dest, size, deduped=True)
            raise GeneratedStoreError(
                f"store is corrupt: {rel_path} hashes to {existing}; refusing to overwrite"
            )

        if self.legacy_object_path(digest).is_file():
            # Same bytes, older layout: adopt them into the flat tree instead of
            # writing a second copy from the caller's file (which may be gone).
            adopted = self._adopt_legacy_object(digest)
            if adopted is not None:
                return CasObject(digest, rel_path, adopted, size, deduped=True)

        dest.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(prefix=f".{digest[:12]}.", suffix=".tmp",
                                            dir=str(dest.parent))
        try:
            with os.fdopen(handle, "wb") as out, open(src, "rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return CasObject(digest, rel_path, dest, size, deduped=False)

    # -- build -------------------------------------------------------------- #
    def build_release(
        self,
        asset_version: Any,
        entries: Sequence[Mapping[str, Any]],
        *,
        source_client_version: Any,
        source_commit: Any,
        translation_commit: Any,
        generated_commit: Any,
        ci_run_id: Any = None,
        build_status: str = "success",
        entries_base: Path | None = None,
    ) -> BuildResult:
        """Publish one ``asset_version`` release into the store.

        ``build_status`` other than ``"success"`` returns immediately with
        ``written=False`` and touches nothing on disk -- not even the store
        root.  ``entries_base`` is the directory that relative
        ``artifact_file``/``source_file`` paths in ``entries`` resolve against.

        ``ci_run_id`` defaults to the runner's own environment
        (:data:`CI_RUN_ID_ENV_VARS`); pass ``None`` explicitly to record no run
        identity at all.  It is provenance and never invented.

        The manifest records the three *input* commits (source, translation,
        generated).  The commit a consumer later reads the release from is the
        consumer's own business -- a mirror resolves the branch and records that
        SHA in its own state -- so it is not written here: putting a
        receipt-side fact into the build's manifest is exactly the confusion
        between "inputs this was built from" and "snapshot this was received at"
        that :func:`check_release_commits` exists to keep apart.
        """
        version = validate_asset_version(asset_version)
        client_version = validate_source_client_version(source_client_version)
        run_id = validate_ci_run_id(ci_run_id if ci_run_id is not None else _env_ci_run_id())

        if build_status != "success":
            return BuildResult(
                asset_version=version,
                status=str(build_status),
                written=False,
                note="build_status is not 'success': the store was not touched at all",
            )

        source_sha = validate_commit(source_commit, "source_commit")
        translation_sha = validate_commit(translation_commit, "translation_commit")
        generated_sha = validate_commit(generated_commit, "generated_commit")
        base = Path(entries_base) if entries_base is not None else Path.cwd()

        accepted, rejected = self._admit_entries(version, client_version, entries, base)

        # 校验 runtime_path 的全批唯一性，且必须早于任何 put_object 写对象/发布文件
        # 的动作：否则一次失败的 build 会先改变对象池，再在发布前才报错。一个客户端
        # 路径只能映射到一个 logical_key，出现冲突时整个 build 失败且不落任何字节。
        # （与主仓 :975-990 及 test_duplicate_runtime_path_fails_closed_before_any_store_change 一致）
        runtime_paths: dict[str, str] = {}
        for item in accepted:
            runtime_path = item.get("runtime_path")
            if runtime_path is None:
                continue
            prior = runtime_paths.get(runtime_path)
            if prior is not None and prior != item["logical_key"]:
                raise GeneratedStoreError(
                    f"runtime_path {runtime_path!r} is used by both {prior!r} and "
                    f"{item['logical_key']!r}; one client path cannot map ambiguously"
                )
            runtime_paths[runtime_path] = item["logical_key"]

        manifest_entries: list[dict[str, Any]] = []
        objects_written = 0
        objects_deduped = 0
        for entry in accepted:
            resolved = dict(entry)
            if resolved.pop("_artifact_file", None) is not None:
                obj = self.put_object(Path(resolved.pop("_artifact_abs")))
                objects_written += 0 if obj.deduped else 1
                objects_deduped += 1 if obj.deduped else 0
            manifest_entries.append(self._finalise_entry(resolved, {
                "asset_version": version,
                "client_version": None,
                "source_client_version": client_version,
                "source_commit": source_sha,
                "translation_commit": translation_sha,
                "generated_commit": generated_sha,
                "ci_run_id": run_id,
            }))
            manifest_entries[-1].pop("_artifact_abs", None)

        manifest_entries.sort(key=lambda item: (item["logical_path"], item["logical_key"]))
        manifest = {
            "kind": MANIFEST_KIND,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "asset_version": version,
            "client_version": None,
            "source_client_version": client_version,
            "source_commit": source_sha,
            "translation_commit": translation_sha,
            "generated_commit": generated_sha,
            "ci_run_id": run_id,
            "build_status": "success",
            "generated_at_utc": _utc_now(),
            "entry_count": len(manifest_entries),
            "entries": manifest_entries,
            "reuse_summary": _reuse_summary(manifest_entries, rejected),
        }

        digests = sorted({entry["artifact_sha256"] for entry in manifest_entries})
        checksums_text = "".join(
            f"{digest}  {cas_object_path(digest).as_posix()}\n" for digest in digests
        )

        # Only now, with every entry validated and every object on disk, do we
        # touch the release directory.  A second successful build of the same
        # asset_version simply overwrites the manifest: the previous build's
        # objects stay on disk until an explicit prune.
        manifest_path = self.manifest_path(version)
        checksums_path = self.checksums_path(version)
        self._write_release_pair(
            manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            checksums_path, checksums_text)

        return BuildResult(
            asset_version=version,
            status="success",
            written=True,
            manifest_path=manifest_path,
            checksums_path=checksums_path,
            accepted=manifest_entries,
            rejected=rejected,
            counts=manifest["reuse_summary"],
            objects_written=objects_written,
            objects_deduped=objects_deduped,
            note=("previous manifest for this asset_version (if any) was replaced; "
                  "its unreferenced objects are left for an explicit prune"),
        )

    # -- the release pair --------------------------------------------------- #
    @staticmethod
    def _release_pair_is_consistent(manifest_path: Path, checksums_path: Path) -> None:
        """The written manifest and checksums must describe each other.

        Raises :class:`GeneratedStoreError` naming the first disagreement.  A
        release that is reported as written but whose own two files contradict
        each other is worse than one that failed: the NAS accepts it and the
        contradiction only surfaces at serve time.
        """
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GeneratedStoreError(
                f"release manifest {manifest_path} is not readable JSON after writing: {exc}"
            ) from exc
        entries = manifest.get("entries") if isinstance(manifest, Mapping) else None
        if not isinstance(entries, list):
            raise GeneratedStoreError(
                f"release manifest {manifest_path} has no entries list after writing")
        declared = [entry.get("artifact_sha256") for entry in entries
                    if isinstance(entry, Mapping)]
        noted = manifest.get("entry_count")
        if noted is not None and noted != len(entries):
            raise GeneratedStoreError(
                f"release manifest {manifest_path} declares entry_count={noted!r} with "
                f"{len(entries)} entries; manifest/checksums would disagree")
        try:
            checksums_text = checksums_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GeneratedStoreError(
                f"release checksums {checksums_path} is not readable after writing: {exc}"
            ) from exc
        listed: dict[str, str] = {}
        for line in checksums_text.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise GeneratedStoreError(
                    f"release checksums {checksums_path} has a malformed line: {line!r}")
            digest, rel = parts[0].strip(), parts[1].strip()
            listed[rel] = digest
        # The digest is what makes a row "the same object", but the pair must
        # also promise the same *place*: a manifest that names the flat path and
        # a checksums file that lists the shard describe two different layouts
        # for one release, and a consumer resolving one against the other gets a
        # file it cannot find.  A release written before the flat switch is
        # consistent in both files, so the exact-path rule costs it nothing.
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            digest = entry.get("artifact_sha256")
            rel = entry.get("object_path")
            if not isinstance(digest, str) or not isinstance(rel, str):
                continue
            listed_digest = listed.get(rel)
            if listed_digest is None:
                raise GeneratedStoreError(
                    f"release checksums {checksums_path} does not list {rel} -> {digest}; "
                    "the manifest and checksums.txt must be one consistent pair")
            if listed_digest != digest:
                raise GeneratedStoreError(
                    f"release checksums {checksums_path} lists {rel} -> {listed_digest}, but "
                    f"the manifest names {digest} there; the manifest and checksums.txt must be "
                    "one consistent pair")
        known_paths = {entry.get("object_path") for entry in entries
                       if isinstance(entry, Mapping)}
        for rel in listed:
            if rel not in known_paths:
                raise GeneratedStoreError(
                    f"release checksums {checksums_path} lists {rel} which no manifest entry "
                    "references; the manifest and checksums.txt must be one consistent pair")

    @classmethod
    def _write_release_pair(cls, manifest_path: Path, manifest_text: str,
                            checksums_path: Path, checksums_text: str) -> None:
        """Replace ``manifest.json`` and ``checksums.txt`` as one unit.

        Both files are staged next to their destinations before either is moved
        into place, so the window in which the pair can disagree is the two
        ``os.replace`` calls themselves and nothing else.  If the second move
        fails, the previous bytes are put back (best effort -- they are still on
        disk in the backup), and the inconsistency is raised rather than
        reported as a written release.  No temporary file survives either path.
        """
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        staged: dict[Path, str] = {}
        backups: dict[Path, Path] = {}
        moved: list[Path] = []
        try:
            for path, text in ((manifest_path, manifest_text),
                               (checksums_path, checksums_text)):
                fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".staged",
                                            dir=str(path.parent))
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                    out.write(text)
                    out.flush()
                    os.fsync(out.fileno())
                staged[path] = name
            for path in (manifest_path, checksums_path):
                if path.exists():
                    fd, backup = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".backup",
                                                  dir=str(path.parent))
                    os.close(fd)
                    shutil.copy2(path, backup)
                    backups[path] = Path(backup)
            for path in (manifest_path, checksums_path):
                os.replace(staged[path], path)
                moved.append(path)
            cls._release_pair_is_consistent(manifest_path, checksums_path)
        except BaseException:
            restored_all = True
            for path in moved:
                backup = backups.get(path)
                if backup is None:
                    continue
                try:
                    os.replace(backup, path)
                except OSError:
                    restored_all = False
            for path, backup in backups.items():
                if backup.exists():
                    try:
                        os.unlink(backup)
                    except OSError:
                        pass
            for name in staged.values():
                try:
                    if os.path.exists(name):
                        os.unlink(name)
                except OSError:
                    pass
            if not restored_all:
                raise GeneratedStoreError(
                    f"the release pair at {manifest_path.parent} is inconsistent and the "
                    "previous bytes could not be restored; do not publish this release")
            raise
        for backup in backups.values():
            try:
                os.unlink(backup)
            except OSError:
                pass

    # -- the transaction ---------------------------------------------------- #
    def transaction(self, *, prune: bool = False, fault_inject: Any = None) -> "StoreTransaction":
        """Stage a whole candidate root and switch it in as one step.

        Usage::

            with store.transaction(prune=True) as staged_store:
                staged_store.put_object(bundle)          # same API as GeneratedStore
                staged_store.build_release("1077100", entries, ...)
                staged_store.verify_release("1077100")   # optional, before the switch

        ``staged_store`` is a complete candidate root: the live releases and
        objects are copied into a sibling directory on the same filesystem, all
        writes (including the ``prune=True`` sweep) happen there, and the live
        root is not written at all inside the block.  On a clean exit the
        candidate is verified and switched in with two directory moves --
        ``live -> backup``, ``candidate -> live`` -- with the backup kept until
        the new root has passed its post-switch verification.  An exception
        inside the block discards the candidate; an exception while switching
        restores the previous root from the backup (and, if even that fails,
        raises with the backup path rather than deleting the only copy).

        ``fault_inject(step, index)`` is called before every copy, both moves
        and the release swaps, so a caller (or a test) can simulate a failure at
        each of them.  Sets :attr:`StoreTransaction.promoted` once the new root
        is in place and verified.
        """
        return StoreTransaction(self, prune=prune, fault_inject=fault_inject)

    # -- admission ---------------------------------------------------------- #
    def _admit_entries(
        self,
        asset_version: str,
        source_client_version: str,
        entries: Sequence[Mapping[str, Any]],
        base: Path,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for index, raw in enumerate(entries):
            if not isinstance(raw, Mapping):
                raise GeneratedStoreError(f"entry #{index} is not an object: {raw!r}")
            entry = dict(raw)
            logical_key = str(entry.get("logical_key") or entry.get("logical_path") or f"#{index}")
            logical_path = _resolve_logical_path(entry.get("logical_path") or logical_key)

            reuse_status = entry.get("reuse_status")
            translation_status = entry.get("translation_status")
            if reuse_status not in ALL_REUSE_STATUSES:
                raise GeneratedStoreError(
                    f"entry {logical_key!r}: unknown reuse_status {reuse_status!r}; "
                    f"expected one of {', '.join(ALL_REUSE_STATUSES)}"
                )
            if translation_status not in ALL_TRANSLATION_STATUSES:
                raise GeneratedStoreError(
                    f"entry {logical_key!r}: unknown translation_status {translation_status!r}; "
                    f"expected one of {', '.join(ALL_TRANSLATION_STATUSES)}"
                )

            if reuse_status not in ADMISSIBLE_REUSE_STATUSES:
                rejected.append({
                    "logical_key": logical_key,
                    "logical_path": logical_path,
                    "reuse_status": reuse_status,
                    "translation_status": translation_status,
                    "reason": (f"reuse_status={reuse_status} cannot enter generated/: "
                               f"{REJECT_HINT[reuse_status]}"),
                })
                continue
            if translation_status not in ADMISSIBLE_TRANSLATION_STATUSES:
                rejected.append({
                    "logical_key": logical_key,
                    "logical_path": logical_path,
                    "reuse_status": reuse_status,
                    "translation_status": translation_status,
                    "reason": (f"translation_status={translation_status} cannot enter generated/: "
                               f"{REJECT_HINT[translation_status]}"),
                })
                continue

            entry["logical_key"] = str(entry.get("logical_key") or logical_path)
            entry["logical_path"] = logical_path
            if entry.get("runtime_path") is not None:
                entry["runtime_path"] = _resolve_runtime_path(entry["runtime_path"])
            entry["source_sha256"] = _hex64(entry.get("source_sha256"), logical_key, "source_sha256")
            # Fail closed, not defaulted: a surface that forgot to declare what it
            # produced would otherwise be silently labelled `other` and a consumer
            # could not tell the omission from a real classification.
            entry["resource_kind"] = validate_resource_kind(entry.get("resource_kind"))

            declared_version = entry.get("asset_version")
            if declared_version is not None and str(declared_version) != asset_version:
                raise GeneratedStoreError(
                    f"entry {logical_key!r}: asset_version {declared_version!r} does not match "
                    f"the release being built ({asset_version!r})"
                )
            declared_client = entry.get("client_version")
            if declared_client not in (None,):
                raise GeneratedStoreError(
                    f"entry {logical_key!r}: client_version must be null on an assets entry; "
                    f"got {declared_client!r} (client and assets versions are separate fields)"
                )
            declared_source_client = entry.get("source_client_version")
            if declared_source_client is not None and \
                    str(declared_source_client) != source_client_version:
                raise GeneratedStoreError(
                    f"entry {logical_key!r}: source_client_version "
                    f"{declared_source_client!r} != {source_client_version!r}"
                )

            channel = entry.get("channel", "assets")
            if channel != "assets":
                raise GeneratedStoreError(
                    f"entry {logical_key!r}: channel {channel!r} is not 'assets'; APK built-in "
                    "surfaces belong to the client repository"
                )

            artifact_file = entry.get("artifact_file")
            if artifact_file:
                artifact_abs = (base / str(artifact_file)).resolve() \
                    if not Path(str(artifact_file)).is_absolute() else Path(str(artifact_file))
                if not artifact_abs.is_file():
                    raise GeneratedStoreError(
                        f"entry {logical_key!r}: artifact_file does not exist: {artifact_abs}"
                    )
                file_digest = sha256_file(artifact_abs)
                declared_artifact = entry.get("artifact_sha256")
                if declared_artifact is not None and \
                        _hex64(declared_artifact, logical_key, "artifact_sha256") != file_digest:
                    raise GeneratedStoreError(
                        f"entry {logical_key!r}: artifact_sha256 {declared_artifact!r} does not "
                        f"match the bytes of {artifact_abs} ({file_digest})"
                    )
                entry["artifact_sha256"] = file_digest
                entry["object_path"] = cas_object_path(file_digest).as_posix()
                entry["_artifact_file"] = str(artifact_file)
                entry["_artifact_abs"] = str(artifact_abs)
                if entry.get("translated_sha256") is None:
                    entry["translated_sha256"] = file_digest
            else:
                entry["artifact_sha256"] = _hex64(
                    entry.get("artifact_sha256"), logical_key, "artifact_sha256")
                entry["object_path"] = self._check_object_reference(entry, logical_key)

            entry["translated_sha256"] = _hex64(
                entry.get("translated_sha256"), logical_key, "translated_sha256")
            accepted.append(entry)

        return accepted, rejected

    def _check_object_reference(self, entry: Mapping[str, Any], logical_key: str) -> str:
        digest = entry["artifact_sha256"]
        expected = cas_object_path(digest).as_posix()
        declared = entry.get("object_path")
        if declared is not None and str(declared) != expected:
            raise GeneratedStoreError(
                f"entry {logical_key!r}: object_path {declared!r} does not match "
                f"artifact_sha256 {digest} (expected {expected!r})"
            )
        path = self.object_file(digest)
        if path is None:
            raise GeneratedStoreError(
                f"entry {logical_key!r}: object {expected} is not in the store; "
                "put the bytes with put_object() (or supply artifact_file) before building"
            )
        actual = sha256_file(path)
        if actual != digest:
            raise GeneratedStoreError(
                f"entry {logical_key!r}: store is corrupt: {expected} hashes to {actual}"
            )
        return expected

    @staticmethod
    def _finalise_entry(entry: dict[str, Any], release: Mapping[str, Any]) -> dict[str, Any]:
        final: dict[str, Any] = {"channel": entry.get("channel", "assets")}
        final.update({
            "asset_version": release["asset_version"],
            "client_version": None,
            "source_client_version": release["source_client_version"],
            "source_commit": entry.get("source_commit") or release["source_commit"],
            "translation_commit": entry.get("translation_commit") or release["translation_commit"],
            "generated_commit": entry.get("generated_commit") or release["generated_commit"],
            # Provenance of the producer, not of the payload: both may override it
            # (a child that really ran elsewhere), but neither may invent one.
            "ci_run_id": entry.get("ci_run_id") or release.get("ci_run_id"),
        })
        for key in ("source_commit", "translation_commit", "generated_commit"):
            final[key] = validate_commit(final[key], f"entry {entry['logical_key']!r} {key}")
        final["ci_run_id"] = validate_ci_run_id(final["ci_run_id"])
        for key in ("logical_key", "logical_path", "source_sha256", "translated_sha256",
                    "object_path", "artifact_sha256", "reuse_status", "translation_status"):
            final[key] = entry[key]
        if entry.get("runtime_path") is not None:
            final["runtime_path"] = entry["runtime_path"]
        final["resource_kind"] = validate_resource_kind(entry.get("resource_kind"))
        return final

    # -- reads -------------------------------------------------------------- #
    def list_releases(self) -> list[str]:
        """Every ``asset_version`` with a manifest in the store, sorted."""
        if not self.root.is_dir():
            return []
        versions: list[str] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name == OBJECTS_DIRNAME:
                continue
            if (child / MANIFEST_NAME).is_file():
                versions.append(child.name)
        return versions

    def load_manifest(self, asset_version: Any) -> dict[str, Any]:
        version = validate_asset_version(asset_version)
        path = self.manifest_path(version)
        if not path.is_file():
            raise GeneratedStoreError(f"no manifest for asset_version {version}: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GeneratedStoreError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise GeneratedStoreError(f"{path} must contain a JSON object")
        return data

    def collect_referenced_objects(self) -> set[str]:
        """Digests referenced by *any* retained ``generated/*/manifest.json``.

        Both ``artifact_sha256`` and the digest embedded in ``object_path`` are
        collected, so a manifest that was edited by hand cannot cause a live
        object to be swept.
        """
        referenced: set[str] = set()
        for version in self.list_releases():
            manifest = self.load_manifest(version)
            for entry in manifest.get("entries") or []:
                if not isinstance(entry, Mapping):
                    continue
                artifact = entry.get("artifact_sha256")
                if isinstance(artifact, str) and HEX64_RE.fullmatch(artifact):
                    referenced.add(artifact)
                object_path = entry.get("object_path")
                if isinstance(object_path, str):
                    name = PurePosixPath(object_path).name
                    if HEX64_RE.fullmatch(name):
                        referenced.add(name)
        return referenced

    def iter_objects(self) -> Iterable[Path]:
        """Every object file in the pool, flat and (legacy) sharded alike.

        ``objects/sha256/<digest>`` and the retired
        ``objects/sha256/<aa>/<digest>`` both count as objects: a store mid
        migration holds both, and a sweep that only saw flat files would leave
        the shards behind forever.
        """
        if not self.objects_dir.is_dir():
            return []
        found: list[Path] = []
        for path in self.objects_dir.iterdir():
            if path.is_file():
                if HEX64_RE.fullmatch(path.name):
                    found.append(path)
            elif path.is_dir() and len(path.name) == 2:
                for candidate in path.iterdir():
                    if candidate.is_file() and HEX64_RE.fullmatch(candidate.name):
                        found.append(candidate)
        return sorted(found)

    def find_orphans(self) -> list[Path]:
        """Objects no retained manifest of this store references.

        A store that holds no release directory at all therefore reports every
        object as an orphan; that is correct for a scratch store and *not* a
        statement about the live root, which holds the retained versions.  A
        sweep of the live root runs through :meth:`transaction`, where each
        version's manifest decides: an object shared with a retained version is
        referenced and kept, and a legacy shard is referenced through the
        ``object_path`` an old manifest embeds.
        """
        referenced = self.collect_referenced_objects()
        return [path for path in self.iter_objects() if path.name not in referenced]

    def prune_orphans(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Report (default) or delete unreferenced objects.  Git history is untouched.

        Reference and layout are separate questions, and this sweeps on the
        first alone: an object no retained manifest names is an orphan whether
        it sits at the flat path, at the legacy shard path, or at both.  A
        digest that *is* referenced keeps every copy of itself -- including a
        shard that a pre-switch manifest embeds -- and one that is not loses all
        of them, so a two-layout store cannot keep orphan bytes alive forever by
        holding each copy as the other's alibi.
        """
        orphans = self.find_orphans()
        removed: list[str] = []
        bytes_freed = 0
        for path in orphans:
            rel = path.relative_to(self.root).as_posix()
            size = path.stat().st_size
            removed.append(rel)
            bytes_freed += size
            if not dry_run:
                _remove_object_file(path, self.root)
        total = len(list(self.iter_objects()))
        return {
            "dry_run": bool(dry_run),
            "removed": removed,
            "kept": total - len(removed),
            "bytes_freed": bytes_freed,
        }

    def verify_tree(self) -> dict[str, Any]:
        """Every retained release must verify; a root with none is not a tree.

        A store whose pool exists but whose release directories are gone is
        broken, not empty: the caller asked it to serve versions and it has
        none.  This is the check the transaction runs after switching the
        candidate in, before the previous root is allowed to be deleted.
        """
        versions = self.list_releases()
        failures: list[str] = []
        if not versions:
            failures.append(
                f"no release manifest under {self.root}: a published store must retain at "
                "least one asset_version")
        checked: dict[str, Any] = {}
        for version in versions:
            report = self.verify_release(version)
            checked[version] = {
                "ok": report.ok,
                "checked_objects": report.checked_objects,
                "notes": report.notes,
            }
            failures.extend(f"{version}: {failure}" for failure in report.failures)
        return {
            "root": str(self.root),
            "ok": not failures,
            "releases": checked,
            "failures": failures,
        }

    # -- verification ------------------------------------------------------- #
    def verify_release(self, asset_version: Any) -> VerifyReport:
        """Recompute every checksum and fail closed on any inconsistency.

        Reads only, and -- unless this store was given a ``snapshot_commit`` to
        check the input provenance against -- reads only from this store.  The
        snapshot is a property of the read: a NAS that resolved the branch
        records the SHA it got in its own state and passes it here; the manifest
        keeps recording the inputs the build consumed.
        """
        version = validate_asset_version(asset_version)
        report = VerifyReport(asset_version=version, ok=False)
        failures = report.failures

        manifest_path = self.manifest_path(version)
        checksums_path = self.checksums_path(version)
        if not manifest_path.is_file():
            failures.append(f"missing manifest: {manifest_path}")
            return report
        if not checksums_path.is_file():
            failures.append(f"missing checksums file: {checksums_path}")
            return report

        try:
            manifest = self.load_manifest(version)
        except GeneratedStoreError as exc:
            failures.append(str(exc))
            return report

        if manifest.get("kind") != MANIFEST_KIND:
            failures.append(f"manifest kind is {manifest.get('kind')!r}, expected {MANIFEST_KIND!r}")
        if manifest.get("asset_version") != version:
            failures.append(
                f"manifest asset_version {manifest.get('asset_version')!r} != directory {version!r}")
        if manifest.get("client_version") is not None:
            failures.append(
                f"manifest client_version must be null on an assets release; got "
                f"{manifest.get('client_version')!r}")
        if manifest.get("build_status") != "success":
            failures.append(
                f"manifest build_status is {manifest.get('build_status')!r}; only a successful "
                "build may be present in the store")
        for field_name in ("source_client_version", "source_commit", "translation_commit",
                           "generated_commit"):
            if field_name not in manifest:
                failures.append(f"manifest is missing {field_name!r}")
        # The three input commits are checked against the *snapshot commit the
        # caller recorded* -- a fact of the read, not of the bytes, which is why
        # it arrives as an argument and is never stored in the manifest.  The
        # manifest of an assets release carries exactly the input provenance
        # (source/translation/generated); a consumer-side snapshot belongs in
        # the consumer's own state.
        if self.snapshot_commit is not None:
            snapshot = validate_commit(self.snapshot_commit, "snapshot_commit")
            for field_name in ("source_commit", "translation_commit", "generated_commit"):
                declared = manifest.get(field_name)
                if declared == snapshot:
                    continue
                try:
                    compare_commit_status(
                        declared, snapshot, repository=self.provenance_repository,
                        fetchImpl=self.provenance_fetch)
                except ReleaseCommitError as error:
                    failures.append(
                        f"manifest {field_name} {declared!r} is not an ancestor of the snapshot "
                        f"commit {snapshot!r} it was read at: {error.problems[0]}")
                except Exception as error:  # noqa: BLE001 - reported, not raised
                    failures.append(
                        f"manifest {field_name} {declared!r} could not be compared with the "
                        f"snapshot commit {snapshot!r}: {error}")
        if "ci_run_id" not in manifest:
            failures.append("manifest is missing 'ci_run_id' (it may be null, but the field "
                            "must be present: absence cannot be told from a lost value)")
        else:
            try:
                validate_ci_run_id(manifest.get("ci_run_id"))
            except GeneratedStoreError as error:
                failures.append(f"manifest {error}")
        for field_name in ("source_commit", "translation_commit", "generated_commit"):
            try:
                validate_commit(manifest.get(field_name), field_name)
            except GeneratedStoreError as error:
                failures.append(f"manifest {error}")

        entries = manifest.get("entries")
        if not isinstance(entries, list):
            failures.append("manifest entries must be a list")
            return report

        checksum_lines: list[tuple[int, str, str]] = []
        legacy_listing = False
        for number, line in enumerate(
                checksums_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                failures.append(f"checksums line {number} is malformed: {line!r}")
                continue
            digest, rel = parts[0].strip(), parts[1].strip()
            if not HEX64_RE.fullmatch(digest):
                failures.append(f"checksums line {number} has a non-sha256 digest: {digest!r}")
                continue
            if not rel.startswith(f"{OBJECTS_DIRNAME}/{HASH_ALGO}/"):
                failures.append(
                    f"checksums line {number} does not point into {OBJECTS_DIRNAME}/{HASH_ALGO}/: "
                    f"{rel!r}")
                continue
            if PurePosixPath(rel).name != digest:
                failures.append(
                    f"checksums line {number}: digest {digest} does not match path {rel!r}")
            if rel == legacy_cas_object_path(digest).as_posix():
                legacy_listing = True
            checksum_lines.append((number, digest, rel))

        # The fan-out form is tolerated so a release written before the flat
        # switch still verifies -- but a *new* checksums.txt must be flat, and
        # saying so once beats discovering it at NAS serve time.
        if legacy_listing:
            report.notes.append(
                f"{CHECKSUMS_NAME} lists objects under the retired fan-out layout "
                f"({OBJECTS_DIRNAME}/{HASH_ALGO}/<aa>/<digest>); a release written now lists "
                f"{OBJECTS_DIRNAME}/{HASH_ALGO}/<digest>. Rebuild to migrate it; this manifest "
                "is still readable and its objects are still resolvable.")

        checksum_digests = {digest for _, digest, _ in checksum_lines}
        manifest_digests: set[str] = set()
        checked = 0

        for index, entry in enumerate(entries):
            label = f"entry #{index}"
            if not isinstance(entry, Mapping):
                failures.append(f"{label} is not an object")
                continue
            label = f"entry #{index} ({entry.get('logical_key') or entry.get('logical_path')})"

            missing = [name for name in _ENTRY_REQUIRED_FIELDS if name not in entry]
            if missing:
                failures.append(f"{label}: missing field(s) {', '.join(missing)}")
                continue

            if entry.get("client_version") is not None:
                failures.append(f"{label}: client_version must be null")
            if entry.get("asset_version") != version:
                failures.append(
                    f"{label}: asset_version {entry.get('asset_version')!r} != {version!r}")
            resource_kind = entry.get("resource_kind")
            if resource_kind not in ALL_RESOURCE_KINDS:
                failures.append(
                    f"{label}: resource_kind {resource_kind!r} is not one of "
                    f"{', '.join(ALL_RESOURCE_KINDS)}")
            try:
                validate_ci_run_id(entry.get("ci_run_id"))
            except GeneratedStoreError as error:
                failures.append(f"{label}: {error}")
            reuse_status = entry.get("reuse_status")
            if reuse_status not in ADMISSIBLE_REUSE_STATUSES:
                failures.append(
                    f"{label}: reuse_status {reuse_status!r} is not admissible in a released "
                    f"manifest (allowed: {', '.join(ADMISSIBLE_REUSE_STATUSES)}); "
                    f"{REJECT_HINT.get(reuse_status, 'see the design document')}")
            translation_status = entry.get("translation_status")
            if translation_status not in ADMISSIBLE_TRANSLATION_STATUSES:
                failures.append(
                    f"{label}: translation_status {translation_status!r} is not admissible in a "
                    f"released manifest (allowed: {', '.join(ADMISSIBLE_TRANSLATION_STATUSES)}); "
                    f"{REJECT_HINT.get(translation_status, 'see the design document')}")

            artifact = entry.get("artifact_sha256")
            if not isinstance(artifact, str) or not HEX64_RE.fullmatch(artifact):
                failures.append(f"{label}: artifact_sha256 is not a sha256 hex digest: {artifact!r}")
                continue
            manifest_digests.add(artifact)

            expected_object = cas_object_path(artifact).as_posix()
            legacy_object = legacy_cas_object_path(artifact).as_posix()
            declared_object = entry.get("object_path")
            if declared_object not in (expected_object, legacy_object):
                # The legacy shard path is accepted for a manifest that predates
                # the flat switch: the bytes it names are still the bytes.
                failures.append(
                    f"{label}: object_path {declared_object!r} does not match "
                    f"artifact_sha256 {artifact} (expected {expected_object!r})")
            elif declared_object == legacy_object:
                report.notes.append(
                    f"{label}: object_path names the retired fan-out layout "
                    f"({legacy_object}); accepted for a manifest written before the flat "
                    f"switch (canonical now: {expected_object})")

            # Exact match on the object its own manifest declares: an old
            # manifest names the shard, a new one names the flat path, and each
            # must find its bytes at *that* path.  Falling back to the other
            # layout is what the writer does; a verifier that also fell back
            # could not tell "the object is at the path I was told" from "the
            # object is somewhere else and the path in the manifest is stale".
            object_path = self.root / declared_object if declared_object in (
                expected_object, legacy_object) else None
            if object_path is None or not object_path.is_file():
                failures.append(f"{label}: missing object {declared_object}")
                continue
            actual = sha256_file(object_path)
            checked += 1
            if actual != artifact:
                failures.append(
                    f"{label}: object {expected_object} hashes to {actual}, expected {artifact}")
            if artifact not in checksum_digests:
                failures.append(f"{label}: artifact {artifact} is not listed in {CHECKSUMS_NAME}")

        for orphan_digest in sorted(checksum_digests - manifest_digests):
            failures.append(
                f"{CHECKSUMS_NAME} lists {orphan_digest} but no manifest entry references it")
        # Every digest the manifest name must be listed; extras are reported
        # above, so a release cannot ship a checksums file that is merely a
        # superset of what it promises.
        for missing_digest in sorted(manifest_digests - checksum_digests):
            failures.append(
                f"{CHECKSUMS_NAME} does not list {missing_digest}, which the manifest names")

        report.checked_objects = checked
        report.ok = not failures
        return report


# --------------------------------------------------------------------------- #
# staged, atomic release promotion
# --------------------------------------------------------------------------- #
class StoreTransaction:
    """A whole-candidate staging tree that is switched in as one step.

    Obtained from :meth:`GeneratedStore.transaction`; used as a context manager::

        with store.transaction(prune=True) as staged_store:
            staged_store.put_object(bundle)
            staged_store.build_release("1077100", entries, ...)
            staged_store.verify_release("1077100")

    ``staged_store`` exposes the whole :class:`GeneratedStore` API and is a
    *complete candidate root*: the live releases and the live object pool are
    copied in, the caller's writes land there, verification and -- with
    ``prune=True`` -- the deletion of unreferenced objects all happen inside it,
    and the live root is not written at all until the switch.  An exception
    inside the block discards the candidate; an exception while switching puts
    the previous root back.  Either way the live root ends as one of the two
    complete roots, never as a half-written mix.

    The candidate is seeded by **copying** the live pool, not by hard-linking
    it.  A link would make the isolation a promise about the caller: a candidate
    write in place (or a corrupted object) would show up in the live root, and a
    later byte comparison could not tell "the candidate is fine" from "both were
    changed together".  A copy costs the volume's block size per object on a
    same-filesystem copy-on-write filesystem, and it is the only version that is
    actually isolated.
    """

    def __init__(self, live: "GeneratedStore", *, prune: bool = False,
                 fault_inject: Any = None) -> None:
        if not isinstance(live, GeneratedStore):  # pragma: no cover - programmer error
            raise GeneratedStoreError("transaction() requires a GeneratedStore")
        self.live = live
        self.prune = bool(prune)
        #: Called as ``fault_inject(step, index)`` before each step that could
        #: fail -- object copies, the prune sweep, both directory moves.  Raising
        #: from it simulates a crash at exactly that point; it exists so every
        #: failure point has a test instead of an argument.
        self.fault_inject = fault_inject
        self._candidate: Path | None = None
        self.staged: "GeneratedStore | None" = None
        self.promoted = False
        self._entered = False
        self._backup: Path | None = None
        self._moved_to_backup = False
        self._candidate_moved = False
        self._notes: list[str] = []

    # -- lifecycle ---------------------------------------------------------- #
    def _fault(self, step: str, index: int = 0) -> None:
        if self.fault_inject is not None:
            self.fault_inject(step, index)

    def __enter__(self) -> "GeneratedStore":
        if self._entered:  # pragma: no cover - single use by construction
            raise GeneratedStoreError("a store transaction is single-use")
        self._entered = True
        # The candidate must share the live root's filesystem: the switch is a
        # directory move, and a move across volumes is a copy that can fail
        # halfway -- which is exactly the failure this class exists to rule out.
        # ``live.root.parent`` also exists already, so creating the candidate
        # cannot be the thing that creates the live store.
        parent = self.live.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._candidate = Path(tempfile.mkdtemp(prefix=f".{self.live.root.name}.candidate-",
                                                dir=str(parent)))
        self.staged = GeneratedStore(self._candidate)
        try:
            self._seed_root()
        except BaseException:
            # A failure while staging happens before the live root is touched;
            # the half-built candidate is removed and the error propagates from
            # ``__enter__`` (so the block never runs).
            self._discard_candidate()
            raise
        return self.staged

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None:
                # The block failed: the candidate is discarded, the live root
                # was never touched, and the caller's exception keeps
                # propagating.
                return False
            self._switch()
        finally:
            # Whatever happened, the candidate either became the live root or is
            # no longer needed.  A backup is *not* discarded here: it is the
            # only copy of the previous root if a restore failed, and
            # ``_restore_root`` has already raised naming it.
            if self._candidate is not None and not self.promoted:
                self._discard_candidate()
        return False

    # -- staging ------------------------------------------------------------ #
    def _seed_root(self) -> None:
        """Copy the live root into the candidate (release trees and pool).

        A copy, not a link: the candidate must be able to diverge -- including a
        release being rebuilt onto new bytes -- without the live root noticing.
        Objects are copied through the same relative paths they have live, so a
        release written before the layout change keeps its layout here too.
        """
        assert self.staged is not None and self._candidate is not None
        root = self.live.root
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and child.name != OBJECTS_DIRNAME \
                        and (child / MANIFEST_NAME).is_file():
                    shutil.copytree(child, self._candidate / child.name, dirs_exist_ok=True)
        for index, path in enumerate(self.live.iter_objects()):
            relative = path.relative_to(root)
            target = self._candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            self._fault("copy_object", index)
            shutil.copy2(path, target)

    # -- promotion ---------------------------------------------------------- #
    def _validate_candidate(self) -> None:
        assert self.staged is not None
        problems: list[str] = []
        for version in self.staged.list_releases():
            report = self.staged.verify_release(version)
            if not report.ok:
                problems.extend(f"{version}: {failure}" for failure in report.failures)
        if problems:
            raise GeneratedStoreError(
                "the candidate root is not publishable; the live store was not touched:\n"
                + "\n".join(f"  - {problem}" for problem in problems)
            )

    def _switch(self) -> None:
        """Validate and prune the candidate, then swap it in as one directory move.

        The whole promotion is ``live -> backup``, ``candidate -> live``: two
        renames of one directory each.  The backup path is recorded *before* the
        first of them, so an interruption between the two is recoverable -- the
        swap is undone and the backup is only ever deleted when the new root is
        standing in place, having passed the verification below.
        """
        assert self.staged is not None and self._candidate is not None
        self._validate_candidate()

        # The candidate must name every version the live root already serves.
        # It was seeded with them, so one that is missing was deleted on purpose
        # -- and "delete a release" is a different operation this transaction
        # does not perform behind the caller's back.
        dropped = [version for version in self.live.list_releases()
                   if version not in self.staged.list_releases()]
        if dropped:
            raise GeneratedStoreError(
                f"the candidate root would drop existing release(s) "
                f"{', '.join(sorted(dropped))}; a transaction promotes releases, it does not "
                "delete them")

        if self.prune:
            # The sweep runs inside the candidate, so the live root keeps its
            # bytes until the switch; a failure here (simulated or real) leaves
            # a candidate that is simply thrown away.
            for index, _digest in enumerate(self.staged.find_orphans()):
                self._fault("prune_object", index)
            outcome = self.staged.prune_orphans(dry_run=False)
            self._notes.append(
                f"pruned {len(outcome['removed'])} object(s) no retained manifest references")

        # Everything from here changes the live root.  Record the live path and
        # its backup path first, so any failure below knows what to put back --
        # and the backup is kept on disk until the new root has passed the
        # post-switch check.
        backup = self._new_backup_path()
        self._backup = backup
        root = self.live.root
        had_root = root.exists()
        try:
            self._fault("switch_live_to_backup", 0)
            if had_root:
                os.replace(root, backup)
                self._moved_to_backup = True
            self._fault("switch_candidate_to_live", 0)
            os.replace(self._candidate, root)
            self._candidate_moved = True
            self._candidate = None
        except BaseException:
            self._restore_root(backup=backup, had_root=had_root)
            raise

        # Post-switch: the switched-in root must be able to serve every release
        # it now holds.  A failure here is not hypothetical (a truncated object,
        # a filesystem that lied about a copy) and the previous root is still on
        # disk, so it rolls back rather than shipping a root that cannot serve.
        try:
            report = GeneratedStore(root).verify_tree()
        except Exception as exc:  # noqa: BLE001 - rolled back and re-raised
            self._restore_root(backup=backup, had_root=had_root)
            raise GeneratedStoreError(
                f"the switched-in root could not be verified ({exc}); the previous root was "
                "restored") from exc
        if not report["ok"]:
            self._restore_root(backup=backup, had_root=had_root)
            raise GeneratedStoreError(
                "the switched-in root failed verification and the previous root was restored:\n"
                + "\n".join(f"  - {problem}" for problem in report["failures"][:10]))

        self.promoted = True
        self._notes.append(
            f"switched in {len(self.staged.list_releases())} release(s); the previous root was "
            "kept as a backup until the new one verified")
        # Only now, with the new root in place and verified, is the previous
        # root redundant.  Failing to delete it leaves a stale directory, not a
        # damaged store, so this is not worth failing the promotion over.
        try:
            if had_root:
                shutil.rmtree(backup, ignore_errors=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass

    def _new_backup_path(self) -> Path:
        """A sibling path for the displaced live root, on the same filesystem."""
        parent = self.live.root.parent
        handle, name = tempfile.mkstemp(prefix=f".{self.live.root.name}.backup-",
                                        suffix=".dir", dir=str(parent))
        os.close(handle)
        # ``mkstemp`` made a file where a directory will be renamed in; remove it
        # so ``os.replace`` moves the root to a fresh name.
        os.unlink(name)
        return Path(name)

    def _restore_root(self, *, backup: Path, had_root: bool) -> None:
        """Undo exactly the steps that completed, no more.

        Three states, and only the third is destructive:

        * nothing moved yet -- the live root is intact and the candidate is
          still the candidate: touching either would destroy data that is fine;
        * only the first move completed -- the previous root is at ``backup``
          and the candidate is still at its own path, so the previous root goes
          back;
        * both moves completed -- the candidate is the live root now, so it is
          removed and the previous root returns.  If the previous root did not
          exist (a first build) the state to restore is "no root", and the
          switched-in one is removed to get there.

        A restore that fails raises with the backup path, which is the only copy
        of the previous root and is deliberately left on disk.
        """
        root = self.live.root
        if not self._moved_to_backup and not self._candidate_moved:
            self._backup = None
            return
        try:
            if self._candidate_moved and not self._moved_to_backup:
                # No previous root existed; the switched-in one is what has to go.
                shutil.rmtree(root)
                self._candidate_moved = False
                self._backup = None
                return
            if root.exists():
                shutil.rmtree(root)
            os.replace(backup, root)
            self._moved_to_backup = False
            self._candidate_moved = False
            self._backup = None
        except OSError as exc:
            raise GeneratedStoreError(
                f"the live root {root} was left inconsistent and could not be restored from "
                f"{backup}: {exc}. The previous root is still at {backup}; recover it by hand "
                "before touching the store again.") from exc

    def _discard_candidate(self) -> None:
        if self._candidate is not None:
            shutil.rmtree(self._candidate, ignore_errors=True)
            self._candidate = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prune": self.prune,
            "promoted": self.promoted,
            "candidate_root": str(self._candidate) if self._candidate else None,
            "backup_root": str(self._backup) if self._backup else None,
            "notes": list(self._notes),
        }


# --------------------------------------------------------------------------- #
# cross-version reuse ledger
# --------------------------------------------------------------------------- #
@dataclass
class ReuseDecision:
    """One row of a reuse decision: the two orthogonal statuses plus a reason."""

    logical_key: str
    logical_path: str
    source_sha256: str
    translated_sha256: str | None
    reuse_status: str
    translation_status: str
    reason: str

    @property
    def eligible(self) -> bool:
        return (self.reuse_status in ADMISSIBLE_REUSE_STATUSES
                and self.translation_status in ADMISSIBLE_TRANSLATION_STATUSES)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "logical_key": self.logical_key,
            "logical_path": self.logical_path,
            "source_sha256": self.source_sha256,
            "translated_sha256": self.translated_sha256,
            "reuse_status": self.reuse_status,
            "translation_status": self.translation_status,
            "eligible": self.eligible,
            "reason": self.reason,
        }
        return payload


class ReuseLedger:
    """Decide reuse between a previous release and a new set of source fingerprints.

    ``verified_records`` maps ``logical_key`` (or ``logical_path``) to
    ``{"from_sha256": ..., "to_sha256": ..., "evidence": "..."}`` and is the only
    way an entry can become ``verified-compatible``.  Similarity alone is never
    enough: paths/keys that merely look alike are reported as ``suggested`` and
    are excluded from the formal ``generated/`` tree.
    """

    def __init__(self, previous_entries: Sequence[Mapping[str, Any]],
                 verified_records: Mapping[str, Mapping[str, Any]] | None = None):
        self.previous_entries = [dict(entry) for entry in previous_entries]
        self.verified_records = {str(key): dict(value)
                                 for key, value in (verified_records or {}).items()}
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_path: dict[str, dict[str, Any]] = {}
        for entry in self.previous_entries:
            key = str(entry.get("logical_key") or "")
            path = str(entry.get("logical_path") or "")
            if key:
                self._by_key.setdefault(key, entry)
            if path:
                self._by_path.setdefault(path, entry)

    def previous_for(self, source: Mapping[str, Any]) -> dict[str, Any] | None:
        key = str(source.get("logical_key") or "")
        path = str(source.get("logical_path") or "")
        if key and key in self._by_key:
            return self._by_key[key]
        if path and path in self._by_path:
            return self._by_path[path]
        return None

    def _verified_record(self, source: Mapping[str, Any],
                         previous: Mapping[str, Any]) -> Mapping[str, Any] | None:
        for name in (str(source.get("logical_key") or ""), str(source.get("logical_path") or "")):
            if not name:
                continue
            record = self.verified_records.get(name)
            if record is None:
                continue
            from_sha = str(record.get("from_sha256") or "")
            to_sha = str(record.get("to_sha256") or "")
            if from_sha and to_sha and from_sha == str(previous.get("source_sha256") or "") \
                    and to_sha == str(source.get("source_sha256") or ""):
                return record
        return None

    def decide(self, sources: Sequence[Mapping[str, Any]]) -> list[ReuseDecision]:
        decisions: list[ReuseDecision] = []
        for index, raw in enumerate(sources):
            source = dict(raw)
            logical_key = str(source.get("logical_key") or source.get("logical_path") or f"#{index}")
            logical_path = _resolve_logical_path(source.get("logical_path") or logical_key)
            source_sha = _hex64(source.get("source_sha256"), logical_key, "source_sha256")
            translated_sha = source.get("translated_sha256")
            if translated_sha is not None:
                translated_sha = _hex64(translated_sha, logical_key, "translated_sha256")

            previous = self.previous_for({**source, "logical_key": logical_key,
                                          "logical_path": logical_path})
            previous_translated = previous.get("translated_sha256") if previous else None

            if previous is not None and str(previous.get("source_sha256") or "") == source_sha:
                reuse_status = "exact"
                reason = "source_sha256 unchanged: automatic reuse of the official source baseline"
            elif previous is not None:
                record = self._verified_record(
                    {**source, "logical_key": logical_key, "logical_path": logical_path}, previous)
                if record is not None:
                    reuse_status = "verified-compatible"
                    reason = ("source changed but a recorded verification authorises reuse "
                              f"(evidence: {record.get('evidence') or 'unspecified'})")
                else:
                    reuse_status = "suggested"
                    reason = (f"source_sha256 changed "
                              f"({str(previous.get('source_sha256'))[:12]}... -> {source_sha[:12]}...)"
                              " and no verification record exists; suggested for manual review "
                              "only (仅供人工参考), never auto-published")
            elif source.get("similarity_hint"):
                reuse_status = "suggested"
                reason = ("a similarity hint was supplied but this logical path/key is not in the "
                          "previous manifest; manual review only (仅供人工参考)")
            else:
                reuse_status = "blocked"
                reason = "no previous entry and no compatibility evidence; reuse is forbidden"

            explicit_translation = source.get("translation_status")
            if explicit_translation is not None:
                if explicit_translation not in ALL_TRANSLATION_STATUSES:
                    raise GeneratedStoreError(
                        f"entry {logical_key!r}: unknown translation_status "
                        f"{explicit_translation!r}")
                translation_status = str(explicit_translation)
                translation_reason = "translation_status was asserted by the caller"
            elif translated_sha is None:
                translation_status = "untranslated"
                translation_reason = "no translated artifact was supplied"
            elif previous_translated is None:
                translation_status = "modified"
                translation_reason = "first translation for this entry"
            elif str(previous_translated) == translated_sha:
                translation_status = "reused"
                translation_reason = "translated bytes are identical to the previous release"
            else:
                translation_status = "modified"
                translation_reason = ("translated bytes changed; a new content-addressed object "
                                      "will be created (reuse_status stays exact when the "
                                      "official source did not change)")

            decisions.append(ReuseDecision(
                logical_key=logical_key,
                logical_path=logical_path,
                source_sha256=source_sha,
                translated_sha256=translated_sha,
                reuse_status=reuse_status,
                translation_status=translation_status,
                reason=f"{reason}; {translation_reason}",
            ))
        return decisions


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _remove_object_file(path: Path, root: Path) -> bool:
    """Delete one object file and an emptied legacy shard directory.

    Returns ``False`` when the file was already gone, which a caller restoring
    a snapshot treats as "nothing to undo".
    """
    try:
        exists = path.is_file()
    except OSError:  # pragma: no cover - unreadable path
        return False
    if not exists:
        return False
    os.unlink(path)
    parent = path.parent
    try:
        if parent != root / OBJECTS_DIRNAME / HASH_ALGO and parent.is_dir() \
                and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return True


def _hex64(value: Any, logical_key: str, field_name: str) -> str:
    text = "" if value is None else str(value).strip().lower()
    if not HEX64_RE.fullmatch(text):
        raise GeneratedStoreError(
            f"entry {logical_key!r}: {field_name} must be a lowercase sha256 hex digest; "
            f"got {value!r}")
    return text


def _reuse_summary(entries: Sequence[Mapping[str, Any]],
                   rejected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reuse_counts = {status: 0 for status in ALL_REUSE_STATUSES}
    translation_counts = {status: 0 for status in ALL_TRANSLATION_STATUSES}
    for entry in entries:
        reuse_counts[str(entry.get("reuse_status"))] = \
            reuse_counts.get(str(entry.get("reuse_status")), 0) + 1
        translation_counts[str(entry.get("translation_status"))] = \
            translation_counts.get(str(entry.get("translation_status")), 0) + 1
    rejected_reasons: dict[str, int] = {}
    for entry in rejected:
        reason = str(entry.get("reason") or "unspecified").split(":", 1)[0]
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
    return {
        "entries": len(entries),
        "reuse_status_counts": reuse_counts,
        "translation_status_counts": translation_counts,
        "rejected_entries": len(rejected),
        "rejected_reasons": rejected_reasons,
    }


def _load_json(path: str) -> Any:
    if path == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _entries_from_document(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        return [dict(item) for item in document]
    if isinstance(document, Mapping):
        entries = document.get("entries")
        if isinstance(entries, list):
            return [dict(item) for item in entries]
    raise GeneratedStoreError("entries document must be a JSON list or an object with 'entries'")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cmd_build(args: argparse.Namespace) -> int:
    entries_path = Path(args.entries)
    document = _load_json(args.entries)
    entries = _entries_from_document(document)
    base = Path(args.entries_base) if args.entries_base else \
        (entries_path.parent if entries_path.is_absolute() or entries_path.exists() else Path.cwd())
    store = GeneratedStore(Path(args.root))
    result = store.build_release(
        args.asset_version,
        entries,
        source_client_version=args.source_client_version,
        source_commit=args.source_commit,
        translation_commit=args.translation_commit,
        generated_commit=args.generated_commit,
        build_status=args.build_status,
        entries_base=base,
    )
    payload = result.to_dict()
    if args.report:
        _atomic_write_text(Path(args.report), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not result.written:
        print(f"ERROR: build_status={args.build_status!r}: generated/ was not touched",
              file=sys.stderr)
        return 2
    if result.rejected:
        print(f"NOTE: {len(result.rejected)} entry/entries were refused admission and reported "
              "with reasons; they are not part of the release", file=sys.stderr)
    # stdout stays pure JSON so callers can pipe it; the human summary goes to stderr.
    print(f"OK: wrote {result.manifest_path} ({len(result.accepted)} entries, "
          f"{result.objects_written} new object(s), {result.objects_deduped} deduplicated)",
          file=sys.stderr)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    store = GeneratedStore(Path(args.root))
    report = store.verify_release(args.asset_version)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.ok:
        for failure in report.failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"OK: {args.asset_version} verified ({report.checked_objects} object(s) re-hashed)",
          file=sys.stderr)
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    store = GeneratedStore(Path(args.root))
    if not store.root.is_dir():
        print(f"ERROR: store root does not exist: {store.root}", file=sys.stderr)
        return 1
    outcome = store.prune_orphans(dry_run=not args.apply)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    mode = "would remove" if outcome["dry_run"] else "removed"
    print(f"{mode} {len(outcome['removed'])} orphan object(s), {outcome['bytes_freed']} bytes; "
          f"{outcome['kept']} object(s) kept. Re-run with --apply to delete.", file=sys.stderr)
    return 0


def _cmd_reuse(args: argparse.Namespace) -> int:
    previous = _load_json(args.previous_manifest)
    if isinstance(previous, Mapping) and "entries" in previous:
        previous_entries = [dict(item) for item in previous["entries"]]
    else:
        previous_entries = _entries_from_document(previous)
    sources = _entries_from_document(_load_json(args.new_sources))
    records = _load_json(args.verified_records) if args.verified_records else {}
    ledger = ReuseLedger(previous_entries, records)
    decisions = [decision.to_dict() for decision in ledger.decide(sources)]
    payload = {
        "decisions": decisions,
        "summary": {
            "total": len(decisions),
            "eligible": sum(1 for item in decisions if item["eligible"]),
            "reuse_status_counts": {
                status: sum(1 for item in decisions if item["reuse_status"] == status)
                for status in ALL_REUSE_STATUSES
            },
            "translation_status_counts": {
                status: sum(1 for item in decisions if item["translation_status"] == status)
                for status in ALL_TRANSLATION_STATUSES
            },
        },
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        _atomic_write_text(Path(args.out), text)
    print(text, end="", flush=True)
    blocked = payload["summary"]["reuse_status_counts"]["blocked"]
    if blocked:
        print(f"NOTE: {blocked} entry/entries are blocked and must not be reused", file=sys.stderr)
    if args.out:
        print(f"OK: wrote {args.out}", file=sys.stderr)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    store = GeneratedStore(Path(args.root))
    releases = []
    for version in store.list_releases():
        manifest = store.load_manifest(version)
        releases.append({
            "asset_version": version,
            "build_status": manifest.get("build_status"),
            "entry_count": manifest.get("entry_count"),
            "generated_at_utc": manifest.get("generated_at_utc"),
            "manifest_path": str(store.manifest_path(version)),
        })
    print(json.dumps({"root": str(store.root), "releases": releases},
                     ensure_ascii=False, indent=2))
    return 0


def _cmd_check_commits(args: argparse.Namespace) -> int:
    """Check the provenance tuple of one or more release manifests.

    Read-only in both senses: it writes nothing, and the only request it makes
    is a ``GET`` of the GitHub compare endpoint, through an injectable fetch so
    a caller can run it entirely offline.  Exit codes: 0 all manifests check
    out; 1 at least one does not; 2 the checker itself could not run.
    """
    repository = args.repository or os.environ.get("MLTD_PROVENANCE_REPOSITORY") \
        or DEFAULT_PROVENANCE_REPOSITORY
    try:
        document = _load_json(args.release)
        releases = releases_from_document(document)
    except (GeneratedStoreError, OSError, ValueError) as exc:
        print(f"ERROR: cannot read {args.release}: {exc}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "release": args.release,
        "snapshot_commit": args.snapshot_commit,
        "repository": repository,
        "releases": [],
    }
    failures = 0
    for manifest in releases:
        version = str(manifest.get("asset_version") or "?")
        try:
            problems = check_release_commits(
                manifest,
                snapshot_commit=args.snapshot_commit,
                repository=repository,
                fetchImpl=None,
            )
        except ReleaseCommitError as exc:
            problems = list(exc.problems)
        except GeneratedStoreError as exc:
            problems = [str(exc)]
        row = {"asset_version": version, "ok": not problems, "problems": problems,
               "commits": {name: manifest.get(name) for name in
                           ("source_commit", "translation_commit", "generated_commit")}}
        payload["releases"].append(row)
        failures += 1 if problems else 0
    payload["ok"] = failures == 0
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        _atomic_write_text(Path(args.report), text)
    print(text, end="", flush=True)
    if args.report:
        print(f"OK: wrote {args.report}", file=sys.stderr)
    for row in payload["releases"]:
        for problem in row["problems"]:
            print(f"FAIL: {row['asset_version']}: {problem}", file=sys.stderr)
    return 0 if failures == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assets_generated_index.py",
        description="Content-addressed store for generated MLTD asset releases "
                    "(channel: assets). Client and assets versions are independent axes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="publish one asset_version manifest into the store")
    build.add_argument("--root", required=True, help="the generated/ store root")
    build.add_argument("--asset-version", required=True, help="digits only, e.g. 1077100")
    build.add_argument("--entries", required=True,
                       help="JSON list (or {\"entries\": [...]}) of manifest entries; "
                            "'-' reads stdin. Relative artifact_file paths resolve against the "
                            "entries file's directory unless --entries-base is given.")
    build.add_argument("--entries-base", default=None)
    build.add_argument("--source-client-version", required=True, help="e.g. 9.0.200")
    build.add_argument("--source-commit", required=True, help="40-hex commit of the official source")
    build.add_argument("--translation-commit", required=True, help="40-hex commit of the translations")
    build.add_argument("--generated-commit", required=True,
                       help="40-hex commit that already contained the generator inputs")
    build.add_argument("--build-status", choices=("success", "failed"), default="success",
                       help="'failed' records nothing and leaves the store untouched")
    build.add_argument("--report", default=None, help="also write the build report JSON here")
    build.set_defaults(func=_cmd_build)

    verify = sub.add_parser("verify", help="re-hash every object of one release")
    verify.add_argument("--root", required=True)
    verify.add_argument("--asset-version", required=True)
    verify.set_defaults(func=_cmd_verify)

    prune = sub.add_parser("prune", help="report/delete objects no retained manifest references")
    prune.add_argument("--root", required=True)
    prune.add_argument("--apply", action="store_true",
                       help="actually delete (default is a dry run that only reports)")
    prune.set_defaults(func=_cmd_prune)

    reuse = sub.add_parser("reuse", help="decide cross-version reuse against a previous manifest")
    reuse.add_argument("--previous-manifest", required=True)
    reuse.add_argument("--new-sources", required=True,
                       help="JSON list of {logical_key, logical_path, source_sha256, "
                            "translated_sha256?} for the new asset version")
    reuse.add_argument("--verified-records", default=None,
                       help="JSON {logical_key: {from_sha256, to_sha256, evidence}}")
    reuse.add_argument("--out", default=None)
    reuse.set_defaults(func=_cmd_reuse)

    listing = sub.add_parser("list", help="list retained releases")
    listing.add_argument("--root", required=True)
    listing.set_defaults(func=_cmd_list)

    commits = sub.add_parser(
        "check-commits",
        help="check that a release's provenance commits descend from its snapshot commit")
    commits.add_argument("--release", required=True,
                         help="a manifest/releases JSON file ('-' reads stdin); the snapshot "
                              "commit the manifest was fetched from is passed separately")
    commits.add_argument("--snapshot-commit", required=True,
                         help="40-hex commit the manifest was fetched from (branch HEAD at read "
                              "time); the source/translation/generated commits must be its "
                              "ancestors or identical to it")
    commits.add_argument("--repository", default=None,
                         help=f"owner/name to compare against (default: "
                              f"${'MLTD_PROVENANCE_REPOSITORY'} or {DEFAULT_PROVENANCE_REPOSITORY})")
    commits.add_argument("--report", default=None, help="also write the JSON report here")
    commits.set_defaults(func=_cmd_check_commits)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # JSON on stdout must stay UTF-8 even when redirected on a Windows console
    # whose locale codepage is not UTF-8 (otherwise the report is written as
    # GBK and a downstream json.load fails with a decoding error).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - detached/odd streams
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (GeneratedStoreError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
