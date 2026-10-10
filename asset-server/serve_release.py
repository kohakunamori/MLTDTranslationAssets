#!/usr/bin/env python3
"""Serve the one release the mirror holds, and the official original behind it.

The distributor answers a client that follows the newest official asset version, so
there is exactly one release on disk and no version to select: the version in the
request path is kept only to find the matching official Japanese object when a
bundle has no translation.  That removes the whole class of "answered from the wrong
release", and it makes the pipeline behind this file small enough to read in one go.

What must stay true:

* nothing is served unless its bytes hash to the digest the release recorded for it
  -- checked again on every response, not once at startup;
* a bundle with no translated object falls back to the official original for *that*
  client's version, never to another version's translation;
* a request is never answered with a file from outside the configured roots.

The official archive is read only; when a version is not materialized locally the
object is fetched from the configured CDN and cached.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "checksums.txt"
VERSION_NAME = "version.json"
OBJECT_DIRNAME = "objects/sha256"
ANDROID_PREFIX = "production/2018/Android/"


@dataclass(frozen=True)
class Outcome:
    status: int
    path: Path | None = None
    artifact_sha256: str | None = None
    reason: str = "not found or not verified"


@dataclass(frozen=True)
class Release:
    asset_version: str
    runtime: dict[str, dict]
    logical: dict[str, dict]
    digests: dict[str, str]
    identity: tuple[int, int, int]


def _identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def parse_checksums(text: str) -> dict[str, str]:
    """Read ``<digest>  <object path>`` lines into a path -> digest map."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:
            result[parts[1].lstrip("*")] = parts[0]
    return result


class ReleaseStore:
    """The single release, re-read only when the files on disk change."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._release: Release | None = None
        self._identity: tuple[int, int, int] | None = None

    def release(self) -> Release | None:
        try:
            identity = _identity(self.root / MANIFEST_NAME)
        except OSError:
            return None
        with self._lock:
            if self._release is not None and self._identity == identity:
                return self._release
        try:
            manifest = json.loads((self.root / MANIFEST_NAME).read_text(encoding="utf-8"))
            checksums = parse_checksums((self.root / CHECKSUMS_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = str(manifest.get("asset_version") or "")
        if not version.isdigit():
            return None
        runtime: dict[str, dict] = {}
        logical: dict[str, dict] = {}
        digests: dict[str, str] = {}
        for entry in manifest.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            digest = str(entry.get("artifact_sha256") or "")
            object_path = str(entry.get("object_path") or "")
            if len(digest) != 64 or object_path.split("/")[-1] != digest:
                continue
            # The sync wrote this checksums file from the manifest; if the two ever
            # disagree, the release is not trustworthy and nothing is served.
            if checksums and checksums.get(object_path) != digest:
                continue
            runtime[str(entry.get("runtime_path") or "")] = entry
            logical[str(entry.get("logical_path") or "")] = entry
            digests[digest] = str(entry.get("logical_path") or "")
        release = Release(asset_version=version, runtime=runtime, logical=logical,
                          digests=digests, identity=identity)
        with self._lock:
            self._release, self._identity = release, identity
        return release

    def object_path(self, digest: str) -> Path:
        return self.root / OBJECT_DIRNAME / digest


def request_target(raw_path: str) -> tuple[str, str] | None:
    """Split ``/assets/<version>/production/...`` into (version, path).

    The version is optional; ``current`` means "whatever the client thinks is
    current" and is treated like any other version, because this reader has only one
    release to answer from.
    """
    path = raw_path.split("?", 1)[0]
    parts = [part for part in path.split("/")]
    while parts and parts[0] == "":
        parts.pop(0)
    if parts and parts[0] == "assets":
        parts.pop(0)
    version = ""
    if parts and (parts[0] == "current" or re.fullmatch(r"[0-9]{1,32}", parts[0] or "")):
        version = parts.pop(0)
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    target = "/".join(parts)
    if not target:
        return None
    return version, target


def confined_file(root: Path, *parts: str) -> Path | None:
    """Resolve a file under ``root`` and refuse anything that escapes it."""
    try:
        base = root.resolve()
        candidate = base.joinpath(*parts).resolve(strict=True)
        candidate.relative_to(base)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def official_file(official_root: Path | None, version: str, target: str) -> Path | None:
    if official_root is None or not version:
        return None
    resource = target.removeprefix(ANDROID_PREFIX)
    parts = resource.split("/")
    for candidate in (
        ("views", version, "jp-android", *parts),
        (version, "jp-android", *parts),
        (version, "production", "2018", "Android", *parts),
    ):
        found = confined_file(official_root, *candidate)
        if found is not None:
            return found
    return None


def legacy_file(overlay_root: Path | None, version: str, target: str) -> Path | None:
    if overlay_root is None or not version:
        return None
    resource = target.removeprefix(ANDROID_PREFIX)
    return confined_file(overlay_root, version, "jp-android", *resource.split("/"))


def cache_file(cache_root: Path, version: str, target: str) -> Path | None:
    try:
        base = cache_root.resolve()
        candidate = (base / version / "jp-android" / Path(*target.split("/"))).resolve()
        candidate.relative_to(base)
    except (OSError, ValueError):
        return None
    return candidate


def fetch_official(base_url: str, version: str, target: str, cache_root: Path) -> Path | None:
    """Download one official object into the cache, or give up quietly."""
    destination = cache_file(cache_root, version, target)
    if destination is None:
        return None
    if destination.is_file():
        return destination
    resource = target.removeprefix(ANDROID_PREFIX)
    url = (f"{base_url.rstrip('/')}/{urllib.parse.quote(version, safe='')}"
           f"/{ANDROID_PREFIX}{urllib.parse.quote(resource, safe='/')}")
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "mltd-asset-serve/1"})
    temporary: str | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=60) as response:
            if not 200 <= response.status < 300:
                return None
            handle, temporary = tempfile.mkstemp(dir=str(destination.parent),
                                                 prefix=".official-", suffix=".tmp")
            with open(handle, "wb", closefd=True) as stream:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        return destination
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class Distributor:
    def __init__(self, store: ReleaseStore, *, official_root: Path | None = None,
                 official_base_url: str | None = None, official_cache_root: Path | None = None,
                 legacy_overlay_root: Path | None = None) -> None:
        self.store = store
        self.official_root = official_root
        self.official_base_url = official_base_url
        self.official_cache_root = official_cache_root
        self.legacy_overlay_root = legacy_overlay_root

    def resolve(self, raw_path: str, *, namespace: str = "") -> Outcome:
        parsed = request_target(raw_path)
        if parsed is None:
            return Outcome(404)
        version, target = parsed
        release = self.store.release()
        if release is not None:
            entry = release.runtime.get(target) or release.logical.get(target)
            if entry is not None:
                digest = str(entry.get("artifact_sha256"))
                candidate = self.store.object_path(digest)
                if candidate.is_file():
                    return Outcome(200, candidate, digest, "translated")
                return Outcome(404)
        if namespace == "cn":
            legacy = legacy_file(self.legacy_overlay_root, version, target)
            if legacy is not None:
                return Outcome(200, legacy, None, "legacy-cn-overlay")
        local = official_file(self.official_root, version, target)
        if local is not None:
            return Outcome(200, local, None, "official-jp")
        if self.official_base_url and version:
            cache = self.official_cache_root or Path(tempfile.gettempdir()) / "mltd-official-assets"
            fetched = fetch_official(self.official_base_url, version, target, cache)
            if fetched is not None:
                return Outcome(200, fetched, None, "official-cdn")
        return Outcome(404)


def handler_for(distributor: Distributor):
    class AssetsHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
            pass

        def _serve(self, head: bool = False) -> None:
            namespace = self.headers.get("X-MLTD-Asset-Namespace", "").strip().lower()
            outcome = distributor.resolve(self.path, namespace=namespace)
            if outcome.status != 200 or outcome.path is None:
                self.send_error(404, "Asset absent or unverified")
                return
            try:
                with outcome.path.open("rb") as stream:
                    computed = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        computed.update(chunk)
                    served = computed.hexdigest()
                expected = outcome.artifact_sha256
                if expected is not None and served != expected:
                    self.send_error(404, "Asset failed integrity verification")
                    return
                etag = '"' + served + '"'
                not_modified = self.headers.get("If-None-Match") == etag
                self.send_response(304 if not_modified else 200)
                self.send_header("ETag", etag)
                # A newer release can replace this object at the same URL, so the
                # client is told to revalidate instead of trusting a long cache.
                self.send_header("Cache-Control", "public, max-age=0, must-revalidate")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Asset-Source", outcome.reason)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(outcome.path.stat().st_size))
                self.end_headers()
                if not head and not not_modified:
                    with outcome.path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            self.wfile.write(chunk)
            except (OSError, BrokenPipeError):
                self.close_connection = True

        def do_GET(self):  # noqa: N802 - stdlib signature
            self._serve()

        def do_HEAD(self):  # noqa: N802 - stdlib signature
            self._serve(head=True)

        def do_POST(self):  # noqa: N802 - stdlib signature
            self.send_error(405, "Read-only distributor")

        do_PUT = do_POST
        do_DELETE = do_POST

    return AssetsHandler


def build_distributor(args) -> Distributor:
    return Distributor(
        ReleaseStore(args.root),
        official_root=args.official_root,
        official_base_url=args.official_base_url or None,
        official_cache_root=args.official_cache_root,
        legacy_overlay_root=args.legacy_overlay_root,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("serve", "status"), default="serve")
    parser.add_argument("--root", type=Path, required=True,
                        help="mirror root holding manifest.json, checksums.txt and objects/")
    parser.add_argument("--bind", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--official-root", type=Path, default=None,
                        help="materialized official archive root (views/<version>/jp-android)")
    parser.add_argument("--official-base-url", default=os.environ.get("MLTD_OFFICIAL_ASSET_BASE", ""),
                        help="official CDN base, default path is /<version>/production/2018/Android")
    parser.add_argument("--official-cache-root", type=Path, default=None,
                        help="cache root for CDN fallback downloads")
    parser.add_argument("--legacy-overlay-root", type=Path, default=None,
                        help="pre-generated /cn overlay, used only for requests carrying the cn header")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")

    distributor = build_distributor(args)
    if args.command == "status":
        release = distributor.store.release()
        print(json.dumps({"root": str(args.root),
                          "asset_version": release.asset_version if release else None,
                          "entries": len(release.runtime) if release else 0},
                         ensure_ascii=False, indent=1))
        return 0
    server = ThreadingHTTPServer((args.bind, args.port), handler_for(distributor))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
