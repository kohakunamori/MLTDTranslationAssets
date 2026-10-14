"""Safe JSONL primitives shared by source, queue, and report pipelines."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any


def jsonl_lines(text: str) -> list[str]:
    """Split on real newlines without treating U+2028 as a record boundary."""

    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def iter_jsonl(path: Path, *, encoding: str = "utf-8") -> Iterator[dict[str, Any]]:
    """Yield JSON objects from ``path`` with line-aware validation errors."""

    path = Path(path)
    with path.open("r", encoding=encoding) as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            yield value


def read_jsonl(path: Path, *, encoding: str = "utf-8") -> list[dict[str, Any]]:
    """Read all JSON objects from ``path``."""

    return list(iter_jsonl(path, encoding=encoding))


def write_jsonl(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    encoding: str = "utf-8",
) -> None:
    """Write compact, UTF-8 JSONL records with a deterministic newline."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding=encoding, newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_text_if_changed(path: Path, text: str, *, encoding: str = "utf-8") -> bool:
    """Write ``text`` only when content differs; return whether it changed."""

    path = Path(path)
    if path.is_file() and path.read_text(encoding=encoding) == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(text, encoding=encoding, newline="\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return True


def append_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
