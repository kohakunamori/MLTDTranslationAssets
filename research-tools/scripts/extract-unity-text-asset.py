#!/usr/bin/env python3
"""Extract one named TextAsset from an MLTD Unity bundle or static asset view."""
from __future__ import annotations

import argparse
from pathlib import Path

import msgpack
import UnityPy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path)
    source.add_argument("--asset-index", type=Path)
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--logical")
    parser.add_argument("--text-asset-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def resolve_bundle(args: argparse.Namespace) -> Path:
    if args.bundle is not None:
        return args.bundle
    if args.asset_root is None or not args.logical:
        raise SystemExit("--asset-index requires --asset-root and --logical")
    raw = msgpack.unpackb(args.asset_index.read_bytes(), raw=False, strict_map_key=False)
    index = raw[0]
    record = index.get(args.logical)
    if not isinstance(record, (list, tuple)) or len(record) < 2:
        raise SystemExit(f"logical not found in asset index: {args.logical}")
    return args.asset_root / str(record[1])


def main() -> int:
    args = parse_args()
    bundle = resolve_bundle(args)
    if not bundle.is_file():
        raise SystemExit(f"bundle not found: {bundle}")
    matches: list[bytes] = []
    for obj in UnityPy.load(str(bundle)).objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        if str(getattr(data, "m_Name", "")) != args.text_asset_name:
            continue
        value = getattr(data, "m_Script", b"")
        if isinstance(value, str):
            matches.append(value.encode("utf-8"))
        elif isinstance(value, memoryview):
            matches.append(value.tobytes())
        else:
            matches.append(bytes(value))
    if len(matches) != 1:
        raise SystemExit(
            f"expected one TextAsset named {args.text_asset_name!r}, found {len(matches)}"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(matches[0])
    print(f"bundle={bundle}")
    print(f"bytes={len(matches[0])}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
