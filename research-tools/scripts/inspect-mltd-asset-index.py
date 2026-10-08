"""Inspect an MLTD MessagePack asset index without modifying it.

The observed wire shape is a one-element array whose first element is a map:

    logical_name -> [catalog_hash, remote_name, size]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import msgpack


def digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_index(path: Path) -> dict[str, list[Any]]:
    root = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(root, list) or len(root) != 1 or not isinstance(root[0], dict):
        raise ValueError("expected [map] at MessagePack root")

    entries = root[0]
    for name, value in entries.items():
        if not isinstance(name, str):
            raise ValueError(f"non-string logical name: {name!r}")
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError(f"invalid entry for {name!r}: expected three-element array")
        catalog_hash, remote_name, size = value
        if not isinstance(catalog_hash, str) or not isinstance(remote_name, str):
            raise ValueError(f"invalid string fields for {name!r}")
        if not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid size for {name!r}: {size!r}")
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=Path)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    path = args.index.resolve()
    entries = load_index(path)
    result = {
        "index": str(path),
        "length": path.stat().st_size,
        "sha256": digest(path, "sha256"),
        "entry_count": len(entries),
        "queries": {},
    }
    for name in args.names:
        value = entries.get(name)
        result["queries"][name] = (
            None
            if value is None
            else {"catalog_hash": value[0], "remote_name": value[1], "size": value[2]}
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"index={path}")
        print(f"length={result['length']} sha256={result['sha256']}")
        print(f"entry_count={result['entry_count']}")
        for name, value in result["queries"].items():
            print(f"{name}: {json.dumps(value, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
