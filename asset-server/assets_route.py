#!/usr/bin/env python3
"""Read-only HTTP mapping for the generated-assets mirror.

The mirror owns synchronization, version selection and retention.  This reader
accepts an explicit version, ``current``, or an omitted version; when a translated
object is absent it can serve the matching official Japanese object from a local
archive or fetch and cache it from the configured official CDN.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import msgpack
except ImportError:  # The official catalog bridge has a stdlib fallback below.
    msgpack = None


class _MiniMsgpack:
    """Decode the small MessagePack subset used by the official asset index."""
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def _read(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise ValueError("truncated MessagePack value")
        value = self.data[self.pos:end]
        self.pos = end
        return value

    def _uint(self, size: int) -> int:
        return int.from_bytes(self._read(size), "big", signed=False)

    def value(self):
        import struct
        code = self._read(1)[0]
        if code <= 0x7f:
            return code
        if code >= 0xe0:
            return code - 0x100
        if 0xa0 <= code <= 0xbf:
            return self._read(code & 0x1f).decode("utf-8")
        if 0x90 <= code <= 0x9f:
            return [self.value() for _ in range(code & 0x0f)]
        if 0x80 <= code <= 0x8f:
            return {self.value(): self.value() for _ in range(code & 0x0f)}
        if code == 0xc0:
            return None
        if code == 0xc2:
            return False
        if code == 0xc3:
            return True
        if code == 0xcc:
            return self._uint(1)
        if code == 0xcd:
            return self._uint(2)
        if code == 0xce:
            return self._uint(4)
        if code == 0xcf:
            return self._uint(8)
        if code == 0xd0:
            return struct.unpack(">b", self._read(1))[0]
        if code == 0xd1:
            return struct.unpack(">h", self._read(2))[0]
        if code == 0xd2:
            return struct.unpack(">i", self._read(4))[0]
        if code == 0xd3:
            return struct.unpack(">q", self._read(8))[0]
        if code in (0xd9, 0xda, 0xdb):
            size = self._uint({0xd9: 1, 0xda: 2, 0xdb: 4}[code])
            return self._read(size).decode("utf-8")
        if code in (0xdc, 0xdd):
            size = self._uint(2 if code == 0xdc else 4)
            return [self.value() for _ in range(size)]
        if code in (0xde, 0xdf):
            size = self._uint(2 if code == 0xde else 4)
            return {self.value(): self.value() for _ in range(size)}
        raise ValueError(f"unsupported MessagePack code 0x{code:02x}")


def _unpack_official_index(data: bytes):
    if msgpack is not None:
        return msgpack.unpackb(data, raw=False, strict_map_key=False)
    decoder = _MiniMsgpack(data)
    value = decoder.value()
    if decoder.pos != len(data):
        raise ValueError("trailing bytes after MessagePack value")
    return value

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.assets_mirror import AssetVersionMirror, ObjectPool, parse_checksums

@dataclass(frozen=True)
class Outcome:
    status: int
    path: Path | None = None
    artifact_sha256: str | None = None
    reason: str = "not found or not verified"


def _version_and_logical_path(path: str, mirror: AssetVersionMirror) -> tuple[str, str] | None:
    parts = path.split("/")
    if len(parts) >= 4 and (parts[2] == "current" or re.fullmatch(r"[0-9]{1,32}", parts[2] or "")):
        version = mirror.current_version() if parts[2] == "current" else parts[2]
        tail = parts[3:]
    elif len(parts) >= 3:
        version = mirror.current_version()
        tail = parts[2:]
    else:
        return None
    if not tail or any(p in ("", ".", "..") for p in tail):
        return None
    logical_path = "/".join(tail)
    # Keep the fixed namespace when it is present.  Generated manifests record
    # the client's original path verbatim (including production/2018/Android),
    # while the official fallback helper strips it only when constructing a
    # CDN/archive lookup.
    if not logical_path:
        return None
    return version, logical_path


def _confined_path(root: Path, version: str, logical_path: str) -> Path | None:
    try:
        base = root.resolve()
        target = (base / version / "jp-android" / Path(*logical_path.split("/"))).resolve()
        target.relative_to(base)
        return target
    except (OSError, ValueError):
        return None


def _official_local_path(root: Path, version: str, logical_path: str) -> Path | None:
    """Find a materialized official view without following paths outside its root."""
    resource_path = logical_path.removeprefix("production/2018/Android/")
    candidates = (
        root / "views" / version / "jp-android" / Path(*resource_path.split("/")),
        root / version / "jp-android" / Path(*resource_path.split("/")),
        root / version / "production" / "2018" / "Android" / Path(*resource_path.split("/")),
    )
    base = root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(base)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _legacy_overlay_local_path(root: Path, version: str, logical_path: str) -> Path | None:
    """Find a pre-generated ``/cn`` overlay object safely."""
    resource_path = logical_path.removeprefix("production/2018/Android/")
    candidate = root / version / "jp-android" / Path(*resource_path.split("/"))
    try:
        base = root.resolve()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


@lru_cache(maxsize=16)
def _official_runtime_map(root_name: str, version: str) -> dict[str, str]:
    """Return official runtime filename -> logical bundle mapping.

    Older generated manifests predate ``runtime_path``.  Deriving the alias
    from the same official `.data` catalog keeps those publications usable
    without copying or hard-coding a second version database.
    """
    root = Path(root_name)
    try:
        metadata = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        release = (metadata.get("releases") or {}).get(version) or {}
        index_name = str(release.get("index_name", ""))
        if not index_name or "/" in index_name or "\\" in index_name:
            return {}
        index = _official_local_path(root, version, f"production/2018/Android/{index_name}")
        if index is None:
            return {}
        decoded = _unpack_official_index(index.read_bytes())
        if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
            return {}
        result: dict[str, str] = {}
        for logical, row in decoded[0].items():
            if isinstance(logical, str) and isinstance(row, list) and len(row) == 3:
                remote = str(row[1])
                if remote.endswith(".unity3d") and "/" not in remote and "\\" not in remote:
                    result[f"production/2018/Android/{remote}"] = logical
        return result
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return {}


def _official_url(base_url: str, version: str, logical_path: str) -> str:
    base = base_url.rstrip("/")
    if "{version}" in base:
        prefix = base.format(version=urllib.parse.quote(version, safe=""))
    else:
        prefix = f"{base}/{urllib.parse.quote(version, safe='')}/production/2018/Android"
    resource_path = logical_path.removeprefix("production/2018/Android/")
    suffix = "/".join(urllib.parse.quote(part, safe="") for part in resource_path.split("/"))
    return f"{prefix}/{suffix}"


def _fetch_official(
    *,
    base_url: str,
    version: str,
    logical_path: str,
    cache_root: Path,
) -> Path | None:
    target = _confined_path(cache_root, version, logical_path)
    if target is None:
        return None
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        _official_url(base_url, version, logical_path),
        method="GET",
        headers={"User-Agent": "mltd-assets-route/1"},
    )
    temp_name: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if not (200 <= response.status < 300):
                return None
            handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".official-", suffix=".tmp")
            with open(handle, "wb", closefd=True) as stream:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
            return target
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _resolve_official(
    *,
    version: str,
    logical_path: str,
    official_root: Path | None,
    official_base_url: str | None,
    official_cache_root: Path | None,
) -> Path | None:
    if official_root is not None:
        local = _official_local_path(official_root, version, logical_path)
        if local is not None:
            return local
    if not official_base_url:
        return None
    cache = official_cache_root or Path(tempfile.gettempdir()) / "mltd-official-assets"
    return _fetch_official(
        base_url=official_base_url,
        version=version,
        logical_path=logical_path,
        cache_root=cache,
    )

def _publication_key(mirror: AssetVersionMirror, version: str) -> tuple[int, int, int, int, int, int]:
    """Return a cheap publication identity for the verification cache.

    The mirror writer replaces state/manifest/checksums atomically.  A request
    therefore only needs a full manifest/object verification when one of those
    three files changes; the selected object is still hashed on every response.
    """
    paths = (
        mirror.state_path(version),
        mirror.manifest_path(version),
        mirror.checksums_path(version),
    )
    values: list[int] = []
    for path in paths:
        stat = path.stat()
        values.extend((stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(values)  # type: ignore[return-value]


def resolve_request(
    raw_path: str,
    mirror: AssetVersionMirror,
    verification_cache: dict[str, tuple[int, int, int, int, int, int]] | None = None,
    verification_lock: threading.Lock | None = None,
    *,
    official_root: Path | None = None,
    official_base_url: str | None = None,
    official_cache_root: Path | None = None,
    legacy_overlay_root: Path | None = None,
) -> Outcome:
    """Resolve one version/path, falling back to the official Japanese asset."""
    try:
        parsed = urlsplit(raw_path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return Outcome(404)
        raw = parsed.path
        if re.search(r"%(?![0-9a-fA-F]{2})|%(?:2[fF]|5[cC])", raw):
            return Outcome(404)
        path = unquote(raw, encoding="utf-8", errors="strict")
        if any(ord(c) < 32 or ord(c) == 127 for c in path) or "\\" in path:
            return Outcome(404)
        parts = path.split("/")
        if len(parts) < 3 or parts[:2] != ["", "assets"]:
            return Outcome(404)
        if legacy_overlay_root is not None and (
            len(parts) < 7 or not re.fullmatch(r"[0-9]{1,32}", parts[2])
            or parts[3:6] != ["production", "2018", "Android"]
        ):
            # /cn is always bound to the version supplied by the game server.
            return Outcome(404)
        selected = _version_and_logical_path(path, mirror)
        if selected is None:
            return Outcome(404)
        version, logical_path = selected
        publication_ready = False
        try:
            state_path = mirror.state_path(version)
            manifest_path = mirror.manifest_path(version)
            checksums_path = mirror.checksums_path(version)
            state_bytes, manifest_bytes = state_path.read_bytes(), manifest_path.read_bytes()
            state, manifest = json.loads(state_bytes), json.loads(manifest_bytes)
            if state.get("sync_status") != "success" or manifest.get("asset_version") != version:
                return Outcome(404)
            if state.get("sync_status") == "success" and manifest.get("asset_version") == version:
                publication_ready = True
                # The sync writer performs the full schema/checksum/object gate;
                # the reader reuses its immutable receipt and hashes the selected
                # object below on every response.
                publication_key = _publication_key(mirror, version)
                if verification_cache is None:
                    if not mirror.verify(version).get("ok"):
                        return Outcome(404)
                else:
                    lock = verification_lock or threading.Lock()
                    with lock:
                        if verification_cache.get(version) != publication_key:
                            verification = state.get("verification")
                            if not isinstance(verification, dict) or verification.get("ok") is not True:
                                return Outcome(404)
                            if _publication_key(mirror, version) != publication_key:
                                verification_cache.pop(version, None)
                                return Outcome(404, reason="publication changed during verification")
                            verification_cache[version] = publication_key
                if state_path.read_bytes() != state_bytes or manifest_path.read_bytes() != manifest_bytes:
                    return Outcome(404, reason="publication changed during verification")
                # The official catalog maps a logical bundle name to a hashed
                # name.  Generated manifests keep both names: translated entries
                # are served by runtime_path for a real client request, while
                # logical_path remains available for diagnostics and older callers.
                lookup_paths = [logical_path]
                prefix = "production/2018/Android/"
                if logical_path.startswith(prefix):
                    lookup_paths.append(logical_path.removeprefix(prefix))
                entry = next((e for e in manifest["entries"]
                              if e.get("runtime_path") in lookup_paths), None)
                if entry is None:
                    entry = next((e for e in manifest["entries"]
                                  if e.get("logical_path") in lookup_paths), None)
                if entry is None and official_root is not None:
                    # Compatibility for generated releases produced before
                    # runtime_path was added: reverse the official .data
                    # catalog, then match the old logical manifest entry.
                    catalog_logical = _official_runtime_map(str(official_root.resolve()), version).get(logical_path)
                    if catalog_logical is not None:
                        entry = next((e for e in manifest["entries"]
                                      if e.get("logical_path") in {
                                          catalog_logical,
                                          f"production/2018/Android/{catalog_logical}",
                                      }), None)
                if entry is not None:
                    # The generated manifest may describe a historical sharded
                    # CAS path while the local pool is intentionally flat.
                    resolved = mirror.resolve(version, str(entry.get("logical_path")))
                    if resolved is None:
                        return Outcome(404)
                    target = Path(resolved.pool_path)
                    try:
                        checksums = parse_checksums(checksums_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError):
                        return Outcome(404)
                    if checksums.get(str(entry.get("object_path"))) != entry.get("artifact_sha256"):
                        return Outcome(404)
                    return Outcome(200, target, entry["artifact_sha256"], "translated")
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            if legacy_overlay_root is None or any(
                p.exists() for p in (state_path, manifest_path, checksums_path)
            ):
                # Only an absent publication can use the migration bridge.
                # Corrupt, failed or partial publications stay fail-closed.
                return Outcome(404)

        # During migration /cn bridges the old overlay.  It is never consulted
        # for the dedicated /generated-assets namespace.
        if legacy_overlay_root is not None:
            legacy = _legacy_overlay_local_path(legacy_overlay_root, version, logical_path)
            if legacy is not None:
                digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
                return Outcome(200, legacy, digest, "legacy-overlay")
        if not publication_ready and legacy_overlay_root is None:
            return Outcome(404)
        fallback = _resolve_official(
            version=version,
            logical_path=logical_path,
            official_root=official_root,
            official_base_url=official_base_url,
            official_cache_root=official_cache_root,
        )
        if fallback is None:
            return Outcome(404)
        return Outcome(200, fallback, hashlib.sha256(fallback.read_bytes()).hexdigest(), "official-jp")
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return Outcome(404)
    except Exception:
        # Mirror validation exceptions are refusals, not error pages containing
        # local paths or a reason to fall back to a different asset version.
        return Outcome(404)

def handler_for(
    mirror: AssetVersionMirror,
    *,
    official_root: Path | None = None,
    official_base_url: str | None = None,
    official_cache_root: Path | None = None,
    legacy_overlay_root: Path | None = None,
):
    verification_cache: dict[str, tuple[int, int, int, int, int, int]] = {}
    verification_lock = threading.Lock()

    class AssetsHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass
        def _serve(self, head=False):
            outcome = resolve_request(
                self.path, mirror, verification_cache, verification_lock,
                official_root=official_root,
                official_base_url=official_base_url,
                official_cache_root=official_cache_root,
                legacy_overlay_root=(
                    legacy_overlay_root
                    if self.headers.get("X-MLTD-Asset-Namespace", "").strip().lower() == "cn"
                    else None
                ),
            )
            if outcome.status != 200 or outcome.path is None:
                self.send_error(404, "Asset absent or unverified")
                return
            try:
                with outcome.path.open("rb") as stream:
                    digest, size = hashlib.sha256(), 0
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                    if digest.hexdigest() != outcome.artifact_sha256:
                        self.send_error(404, "Asset failed integrity verification")
                        return
                    etag = '"' + outcome.artifact_sha256 + '"'
                    not_modified = self.headers.get("If-None-Match") == etag
                    self.send_response(304 if not_modified else 200)
                    self.send_header("ETag", etag)
                    # A retained asset_version may receive a newer successful
                    # translation: the logical URL is not immutable forever.
                    self.send_header("Cache-Control", "public, max-age=0, must-revalidate")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("X-Asset-Source", outcome.reason)
                    if not not_modified:
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(size))
                    self.end_headers()
                    if not head and not not_modified:
                        stream.seek(0)
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            self.wfile.write(chunk)
            except (OSError, BrokenPipeError):
                self.close_connection = True
        def do_GET(self):
            self._serve()
        def do_HEAD(self):
            self._serve(head=True)
        def do_POST(self):
            self.send_error(405, "Read-only distributor")
        do_PUT = do_POST
        do_DELETE = do_POST
    return AssetsHandler

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "list"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bind", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--official-root", type=Path, default=None,
                        help="materialized official archive root (views/<version>/jp-android)")
    parser.add_argument("--official-base-url", default=os.environ.get("MLTD_OFFICIAL_ASSET_BASE", ""),
                        help="official CDN base, default path is /<version>/production/2018/Android")
    parser.add_argument("--official-cache-root", type=Path, default=None,
                        help="cache root for CDN fallback downloads")
    parser.add_argument("--legacy-overlay-root", type=Path, default=None,
                        help="legacy /cn overlay root used only for requests carrying the cn namespace header")
    args = parser.parse_args(argv)
    # Reader methods do not use source; prohibit implicit outbound sync here.
    mirror = AssetVersionMirror(None, ObjectPool(args.root), args.root)
    if args.command == "list":
        print(json.dumps(mirror.list_versions(), ensure_ascii=False, indent=2))
        return 0
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    server = ThreadingHTTPServer(
        (args.bind, args.port),
        handler_for(
            mirror,
            official_root=args.official_root,
            official_base_url=args.official_base_url or None,
            official_cache_root=args.official_cache_root,
            legacy_overlay_root=args.legacy_overlay_root,
        ),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
