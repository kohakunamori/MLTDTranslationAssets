#!/usr/bin/env python3
"""Audit Android string resources for Japanese values lacking zh-rCN coverage."""
from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")


def load_strings(path: Path) -> dict[tuple[str, str], str]:
    if not path.is_file():
        return {}
    out: dict[tuple[str, str], str] = {}
    root = ET.parse(path).getroot()
    for node in root:
        name = node.attrib.get("name")
        if not name:
            continue
        out[(node.tag, name)] = "".join(node.itertext())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--res-root",
        type=Path,
        default=Path(
            "work/reference-capture-full-session/private/"
            "build-90200-appguard-free/apktool-work/res"
        ),
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/android-resource-localization-audit.json"),
    )
    args = ap.parse_args()

    default = load_strings(args.res_root / "values" / "strings.xml")
    ja = load_strings(args.res_root / "values-ja" / "strings.xml")
    zh = load_strings(args.res_root / "values-zh-rCN" / "strings.xml")

    jp_keys: set[tuple[str, str]] = set()
    for table in (default, ja):
        for key, value in table.items():
            if JP_KANA_RE.search(value):
                jp_keys.add(key)

    missing: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    for key in sorted(jp_keys):
        source = ja.get(key, default.get(key, ""))
        target = zh.get(key)
        row = {
            "tag": key[0],
            "name": key[1],
            "source": source,
            "zh_rCN": target,
        }
        if target is None:
            missing.append(row)
        else:
            covered.append(row)

    result = {
        "schema_version": 1,
        "kind": "mltd-android-resource-localization-audit",
        "res_root": str(args.res_root),
        "jp_source_keys": len(jp_keys),
        "zh_rCN_covered": len(covered),
        "zh_rCN_missing": len(missing),
        "missing": missing,
        "covered": covered,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "jp_source_keys": len(jp_keys),
        "zh_rCN_covered": len(covered),
        "zh_rCN_missing": len(missing),
        "missing": missing,
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
