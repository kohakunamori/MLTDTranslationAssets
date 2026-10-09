#!/usr/bin/env python3
"""Sync, verify and serve MLTD assets from a version-aware CAS."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from server.asset_archive import parse_manifest_objects, safe_relative_name  # noqa: E402
from server.versioned_asset_store import (  # noqa: E402
    LEGACY_ENTRY_INDEX,
    UNUSED_ENTRY_INDEXES,
    VersionConflict,
    VersionedAssetStore,
)


CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
MD5_ETAG_RE = re.compile(r'^["\']?([0-9a-fA-F]{32})["\']?$')
MAX_RESUME_SEGMENTS = 64
MAX_FETCH_FAILURES = 3
# Bindings per sorted transaction in `sync`.  The bind phase is bound by the pool's
# random 4 KB writes, not by SQLite: the same 4,000 bindings take 0.1 s in tmpfs but
# 33-1080 s on the archive pool.  Each row costs ~7.5 KB of WAL at 512 rows per
# transaction and ~3.3 KB at 2000, so the default is on the larger side.
DEFAULT_BIND_BATCH = 2000
# Names resolved in parallel per round.  Completions arrive in random order, so a
# batch is only name-ordered across the window it is collected from: a window of
# 20k makes consecutive batches walk forwards through the entries pages instead of
# scattering writes over the whole 168k-row version.
DEFAULT_BIND_WINDOW = 20000
# Compact the index once the freelist passes this: the measured break-even is far
# below it (a 216,700-page freelist cost every sync ~2 minutes; VACUUM takes ~3.5
# minutes), and after a VACUUM the freelist is zero, so an idle controller checks
# this on every cycle without ever doing redundant work.
DEFAULT_VACUUM_MIN_FREELIST_MB = 200.0
DEFAULT_VACUUM_MIN_FREELIST_BYTES = int(DEFAULT_VACUUM_MIN_FREELIST_MB * 1024 * 1024)


def etag_md5_hint(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value or value.lower().startswith("w/"):
        return None
    match = MD5_ETAG_RE.fullmatch(value)
    return match.group(1).lower() if match else None


def google_hash_md5(headers) -> str | None:
    raw = str(headers.get("X-Goog-Hash") or "")
    for token in raw.split(","):
        token = token.strip()
        if not token.lower().startswith("md5="):
            continue
        encoded = token.split("=", 1)[1].strip()
        try:
            digest = base64.b64decode(encoded, validate=True)
        except Exception:
            return None
        if len(digest) == 16:
            return digest.hex()
    return None


def write_progress(root: Path, version: str, scope: str, payload: dict) -> None:
    path = Path(root) / "versions" / str(version) / "archive-progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    document = {
        "schema_version": 1,
        "game": "mltd",
        "version": str(version),
        "scope": str(scope),
        "updated_at": time.time(),
        **payload,
    }
    temp.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


class Client:
    def __init__(
        self,
        store: VersionedAssetStore,
        *,
        version: str,
        scope: str,
        asset_root: str,
        proxy: str | None = None,
        timeout: float = 60.0,
        durable: bool = False,
    ):
        self.store = store
        self.version, self.scope = store.normalize_identity(version, scope)
        self.asset_root = asset_root.rstrip("/")
        self.proxy = proxy
        self.timeout = timeout
        self.durable = durable
        self._local = threading.local()
        self._reuse_hints: dict[str, list[dict]] | None = None
        self._reuse_hints_lock = threading.Lock()
        self._has_checksum_cache: bool | None = None
        self._checksum_index: dict[tuple[str, int], str] | None = None
        self._checksum_index_lock = threading.Lock()
        self._verified_md5: dict[str, str] = {}
        self._verified_md5_lock = threading.Lock()
        self._reuse_hash_slots = threading.Semaphore(16)

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=1, pool_maxsize=1, max_retries=3
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            session.headers["User-Agent"] = "mltd-versioned-asset-archive/1"
            if self.proxy:
                session.proxies.update({"http": self.proxy, "https": self.proxy})
            self._local.session = session
        return session

    def url(self, name: str) -> str:
        name = safe_relative_name(name)
        encoded = "/".join(quote(part, safe="") for part in name.split("/"))
        return f"{self.asset_root}/{encoded}"

    def reuse_hints(self) -> dict[str, list[dict]]:
        cached = self._reuse_hints
        if cached is not None:
            return cached
        with self._reuse_hints_lock:
            if self._reuse_hints is not None:
                return self._reuse_hints
            hints: dict[str, list[dict]] = {}
            for candidate in self.store.reuse_etag_hints(self.version, self.scope):
                md5 = etag_md5_hint(candidate.get("etag"))
                if md5 is None:
                    continue
                hints.setdefault(md5, []).append(candidate)
            self._reuse_hints = hints
            return hints

    def checksum_index(self) -> dict[tuple[str, int], str]:
        """Map every stored (md5, size) to its CAS object, read once per client.

        Reuse used to ask the store for one (md5, size) row per asset.  On the
        NAS index that lookup cost ~1.9 MB of random page reads and ~48 ms, so an
        11.8k-asset sync burned tens of GB and tens of minutes before its first
        transfer.  One batch read of the checksum table is ~48 MB and turns every
        later lookup into a dict hit.
        """
        cached = self._checksum_index
        if cached is not None:
            return cached
        with self._checksum_index_lock:
            if self._checksum_index is None:
                self._checksum_index = {
                    (str(md5).lower(), int(size)): str(sha256)
                    for sha256, (size, md5) in self.store.object_checksum_index().items()
                }
            return self._checksum_index

    def remember_checksum(self, md5: str, size: int, sha256: str) -> None:
        """Record a freshly learned identity so this process reuses it at once."""
        index = self._checksum_index
        if index is not None:
            index[(str(md5).lower(), int(size))] = str(sha256)
        self._has_checksum_cache = True

    def checksum_cache_available(self) -> bool:
        if self._has_checksum_cache:
            return True
        return bool(self.checksum_index())

    def verified_candidate_md5(self, candidate: dict) -> str | None:
        sha256 = str(candidate["sha256"])
        with self._verified_md5_lock:
            cached = self._verified_md5.get(sha256)
        if cached is not None:
            return cached
        source = self.store.object_path(sha256)
        expected_size = int(candidate["size"])
        if not source.is_file() or source.stat().st_size != expected_size:
            return None
        # Bound concurrent backfill hashing so a version transition cannot turn
        # hundreds of archive workers into random-I/O saturation on the NAS.
        with self._reuse_hash_slots:
            actual = self.store.md5_file(source)
        with self._verified_md5_lock:
            self._verified_md5.setdefault(sha256, actual)
        return actual

    def find_md5_reuse(self, md5: str, size: int) -> dict | None:
        md5 = str(md5).lower()
        size = int(size)
        index = self.checksum_index()
        sha256 = index.get((md5, size))
        if sha256 is not None:
            source = self.store.object_path(sha256)
            if source.is_file() and source.stat().st_size == size:
                return {
                    "sha256": sha256,
                    "size": size,
                    "md5": md5,
                    "source_version": "checksum-cache",
                }

        if index:
            # The checksum table covers the store, so a miss is genuinely new
            # content.  Falling through to the ETag hints costs a full scan of
            # `entries` (3M rows / 2 GB on the NAS, measured at 65 s per call),
            # and that path only exists for stores built before object_checksums.
            return None

        for candidate in self.reuse_hints().get(md5, []):
            if int(candidate["size"]) != size:
                continue
            if self.verified_candidate_md5(candidate) != md5:
                continue
            return candidate
        return None

    def try_cross_version_reuse(
        self, name: str, part: Path
    ) -> tuple[dict | None, dict | None]:
        """Probe one byte, then reuse a prior CAS object by authoritative MD5.

        MLTD changes every logical asset filename between resource versions, so
        same-name comparison cannot deduplicate transfers. Google Storage exposes
        the current object's MD5 in X-Goog-Hash on Range responses. We use that
        current URL metadata to find an existing object, and verify legacy ETag
        hints against the local candidate bytes before binding it.
        """
        if not self.checksum_cache_available() and not self.reuse_hints():
            return None, None

        headers = {"Accept-Encoding": "identity", "Range": "bytes=0-0"}
        try:
            response = self.session().get(
                self.url(name), headers=headers, stream=True, timeout=self.timeout
            )
        except requests.RequestException:
            return None, None

        try:
            if int(response.status_code) != 206:
                return None, None
            content_range = str(response.headers.get("Content-Range") or "").strip()
            match = CONTENT_RANGE_RE.fullmatch(content_range)
            if (
                match is None
                or int(match.group(1)) != 0
                or int(match.group(2)) != 0
                or match.group(3) == "*"
            ):
                return None, None
            size = int(match.group(3))
            md5 = google_hash_md5(response.headers)
            if md5 is None:
                return None, None
            probe = {"md5": md5, "size": size}
            candidate = self.find_md5_reuse(md5, size)
            if candidate is None:
                return None, probe

            sha256 = str(candidate["sha256"])
            source = self.store.object_path(sha256)
            if not source.is_file() or source.stat().st_size != size:
                return None, probe
            payload = self.bind_payload(
                sha256=sha256,
                size=size,
                status=206,
                headers=response.headers,
                content_md5=md5,
            )
            self.remember_checksum(md5, size, sha256)
            part.unlink(missing_ok=True)
            source.chmod(0o644)
            return {
                "name": name,
                "status": "reused",
                "size": size,
                "sha256": sha256,
                "content_md5": md5,
                "source_version": str(candidate.get("version") or candidate.get("source_version")),
                "bind": payload,
            }, probe
        finally:
            response.close()

    @staticmethod
    def validate_download_identity(
        probe: dict | None, headers, size: int, content_md5: str
    ) -> None:
        expected_md5 = probe.get("md5") if probe else None
        expected_size = int(probe["size"]) if probe else None
        response_md5 = google_hash_md5(headers)
        if expected_size is not None and int(size) != expected_size:
            raise IOError(
                f"probe/download size changed: probe={expected_size} download={size}"
            )
        if expected_md5 and response_md5 and expected_md5 != response_md5:
            raise IOError(
                f"probe/download MD5 changed: probe={expected_md5} response={response_md5}"
            )
        authoritative_md5 = expected_md5 or response_md5
        if authoritative_md5 and content_md5 != authoritative_md5:
            raise IOError(
                f"download MD5 mismatch: expected={authoritative_md5} actual={content_md5}"
            )

    @staticmethod
    def bind_payload(
        *, sha256: str, size: int, status: int, headers, content_md5: str | None = None
    ) -> dict:
        """Metadata for one object, ready for VersionedAssetStore.bind_objects."""
        get = headers.get if headers is not None else (lambda _k: None)
        return {
            "sha256": str(sha256).lower(),
            "size": int(size),
            "status": int(status),
            "content_type": get("Content-Type"),
            "etag": get("ETag"),
            "last_modified": get("Last-Modified"),
            "cache_control": get("Cache-Control"),
            "content_md5": None if content_md5 is None else str(content_md5).lower(),
        }

    def fetch(self, name: str, *, force: bool = False) -> dict:
        """Resolve one object and bind it immediately.

        The sync pool uses `resolve` and batches the binds instead; this wrapper
        keeps the one-object contract for serve/verify/one-off callers.
        """
        result = self.resolve(name, force=force)
        payload = result.pop("bind", None)
        if payload is not None:
            self.store.bind_objects(
                self.version, self.scope, [{"name": result["name"], **payload}]
            )
        return result

    def resolve(self, name: str, *, force: bool = False) -> dict:
        """Probe, download or reuse one object without touching the index.

        Returns the fetch result plus a `bind` payload when the caller still has
        to publish the metadata.  Publishing is deferred so an archive sync can
        apply thousands of bindings in one sorted transaction (see
        VersionedAssetStore.bind_objects).
        """
        name = safe_relative_name(name)
        row = self.store.lookup(self.version, self.scope, name)
        if row is None:
            raise KeyError(f"unregistered asset: {self.version}/{self.scope}/{name}")
        digest = row.get("sha256")
        if not force and digest:
            destination = self.store.object_path(digest)
            if destination.is_file() and destination.stat().st_size == int(row.get("size") or -1):
                destination.chmod(0o644)
                return {
                    "name": name,
                    "status": "cached",
                    "size": destination.stat().st_size,
                    "sha256": digest,
                }

        part = self.store.part_path(self.version, self.scope, name)
        probe_identity = None
        if not force and (not part.exists() or part.stat().st_size == 0):
            reused, probe_identity = self.try_cross_version_reuse(name, part)
            if reused is not None:
                return reused
        failures = 0
        segments = 0
        last_headers = {}
        last_status = 200
        last_error: Exception | None = None

        while failures <= MAX_FETCH_FAILURES and segments < MAX_RESUME_SEGMENTS:
            try:
                resume_at = part.stat().st_size
            except OSError:
                resume_at = 0
            start_size = resume_at

            headers = {"Accept-Encoding": "identity"}
            if resume_at:
                headers["Range"] = f"bytes={resume_at}-"

            response = self.session().get(
                self.url(name), headers=headers, stream=True, timeout=self.timeout
            )
            expected_total: int | None = None
            mode = "wb"
            try:
                last_headers = dict(response.headers)
                last_status = int(response.status_code)

                if response.status_code == 416 and resume_at:
                    content_range = response.headers.get("Content-Range", "").strip()
                    if content_range.startswith("bytes */"):
                        try:
                            expected_total = int(content_range.rsplit("/", 1)[1])
                        except ValueError:
                            expected_total = None
                    if expected_total is not None and resume_at == expected_total:
                        digest, content_md5 = self.store.sha256_md5_file(part)
                        size = part.stat().st_size
                        self.validate_download_identity(
                            probe_identity, response.headers, size, content_md5
                        )
                        destination = self.store.commit_part(part, digest)
                        if destination.stat().st_size != size:
                            raise IOError("content-addressed destination size mismatch")
                        payload = self.bind_payload(
                            sha256=digest,
                            size=size,
                            status=206,
                            headers=response.headers,
                            content_md5=content_md5,
                        )
                        self.remember_checksum(content_md5, size, digest)
                        return {
                            "name": name,
                            "status": "downloaded",
                            "size": size,
                            "sha256": digest,
                            "bind": payload,
                        }
                    part.unlink(missing_ok=True)
                    failures += 1
                    last_error = IOError(
                        f"range not satisfiable at offset {resume_at}"
                    )
                    time.sleep(min(2 ** max(failures - 1, 0), 8))
                    continue

                response.raise_for_status()

                if resume_at and response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "").strip()
                    match = CONTENT_RANGE_RE.fullmatch(content_range)
                    if match is None or int(match.group(1)) != resume_at:
                        raise IOError(
                            f"invalid resume Content-Range {content_range!r} "
                            f"for offset {resume_at}"
                        )
                    if match.group(3) != "*":
                        expected_total = int(match.group(3))
                    mode = "ab"
                else:
                    if resume_at:
                        # Origin ignored Range; restart safely from this full body.
                        part.unlink(missing_ok=True)
                        resume_at = 0
                        start_size = 0
                    content_length = response.headers.get("Content-Length")
                    if content_length and content_length.isdigit():
                        expected_total = int(content_length)
                    mode = "wb"

                stream_error: Exception | None = None
                try:
                    with part.open(mode) as stream:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                stream.write(chunk)
                        stream.flush()
                        if self.durable:
                            os.fsync(stream.fileno())
                except requests.RequestException as exc:
                    stream_error = exc

                size = part.stat().st_size if part.exists() else 0

                if expected_total is not None and size > expected_total:
                    part.unlink(missing_ok=True)
                    failures += 1
                    segments = 0
                    last_error = IOError(
                        f"response exceeded expected size {expected_total}: {size}"
                    )
                    if failures <= MAX_FETCH_FAILURES:
                        time.sleep(min(2 ** max(failures - 1, 0), 8))
                        continue
                    break

                if stream_error is not None:
                    last_error = stream_error
                    if size > start_size:
                        segments += 1
                        time.sleep(min(0.1 * segments, 1.0))
                        continue
                    failures += 1
                    if failures <= MAX_FETCH_FAILURES:
                        time.sleep(min(2 ** max(failures - 1, 0), 8))
                        continue
                    break

                if expected_total is not None and size < expected_total:
                    last_error = IOError(
                        f"incomplete response: expected {expected_total}, received {size}"
                    )
                    if size > start_size:
                        segments += 1
                        time.sleep(min(0.1 * segments, 1.0))
                        continue
                    failures += 1
                    if failures <= MAX_FETCH_FAILURES:
                        time.sleep(min(2 ** max(failures - 1, 0), 8))
                        continue
                    break

                # No transfer error and either the expected total was reached or
                # the origin supplied no total length.  Hash and atomically admit.
                digest, content_md5 = self.store.sha256_md5_file(part)
                size = part.stat().st_size
                self.validate_download_identity(
                    probe_identity, last_headers, size, content_md5
                )
                destination = self.store.commit_part(part, digest)
                if destination.stat().st_size != size:
                    raise IOError("content-addressed destination size mismatch")
                payload = self.bind_payload(
                    sha256=digest,
                    size=size,
                    status=last_status,
                    headers=last_headers,
                    content_md5=content_md5,
                )
                self.remember_checksum(content_md5, size, digest)
                return {
                    "name": name,
                    "status": "downloaded",
                    "size": size,
                    "sha256": digest,
                    "bind": payload,
                }

            except requests.RequestException as exc:
                last_error = exc
                current_size = part.stat().st_size if part.exists() else 0
                if current_size > start_size:
                    segments += 1
                    time.sleep(min(0.1 * segments, 1.0))
                    continue
                failures += 1
                if failures <= MAX_FETCH_FAILURES:
                    time.sleep(min(2 ** max(failures - 1, 0), 8))
                    continue
                break
            finally:
                response.close()

        if segments >= MAX_RESUME_SEGMENTS:
            raise IOError(
                f"resume segment limit {MAX_RESUME_SEGMENTS} reached for {name}; "
                f"partial={part.stat().st_size if part.exists() else 0}"
            ) from last_error
        if last_error is not None:
            raise last_error
        raise IOError(f"unable to complete asset download: {name}")


def sync(args) -> int:
    store = VersionedAssetStore(args.root)
    store.ensure_version(
        args.version,
        args.scope,
        asset_root=args.asset_root,
        manifest_name=args.manifest,
    )
    store.register_names(args.version, args.scope, [args.manifest])
    client = Client(
        store,
        version=args.version,
        scope=args.scope,
        asset_root=args.asset_root,
        proxy=args.proxy,
        timeout=args.timeout,
        durable=args.durable,
    )

    existing_identity = store.version(args.version, args.scope)
    refresh_manifest = bool(
        args.refresh_manifest
        or not existing_identity
        or not existing_identity.get("manifest_sha256")
    )
    manifest_row = client.fetch(args.manifest, force=refresh_manifest)
    manifest_path = store.object_path(manifest_row["sha256"])
    names = parse_manifest_objects(manifest_path.read_bytes())
    all_names = [args.manifest, *names]
    store.ensure_version(
        args.version,
        args.scope,
        asset_root=args.asset_root,
        manifest_name=args.manifest,
        manifest_sha256=manifest_row["sha256"],
        object_count=len(all_names),
    )
    if store.registered_count(args.version, args.scope) != len(all_names):
        store.register_names(args.version, args.scope, all_names)

    selected = names
    if args.contains:
        selected = [name for name in selected if args.contains in name]
    if args.limit is not None:
        selected = selected[: max(0, args.limit)]
    if args.manifest_only:
        selected = []
    # Publish in primary-key order: completed probes arrive in whatever order the
    # pool finishes them, so sorting here is what lets each batch walk the entries
    # leaf pages forwards instead of scattering writes across the whole version.
    selected = sorted(selected)

    downloaded = cached = reused = failed = 0
    processed_bytes = int(manifest_row.get("size") or 0)
    failures: list[dict] = []
    started = time.time()
    total_objects = len(all_names)
    last_progress = 0.0

    def publish_progress(*, force: bool = False) -> None:
        nonlocal last_progress
        now = time.monotonic()
        if not force and now - last_progress < 1.0:
            return
        successful = 1 + downloaded + cached + reused
        write_progress(
            Path(args.root),
            args.version,
            args.scope,
            {
                "total_objects": total_objects,
                "successful_objects": successful,
                "missing_objects": max(0, total_objects - successful),
                "downloaded": downloaded,
                "cached": cached,
                "reused": reused,
                "failed": failed,
                "processed_bytes": processed_bytes,
                "percent": (successful * 100.0 / total_objects) if total_objects else 100.0,
                "running": True,
                "complete": False,
            },
        )
        last_progress = now

    publish_progress(force=True)
    bind_batch = max(1, int(getattr(args, "bind_batch", 0) or DEFAULT_BIND_BATCH))
    bind_window = max(bind_batch, int(getattr(args, "bind_window", 0) or DEFAULT_BIND_WINDOW))

    def flush_binds(pending: list[tuple[str, dict]]) -> None:
        """Apply one window of bindings as sorted, contiguous batches.

        Sorting the window (not each completion batch) is what keeps the batches
        contiguous: a batch flushed in completion order re-dirties the leaf pages
        the previous batch just wrote, which on this pool costs ~18 KB of physical
        writes per row instead of ~3 KB.
        """
        if not pending:
            return
        pending.sort(key=lambda item: item[0])
        while pending:
            chunk, pending[:] = pending[:bind_batch], pending[bind_batch:]
            rows = [{"name": name, **payload} for name, payload in chunk]
            try:
                store.bind_objects(args.version, args.scope, rows)
                continue
            except Exception as exc:
                # A batch is all-or-nothing, so fall back to one transaction per
                # object to keep `failures` naming the objects that really failed.
                print(f"batch bind failed ({exc}); retrying per object", file=sys.stderr, flush=True)
            for row in rows:
                try:
                    store.bind_objects(args.version, args.scope, [row])
                except Exception as row_exc:
                    nonlocal failed
                    failed += 1
                    failures.append({"name": row["name"], "error": str(row_exc)})
                    print(f"FAILED {row['name']}: {row_exc}", file=sys.stderr, flush=True)

    if selected:
        # 168k short bind transactions: hold WAL autocheckpoints until the pool is
        # done, otherwise every commit also writes dirty pages back into the 2 GB
        # index at random offsets and the pool crawls at ~4 objects per second.
        with store.bulk_writes():
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                for start in range(0, len(selected), bind_window):
                    window = selected[start : start + bind_window]
                    futures = {
                        pool.submit(client.resolve, name, force=args.force): name
                        for name in window
                    }
                    pending: list[tuple[str, dict]] = []
                    for future in as_completed(futures):
                        name = futures[future]
                        try:
                            result = future.result()
                            payload = result.pop("bind", None)
                            if payload is not None:
                                pending.append((result["name"], payload))
                            if result["status"] == "downloaded":
                                downloaded += 1
                            elif result["status"] == "reused":
                                reused += 1
                            else:
                                cached += 1
                            processed_bytes += int(result.get("size") or 0)
                            if args.verbose:
                                print(f"{result['status']:10} {result['size']:12d} {name}", flush=True)
                        except Exception as exc:
                            failed += 1
                            failures.append({"name": name, "error": str(exc)})
                            print(f"FAILED {name}: {exc}", file=sys.stderr, flush=True)
                        publish_progress()
                    flush_binds(pending)

    stats = store.stats(args.version, args.scope)
    complete = failed == 0 and stats["missing"] == 0
    store.mark_complete(args.version, args.scope, complete)
    write_progress(
        Path(args.root),
        args.version,
        args.scope,
        {
            "total_objects": total_objects,
            "successful_objects": stats["mapped"],
            "missing_objects": stats["missing"],
            "downloaded": downloaded,
            "cached": cached,
            "reused": reused,
            "failed": failed,
            "processed_bytes": stats["logical_bytes"],
            "percent": (stats["mapped"] * 100.0 / stats["registered"]) if stats["registered"] else 100.0,
            "running": False,
            "complete": complete,
        },
    )
    report = {
        "schema_version": 1,
        "version": str(args.version),
        "scope": args.scope,
        "asset_root": args.asset_root.rstrip("/"),
        "manifest": args.manifest,
        "manifest_sha256": manifest_row["sha256"],
        "manifest_object_count": len(names),
        "downloaded": downloaded,
        "cached": cached,
        "reused": reused,
        "failed": failed,
        "complete": complete,
        "duration_seconds": round(time.time() - started, 3),
        "store": stats,
        "failures": failures[:100],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 2


def verify(args) -> int:
    store = VersionedAssetStore(args.root)
    identity = store.version(args.version, args.scope)
    if identity is None:
        print(json.dumps({"complete": False, "error": "version_not_found"}))
        return 2
    checked = missing = mismatched = 0
    verified_objects: dict[str, tuple[int, str]] = {}
    checksum_index = store.object_checksum_index() if args.hash else {}
    checksum_cache_added = 0
    hash_workers = max(1, int(getattr(args, "hash_workers", 8)))
    with store.db() as conn:
        registered, unmapped = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN sha256 IS NULL THEN 1 ELSE 0 END)
            FROM entries
            WHERE version=? AND scope=?
            """,
            (str(args.version), args.scope),
        ).fetchone()
        grouped_rows = conn.execute(
            """
            SELECT sha256,size,COUNT(*)
            FROM entries
            WHERE version=? AND scope=? AND sha256 IS NOT NULL
            GROUP BY sha256,size
            """,
            (str(args.version), args.scope),
        ).fetchall()

    registered = int(registered or 0)
    missing += int(unmapped or 0)

    # Group references in SQLite using a covering (version,scope,sha256,size)
    # index. This avoids 100k+ primary-index -> table lookups on the NAS ZFS/HDD
    # pool before hashing starts.
    object_specs: dict[str, tuple[int, int]] = {}
    invalid_specs: dict[str, int] = {}
    for digest, size, ref_count in grouped_rows:
        expected_size = int(size or -1)
        ref_count = int(ref_count)
        if digest in invalid_specs:
            invalid_specs[digest] += ref_count
            continue
        previous = object_specs.get(digest)
        if previous is None:
            object_specs[digest] = (expected_size, ref_count)
        elif previous[0] != expected_size:
            invalid_specs[digest] = previous[1] + ref_count
            object_specs.pop(digest, None)
        else:
            object_specs[digest] = (previous[0], previous[1] + ref_count)
    mismatched += sum(invalid_specs.values())

    def verify_one(item: tuple[str, tuple[int, int]]):
        digest, (expected_size, count) = item
        path = store.object_path(digest)
        if not path.is_file():
            return "missing", digest, expected_size, count, None, False
        path_size = path.stat().st_size
        if path_size != expected_size:
            return "mismatched", digest, path_size, count, None, False
        if not args.hash:
            return "ok", digest, path_size, count, None, False
        existing_checksum = checksum_index.get(digest)
        if existing_checksum is not None and existing_checksum[0] == path_size:
            if store.sha256_file(path) != digest:
                return "mismatched", digest, path_size, count, None, False
            return "ok", digest, path_size, count, existing_checksum[1], False
        actual_sha256, actual_md5 = store.sha256_md5_file(path)
        if actual_sha256 != digest:
            return "mismatched", digest, path_size, count, None, False
        return "ok", digest, path_size, count, actual_md5, True

    verify_workers = hash_workers if args.hash else 1
    items = list(object_specs.items())
    with ThreadPoolExecutor(max_workers=verify_workers) as pool:
        # Bound queued futures so 167k MLTD objects do not become 167k Future
        # instances at once. 512 keeps eight workers continuously fed.
        for offset in range(0, len(items), 512):
            futures = [
                pool.submit(verify_one, item)
                for item in items[offset : offset + 512]
            ]
            batch_checksum_updates: list[tuple[str, int, str]] = []
            for future in as_completed(futures):
                status, digest, path_size, count, md5, needs_cache = future.result()
                if status == "missing":
                    missing += count
                    continue
                if status != "ok":
                    mismatched += count
                    continue
                checked += count
                if args.hash and md5 is not None:
                    verified_objects[digest] = (path_size, md5)
                    if needs_cache:
                        batch_checksum_updates.append((digest, path_size, md5))
            if batch_checksum_updates:
                store.record_object_checksums(batch_checksum_updates)
                checksum_cache_added += len(batch_checksum_updates)
    report = {
        "version": str(args.version),
        "scope": args.scope,
        "registered": registered,
        "checked": checked,
        "missing": missing,
        "mismatched": mismatched,
        "hash_workers": hash_workers if args.hash else 0,
        "checksum_cache_objects": len(verified_objects) if args.hash else 0,
        "checksum_cache_added": checksum_cache_added if args.hash else 0,
        "complete": missing == 0 and mismatched == 0,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["complete"] else 2


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def parse_range(value: str | None, size: int):
    if not value:
        return None
    match = _RANGE_RE.fullmatch(value.strip())
    if not match or size <= 0:
        return False
    first, last = match.groups()
    if not first and not last:
        return False
    if not first:
        suffix = int(last)
        if suffix <= 0:
            return False
        return max(0, size - suffix), size - 1
    start = int(first)
    if start >= size:
        return False
    end = size - 1 if not last else min(int(last), size - 1)
    if end < start:
        return False
    return start, end


class VersionedAssetHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        address,
        *,
        store,
        prefix,
        fetch_missing,
        proxy,
        timeout,
        require_complete=False,
    ):
        super().__init__(address, VersionedAssetHandler)
        self.store = store
        self.prefix = "/" + prefix.strip("/") + "/"
        self.fetch_missing = bool(fetch_missing)
        self.require_complete = bool(require_complete)
        self.proxy = proxy
        self.timeout = timeout
        self._locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def lock_for(self, key):
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())


class VersionedAssetHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MLTDVersionedAsset/1"

    def log_message(self, fmt, *args):
        return

    def resolve(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            return "health"
        if not path.startswith(self.server.prefix):
            return None
        rel = path[len(self.server.prefix):].lstrip("/")
        parts = rel.split("/", 2)
        if len(parts) != 3:
            return None
        version, scope, encoded_name = parts
        try:
            requested_version = unquote(version)
            if requested_version == "current":
                requested_version = self.server.store.current_version()
            version, scope = self.server.store.normalize_identity(
                requested_version, unquote(scope)
            )
            name = safe_relative_name(unquote(encoded_name))
        except (OSError, ValueError, UnicodeError):
            return None
        identity = self.server.store.version(version, scope)
        if identity is None:
            return None
        if self.server.require_complete and not identity["complete"]:
            return None
        row = self.server.store.lookup(version, scope, name)
        if row is None:
            return None
        return version, scope, name, row

    def ensure_object(self, version, scope, name, row):
        digest = row.get("sha256")
        if digest:
            path = self.server.store.object_path(digest)
            if path.is_file():
                return path, row
        if not self.server.fetch_missing:
            return None, row
        identity = self.server.store.version(version, scope)
        if identity is None:
            return None, row
        key = (version, scope, name)
        with self.server.lock_for(key):
            row = self.server.store.lookup(version, scope, name) or row
            digest = row.get("sha256")
            if digest:
                path = self.server.store.object_path(digest)
                if path.is_file():
                    return path, row
            client = Client(
                self.server.store,
                version=version,
                scope=scope,
                asset_root=identity["asset_root"],
                proxy=self.server.proxy,
                timeout=self.server.timeout,
                durable=True,
            )
            try:
                result = client.fetch(name)
            except Exception:
                return None, row
            row = self.server.store.lookup(version, scope, name) or row
            return self.server.store.object_path(result["sha256"]), row

    def do_HEAD(self):
        self.serve(send_body=False)

    def do_GET(self):
        self.serve(send_body=True)

    def serve(self, *, send_body: bool):
        resolved = self.resolve()
        if resolved == "health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return
        if resolved is None:
            self.send_error(404)
            return
        version, scope, name, row = resolved
        path, row = self.ensure_object(version, scope, name, row)
        if path is None or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        byte_range = parse_range(self.headers.get("Range"), size)
        if byte_range is False:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return
        if byte_range is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end = byte_range
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        ctype = row.get("content_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if row.get("sha256"):
            self.send_header("ETag", f'"sha256:{row["sha256"]}"')
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body or length <= 0:
            return
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def list_versions(args) -> int:
    store = VersionedAssetStore(args.root)
    with store.db() as conn:
        rows = conn.execute(
            """
            SELECT version,scope,asset_root,manifest_name,manifest_sha256,
                   object_count,complete,created_at,updated_at
            FROM versions ORDER BY version,scope
            """
        ).fetchall()
    out = []
    for row in rows:
        (
            version, scope, asset_root, manifest_name, manifest_sha256,
            object_count, complete, created_at, updated_at,
        ) = row
        stats = store.stats(version, scope)
        out.append(
            {
                "version": version,
                "scope": scope,
                "asset_root": asset_root,
                "manifest_name": manifest_name,
                "manifest_sha256": manifest_sha256,
                "object_count": object_count,
                "complete": bool(complete),
                "created_at": created_at,
                "updated_at": updated_at,
                "store": stats,
            }
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def gc(args) -> int:
    store = VersionedAssetStore(args.root)
    with store.db() as conn:
        referenced = {
            str(row[0]).lower()
            for row in conn.execute(
                "SELECT DISTINCT sha256 FROM entries WHERE sha256 IS NOT NULL"
            )
        }

    candidates: list[Path] = []
    bytes_reclaimable = 0
    if store.objects_root.exists():
        for path in store.objects_root.glob("*/*"):
            if not path.is_file() or path.name.lower() in referenced:
                continue
            candidates.append(path)
            bytes_reclaimable += path.stat().st_size

    report = {
        "referenced_objects": len(referenced),
        "unreferenced_objects": len(candidates),
        "bytes_reclaimable": bytes_reclaimable,
        "dry_run": not args.delete,
    }
    if args.delete:
        for path in candidates:
            path.unlink()
        for bucket in store.objects_root.iterdir():
            if bucket.is_dir():
                try:
                    bucket.rmdir()
                except OSError:
                    pass
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def serve(args) -> int:
    # Runtime serving is deliberately read-only.  Archive population and
    # version registration happen through the explicit sync/tooling path.
    store = VersionedAssetStore(args.root, read_only=True)
    server = VersionedAssetHTTPServer(
        (args.bind, args.port),
        store=store,
        prefix=args.prefix,
        fetch_missing=args.fetch_missing,
        proxy=args.proxy,
        timeout=args.timeout,
        require_complete=args.require_complete,
    )
    print(
        f"MLTD_VERSIONED_ASSET_READY http://{args.bind}:{args.port}/{args.prefix.strip('/')}/",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def vacuum_store(
    root: str | os.PathLike[str],
    *,
    if_needed: bool = False,
    min_freelist_bytes: int = DEFAULT_VACUUM_MIN_FREELIST_BYTES,
    dry_run: bool = False,
) -> dict:
    """Compact the index, optionally only when fragmentation has built up.

    Binding rewrites a version's ~168k rows in place, so after a few versions the
    rows are no longer contiguous and the "scan this version" query in `stats()`
    degrades into one random page read per row: measured 50-108 s per sync against
    0.8 s after a VACUUM.  This is the maintenance action for that, and it is
    deliberately not part of store construction or `_init_db`.
    """
    db_path = Path(root) / "index.sqlite3"
    before = index_health(db_path)
    if before is None:
        return {"vacuum": "absent", "changed": False}
    if if_needed and before["freelist_bytes"] < int(min_freelist_bytes):
        return {
            "vacuum": "skipped",
            "changed": False,
            "reason": (
                f"freelist {before['freelist_bytes']} bytes below threshold "
                f"{int(min_freelist_bytes)}"
            ),
            **before,
        }
    if dry_run:
        return {"vacuum": "planned", "changed": False, "dry_run": True, **before}

    started = time.time()
    # A dedicated connection: VACUUM needs the whole database to itself, and the
    # pooled writer may still hold a page cache or a read mark from earlier work.
    conn = sqlite3.connect(db_path, timeout=3600)
    try:
        conn.execute("PRAGMA busy_timeout=3600000")
        checkpoint = tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = index_health(db_path) or before
    return {
        "vacuum": "done",
        "changed": True,
        "seconds": round(time.time() - started, 1),
        "wal_checkpoint": list(checkpoint),
        "before": before,
        "after": after,
    }


def index_health(db_path: Path) -> dict | None:
    """Size, page count and freelist of an index database, or None when absent."""
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        conn.close()
    return {
        "bytes": Path(db_path).stat().st_size,
        "page_count": page_count,
        "page_size": page_size,
        "freelist_pages": freelist,
        "freelist_bytes": freelist * page_size,
    }


def maintenance(args) -> int:
    """One-off index repairs that must not run on the store's startup path.

    Dropping a multi-hundred-megabyte index reads tens of GB and runs for minutes
    on the live NAS database, so it is an explicit, dry-runnable action instead of
    something `VersionedAssetStore.__init__` does to every sync.
    """
    store = VersionedAssetStore(args.root)
    try:
        if getattr(args, "vacuum", False):
            print(
                json.dumps(
                    vacuum_store(
                        args.root,
                        if_needed=bool(getattr(args, "if_needed", False)),
                        min_freelist_bytes=int(
                            float(getattr(args, "min_freelist_mb", 0) or 0) * 1024 * 1024
                        ),
                        dry_run=bool(args.dry_run),
                    )
                ),
                flush=True,
            )
            return 0
        with store.db() as conn:
            present = [
                name
                for name in UNUSED_ENTRY_INDEXES
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                    (name,),
                ).fetchone()
            ]
        if not present:
            print(
                json.dumps({"unused_entry_indexes": "absent", "changed": False}),
                flush=True,
            )
            return 0
        if args.dry_run:
            index_bytes: dict[str, int | None] = {}
            with store.db() as conn:
                for name in present:
                    try:
                        index_bytes[name] = conn.execute(
                            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name=?",
                            (name,),
                        ).fetchone()[0]
                    except sqlite3.OperationalError:
                        index_bytes[name] = None
            print(
                json.dumps(
                    {
                        "unused_entry_indexes": present,
                        "changed": False,
                        "dry_run": True,
                        "index_bytes": index_bytes,
                    }
                ),
                flush=True,
            )
            return 0
        started = time.time()
        with store.write_db() as conn:
            for name in present:
                conn.execute(f"DROP INDEX {name}")
        print(
            json.dumps(
                {
                    "unused_entry_indexes": present,
                    "changed": True,
                    "seconds": round(time.time() - started, 1),
                }
            ),
            flush=True,
        )
    finally:
        store.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync")
    sync_p.add_argument("--root", required=True)
    sync_p.add_argument("--version", required=True)
    sync_p.add_argument("--scope", default="jp-android")
    sync_p.add_argument("--asset-root", required=True)
    sync_p.add_argument("--manifest", required=True)
    sync_p.add_argument("--proxy")
    sync_p.add_argument("--workers", type=int, default=8)
    sync_p.add_argument("--timeout", type=float, default=60.0)
    sync_p.add_argument("--durable", action="store_true")
    sync_p.add_argument("--force", action="store_true")
    sync_p.add_argument(
        "--refresh-manifest",
        action="store_true",
        help="re-fetch the upstream manifest even when its verified CAS object is already registered",
    )
    sync_p.add_argument("--manifest-only", action="store_true")
    sync_p.add_argument("--contains")
    sync_p.add_argument("--limit", type=int)
    sync_p.add_argument(
        "--bind-batch",
        type=int,
        default=DEFAULT_BIND_BATCH,
        help=(
            "bindings applied per sorted transaction; larger batches write each "
            "leaf page fewer times, smaller ones bound the WAL"
        ),
    )
    sync_p.add_argument(
        "--bind-window",
        type=int,
        default=DEFAULT_BIND_WINDOW,
        help=(
            "names resolved per pool round, and therefore the span a sorted apply "
            "can be in primary-key order over"
        ),
    )
    sync_p.add_argument("--verbose", action="store_true")
    sync_p.set_defaults(func=sync)

    verify_p = sub.add_parser("verify")
    verify_p.add_argument("--root", required=True)
    verify_p.add_argument("--version", required=True)
    verify_p.add_argument("--scope", default="jp-android")
    verify_p.add_argument("--hash", action="store_true")
    verify_p.add_argument("--hash-workers", type=int, default=8)
    verify_p.set_defaults(func=verify)

    list_p = sub.add_parser("list")
    list_p.add_argument("--root", required=True)
    list_p.set_defaults(func=list_versions)

    gc_p = sub.add_parser("gc")
    gc_p.add_argument("--root", required=True)
    gc_p.add_argument(
        "--delete",
        action="store_true",
        help="delete objects unreferenced by every retained version; default is dry-run",
    )
    gc_p.set_defaults(func=gc)

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--root", required=True)
    serve_p.add_argument("--bind", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=3027)
    serve_p.add_argument("--prefix", default="assets")
    serve_p.add_argument("--fetch-missing", action="store_true")
    serve_p.add_argument(
        "--require-complete",
        action="store_true",
        help="serve only versions whose archive index is marked complete",
    )
    serve_p.add_argument("--proxy")
    serve_p.add_argument("--timeout", type=float, default=60.0)
    serve_p.set_defaults(func=serve)

    maintenance_p = sub.add_parser("maintenance")
    maintenance_p.add_argument("--root", required=True)
    maintenance_p.add_argument(
        "--drop-unused-entry-indexes",
        "--drop-legacy-entry-index",
        dest="drop_unused_entry_indexes",
        action="store_true",
        help=(
            "drop the `entries` indexes nothing queries "
            f"({', '.join(UNUSED_ENTRY_INDEXES)}); without it this only reports "
            "and estimates"
        ),
    )
    maintenance_p.add_argument(
        "--vacuum",
        action="store_true",
        help=(
            "compact the index (wal_checkpoint(TRUNCATE) + VACUUM); in-place binds "
            "fragment the version's rows and cost every later sync ~2 minutes"
        ),
    )
    maintenance_p.add_argument(
        "--if-needed",
        dest="if_needed",
        action="store_true",
        help="with --vacuum, only compact when the freelist exceeds --min-freelist-mb",
    )
    maintenance_p.add_argument(
        "--min-freelist-mb",
        type=float,
        default=DEFAULT_VACUUM_MIN_FREELIST_MB,
        help="freelist threshold for --if-needed",
    )
    maintenance_p.add_argument("--dry-run", action="store_true")
    maintenance_p.set_defaults(func=maintenance)

    args = ap.parse_args()
    if args.command == "maintenance" and not (
        args.drop_unused_entry_indexes or args.vacuum
    ):
        args.dry_run = True
    try:
        return args.func(args)
    except VersionConflict as exc:
        print(f"version conflict: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
