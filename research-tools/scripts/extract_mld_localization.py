#!/usr/bin/env python3
"""Extract source-backed visible localization values from decoded MLTD MLD config.

MLD contains both internal configuration tokens and strings that are consumed as
visible labels.  This extractor is deliberately allowlist-based: only key
families whose names describe display text/name fields are promoted.  Internal
compound configuration values such as AdvancedCommuIdolParam remain audit-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")
VISIBLE_DATA_MAP_PATTERNS = (
    re.compile(r"^hotate_system_value_navi_chara_name_\d+$"),
    re.compile(r"^param_eventview_story_displaybutton_text_\d+_\d+$"),
    re.compile(r"^param_event_story_overwrite_title_\d+_\d+$"),
    re.compile(r"^param_lesson_wear_group_short_name_\d+$"),
)


def source_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_visible_data_map_key(key: str) -> bool:
    return any(pattern.fullmatch(key) for pattern in VISIBLE_DATA_MAP_PATTERNS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--decoded",
        type=Path,
        default=Path("build/live-consistency-reverse-90200/MD.mld.decoded.json"),
    )
    ap.add_argument(
        "--logical",
        default="md.mld.unity3d",
    )
    ap.add_argument(
        "--textasset-name",
        default="MD.mld",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/mld-translation-queue.jsonl"),
    )
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path("build/localization-90200/mld-localization-summary.json"),
    )
    args = ap.parse_args()

    doc = json.loads(args.decoded.read_text(encoding="utf-8-sig"))
    data_map = doc.get("data_map", {})
    data_list = doc.get("data_list", {})
    if not isinstance(data_map, dict) or not isinstance(data_list, dict):
        raise ValueError("decoded MLD must contain data_map and data_list dictionaries")

    all_jp: list[dict[str, Any]] = []
    visible: list[dict[str, Any]] = []
    internal: list[dict[str, Any]] = []

    for key, raw in data_map.items():
        value = str(raw)
        if not JP_KANA_RE.search(value):
            continue
        row = {"section": "data_map", "key": str(key), "value": value}
        all_jp.append(row)
        if is_visible_data_map_key(str(key)):
            visible.append(row)
        else:
            internal.append(row)

    for key, raw_values in data_list.items():
        if not isinstance(raw_values, list):
            continue
        for index, raw in enumerate(raw_values):
            value = str(raw)
            if not JP_KANA_RE.search(value):
                continue
            row = {
                "section": "data_list",
                "key": str(key),
                "index": index,
                "value": value,
            }
            all_jp.append(row)
            internal.append(row)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in visible:
        grouped[row["value"]].append(row)

    queue_rows = []
    for value, occurrences in grouped.items():
        examples = []
        for row in occurrences:
            example = {
                "surface": "mld_config",
                "logical": args.logical,
                "textasset_name": args.textasset_name,
                "section": row["section"],
                "key": row["key"],
            }
            if "index" in row:
                example["index"] = row["index"]
            examples.append(example)
        queue_rows.append(
            {
                "source_sha256": source_id(value),
                "source": value,
                "translation": "",
                "status": "pending",
                "occurrences": len(occurrences),
                "examples": examples,
                "usage_profile": {
                    "categories": ["MLD_CONFIG_UI"],
                    "category_count": 1,
                    "source_kind": "mld_config",
                    "multi_category": False,
                    "requires_cross_context_consistency": len(occurrences) > 1,
                },
                "queue_reason": "source_backed_mld_visible_config",
            }
        )

    queue_rows.sort(key=lambda row: str(row["source"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in queue_rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    internal_counts = Counter(str(row["key"]) for row in internal)
    summary = {
        "schema_version": 1,
        "kind": "mltd-mld-localization-extraction",
        "decoded": str(args.decoded),
        "logical": args.logical,
        "textasset_name": args.textasset_name,
        "data_map_keys": len(data_map),
        "data_list_keys": len(data_list),
        "jp_occurrences": len(all_jp),
        "jp_unique_values": len({row["value"] for row in all_jp}),
        "visible_occurrences": len(visible),
        "visible_unique": len(queue_rows),
        "internal_occurrences": len(internal),
        "visible_key_families": [pattern.pattern for pattern in VISIBLE_DATA_MAP_PATTERNS],
        "internal_key_counts": dict(internal_counts),
        "internal_examples": internal[:50],
        "output": str(args.output),
        "policy": (
            "Only explicit display/name key families are promoted. Internal MLD values "
            "remain audit evidence and are not translated."
        ),
    }
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
