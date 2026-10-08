#!/usr/bin/env python3
"""Create source-bound fixes for three archived legacy GTX control-code omissions.

Do not modify the official-legacy input.  Refuse to write unless every exact
bundle/key, current source SHA, expected legacy span, and GTX token QA agree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.mltd_localize_gtx import validate_translation

REPAIRS = {
    ("MB_jp.gtx", "blog_card_021mat0064_title"): {
        "sha": "fe8247bf080a163e54f04e8bed75ad74d16a13b5cb273a81a5a8eea4376d17ef",
        "old": "真是太美妙了唷\\17\\",
        "new": "真是太美妙了\\01\\唷\\17\\",
    },
    ("MB_jp.gtx", "blog_card_011ami0074_text"): {
        "sha": "f8faf75bbc6692da342e190352a0022530a9c023802ed6d8f7d15268bd6a29d0",
        "old": "一份讓你們大～吃好多驚的禮物喔～\n\n請看\\10\\",
        "new": "一份讓你們大～吃好多驚的禮物喔～\\04\\\n\n請看\\10\\",
    },
    ("MB_jp.gtx", "mail_046rio_201000_text"): {
        "sha": "9645ca8d340272e36d8c4b920bd299ca4833a4c3cc201886e295a2e82ae70336",
        "old": "我也每天都會吃喔14\\",
        "new": "我也每天都會吃喔\\14\\",
    },
}


def build(input_path: Path, output_path: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    with input_path.open(encoding="utf-8") as source_file:
        for line in source_file:
            original = json.loads(line)
            identity = (original.get("bundle"), original.get("key"))
            spec = REPAIRS.get(identity)
            if spec is None:
                continue
            if identity in seen:
                raise ValueError(f"duplicate legacy identity: {identity}")
            seen.add(identity)
            source = original["source"]
            translation = original["translation"]
            source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if source_sha != spec["sha"]:
                raise ValueError(f"changed legacy source for {identity}: {source_sha}")
            if translation.count(spec["old"]) != 1:
                raise ValueError(f"legacy translation repair anchor mismatch: {identity}")
            fixed = translation.replace(spec["old"], spec["new"], 1)
            validate_translation(source, fixed)
            rows.append({
                "bundle": identity[0],
                "key": identity[1],
                "source": source,
                "source_sha256": source_sha,
                "translation": fixed,
                "status": "agent_translated",
                "provenance": {
                    "kind": "control-token-only-legacy-repair",
                    "source": str(input_path),
                    "not_human_verified": True,
                    "replaced_span_count": 1,
                    "source_version_key": "jp-client-9.0.200-assets-1077100",
                },
            })
    if seen != REPAIRS.keys():
        raise ValueError(f"missing legacy rows: {sorted(REPAIRS.keys() - seen)}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    temp.replace(output_path)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = build(args.legacy, args.output)
    print(json.dumps({
        "repaired_rows": len(rows),
        "output": str(args.output),
        "source_version_key": "jp-client-9.0.200-assets-1077100",
        "human_verified": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
