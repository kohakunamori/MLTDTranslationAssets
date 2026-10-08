#!/usr/bin/env python3
"""Build the confirmed non-GTX MLTD translation companion queue.

The main 9.0.200 translation queue is the GTX source universe.  Several
source-backed text surfaces live outside GTX (FontRender, the APK-embedded
bootstrap BI table, event-unit JSON TextAssets, and visible MLD config values).
This builder removes
anything already covered by GTX, merges duplicate non-GTX source IDs, preserves
surface provenance, and normalizes context/speaker metadata so the existing API
translator can consume the result with a separate output file.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            yield row


def source_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        str(row.get("source_sha256", "")).strip()
        for row in read_jsonl(path)
        if str(row.get("source_sha256", "")).strip()
    }


def load_speakers(path: Path) -> dict[int, dict[str, Any]]:
    if not path.is_file():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    speakers = doc.get("speakers", {}) if isinstance(doc, dict) else {}
    result: dict[int, dict[str, Any]] = {}
    if not isinstance(speakers, dict):
        return result
    for code, raw in speakers.items():
        if not isinstance(raw, dict):
            continue
        try:
            idol_id = int(raw.get("idol_id"))
        except (TypeError, ValueError):
            continue
        name = str(raw.get("name_jp", "")).strip()
        result[idol_id] = {
            "speaker_code": str(raw.get("speaker_code") or code),
            "idol_id": idol_id,
            "name_jp": name,
            "identity_source": str(raw.get("identity_source", "")),
            "profile_status": str(raw.get("profile_status", "evidence_only")),
        }
    return result


def event_context_examples(
    row: dict[str, Any],
    speakers: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    source = str(row.get("source", ""))
    sid = str(row.get("source_sha256", ""))
    out: list[dict[str, Any]] = []
    examples = row.get("examples", [])
    if not isinstance(examples, list):
        return out
    for example in examples:
        if not isinstance(example, dict):
            continue
        try:
            idol_id = int(example.get("idol") or 0)
        except (TypeError, ValueError):
            idol_id = 0
        speaker = speakers.get(idol_id)
        window: list[dict[str, Any]] = []
        previous = str(example.get("previous", "")).strip()
        following = str(example.get("next", "")).strip()
        if previous:
            window.append({"relative": -1, "source": previous})
        center: dict[str, Any] = {"relative": 0, "source": source, "source_sha256": sid}
        if speaker:
            center["speaker"] = speaker
        window.append(center)
        if following:
            window.append({"relative": 1, "source": following})
        ctx = {
            "logical": example.get("logical"),
            "remote": example.get("remote"),
            "path_id": example.get("path_id"),
            "command_index": example.get("command_index"),
            "scene_type": "event_unit",
            "context": window,
        }
        if speaker:
            ctx["speaker"] = speaker
        out.append(ctx)
    return out


def unique_dicts(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def infer_task(row: dict[str, Any]) -> str:
    source = str(row.get("source", ""))
    usage = row.get("usage_profile", {})
    usage = usage if isinstance(usage, dict) else {}
    categories = {str(x).upper() for x in usage.get("categories", []) if str(x).strip()}
    kind = str(usage.get("source_kind", "")).lower()
    codes = [str(x) for x in usage.get("speaker_codes", []) if str(x).strip()]
    multi = bool(
        usage.get("multi_speaker")
        or usage.get("requires_cross_context_consistency")
        or len(codes) > 1
    )

    if "TITLE" in categories:
        return "TITLE"
    if "DIALOGUE" in categories or "EVENT_UNIT_DIALOGUE" in categories or "live_mc" in {
        x.lower() for x in categories
    }:
        if multi:
            return "SHARED_SIMPLE" if len(source) <= 24 else "SHARED"
        return "DIALOGUE"
    if kind == "event_unit_json":
        return "DIALOGUE"
    if kind == "apk_embedded_gtx" or "APK_BOOTSTRAP_UI" in categories:
        return "UI" if len(source) <= 40 else "DESCRIPTION"
    if kind == "non_gtx_fontrender":
        if "LIVE_MC" in categories:
            return "DIALOGUE"
        return "UI" if len(source) <= 40 else "DESCRIPTION"
    return "UI" if len(source) <= 40 else "DESCRIPTION"


def normalized_row(
    raw: dict[str, Any],
    surface: str,
    speakers: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    row = dict(raw)
    row["translation"] = ""
    row["status"] = "pending"
    row["queue_reason"] = "confirmed_non_gtx_companion"
    row["companion_surfaces"] = [surface]

    usage = dict(row.get("usage_profile", {})) if isinstance(row.get("usage_profile"), dict) else {}
    categories = [str(x) for x in usage.get("categories", []) if str(x).strip()]

    if surface == "event_unit":
        examples = row.get("examples", [])
        idol_ids: set[int] = set()
        if isinstance(examples, list):
            for example in examples:
                if not isinstance(example, dict):
                    continue
                try:
                    idol_id = int(example.get("idol") or 0)
                except (TypeError, ValueError):
                    idol_id = 0
                if idol_id > 0:
                    idol_ids.add(idol_id)
        speaker_rows = [speakers[i] for i in sorted(idol_ids) if i in speakers]
        usage["speaker_ids"] = sorted(idol_ids)
        usage["speaker_codes"] = [str(x["speaker_code"]) for x in speaker_rows]
        usage["speaker_names"] = [str(x["name_jp"]) for x in speaker_rows if x.get("name_jp")]
        usage["speaker_count"] = len(speaker_rows)
        usage["multi_speaker"] = len(speaker_rows) > 1
        usage["requires_cross_context_consistency"] = bool(
            usage.get("requires_cross_context_consistency")
            or len(speaker_rows) > 1
            or int(row.get("occurrences", 1) or 1) > 1
        )
        row["context_examples"] = event_context_examples(row, speakers)

    row["usage_profile"] = usage
    row["task_hint"] = infer_task(row)
    return row


def merge_rows(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if str(left.get("source", "")) != str(right.get("source", "")):
        raise ValueError("source_sha256 collision with different source text")
    out = dict(left)
    out["occurrences"] = int(left.get("occurrences", 1) or 1) + int(right.get("occurrences", 1) or 1)
    out["examples"] = unique_dicts(
        [
            *(left.get("examples", []) if isinstance(left.get("examples"), list) else []),
            *(right.get("examples", []) if isinstance(right.get("examples"), list) else []),
        ]
    )
    out["context_examples"] = unique_dicts(
        [
            *(left.get("context_examples", []) if isinstance(left.get("context_examples"), list) else []),
            *(right.get("context_examples", []) if isinstance(right.get("context_examples"), list) else []),
        ]
    )
    out["companion_surfaces"] = sorted(
        set(str(x) for x in left.get("companion_surfaces", []))
        | set(str(x) for x in right.get("companion_surfaces", []))
    )

    lu = left.get("usage_profile", {}) if isinstance(left.get("usage_profile"), dict) else {}
    ru = right.get("usage_profile", {}) if isinstance(right.get("usage_profile"), dict) else {}
    usage = dict(lu)
    for key in ("categories", "speaker_codes", "speaker_names", "speaker_ids"):
        values = list(lu.get(key, [])) + list(ru.get(key, []))
        usage[key] = sorted(set(values), key=lambda x: str(x))
    usage["speaker_count"] = len(usage.get("speaker_codes", []))
    usage["category_count"] = len(usage.get("categories", []))
    usage["multi_speaker"] = bool(usage["speaker_count"] > 1)
    usage["multi_category"] = bool(usage["category_count"] > 1)
    usage["requires_cross_context_consistency"] = bool(
        lu.get("requires_cross_context_consistency")
        or ru.get("requires_cross_context_consistency")
        or usage["multi_speaker"]
        or usage["multi_category"]
        or len(out["companion_surfaces"]) > 1
    )
    kinds = sorted(
        {
            str(x)
            for x in (lu.get("source_kind"), ru.get("source_kind"))
            if x
        }
    )
    usage["source_kind"] = kinds[0] if len(kinds) == 1 else "multi_non_gtx"
    out["usage_profile"] = usage
    out["task_hint"] = infer_task(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", type=Path, default=Path("build/localization-90200"))
    ap.add_argument("--main-memory", type=Path)
    ap.add_argument("--fontrender", type=Path)
    ap.add_argument("--apk-bi", type=Path)
    ap.add_argument("--event-unit", type=Path)
    ap.add_argument("--mld", type=Path)
    ap.add_argument("--speaker-evidence", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--summary", type=Path)
    args = ap.parse_args()

    w = args.workspace
    main_memory = args.main_memory or (w / "translation-memory.jsonl")
    fontrender = args.fontrender or (w / "fontrender-translation-queue.jsonl")
    apk_bi = args.apk_bi or (w / "apk-bi-jp-translation-queue.jsonl")
    event_unit = args.event_unit or (w / "event-unit-translation-queue-missing-main.jsonl")
    mld = args.mld or (w / "mld-translation-queue.jsonl")
    speaker_evidence = args.speaker_evidence or (w / "character-voice-evidence.json")
    output = args.output or (w / "machine-translation-nongtx-queue.jsonl")
    summary_path = args.summary or (w / "machine-translation-nongtx-queue-summary.json")

    required = [main_memory, fontrender, apk_bi, event_unit, mld, speaker_evidence]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise SystemExit("missing input(s): " + ", ".join(missing))

    main = source_ids(main_memory)
    speakers = load_speakers(speaker_evidence)
    sources = [
        ("fontrender", fontrender),
        ("apk_bi", apk_bi),
        ("event_unit", event_unit),
        ("mld_config", mld),
    ]
    merged: dict[str, dict[str, Any]] = {}
    raw_counts: Counter[str] = Counter()
    excluded_main: Counter[str] = Counter()
    duplicate_companion = 0

    for surface, path in sources:
        for raw in read_jsonl(path):
            raw_counts[surface] += 1
            sid = str(raw.get("source_sha256", "")).strip()
            source = str(raw.get("source", ""))
            if not sid or not source:
                raise ValueError(f"{path}: row missing source/source_sha256")
            if sid in main:
                excluded_main[surface] += 1
                continue
            row = normalized_row(raw, surface, speakers)
            if sid in merged:
                duplicate_companion += 1
                merged[sid] = merge_rows(merged[sid], row)
            else:
                merged[sid] = row

    rows = list(merged.values())
    task_counts = Counter(str(row.get("task_hint", "")) for row in rows)
    surface_counts = Counter()
    for row in rows:
        for surface in row.get("companion_surfaces", []):
            surface_counts[str(surface)] += 1

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, output)

    summary = {
        "schema_version": 1,
        "kind": "mltd-confirmed-nongtx-companion-queue",
        "main_unique": len(main),
        "input_rows": dict(raw_counts),
        "excluded_already_in_main": dict(excluded_main),
        "cross_surface_duplicate_rows": duplicate_companion,
        "companion_unique": len(rows),
        "surface_unique_membership": dict(surface_counts),
        "task_counts": dict(task_counts),
        "speaker_registry": {
            "path": str(speaker_evidence),
            "idol_ids": len(speakers),
        },
        "inputs": {
            "main_memory": str(main_memory),
            "fontrender": str(fontrender),
            "apk_bi": str(apk_bi),
            "event_unit": str(event_unit),
            "mld": str(mld),
        },
        "output": str(output),
        "recommended_translation_output": str(
            w / "machine-translations-nongtx-api.jsonl"
        ),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
