#!/usr/bin/env python3
"""Enrich deduplicated MLTD translation queue rows with neighboring GTX scene context."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import read_jsonl
from scripts.build_character_voice_evidence import infer_speaker_code


def load_speakers(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    speakers = value.get("speakers", {}) if isinstance(value, dict) else {}
    if not isinstance(speakers, dict):
        raise ValueError("speaker registry must contain an object field 'speakers'")
    return speakers


def speaker_hint(key: str, speakers: dict[str, dict]) -> dict | None:
    code = infer_speaker_code(key)
    if not code:
        return None
    evidence = speakers.get(code, {})
    return {
        "speaker_code": code,
        "idol_id": evidence.get("idol_id"),
        "name_jp": evidence.get("name_jp", ""),
        "identity_source": evidence.get("identity_source", f"key_suffix:{code}"),
        "profile_status": evidence.get("profile_status", "code_only"),
    }


def build_context_index(
    catalogue: list[dict],
    window: int,
    speakers: dict[str, dict] | None = None,
) -> dict[tuple[str, str], list[dict]]:
    speakers = speakers or {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in catalogue:
        logical = str(row.get("logical", ""))
        key = str(row.get("key", ""))
        if logical and key:
            groups[logical].append(row)

    index: dict[tuple[str, str], list[dict]] = {}
    for logical, rows in groups.items():
        for i, row in enumerate(rows):
            context: list[dict] = []
            lo = max(0, i - window)
            hi = min(len(rows), i + window + 1)
            for j in range(lo, hi):
                peer = rows[j]
                peer_key = str(peer.get("key", ""))
                context.append({
                    "relative": j - i,
                    "key": peer_key,
                    "source": peer.get("source", ""),
                    "source_sha256": peer.get("source_sha256", ""),
                    "speaker": speaker_hint(peer_key, speakers),
                })
            index[(logical, str(row["key"]))] = context
    return index


def build_usage_profiles(
    catalogue: list[dict],
    speakers: dict[str, dict] | None = None,
) -> dict[str, dict]:
    speakers = speakers or {}
    temp: dict[str, dict] = {}
    for row in catalogue:
        sid = str(row.get("source_sha256", ""))
        if not sid:
            continue
        logical = str(row.get("logical", ""))
        key = str(row.get("key", ""))
        category = logical.split("_", 1)[0] if logical else ""
        hint = speaker_hint(key, speakers)
        state = temp.setdefault(
            sid,
            {
                "catalogue_occurrences": 0,
                "categories": set(),
                "speaker_codes": set(),
            },
        )
        state["catalogue_occurrences"] += 1
        if category:
            state["categories"].add(category)
        if hint:
            state["speaker_codes"].add(hint["speaker_code"])

    result: dict[str, dict] = {}
    for sid, state in temp.items():
        categories = sorted(state["categories"])
        codes = sorted(state["speaker_codes"])
        # Keep names positionally aligned with speaker_codes.  The previous
        # implementation sorted an independent set of names, which silently
        # paired the wrong identity with a code whenever a deduplicated source
        # appeared under multiple speakers.  Unknown names stay as empty slots.
        names = [
            str(speakers.get(code, {}).get("name_jp", ""))
            if isinstance(speakers.get(code, {}), dict)
            else ""
            for code in codes
        ]
        result[sid] = {
            "catalogue_occurrences": state["catalogue_occurrences"],
            "categories": categories,
            "speaker_codes": codes,
            "speaker_names": names,
            "speaker_identities": [
                {"speaker_code": code, "name_jp": names[index]}
                for index, code in enumerate(codes)
            ],
            "speaker_count": len(codes),
            "category_count": len(categories),
            "multi_speaker": len(codes) > 1,
            "multi_category": len(categories) > 1,
            "requires_cross_context_consistency": len(codes) > 1 or len(categories) > 1,
        }
    return result


def enrich(
    queue: list[dict],
    context_index: dict[tuple[str, str], list[dict]],
    max_examples: int,
    speakers: dict[str, dict] | None = None,
    usage_profiles: dict[str, dict] | None = None,
) -> tuple[list[dict], dict]:
    speakers = speakers or {}
    usage_profiles = usage_profiles or {}
    output: list[dict] = []
    matched_examples = 0
    rows_with_context = 0
    for row in queue:
        enriched = dict(row)
        context_examples: list[dict] = []
        for example in row.get("examples", [])[:max_examples]:
            logical = str(example.get("logical", ""))
            key = str(example.get("key", ""))
            context = context_index.get((logical, key))
            if not context:
                continue
            context_examples.append({
                "logical": logical,
                "bundle": example.get("bundle", ""),
                "key": key,
                "scene_type": logical.split("_", 1)[0] if logical else "",
                "speaker": speaker_hint(key, speakers),
                "context": context,
            })
            matched_examples += 1
        enriched["context_examples"] = context_examples
        enriched["usage_profile"] = usage_profiles.get(
            str(row.get("source_sha256", "")),
            {
                "catalogue_occurrences": row.get("occurrences", 0),
                "categories": [],
                "speaker_codes": [],
                "speaker_names": [],
                "speaker_count": 0,
                "category_count": 0,
                "multi_speaker": False,
                "multi_category": False,
                "requires_cross_context_consistency": False,
            },
        )
        if context_examples:
            rows_with_context += 1
        output.append(enriched)
    return output, {
        "queue_rows": len(queue),
        "rows_with_context": rows_with_context,
        "matched_examples": matched_examples,
        "coverage": rows_with_context / len(queue) if queue else 0.0,
    }


def write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalogue", type=Path, required=True)
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--window", type=int, default=2)
    ap.add_argument("--max-examples", type=int, default=2)
    ap.add_argument("--speaker-registry", type=Path)
    args = ap.parse_args()
    if args.window < 0 or args.max_examples <= 0:
        raise SystemExit("--window must be >=0 and --max-examples must be >0")
    catalogue = read_jsonl(args.catalogue)
    queue = read_jsonl(args.queue)
    speakers = load_speakers(args.speaker_registry)
    context_index = build_context_index(catalogue, args.window, speakers)
    usage_profiles = build_usage_profiles(catalogue, speakers)
    rows, summary = enrich(
        queue,
        context_index,
        args.max_examples,
        speakers,
        usage_profiles,
    )
    summary.update({
        "schema_version": 1,
        "catalogue_rows": len(catalogue),
        "context_identities": len(context_index),
        "window": args.window,
        "max_examples": args.max_examples,
        "speaker_registry": str(args.speaker_registry) if args.speaker_registry else None,
        "speaker_codes_loaded": len(speakers),
        "context_examples_with_speaker": sum(
            1
            for row in rows
            for example in row.get("context_examples", [])
            if example.get("speaker")
        ),
        "usage_profiles": len(usage_profiles),
        "multi_speaker_queue_rows": sum(
            bool(row.get("usage_profile", {}).get("multi_speaker"))
            for row in rows
        ),
        "multi_category_queue_rows": sum(
            bool(row.get("usage_profile", {}).get("multi_category"))
            for row in rows
        ),
        "cross_context_consistency_queue_rows": sum(
            bool(row.get("usage_profile", {}).get("requires_cross_context_consistency"))
            for row in rows
        ),
    })
    write_jsonl_atomic(args.output, rows)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
