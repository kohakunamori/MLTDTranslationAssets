#!/usr/bin/env python3
"""Build a fail-closed overlay for localized MLTD FontRenderParams bundles.

Input translations must be output from mltd_translation_release_gate.py: every
row used here must carry release_gate=accepted.  Source strings are matched
exactly against the current serialized FontRenderParams fields, written with
UnityPy, then the resulting bundle is reloaded and every changed locator is
verified before promotion to the overlay path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import UnityPy

from scripts.extract_fontrender_localization import (
    TEXT_FIELDS,
    extract_bundle,
    load_asset_index,
    sha256_file,
)
from pipelines.text.mltd_localize_gtx import SOURCE_TEXT_RE, read_jsonl, validate_translation
from scripts.mltd_translation_quality import source_id


def load_release_translations(paths: list[Path]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in paths:
        for row in read_jsonl(path):
            if str(row.get("release_gate", "")).lower() != "accepted":
                continue
            source = str(row.get("source", ""))
            translation = str(row.get("translation", ""))
            sid = str(row.get("source_sha256", "")) or source_id(source)
            if not source or not translation or sid != source_id(source):
                raise ValueError(f"{path}: invalid accepted translation identity {sid!r}")
            validate_translation(source, translation)
            prior = out.get(sid)
            if prior is not None and (
                str(prior["source"]) != source
                or str(prior["translation"]) != translation
            ):
                raise ValueError(f"conflicting accepted translation for {sid}")
            out[sid] = row
    return out


def _font_params_objects(env) -> list:
    result = []
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            tree = obj.read_typetree()
        except Exception:
            continue
        if not isinstance(tree, dict):
            continue
        if any(field in tree for field in (*TEXT_FIELDS, "msgs")):
            result.append((obj, tree))
    return result


def modify_bundle(
    source_path: Path,
    output_path: Path,
    translations: dict[str, dict],
) -> tuple[list[dict], dict]:
    env = UnityPy.load(str(source_path))
    params = _font_params_objects(env)
    if len(params) != 1:
        raise ValueError(
            f"{source_path}: expected exactly one FontRenderParams object, found {len(params)}"
        )

    obj, tree = params[0]
    before_object_count = len(env.objects)
    changes: list[dict] = []

    def replace_scalar(field: str) -> None:
        source = tree.get(field)
        if not isinstance(source, str) or not source or not SOURCE_TEXT_RE.search(source):
            return
        sid = source_id(source)
        candidate = translations.get(sid)
        if candidate is None:
            return
        if str(candidate.get("source", "")) != source:
            raise ValueError(f"{source_path}: stale source for {sid}")
        translation = str(candidate["translation"])
        validate_translation(source, translation)
        if translation == source:
            return
        tree[field] = translation
        changes.append(
            {
                "path_id": int(obj.path_id),
                "field": field,
                "index": None,
                "source_sha256": sid,
                "source": source,
                "translation": translation,
            }
        )

    for field in TEXT_FIELDS:
        replace_scalar(field)

    msgs = tree.get("msgs")
    if isinstance(msgs, list):
        for index, source in enumerate(list(msgs)):
            if not isinstance(source, str) or not source or not SOURCE_TEXT_RE.search(source):
                continue
            sid = source_id(source)
            candidate = translations.get(sid)
            if candidate is None:
                continue
            if str(candidate.get("source", "")) != source:
                raise ValueError(f"{source_path}: stale msgs[{index}] source for {sid}")
            translation = str(candidate["translation"])
            validate_translation(source, translation)
            if translation == source:
                continue
            msgs[index] = translation
            changes.append(
                {
                    "path_id": int(obj.path_id),
                    "field": "msgs",
                    "index": index,
                    "source_sha256": sid,
                    "source": source,
                    "translation": translation,
                }
            )
        tree["msgs"] = msgs

    if not changes:
        return [], {
            "source_sha256": sha256_file(source_path),
            "output_sha256": None,
            "roundtrip_verified": True,
            "object_count": before_object_count,
        }

    obj.save_typetree(tree)
    bundle_files = list(env.files.values())
    if len(bundle_files) != 1 or not hasattr(bundle_files[0], "save"):
        raise ValueError(f"{source_path}: unsupported UnityPy container layout")
    data = bundle_files[0].save()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_bytes(data)

    # Round-trip verification is performed on the temporary path before atomic
    # promotion, so a bad serializer result never replaces a previous overlay.
    check = UnityPy.load(str(tmp))
    if len(check.objects) != before_object_count:
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"{source_path}: object count changed {before_object_count} -> {len(check.objects)}"
        )
    verified = 0
    expected = {
        (int(row["path_id"]), row["field"], row["index"]): row["translation"]
        for row in changes
    }
    for check_obj, check_tree in _font_params_objects(check):
        for (path_id, field, index), expected_text in expected.items():
            if int(check_obj.path_id) != path_id:
                continue
            if index is None:
                actual = check_tree.get(field)
            else:
                values = check_tree.get(field)
                actual = (
                    values[index]
                    if isinstance(values, list) and 0 <= int(index) < len(values)
                    else None
                )
            if actual != expected_text:
                tmp.unlink(missing_ok=True)
                raise ValueError(
                    f"{source_path}: round-trip mismatch path={path_id} "
                    f"field={field}[{index}] expected={expected_text!r} actual={actual!r}"
                )
            verified += 1
    if verified != len(expected):
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"{source_path}: verified {verified}/{len(expected)} changed fields"
        )

    tmp.replace(output_path)
    return changes, {
        "source_sha256": sha256_file(source_path),
        "output_sha256": sha256_file(output_path),
        "roundtrip_verified": True,
        "object_count": before_object_count,
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset-index", type=Path, required=True)
    ap.add_argument("--bundle-root", type=Path, required=True)
    ap.add_argument("--translations", type=Path, action="append", required=True)
    ap.add_argument("--output-root", type=Path, required=True)
    ap.add_argument("--scope", default="jp-android")
    ap.add_argument("--summary", type=Path)
    ap.add_argument(
        "--require-complete",
        action="store_true",
        help="exit 3 unless every current FontRender source candidate is translated",
    )
    args = ap.parse_args()

    for path in args.translations:
        if not path.is_file():
            raise SystemExit(f"translation file missing: {path}")
    translations = load_release_translations(args.translations)
    index = load_asset_index(args.asset_index)
    selected = sorted(
        logical for logical in index
        if logical.startswith("fontrender_") and logical.endswith(".unity3d")
    )
    if not selected:
        raise SystemExit("no fontrender_*.unity3d entries found")

    counts = Counter()
    manifest_rows: list[dict] = []
    current_source_ids: set[str] = set()

    for logical in selected:
        _catalog_hash, remote, declared_size = index[logical][:3]
        source_path = args.bundle_root / str(remote)
        if not source_path.is_file():
            raise SystemExit(f"missing current bundle: {source_path}")
        if source_path.stat().st_size != int(declared_size):
            raise ValueError(
                f"{logical}: source size mismatch {source_path.stat().st_size} != {declared_size}"
            )
        current_rows = extract_bundle(logical, str(remote), source_path)
        bundle_source_ids = {
            source_id(str(row["source"]))
            for row in current_rows
            if SOURCE_TEXT_RE.search(str(row["source"]))
        }
        current_source_ids.update(bundle_source_ids)
        counts["bundles_scanned"] += 1
        counts["source_candidate_fields"] += sum(
            bool(SOURCE_TEXT_RE.search(str(row["source"]))) for row in current_rows
        )

        output_path = args.output_root / args.scope / str(remote)
        changes, verification = modify_bundle(source_path, output_path, translations)
        if changes:
            counts["bundles_written"] += 1
            counts["fields_changed"] += len(changes)
            manifest_rows.append(
                {
                    "logical": logical,
                    "remote": str(remote),
                    "declared_size": int(declared_size),
                    **verification,
                    "changes": changes,
                }
            )

    translated_current = current_source_ids & set(translations)
    unresolved = current_source_ids - set(translations)
    result = {
        "schema_version": 1,
        "kind": "mltd-fontrender-localization-overlay",
        "asset_index": str(args.asset_index),
        "asset_index_sha256": sha256_file(args.asset_index),
        "bundle_root": str(args.bundle_root),
        "output_root": str(args.output_root),
        "scope": args.scope,
        **dict(counts),
        "unique_source_values": len(current_source_ids),
        "accepted_translation_rows_loaded": len(translations),
        "translated_current_unique": len(translated_current),
        "unresolved_current_unique": len(unresolved),
        "coverage": (
            len(translated_current) / len(current_source_ids)
            if current_source_ids
            else 1.0
        ),
        "require_complete": bool(args.require_complete),
        "roundtrip_verified_bundles": len(manifest_rows),
        "bundles": manifest_rows,
    }
    summary_path = args.summary or args.output_root / "fontrender-localization-manifest.json"
    write_json(summary_path, result)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "bundles"},
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_complete and unresolved:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
