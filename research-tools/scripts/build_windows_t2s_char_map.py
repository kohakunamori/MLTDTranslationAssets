#!/usr/bin/env python3
"""Build a static Traditional->Simplified CJK character map using Windows NLS.

The generated JSON is runtime-portable: deterministic QA can consume it on any OS.
Only one-code-point mappings in CJK Extension A + Unified Ideographs are emitted.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path

LCMAP_SIMPLIFIED_CHINESE = 0x02000000
RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))


def windows_t2s(text: str) -> str:
    if os.name != "nt":
        raise RuntimeError("This builder requires Windows LCMapStringEx")
    kernel32 = ctypes.windll.kernel32
    kernel32.LCMapStringEx.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_longlong,
    ]
    kernel32.LCMapStringEx.restype = ctypes.c_int
    needed = kernel32.LCMapStringEx(
        "zh-CN", LCMAP_SIMPLIFIED_CHINESE, text, len(text), None, 0, None, None, 0
    )
    if needed <= 0:
        raise ctypes.WinError()
    buf = ctypes.create_unicode_buffer(needed)
    written = kernel32.LCMapStringEx(
        "zh-CN",
        LCMAP_SIMPLIFIED_CHINESE,
        text,
        len(text),
        buf,
        needed,
        None,
        None,
        0,
    )
    if written <= 0:
        raise ctypes.WinError()
    return buf[:written]


def build_map() -> dict[str, str]:
    result: dict[str, str] = {}
    for start, end in RANGES:
        for codepoint in range(start, end + 1):
            source = chr(codepoint)
            target = windows_t2s(source)
            if len(target) == 1 and target != source:
                result[source] = target
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("localization/quality/traditional-to-simplified-char-map.json"),
    )
    args = ap.parse_args()
    mapping = build_map()
    doc = {
        "schema_version": 1,
        "source": "Windows LCMapStringEx zh-CN LCMAP_SIMPLIFIED_CHINESE",
        "ranges": [[hex(a), hex(b)] for a, b in RANGES],
        "entries": len(mapping),
        "map": mapping,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "entries": len(mapping)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
