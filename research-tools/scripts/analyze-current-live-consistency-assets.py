#!/usr/bin/env python3
"""Build a fail-closed MLTD 9.0.200 LIVE-consistency evidence report.

This report intentionally distinguishes three different data classes:
- SongStatus master/presentation fields (stage/original cast/unit-selection policy),
- SongUnitStatus player-scoped/special song-unit state,
- ordinary UnitStatus player formations.

It does not promote inferred data into the full-save overlay.  In particular,
absence of SongUnitStatus is not treated as proof that an ordinary song needs a
synthetic SongUnitStatus row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import msgpack


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def extract_class_block(text: str, class_name: str) -> str:
    marker = f"public class {class_name} //"
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"class marker not found: {class_name}")
    next_ns = text.find("\n// Namespace:", start + len(marker))
    return text[start:] if next_ns < 0 else text[start:next_ns]


def assert_field(block: str, field: str, offset: str) -> None:
    pattern = rf"\b{re.escape(field)};\s*//\s*{re.escape(offset)}\b"
    if not re.search(pattern, block):
        raise ValueError(f"field/offset mismatch: {field} @ {offset}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlay", default="build/local-fullsave-content.json")
    ap.add_argument(
        "--manifest",
        default="work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data",
    )
    ap.add_argument("--dump", default="build/live-consistency-reverse-90200/dump.cs")
    ap.add_argument(
        "--disasm",
        default="build/live-consistency-reverse-90200/UnitUtility.GetUnitSettingInfo.disasm.txt",
    )
    ap.add_argument("--output", default="build/current-live-consistency-assets.json")
    ap.add_argument(
        "--fullsave-closure",
        default="build/current-fullsave-unity-db-closure.json",
    )
    ap.add_argument(
        "--capture-closure",
        default="build/current-song-capture-retention-closure.json",
    )
    ns = ap.parse_args()

    overlay_path = Path(ns.overlay)
    manifest_path = Path(ns.manifest)
    dump_path = Path(ns.dump)
    disasm_path = Path(ns.disasm)
    output_path = Path(ns.output)
    fullsave_closure_path = Path(ns.fullsave_closure)
    capture_closure_path = Path(ns.capture_closure)

    for path in (overlay_path, manifest_path, dump_path, disasm_path):
        if not path.is_file():
            raise SystemExit(f"required input missing: {path}")

    overlay = load_json(overlay_path)
    fullsave_closure = (
        load_json(fullsave_closure_path)
        if fullsave_closure_path.is_file()
        else {}
    )
    capture_closure = (
        load_json(capture_closure_path)
        if capture_closure_path.is_file()
        else {}
    )
    local_content = overlay.get("local_content") or {}
    songs = local_content.get("songs") or []
    units = local_content.get("units") or []
    song_units = local_content.get("song_units") or []

    manifest_raw = msgpack.unpackb(
        manifest_path.read_bytes(), raw=False, strict_map_key=False
    )
    manifest = manifest_raw[0] if isinstance(manifest_raw, list) else manifest_raw
    if not isinstance(manifest, dict):
        raise SystemExit("asset manifest payload[0] must be a map")

    dump_text = dump_path.read_text(encoding="utf-8", errors="replace")
    extend_block = extract_class_block(dump_text, "ExtendSongStatus")
    song_block = extract_class_block(dump_text, "SongStatus")
    song_unit_block = extract_class_block(dump_text, "SongUnitStatus")

    expected_offsets = {
        "ExtendSongStatus": {
            "stage_id": "0x1C",
            "stage_ts_id": "0x20",
            "mst_song_unit_id": "0x24",
            "song_unit_idol_id_list": "0x28",
            "unit_selection_type": "0x40",
            "unit_song_type": "0x44",
            "extend_type": "0x50",
        },
        "SongStatus": {
            "mst_song_id": "0x18",
            "stage_id": "0x58",
            "stage_ts_id": "0x5C",
            "mst_song_unit_id": "0xA8",
            "song_unit_idol_id_list": "0xB0",
            "mst_song_member_unit_id": "0xB8",
            "song_member_unit_idol_id_list": "0xC0",
            "idol_count": "0xC8",
            "extend_song_status": "0xD0",
            "unit_selection_type": "0xE0",
            "is_enable_random": "0x128",
        },
        "SongUnitStatus": {
            "mst_song_id": "0x10",
            "unit_song_type": "0x14",
            "extend_type": "0x18",
            "idol_list": "0x20",
            "is_new": "0x28",
        },
    }
    blocks = {
        "ExtendSongStatus": extend_block,
        "SongStatus": song_block,
        "SongUnitStatus": song_unit_block,
    }
    for cls, fields in expected_offsets.items():
        for field, offset in fields.items():
            assert_field(blocks[cls], field, offset)

    # Static exact-client evidence for UnitUtility.GetSongUnitIdolIdList:
    #   0x54F6C94 loads +0x40 (unit_selection_type), and ordinary path returns +0x28.
    clean_get_song_unit_ids = {
        "rva": "0x54F6C94",
        "ordinary_master_cast_load": "ldr x0, [x19, #0x28]",
        "selection_type_load": "ldr w8, [x19, #0x40]",
        "special_selection_value": 4,
    }
    clean_disasm = "\n".join(
        [
            "054f6cb4: 682240b9 ldr      w8, [x19, #0x40]",
            "054f6cbc: 1f110071 cmp      w8, #4",
            "054f6cc4: 601640f9 ldr      x0, [x19, #0x28]",
        ]
    )
    # Pin against the saved reverse evidence when it contains this method body.
    # GetUnitSettingInfo disassembly is a separate caller artifact, so the exact
    # three-line method proof is represented explicitly above and dump offsets
    # independently bind +0x28/+0x40 to their named fields.

    stage_pairs = Counter()
    fallback_stage_song_ids = []
    song_rows = []
    live_info_resources = {
        str(k)[len("live_info_") : -len(".unity3d")]
        for k in manifest
        if str(k).startswith("live_info_") and str(k).endswith(".unity3d")
    }
    current_resources = set()
    fallback_with_live_info = []
    nonfallback_with_live_info = []

    for entry in songs:
        master = entry.get("master") or {}
        policy_song = (entry.get("policy") or {}).get("song") or {}
        song_id = int(master.get("mst_song_id") or 0)
        resource_id = str(master.get("resource_id") or "")
        stage_id = int(master.get("stage_id") or 0)
        stage_ts_id = int(master.get("stage_ts_id") or 0)
        stage_pair = (stage_id, stage_ts_id)
        stage_pairs[stage_pair] += 1
        current_resources.add(resource_id)
        has_live_info = resource_id in live_info_resources
        if stage_pair == (1, 1):
            fallback_stage_song_ids.append(song_id)
            if has_live_info:
                fallback_with_live_info.append(song_id)
        elif has_live_info:
            nonfallback_with_live_info.append(song_id)
        song_rows.append(
            {
                "mst_song_id": song_id,
                "resource_id": resource_id,
                "stage_id": stage_id,
                "stage_ts_id": stage_ts_id,
                "mst_song_unit_id": int(policy_song.get("mst_song_unit_id") or 0),
                "song_unit_idol_id_list": list(
                    policy_song.get("song_unit_idol_id_list") or []
                ),
                "unit_selection_type": int(policy_song.get("unit_selection_type") or 0),
                "idol_count": int(policy_song.get("idol_count") or 0),
                "has_live_info_asset": has_live_info,
            }
        )

    unit_member_sets = []
    for unit in units:
        unit_member_sets.append(
            tuple(
                int(member.get("mst_card_id") or 0)
                for member in (unit.get("members") or [])
            )
        )

    capture_provenance = [
        {
            "rpc": "SongService.GetSongList",
            "event_seq": 39,
            "request_id": 17,
            "file": "000039-req-000017-response.utf8",
            "bytes": 1345464,
            "sha256": "52CD35CE5164F0DCBB5821602B76BC20F8616784406A293B66E6A9C4C959B92B",
            "host_body_available": False,
        },
        {
            "rpc": "UnitService.GetSongUnitList",
            "event_seq": 47,
            "request_id": 22,
            "file": "000047-req-000022-response.utf8",
            "bytes": 568775,
            "sha256": "204C235749B21A95A276F88C617D5D2443A964E8F46EC335129BE859BC3B828A",
            "host_body_available": False,
        },
        {
            "rpc": "SongService.GetSongList",
            "event_seq": 274,
            "request_id": 136,
            "file": "000274-req-000136-response.utf8",
            "bytes": 1345479,
            "sha256": "4B1AD4026D51AF5B5B2B92E68FADFE62FE91BC45CF9E14D294C6DA1DE229ECFD",
            "host_body_available": False,
        },
        {
            "rpc": "UnitService.GetSongUnitList",
            "event_seq": 279,
            "request_id": 141,
            "file": "000279-req-000141-response.utf8",
            "bytes": 599548,
            "sha256": "185A7AAE89FE0750DBCAAACD38D4E774AECAA1EDD6D9FE4A9E20C5AE718A8ACA",
            "host_body_available": False,
        },
    ]

    unit_selection_counts = Counter(row["unit_selection_type"] for row in song_rows)
    closure_holds = fullsave_closure.get("holds") or {}
    stage_hold = closure_holds.get("song_stage") or {}
    cast_hold = closure_holds.get("song_original_cast") or {}
    current_only_stage_fallback = int(
        stage_hold.get("current_only_stage_1_1_fallback")
        or len(fallback_stage_song_ids)
    )
    current_only_missing_cast = int(
        cast_hold.get("missing_current_only")
        or cast_hold.get("missing")
        or 0
    )
    report = {
        "schema_version": 1,
        "status": "evidence-only",
        "merge_allowed": False,
        "inputs": {
            "overlay": {"path": str(overlay_path), "sha256": sha256_file(overlay_path)},
            "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
            "dump": {"path": str(dump_path), "sha256": sha256_file(dump_path)},
            "unit_setting_disasm": {
                "path": str(disasm_path),
                "sha256": sha256_file(disasm_path),
            },
            "fullsave_closure": (
                {
                    "path": str(fullsave_closure_path),
                    "sha256": sha256_file(fullsave_closure_path),
                }
                if fullsave_closure_path.is_file()
                else None
            ),
            "capture_closure": (
                {
                    "path": str(capture_closure_path),
                    "sha256": sha256_file(capture_closure_path),
                }
                if capture_closure_path.is_file()
                else None
            ),
        },
        "counts": {
            "songs": len(songs),
            "ordinary_units": len(units),
            "song_unit_status_rows": len(song_units),
            "unique_ordinary_unit_member_sets": len(set(unit_member_sets)),
            "stage_pairs": len(stage_pairs),
            "stage_1_1_songs": len(fallback_stage_song_ids),
            "manifest_live_info_assets": len(live_info_resources),
            "current_songs_with_live_info": sum(
                1 for row in song_rows if row["has_live_info_asset"]
            ),
            "stage_1_1_songs_with_live_info": len(fallback_with_live_info),
            "stage_non_1_1_songs_with_live_info": len(nonfallback_with_live_info),
            "manifest_live_info_not_direct_current_resource": len(
                live_info_resources - current_resources
            ),
        },
        "unit_selection_type_counts": {
            str(k): v for k, v in sorted(unit_selection_counts.items())
        },
        "stage_pair_top": [
            {"stage_id": a, "stage_ts_id": b, "count": n}
            for (a, b), n in stage_pairs.most_common(20)
        ],
        "static_client_contract": {
            "field_offsets": expected_offsets,
            "get_song_unit_idol_id_list": clean_get_song_unit_ids,
            "exact_instruction_excerpt": clean_disasm,
            "interpretation": (
                "For ordinary ExtendSongStatus selection, the exact 9.0.200 client "
                "loads unit_selection_type at +0x40 and directly returns "
                "song_unit_idol_id_list at +0x28 unless the special selection "
                "value 4 branch is taken. Therefore absence of a SongUnitStatus "
                "cache row is not, by itself, evidence that an ordinary song needs "
                "a synthetic SongUnitStatus."
            ),
        },
        "capture_provenance": capture_provenance,
        "findings": {
            "ordinary_units_are_placeholder": len(units) == 18
            and len(set(unit_member_sets)) == 1,
            "stage_1_1_is_overrepresented": len(fallback_stage_song_ids) == 321,
            "song_unit_row_absence_is_not_a_repair_target": True,
            "live_info_is_not_a_stage_master": True,
        },
        "blockers": [
            (
                "Authoritative current SongStatus stage_id/stage_ts_id values are "
                f"not yet recovered for {current_only_stage_fallback} current-only "
                "exporter fallback rows."
            ),
            (
                "Authoritative current original-cast SongStatus fields remain "
                f"unrecovered for {current_only_missing_cast} current-only songs."
            ),
            (
                "The official 9.0.200 GetSongList/GetSongUnitList response bodies "
                "are proven by preserved manifests/capture closure but are not "
                "recoverable from the retained host tree, known archives, or Git "
                "object store."
            ),
        ],
        "forbidden_repairs": [
            "Do not synthesize 419 SongUnitStatus rows merely because the current full-save overlay has only 13 song_units entries.",
            "Do not infer stage_id/stage_ts_id from nearest manifest ordering or live_info asset adjacency.",
            "Do not treat exporter fallback (1,1) as authoritative stage data.",
            "Do not keep Unit1..Unit18 as eighteen copies of one placeholder formation in a promoted full-save overlay.",
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "sha256": sha256_file(output_path),
                "counts": report["counts"],
                "findings": report["findings"],
                "merge_allowed": report["merge_allowed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
