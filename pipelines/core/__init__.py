"""Dependency-light primitives shared by the repository's pipelines.

The core layer intentionally knows nothing about Unity, translation providers,
or release policy. Domain packages can depend on it; it must not depend back on
them. This keeps small validation and indexing tools usable without importing
large optional runtimes.
"""

from .hashing import sha256_bytes, sha256_file, sha256_text
from .jsonl import iter_jsonl, jsonl_lines, read_jsonl, write_jsonl, write_text_if_changed

__all__ = [
    "iter_jsonl",
    "jsonl_lines",
    "read_jsonl",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "write_jsonl",
    "write_text_if_changed",
]
