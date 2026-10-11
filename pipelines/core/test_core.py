from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipelines.core.hashing import sha256_bytes, sha256_file, sha256_text
from pipelines.core.jsonl import jsonl_lines, read_jsonl, write_jsonl


def test_hashing_primitives_share_one_digest_contract(tmp_path: Path) -> None:
    payload = "歌词\u2028仍在同一条 JSON 字符串里"
    path = tmp_path / "payload.txt"
    path.write_text(payload, encoding="utf-8")

    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert sha256_text(payload) == expected
    assert sha256_bytes(payload.encode("utf-8")) == expected
    assert sha256_file(path) == expected


def test_jsonl_lines_does_not_split_unicode_line_separator() -> None:
    text = json.dumps({"ja": "第一行\u2028第二行"}, ensure_ascii=False) + "\n"
    assert jsonl_lines(text) == [text.rstrip("\n")]


def test_write_and_read_jsonl_are_atomic_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "rows.jsonl"
    rows = [{"id": 1, "text": "中文"}, {"id": 2, "text": "第二行"}]

    write_jsonl(path, rows)

    assert read_jsonl(path) == rows
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_write_jsonl_removes_partial_file_when_row_serialization_fails(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"

    def broken_rows():
        yield {"id": 1}
        yield {"id": object()}

    try:
        write_jsonl(path, broken_rows())
    except TypeError:
        pass
    else:  # pragma: no cover - the serializer must reject object()
        raise AssertionError("expected JSON serialization to fail")

    assert not path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
