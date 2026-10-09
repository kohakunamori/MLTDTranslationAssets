#!/usr/bin/env python3
"""Version-aware content-addressed MLTD asset storage."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from server.asset_archive import safe_relative_name


class VersionConflict(RuntimeError):
    pass


# Indexes on `entries` that no query in this tree uses.  They are pure write
# amplification for a bulk bind: binding changes `sha256`, so SQLite removes and
# re-inserts an entry in each of them, and those keys are content hashes -- a
# random leaf page of a multi-million-entry index for every one of a version's
# ~168k objects.  That is what kept the bind phase at ~34 objects/s even after the
# updates themselves were sorted into batches:
#   idx_entries_scope_name_version -- legacy; nothing queries it
#   idx_entries_sha256            -- object_by_md5() reads object_checksums
#   idx_entries_verify_cover      -- stats()'s DISTINCT scan uses the primary key
# `versioned_assets.py maintenance --drop-unused-entry-indexes` removes them and
# `_init_db` no longer creates them (dropping them from `_init_db` itself would put
# a multi-minute DDL on every sync's startup path).
UNUSED_ENTRY_INDEXES = (
    "idx_entries_scope_name_version",
    "idx_entries_sha256",
    "idx_entries_verify_cover",
)
# Kept as the name the maintenance CLI and the deployment docs already use.
LEGACY_ENTRY_INDEX = UNUSED_ENTRY_INDEXES[0]


class VersionedAssetStore:
    # One fresh sqlite3 connection per store call used to cost ~1.9 MB of random
    # page reads and ~48 ms on the NAS index (1.9 GB main file plus a 124 MB WAL
    # on a loaded, nearly full pool).  A version sync makes two or three such
    # calls per asset, so it burned tens of GB of reads and tens of minutes
    # before its first transfer.  Connections are pooled instead: one serialized
    # writer shared by every thread, one reader per thread.
    READER_CACHE_KIB = 8192
    WRITER_CACHE_KIB = 131072
    MMAP_BYTES = 268435456
    # Autocheckpoint threshold (in 4 KB pages) used while bulk-binding.  The
    # default 1000 (4 MB) checkpointed on nearly every bind; disabling it
    # entirely let the WAL grow past 500 MB and the pool collapsed again, so the
    # threshold is raised instead of removed: 10000 pages keeps the WAL near 40
    # MB while cutting checkpoint frequency by an order of magnitude.
    BULK_AUTOCHECKPOINT_PAGES = 10000
    # Cap left behind by a checkpoint.  Dropping an index wrote a 310 MB WAL; the
    # file kept that size as a high-water mark, so every later commit check-pointed
    # a huge backlog and reads had to consult the matching wal-index.  64 MB is
    # comfortably above the 40 MB bulk threshold.
    JOURNAL_SIZE_LIMIT = 67108864

    def __init__(self, root: str | os.PathLike[str], *, read_only: bool = False):
        self.root = Path(root).resolve()
        self.read_only = bool(read_only)
        self.db_path = self.root / "index.sqlite3"
        self.objects_root = self.root / "objects"
        self.parts_root = self.root / ".parts"
        # Re-entrant: bind_object and record_object_checksums call db() from
        # inside their own guard on the same lock.
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._write_conn: sqlite3.Connection | None = None
        self._readers_lock = threading.Lock()
        self._readers: list[sqlite3.Connection] = []
        if self.read_only:
            if not self.root.is_dir():
                raise FileNotFoundError(self.root)
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            if not self.objects_root.is_dir():
                raise FileNotFoundError(self.objects_root)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.objects_root.mkdir(parents=True, exist_ok=True)
            self.parts_root.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def require_write(self) -> None:
        if self.read_only:
            raise PermissionError("versioned asset store is read-only")

    def _configure(self, conn: sqlite3.Connection, *, busy_timeout: int, cache_kib: int):
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
        conn.execute(f"PRAGMA mmap_size={int(self.MMAP_BYTES)}")
        # Temp b-trees (the ORDER BY on the checksum index) must stay in memory.
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "reader", None)
        if conn is None:
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            conn = self._configure(
                # Registered for close() from another thread, so the same-thread
                # check has to go; each reader stays private to its own thread.
                sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False),
                busy_timeout=30000,
                cache_kib=self.READER_CACHE_KIB,
            )
            with self._readers_lock:
                self._readers.append(conn)
            self._local.reader = conn
        return conn

    def _writer(self) -> sqlite3.Connection:
        conn = self._write_conn
        if conn is None:
            with self._write_lock:
                conn = self._write_conn
                if conn is None:
                    # Shared by every worker thread, so the same-thread check has
                    # to go: db() serializes writers on _write_lock instead.
                    conn = self._configure(
                        sqlite3.connect(self.db_path, timeout=120, check_same_thread=False),
                        busy_timeout=120000,
                        cache_kib=self.WRITER_CACHE_KIB,
                    )
                    # WAL + NORMAL avoids a physical fdatasync for every
                    # individual archive metadata update. Payload bytes are
                    # already content-addressed and hash-verified before metadata
                    # publication, so a crash can at worst lose a small tail of
                    # bindings that the idempotent sync repairs.
                    conn.execute("PRAGMA synchronous=NORMAL")
                    # A big maintenance transaction (dropping an index) leaves a
                    # multi-hundred-megabyte WAL behind.  Without a size limit the
                    # file keeps that high-water mark forever, every later commit
                    # has to checkpoint that backlog, and the bind phase crawls back
                    # down to ~20 objects/s.  Cap the file so it is truncated after
                    # a checkpoint instead of staying huge.
                    conn.execute(f"PRAGMA journal_size_limit={int(self.JOURNAL_SIZE_LIMIT)}")
                    self._write_conn = conn
        return conn

    def close(self) -> None:
        """Close the pooled connections (tests and long-lived CLI processes)."""
        writer = getattr(self, "_write_conn", None)
        self._write_conn = None
        if writer is not None:
            writer.close()
        lock = getattr(self, "_readers_lock", None)
        readers = list(getattr(self, "_readers", []))
        if lock is not None:
            with lock:
                readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()
        local = getattr(self, "_local", None)
        if local is not None:
            local.reader = None

    def __del__(self):
        # Safety net for in-process callers (the CLI commands run as subprocesses,
        # but verify()/sync() may also be imported and embedded).
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def db(self):
        """Read view: each thread uses its own read-only connection, unlocked.

        Reads must never queue behind the writer.  An archive sync runs 256
        worker threads, each of which looks an entry up before it binds it, and a
        single writer connection guarded by one lock made every one of those
        lookups wait for the transaction in flight -- measured at 4 bound objects
        per second with 132 of 256 threads parked in `db()` waiting for a commit
        that was checkpointing the WAL.
        """
        yield self._reader()

    @contextmanager
    def write_db(self):
        """Write view: one connection shared by all threads, serialized."""
        self.require_write()
        conn = self._writer()
        with self._write_lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def bulk_writes(self):
        """Raise the WAL autocheckpoint threshold while many small transactions land.

        A sync binds ~168k objects one short transaction at a time.  With the
        default autocheckpoint (every 1000 pages of WAL) each of those commits
        also wrote dirty pages back into the 2 GB index at random offsets: the
        serialized bind cost 260 ms, the pool ran at 3.8 objects per second with
        255 of 256 threads waiting on the write lock, and the process read ~17
        MB/s while binding.  Disabling checkpoints entirely is worse -- the WAL
        passed 500 MB and the pool collapsed to 4 objects/s again -- so the
        threshold is raised to BULK_AUTOCHECKPOINT_PAGES and restored, with one
        final PASSIVE checkpoint, when the bulk phase ends.
        """
        self.require_write()
        with self._write_lock:
            conn = self._writer()
            previous = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            conn.execute(f"PRAGMA wal_autocheckpoint={int(self.BULK_AUTOCHECKPOINT_PAGES)}")
        try:
            yield
        finally:
            with self._write_lock:
                conn = self._writer()
                conn.execute(f"PRAGMA wal_autocheckpoint={int(previous)}")
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def _init_db(self):
        # Journal mode is a database-level setting.  Set it once during
        # initialization; doing this on every worker connection takes an
        # exclusive lock and collapses under parallel archive admission.
        conn = sqlite3.connect(self.db_path, timeout=120)
        try:
            conn.execute("PRAGMA busy_timeout=120000")
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
        with self.write_db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    version TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    asset_root TEXT NOT NULL,
                    manifest_name TEXT NOT NULL,
                    manifest_sha256 TEXT,
                    object_count INTEGER NOT NULL DEFAULT 0,
                    complete INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (version, scope)
                );
                CREATE TABLE IF NOT EXISTS entries (
                    version TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sha256 TEXT,
                    size INTEGER,
                    status INTEGER,
                    content_type TEXT,
                    etag TEXT,
                    last_modified TEXT,
                    cache_control TEXT,
                    fetched_at REAL,
                    PRIMARY KEY (version, scope, name),
                    FOREIGN KEY (version, scope)
                        REFERENCES versions(version, scope) ON DELETE CASCADE
                );
                -- No secondary indexes on `entries`: see UNUSED_ENTRY_INDEXES.  A
                -- bulk bind rewrites sha256 for ~168k rows, and each hash-keyed
                -- index turns that into a random leaf page write; the primary key
                -- plus a name-sorted batch keeps every page sequential instead.
                CREATE TABLE IF NOT EXISTS object_checksums (
                    sha256 TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    md5 TEXT NOT NULL,
                    verified_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_object_checksums_md5_size
                    ON object_checksums(md5,size);
                """
            )

    def current_version(self) -> str:
        link = self.root / "current"
        if not link.is_symlink():
            raise FileNotFoundError("current version link is not configured")
        target = link.resolve(strict=True)
        views_root = (self.root / "views").resolve()
        if target.parent != views_root:
            raise ValueError("current version link escapes the static views root")
        version = target.name
        if not version or "/" in version or "\\" in version or version in {".", ".."}:
            raise ValueError("current version link has an unsafe target")
        return version

    @staticmethod
    def normalize_identity(version: str, scope: str) -> tuple[str, str]:
        version = str(version).strip()
        if not version or "/" in version or "\\" in version or version in {".", ".."}:
            raise ValueError("version must be one safe path component")
        scope = safe_relative_name(scope)
        if "/" in scope:
            raise ValueError("scope must be one safe path component")
        return version, scope

    def object_path(self, sha256: str) -> Path:
        digest = str(sha256).lower()
        if len(digest) != 64:
            raise ValueError("sha256 must contain 64 hex characters")
        int(digest, 16)
        path = self.objects_root / digest[:2] / digest
        if not self.read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def part_path(self, version: str, scope: str, name: str) -> Path:
        self.require_write()
        version, scope = self.normalize_identity(version, scope)
        name = safe_relative_name(name)
        path = self.parts_root / version / scope
        for part in name.split("/"):
            path = path / part
        path = path.with_name(path.name + ".part")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_version(
        self,
        version: str,
        scope: str,
        *,
        asset_root: str,
        manifest_name: str,
        manifest_sha256: str | None = None,
        object_count: int | None = None,
    ):
        self.require_write()
        version, scope = self.normalize_identity(version, scope)
        manifest_name = safe_relative_name(manifest_name)
        now = time.time()
        with self.write_db() as conn:
            row = conn.execute(
                "SELECT asset_root,manifest_name,manifest_sha256 FROM versions WHERE version=? AND scope=?",
                (version, scope),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO versions(
                        version,scope,asset_root,manifest_name,manifest_sha256,
                        object_count,complete,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,0,?,?)
                    """,
                    (
                        version, scope, asset_root.rstrip("/"), manifest_name,
                        manifest_sha256.lower() if manifest_sha256 else None,
                        int(object_count or 0), now, now,
                    ),
                )
                return
            old_root, old_manifest, old_sha = row
            if old_manifest != manifest_name:
                raise VersionConflict(
                    f"{version}/{scope} manifest changed: {old_manifest!r} -> {manifest_name!r}"
                )
            if old_sha and manifest_sha256 and old_sha.lower() != manifest_sha256.lower():
                raise VersionConflict(
                    f"{version}/{scope} manifest hash changed: {old_sha} -> {manifest_sha256}"
                )
            if old_root.rstrip("/") != asset_root.rstrip("/"):
                raise VersionConflict(
                    f"{version}/{scope} upstream root changed: {old_root!r} -> {asset_root!r}"
                )
            conn.execute(
                """
                UPDATE versions
                SET manifest_sha256=COALESCE(manifest_sha256,?),
                    object_count=CASE WHEN ? IS NULL THEN object_count ELSE ? END,
                    updated_at=?
                WHERE version=? AND scope=?
                """,
                (
                    manifest_sha256.lower() if manifest_sha256 else None,
                    object_count, int(object_count or 0), now, version, scope,
                ),
            )

    def register_names(self, version: str, scope: str, names: Iterable[str]):
        self.require_write()
        version, scope = self.normalize_identity(version, scope)
        rows = [(version, scope, safe_relative_name(name)) for name in names]
        # A version manifest carries ~168k names and the list is in arbitrary
        # order.  Unordered inserts scatter every new row across the primary key,
        # the (scope,name,version) index and the verify-cover index: one random
        # 4 KB page (or more) per row, which measured 168k rows against a 2 GB
        # index at tens of MB/s of reads and tens of minutes.  Sorting makes the
        # new keys append sequentially in all of them instead.
        rows.sort()
        with self.write_db() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO entries(version,scope,name) VALUES(?,?,?)",
                rows,
            )
            conn.execute(
                "UPDATE versions SET object_count=?, updated_at=? WHERE version=? AND scope=?",
                (len(rows), time.time(), version, scope),
            )

    def registered_count(self, version: str, scope: str) -> int:
        version, scope = self.normalize_identity(version, scope)
        with self.db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE version=? AND scope=?",
                (version, scope),
            ).fetchone()
        return int(row[0] or 0)

    def version(self, version: str, scope: str) -> dict | None:
        version, scope = self.normalize_identity(version, scope)
        with self.db() as conn:
            row = conn.execute(
                """
                SELECT asset_root,manifest_name,manifest_sha256,object_count,complete,
                       created_at,updated_at
                FROM versions WHERE version=? AND scope=?
                """,
                (version, scope),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "asset_root","manifest_name","manifest_sha256","object_count","complete",
            "created_at","updated_at",
        )
        out = dict(zip(keys, row))
        out["version"] = version
        out["scope"] = scope
        out["complete"] = bool(out["complete"])
        return out

    def lookup(self, version: str, scope: str, name: str) -> dict | None:
        version, scope = self.normalize_identity(version, scope)
        name = safe_relative_name(name)
        with self.db() as conn:
            row = conn.execute(
                """
                SELECT sha256,size,status,content_type,etag,last_modified,cache_control,fetched_at
                FROM entries WHERE version=? AND scope=? AND name=?
                """,
                (version, scope, name),
            ).fetchone()
        if row is None:
            return None
        keys = ("sha256","size","status","content_type","etag","last_modified","cache_control","fetched_at")
        return dict(zip(keys, row))

    def reuse_etag_hints(self, version: str, scope: str) -> list[dict]:
        """Return prior-version CAS mappings whose ETags can hint at content MD5.

        ETags are only hints here.  Callers must verify the local candidate bytes
        against authoritative current-object MD5 metadata before reuse.
        """
        version, scope = self.normalize_identity(version, scope)
        with self.db() as conn:
            rows = conn.execute(
                """
                SELECT version,name,sha256,size,etag,fetched_at
                FROM entries
                WHERE scope=? AND version<>?
                  AND sha256 IS NOT NULL AND size IS NOT NULL AND etag IS NOT NULL
                """,
                (scope, version),
            ).fetchall()
        keys = ("version","name","sha256","size","etag","fetched_at")
        return [dict(zip(keys, row)) for row in rows]

    def object_by_md5(self, md5: str, size: int) -> dict | None:
        md5 = str(md5).lower()
        if len(md5) != 32:
            raise ValueError("md5 must contain 32 hex characters")
        int(md5, 16)
        with self.db() as conn:
            row = conn.execute(
                """
                SELECT sha256,size,verified_at
                FROM object_checksums
                WHERE md5=? AND size=?
                ORDER BY verified_at DESC
                LIMIT 1
                """,
                (md5, int(size)),
            ).fetchone()
        if row is None:
            return None
        sha256, stored_size, verified_at = row
        path = self.object_path(str(sha256))
        if not path.is_file() or path.stat().st_size != int(stored_size):
            return None
        return {
            "sha256": str(sha256),
            "size": int(stored_size),
            "md5": md5,
            "verified_at": verified_at,
        }

    def has_object_checksums(self) -> bool:
        with self.db() as conn:
            return conn.execute(
                "SELECT 1 FROM object_checksums LIMIT 1"
            ).fetchone() is not None

    def object_checksum_index(self) -> dict[str, tuple[int, str]]:
        with self.db() as conn:
            rows = conn.execute(
                "SELECT sha256,size,md5 FROM object_checksums"
            ).fetchall()
        return {
            str(sha256): (int(size), str(md5))
            for sha256, size, md5 in rows
        }

    def record_object_checksums(
        self, rows: Iterable[tuple[str, int, str]]
    ) -> None:
        """Persist SHA-256 -> MD5 mappings produced by a trusted local read."""
        self.require_write()
        normalized = []
        now = time.time()
        for sha256, size, md5 in rows:
            sha256 = str(sha256).lower()
            md5 = str(md5).lower()
            if len(sha256) != 64 or len(md5) != 32:
                raise ValueError("invalid checksum length")
            int(sha256, 16)
            int(md5, 16)
            normalized.append((sha256, int(size), md5, now))
        if not normalized:
            return
        with self.write_db() as conn:
            conn.executemany(
                """
                INSERT INTO object_checksums(sha256,size,md5,verified_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(sha256) DO UPDATE SET
                        size=excluded.size,
                        md5=excluded.md5,
                        verified_at=excluded.verified_at
                    """,
                    normalized,
                )

    def bind_object(
        self,
        version: str,
        scope: str,
        name: str,
        *,
        sha256: str,
        size: int,
        status: int,
        headers,
        content_md5: str | None = None,
    ):
        get = headers.get if headers is not None else lambda _k: None
        self.bind_objects(
            version,
            scope,
            [
                {
                    "name": name,
                    "sha256": sha256,
                    "size": size,
                    "status": status,
                    "content_type": get("Content-Type"),
                    "etag": get("ETag"),
                    "last_modified": get("Last-Modified"),
                    "cache_control": get("Cache-Control"),
                    "content_md5": content_md5,
                }
            ],
        )

    def bind_objects(self, version: str, scope: str, rows: Iterable[dict]) -> int:
        """Bind many objects in one transaction, sorted by primary key.

        Binding an archived version is ~168k objects.  One transaction per object
        dirties the same leaf pages over and over, and every WAL checkpoint writes
        them back into the 2 GB index at random offsets: measured at ~35 KB of
        physical writes per object -- ~5.9 GB for one version -- against a pool
        that does ~1 MB/s of random writes, which is what made the phase take over
        an hour.  Applying the batch in primary-key order walks those leaf pages
        once and writes each page once.
        """
        self.require_write()
        version, scope = self.normalize_identity(version, scope)
        now = time.time()
        prepared: list[tuple] = []
        checksums: list[tuple] = []
        for row in rows:
            name = safe_relative_name(row["name"])
            sha256 = str(row["sha256"]).lower()
            size = int(row["size"])
            content_md5 = row.get("content_md5")
            if content_md5 is not None:
                md5 = str(content_md5).lower()
                if len(md5) != 32:
                    raise ValueError("content_md5 must contain 32 hex characters")
                int(md5, 16)
                checksums.append((sha256, size, md5, now))
            prepared.append(
                (
                    sha256,
                    size,
                    int(row["status"]),
                    row.get("content_type"),
                    row.get("etag"),
                    row.get("last_modified"),
                    row.get("cache_control"),
                    now,
                    version,
                    scope,
                    name,
                )
            )
        if not prepared:
            return 0
        prepared.sort(key=lambda item: item[-1])
        with self.write_db() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                UPDATE entries SET
                    sha256=?,size=?,status=?,content_type=?,etag=?,last_modified=?,
                    cache_control=?,fetched_at=?
                WHERE version=? AND scope=? AND name=?
                """,
                prepared,
            )
            if conn.total_changes - before != len(prepared):
                raise KeyError(
                    f"assets not registered for {version}/{scope}: "
                    f"{self._unregistered_names(conn, version, scope, [row[-1] for row in prepared])}"
                )
            if checksums:
                conn.executemany(
                    """
                    INSERT INTO object_checksums(sha256,size,md5,verified_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(sha256) DO UPDATE SET
                        size=excluded.size,
                        md5=excluded.md5,
                        verified_at=excluded.verified_at
                    """,
                    checksums,
                )
        return len(prepared)

    @staticmethod
    def _unregistered_names(conn, version: str, scope: str, names: list[str]) -> list[str]:
        """Name the rows a batch update missed, for the per-object fallback."""
        found: set[str] = set()
        for offset in range(0, len(names), 400):
            chunk = names[offset : offset + 400]
            placeholders = ",".join("?" * len(chunk))
            found.update(
                row[0]
                for row in conn.execute(
                    f"SELECT name FROM entries WHERE version=? AND scope=? "
                    f"AND name IN ({placeholders})",
                    (version, scope, *chunk),
                )
            )
        return [name for name in names if name not in found][:10]

    def commit_part(self, part: Path, sha256: str) -> Path:
        """Publish one verified temp file into CAS without ever overwriting an object."""
        self.require_write()
        destination = self.object_path(sha256)
        if destination.is_file():
            if destination.stat().st_size != part.stat().st_size:
                raise IOError("existing CAS object size does not match incoming object")
            if self.sha256_file(destination) != sha256.lower():
                raise IOError("existing CAS object hash does not match its key")
            part.unlink(missing_ok=True)
            destination.chmod(0o644)
            return destination

        try:
            # .parts and objects live below the same store root, so a hard-link is
            # an atomic no-overwrite publish on supported local filesystems.
            os.link(part, destination)
        except FileExistsError:
            if destination.stat().st_size != part.stat().st_size:
                raise IOError("raced CAS object has a different size")
            if self.sha256_file(destination) != sha256.lower():
                raise IOError("raced CAS object does not match its key")
        except OSError:
            # Some filesystems/platforms may not permit hard links.  Exclusive
            # creation still preserves the no-overwrite invariant.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(destination, flags, 0o644)
            except FileExistsError:
                if destination.stat().st_size != part.stat().st_size:
                    raise IOError("raced CAS object has a different size")
                if self.sha256_file(destination) != sha256.lower():
                    raise IOError("raced CAS object does not match its key")
            else:
                try:
                    with os.fdopen(fd, "wb") as output, part.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
        finally:
            part.unlink(missing_ok=True)
        destination.chmod(0o644)
        return destination

    def mark_complete(self, version: str, scope: str, complete: bool):
        self.require_write()
        version, scope = self.normalize_identity(version, scope)
        with self.write_db() as conn:
            conn.execute(
                "UPDATE versions SET complete=?,updated_at=? WHERE version=? AND scope=?",
                (1 if complete else 0, time.time(), version, scope),
            )

    def stats(self, version: str, scope: str) -> dict:
        version, scope = self.normalize_identity(version, scope)
        with self.db() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), SUM(CASE WHEN sha256 IS NOT NULL THEN 1 ELSE 0 END),
                       COALESCE(SUM(size),0)
                FROM entries WHERE version=? AND scope=?
                """,
                (version, scope),
            ).fetchone()
            unique = conn.execute(
                """
                SELECT COUNT(DISTINCT sha256)
                FROM entries WHERE version=? AND scope=? AND sha256 IS NOT NULL
                """,
                (version, scope),
            ).fetchone()[0]
        total, mapped, logical_bytes = row
        return {
            "version": version,
            "scope": scope,
            "registered": int(total or 0),
            "mapped": int(mapped or 0),
            "missing": int((total or 0) - (mapped or 0)),
            "unique_objects": int(unique or 0),
            "logical_bytes": int(logical_bytes or 0),
        }

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def md5_file(path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def sha256_md5_file(path: Path) -> tuple[str, str]:
        sha256 = hashlib.sha256()
        md5 = hashlib.md5()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha256.update(chunk)
                md5.update(chunk)
        return sha256.hexdigest(), md5.hexdigest()
