"""Streaming content hashing primitives used across pipeline domains."""
from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest for ``payload``."""

    return hashlib.sha256(payload).hexdigest()


def sha256_text(value: str) -> str:
    """Return the lowercase SHA-256 digest of UTF-8 encoded text."""

    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash a file without loading it into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
