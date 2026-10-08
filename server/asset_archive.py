#!/usr/bin/env python3
"""Durable on-disk MLTD asset archive primitives."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import msgpack


def safe_relative_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("asset name must be non-empty text")
    value = value.replace("\\", "/")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe asset path: {value!r}")
    return "/".join(path.parts)


def parse_manifest_objects(data: bytes) -> list[str]:
    manifest = msgpack.unpackb(data, raw=False, strict_map_key=False)
    if not isinstance(manifest, (list, tuple)) or not manifest:
        raise ValueError("invalid MLTD asset manifest root")
    table = manifest[0]
    if not isinstance(table, dict):
        raise ValueError("invalid MLTD asset manifest table")
    out: list[str] = []
    seen: set[str] = set()
    for record in table.values():
        if not isinstance(record, (list, tuple)) or len(record) < 2:
            raise ValueError("invalid MLTD asset manifest record")
        name = record[1]
        if isinstance(name, bytes):
            name = name.decode("utf-8")
        name = safe_relative_name(name)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


class AssetArchive:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / ".asset-index.sqlite3"
        self._init_db()

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS object_metadata (
                    scope TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    content_type TEXT,
                    etag TEXT,
                    last_modified TEXT,
                    cache_control TEXT,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (scope, name)
                )
                """
            )

    def scope_dir(self, scope: str) -> Path:
        scope = safe_relative_name(scope)
        if "/" in scope:
            raise ValueError("scope must be one path component")
        path = self.root / scope
        path.mkdir(parents=True, exist_ok=True)
        return path

    def object_path(self, scope: str, name: str) -> Path:
        name = safe_relative_name(name)
        path = self.scope_dir(scope).joinpath(*name.split("/")).resolve()
        base = self.scope_dir(scope).resolve()
        if path != base and base not in path.parents:
            raise ValueError("asset path escaped scope")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def part_path(self, scope: str, name: str) -> Path:
        path = self.object_path(scope, name)
        return path.with_name(path.name + ".part")

    def put_metadata(
        self,
        scope: str,
        name: str,
        *,
        status: int,
        size: int,
        sha256: str,
        headers,
        fetched_at: float | None = None,
    ):
        name = safe_relative_name(name)
        get = headers.get if headers is not None else lambda _k: None
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO object_metadata (
                    scope,name,status,size,sha256,content_type,etag,last_modified,
                    cache_control,fetched_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scope,name) DO UPDATE SET
                    status=excluded.status,
                    size=excluded.size,
                    sha256=excluded.sha256,
                    content_type=excluded.content_type,
                    etag=excluded.etag,
                    last_modified=excluded.last_modified,
                    cache_control=excluded.cache_control,
                    fetched_at=excluded.fetched_at
                """,
                (
                    scope,
                    name,
                    int(status),
                    int(size),
                    sha256.lower(),
                    get("Content-Type"),
                    get("ETag"),
                    get("Last-Modified"),
                    get("Cache-Control"),
                    time.time() if fetched_at is None else fetched_at,
                ),
            )

    def metadata(self, scope: str, name: str):
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT status,size,sha256,content_type,etag,last_modified,
                       cache_control,fetched_at
                FROM object_metadata WHERE scope=? AND name=?
                """,
                (scope, safe_relative_name(name)),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "status","size","sha256","content_type","etag","last_modified",
            "cache_control","fetched_at",
        )
        return dict(zip(keys, row))

    def is_complete(self, scope: str, name: str, *, verify=False) -> bool:
        path = self.object_path(scope, name)
        meta = self.metadata(scope, name)
        if meta is None:
            return False
        try:
            if path.stat().st_size != meta["size"]:
                return False
        except OSError:
            return False
        if not verify:
            return True
        return self.sha256_file(path) == meta["sha256"]

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def verify_many(self, scope: str, names: Iterable[str]) -> tuple[int, list[str]]:
        ok = 0
        bad: list[str] = []
        for name in names:
            if self.is_complete(scope, name, verify=True):
                ok += 1
            else:
                bad.append(name)
        return ok, bad
