#!/usr/bin/env python3
"""Extract non-GTX FontRenderParams strings into the normal MLTD translation queue.

These bundles are runtime text-render scenes.  Text is serialized in
Imas.Live.FontRenderParams (fix1Text/fix2Text/nameText/msgs[]) even though the
rendered result is a texture.  The extractor keeps exact Unity object/field
provenance so an accepted translation can later be written back fail-closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import msgpack
import UnityPy

from pipelines.text.mltd_localize_gtx import SOURCE_TEXT_RE
from scripts.mltd_translation_quality import source_id
from scripts.build_character_voice_evidence import load_character_evidence

TEXT_FIELDS = ("fix1Text", "fix2Text", "nameText")
VARFIN_GROUP_SIZE = 4


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_asset_index(path: Path) -> dict[str, list]:
    value = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not isinstance(value[0], dict)
    ):
        raise ValueError("unexpected MLTD asset index shape")
    out: dict[str, list] = {}
    for logical, row in value[0].items():
        if not isinstance(logical, str) or not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        out[logical] = list(row)
    return out


def extract_tree_fields(
    logical: str,
    remote: str,
    path_id: int,
    tree: dict,
) -> list[dict]:
    """Extract source-bearing FontRenderParams fields from one typetree."""
    rows: list[dict] = []
    for field in TEXT_FIELDS:
        value = tree.get(field)
        if isinstance(value, str) and value:
            rows.append(
                {
                    "logical": logical,
                    "remote": remote,
                    "path_id": int(path_id),
                    "field": field,
                    "index": None,
                    "source": value,
                    "scene_type": "fontrender_fixed",
                }
            )
    msgs = tree.get("msgs")
    if isinstance(msgs, list):
        for index, value in enumerate(msgs):
            if not isinstance(value, str) or not value:
                continue
            row = {
                "logical": logical,
                "remote": remote,
                "path_id": int(path_id),
                "field": "msgs",
                "index": index,
                "source": value,
                "scene_type": "live_mc" if logical == "fontrender_varfin.unity3d" else "fontrender_message",
            }
            if logical == "fontrender_varfin.unity3d" and len(msgs) % VARFIN_GROUP_SIZE == 0:
                group = index // VARFIN_GROUP_SIZE
                row.update(
                    {
                        "sequence_group": group,
                        # 9.0.200 FontRenderMgr.SetUpTextRender @ 0x40A9E58
                        # reads DanceIdolInfo.mstId @ +0x58 and constructs
                        # msgs[(mstId - 1) * 4 + (n & 3)].  dump.cs independently
                        # defines DanceIdolInfo.mstId at +0x58.
                        "speaker_mapping_status": "verified_native_mstid_4way",
                        "speaker_id_verified": group + 1,
                    }
                )
            rows.append(row)
    return rows


def attach_verified_speakers(
    occurrences: list[dict],
    speakers: dict[str, dict],
) -> list[dict]:
    """Attach source-backed speaker identity to native-verified varfin groups."""
    by_id: dict[int, tuple[str, dict]] = {}
    for code, evidence in speakers.items():
        if not isinstance(evidence, dict):
            continue
        idol_id = evidence.get("idol_id")
        if not isinstance(idol_id, int):
            continue
        # varfin's native table is exactly the 52 playable idols.  Evidence
        # rows for Producer/special identities (for example 404) are outside
        # this table and may legitimately share an auxiliary id.
        if not 1 <= idol_id <= 52:
            continue
        if idol_id in by_id:
            raise ValueError(f"multiple speaker codes map to idol_id={idol_id}")
        by_id[idol_id] = (str(code), evidence)

    out: list[dict] = []
    for original in occurrences:
        row = dict(original)
        idol_id = row.get("speaker_id_verified")
        if isinstance(idol_id, int):
            mapped = by_id.get(idol_id)
            if mapped is None:
                raise ValueError(f"no speaker evidence for verified idol_id={idol_id}")
            code, evidence = mapped
            row["speaker"] = {
                "speaker_code": code,
                "idol_id": idol_id,
                "name_jp": str(evidence.get("name_jp", "")),
                "identity_source": (
                    "FontRenderMgr.SetUpTextRender@0x40A9E58:"
                    "DanceIdolInfo.mstId@+0x58"
                ),
                "profile_status": evidence.get("profile_status", "evidence_only"),
            }
        out.append(row)
    return out


def extract_bundle(logical: str, remote: str, path: Path) -> list[dict]:
    env = UnityPy.load(str(path))
    rows: list[dict] = []
    font_params_objects = 0
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            tree = obj.read_typetree()
        except Exception:
            continue
        if not isinstance(tree, dict):
            continue
        if not any(field in tree for field in (*TEXT_FIELDS, "msgs")):
            continue
        # FontRenderParams evolved across these historical bundles: older
        # variants expose only fix1/fix2/name text + color fields, while varfin
        # also exposes msgs/typing/texture dimensions.  The custom field names
        # above are already narrower than UnityEngine.UI.Text (which uses m_Text),
        # so do not require the newer texNum/texWidth/texHeight fields.
        font_params_objects += 1
        rows.extend(extract_tree_fields(logical, remote, int(obj.path_id), tree))
    if font_params_objects != 1:
        raise ValueError(
            f"{logical}: expected exactly one FontRenderParams object, found {font_params_objects}"
        )
    return rows


def build_context(occurrence: dict, by_location: dict[tuple, list[dict]]) -> list[dict]:
    if occurrence["field"] == "msgs" and occurrence.get("sequence_group") is not None:
        key = (occurrence["logical"], occurrence["path_id"], occurrence["sequence_group"])
        group = sorted(
            by_location.get(key, []),
            key=lambda row: int(row.get("index", -1)),
        )
        return [
            {
                "relative": int(row["index"]) - int(occurrence["index"]),
                "field": "msgs",
                "index": row["index"],
                "source": row["source"],
                "source_sha256": source_id(row["source"]),
                "speaker": row.get("speaker"),
                "speaker_mapping_status": row.get("speaker_mapping_status", ""),
                "speaker_id_verified": row.get("speaker_id_verified"),
            }
            for row in group
        ]

    key = (occurrence["logical"], occurrence["path_id"], "fixed")
    group = by_location.get(key, [])
    return [
        {
            "relative": 0 if row["field"] == occurrence["field"] else None,
            "field": row["field"],
            "index": row.get("index"),
            "source": row["source"],
            "source_sha256": source_id(row["source"]),
            "speaker": row.get("speaker"),
        }
        for row in group
    ]


def build_queue(occurrences: list[dict]) -> tuple[list[dict], dict]:
    grouped: dict[str, dict] = {}
    by_location: dict[tuple, list[dict]] = defaultdict(list)

    for row in occurrences:
        if row["field"] == "msgs" and row.get("sequence_group") is not None:
            key = (row["logical"], row["path_id"], row["sequence_group"])
        else:
            key = (row["logical"], row["path_id"], "fixed")
        by_location[key].append(row)

    for row in occurrences:
        source = str(row["source"])
        if not SOURCE_TEXT_RE.search(source):
            continue
        sid = source_id(source)
        example = {
            "logical": row["logical"],
            "remote": row["remote"],
            "path_id": row["path_id"],
            "field": row["field"],
            "index": row.get("index"),
        }
        if row.get("sequence_group") is not None:
            example.update(
                {
                    "sequence_group": row["sequence_group"],
                    "speaker_mapping_status": row.get("speaker_mapping_status"),
                    "speaker_id_verified": row.get("speaker_id_verified"),
                }
            )
        item = grouped.get(sid)
        if item is None:
            categories = [str(row["scene_type"])]
            item = {
                "source_sha256": sid,
                "source": source,
                "translation": "",
                "status": "pending",
                "occurrences": 0,
                "examples": [],
                "queue_reason": "non_gtx_fontrender",
                "context_examples": [],
                "usage_profile": {
                    "catalogue_occurrences": 0,
                    "categories": categories,
                    "speaker_codes": [],
                    "speaker_names": [],
                    "speaker_count": 0,
                    "category_count": 1,
                    "multi_speaker": False,
                    "multi_category": False,
                    "requires_cross_context_consistency": False,
                    "source_kind": "non_gtx_fontrender",
                },
            }
            grouped[sid] = item
        if item["source"] != source:
            raise ValueError(f"source hash collision: {sid}")
        item["occurrences"] += 1
        item["usage_profile"]["catalogue_occurrences"] += 1
        category = str(row["scene_type"])
        if category not in item["usage_profile"]["categories"]:
            item["usage_profile"]["categories"].append(category)
            item["usage_profile"]["categories"].sort()
        speaker = row.get("speaker")
        if isinstance(speaker, dict):
            code = str(speaker.get("speaker_code", ""))
            name = str(speaker.get("name_jp", ""))
            if code and code not in item["usage_profile"]["speaker_codes"]:
                item["usage_profile"]["speaker_codes"].append(code)
                item["usage_profile"]["speaker_codes"].sort()
            if name and name not in item["usage_profile"]["speaker_names"]:
                item["usage_profile"]["speaker_names"].append(name)
                item["usage_profile"]["speaker_names"].sort()
        if example not in item["examples"]:
            item["examples"].append(example)
        ctx = {
            "logical": row["logical"],
            "remote": row["remote"],
            "path_id": row["path_id"],
            "field": row["field"],
            "index": row.get("index"),
            "scene_type": row["scene_type"],
            "speaker": row.get("speaker"),
            "context": build_context(row, by_location),
        }
        if row.get("sequence_group") is not None:
            ctx.update(
                {
                    "sequence_group": row["sequence_group"],
                    "speaker_mapping_status": row.get("speaker_mapping_status"),
                    "speaker_id_verified": row.get("speaker_id_verified"),
                }
            )
        item["context_examples"].append(ctx)

    for item in grouped.values():
        usage = item["usage_profile"]
        usage["speaker_count"] = len(usage["speaker_codes"])
        usage["category_count"] = len(usage["categories"])
        usage["multi_speaker"] = usage["speaker_count"] > 1
        usage["multi_category"] = usage["category_count"] > 1
        usage["requires_cross_context_consistency"] = bool(
            usage["multi_speaker"] or usage["multi_category"]
        )

    queue = sorted(
        grouped.values(),
        key=lambda row: (-int(row["occurrences"]), str(row["source"])),
    )
    counts = Counter()
    for row in queue:
        counts["unique_source_values"] += 1
        counts["source_occurrences"] += int(row["occurrences"])
        for category in row["usage_profile"]["categories"]:
            counts[f"category:{category}"] += 1
    return queue, dict(counts)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset-index", type=Path, required=True)
    ap.add_argument("--bundle-root", type=Path, required=True)
    ap.add_argument(
        "--speaker-evidence",
        type=Path,
        help="character-voice-evidence.json; required to attach verified varfin speakers",
    )
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    args = ap.parse_args()

    index = load_asset_index(args.asset_index)
    selected = sorted(
        logical for logical in index if logical.startswith("fontrender_") and logical.endswith(".unity3d")
    )
    if not selected:
        raise SystemExit("no fontrender_*.unity3d entries found")

    occurrences: list[dict] = []
    bundles: list[dict] = []
    missing: list[str] = []
    for logical in selected:
        catalog_hash, remote, declared_size = index[logical][:3]
        path = args.bundle_root / str(remote)
        if not path.is_file():
            missing.append(str(remote))
            continue
        if path.stat().st_size != int(declared_size):
            raise ValueError(
                f"{logical}: size mismatch current={path.stat().st_size} expected={declared_size}"
            )
        rows = extract_bundle(logical, str(remote), path)
        occurrences.extend(rows)
        bundles.append(
            {
                "logical": logical,
                "catalog_hash": str(catalog_hash),
                "remote": str(remote),
                "declared_size": int(declared_size),
                "sha256": sha256_file(path),
                "text_fields": len(rows),
                "source_fields": sum(bool(SOURCE_TEXT_RE.search(str(row["source"]))) for row in rows),
            }
        )

    if missing:
        raise SystemExit(
            f"missing {len(missing)}/{len(selected)} fontrender bundles under {args.bundle_root}; "
            f"first={missing[0]}"
        )

    speakers = load_character_evidence(args.speaker_evidence)
    if args.speaker_evidence:
        occurrences = attach_verified_speakers(occurrences, speakers)
        verified_ids = {
            int(row["speaker_id_verified"])
            for row in occurrences
            if isinstance(row.get("speaker_id_verified"), int)
        }
        if verified_ids != set(range(1, 53)):
            raise ValueError(
                "varfin native speaker mapping did not cover exactly idol_id 1..52"
            )
    queue, counts = build_queue(occurrences)
    write_jsonl(args.output, queue)
    summary = {
        "schema_version": 1,
        "kind": "mltd-non-gtx-fontrender-localization",
        "asset_index": str(args.asset_index),
        "asset_index_sha256": sha256_file(args.asset_index),
        "bundle_root": str(args.bundle_root),
        "selected_bundles": len(selected),
        "raw_nonempty_text_fields": len(occurrences),
        "unique_nonempty_text_values": len({str(row["source"]) for row in occurrences}),
        "source_candidate_fields": sum(bool(SOURCE_TEXT_RE.search(str(row["source"]))) for row in occurrences),
        **counts,
        "varfin_sequence_policy": {
            "group_size": VARFIN_GROUP_SIZE,
            "speaker_mapping_status": "verified_native_mstid_4way",
            "native_formula": "msgs[(DanceIdolInfo.mstId-1)*4+(n&3)]",
            "set_up_text_render_rva": "0x40A9E58",
            "dance_idol_info_mst_id_offset": "0x58",
            "speaker_codes_injected": bool(args.speaker_evidence),
        },
        "bundles": bundles,
        "output": str(args.output),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "bundles"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
