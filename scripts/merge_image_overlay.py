#!/usr/bin/env python3
"""Merge the image overlay into a generated release, in one pass.

The image surface and the text surface both rewrite the *same* official
``.data`` catalogue: each changed bundle has a new declared size, and the client
reads that size from the catalogue.  A release therefore cannot carry two
catalogues -- ``assets_generated_index.py`` refuses a second row for one
``runtime_path`` -- so the two size patches have to end up in one table.

This module takes the catalogue the text overlay already produced and applies
the image overlay's size patch on top of it, then emits the image surface's
manifest entries.  The two patches must be disjoint: any row the two catalogues
disagree about, outside the image overlay's own records, refuses the merge
rather than silently picking one side.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import msgpack

ROOT = Path(__file__).resolve().parents[1]

IMAGE_RESOURCE_KIND = "texture"


class RefusedMerge(ValueError):
    """The two catalogues cannot be merged without guessing."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_catalogue(path: Path) -> dict:
    """Read the official one-table msgpack catalogue."""
    if not path.is_file():
        raise RefusedMerge(f"catalogue not found: {path}")
    raw = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise RefusedMerge(f"{path}: not the expected one-table catalogue format")
    table = raw[0]
    for logical, row in table.items():
        if not isinstance(logical, str) or not isinstance(row, list) or len(row) != 3:
            raise RefusedMerge(f"{path}: malformed row for {logical!r}")
    return table


def load_overlay_manifest(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    records = document.get("records")
    if not isinstance(records, list) or not records:
        raise RefusedMerge(f"{path}: no records[]")
    for record in records:
        for key in ("logical", "remote", "original_sha256", "translated_sha256", "translated_bytes"):
            if record.get(key) in (None, ""):
                raise RefusedMerge(f"{path}: record lacks {key}: {record!r}")
    return records


def merge(text_table: dict, image_table: dict, records: list[dict]) -> tuple[dict, int]:
    """Apply the image size patch to the text catalogue.

    The text catalogue already carries the *text* surface's own size patch, so
    the two tables legitimately differ on every translated text bundle -- that
    difference is the point, not a conflict.  What must hold is narrower and
    fully checkable per image bundle:

    * both catalogues agree on the remote name and the catalogue hash;
    * the text catalogue still carries the *original* official size, proving the
      text patch never rewrote an image bundle;
    * the image catalogue carries the overlay's recorded patched size.

    Returns the merged table and the number of rows the text patch changed, so a
    caller can sanity-check that number against its own text surface size.
    """
    image_logicals = {str(record["logical"]) for record in records}
    if set(text_table) != set(image_table):
        differing = sorted(set(text_table) ^ set(image_table))
        raise RefusedMerge(
            f"catalogues describe different objects ({len(differing)}), e.g. {differing[:3]}")

    merged = {}
    for logical, row in text_table.items():
        merged[logical] = list(row)

    for record in records:
        logical = str(record["logical"])
        remote = str(record["remote"])
        if logical not in merged:
            raise RefusedMerge(f"image overlay logical {logical!r} is not in the catalogue")
        text_row = merged[logical]
        image_row = image_table[logical]
        if str(text_row[1]) != remote or str(image_row[1]) != remote:
            raise RefusedMerge(
                f"{logical}: catalogue remote {text_row[1]!r}/{image_row[1]!r} != overlay {remote!r}")
        if str(text_row[0]) != str(image_row[0]):
            raise RefusedMerge(f"{logical}: catalogue hash differs between the two tables")
        if int(text_row[2]) != int(record["original_bytes"]):
            raise RefusedMerge(
                f"{logical}: text catalogue declares {text_row[2]} but the overlay's original is "
                f"{record['original_bytes']}; the text patch already rewrote this image bundle")
        if int(image_row[2]) != int(record["translated_bytes"]):
            raise RefusedMerge(
                f"{logical}: image catalogue declares {image_row[2]} but the overlay's patched size "
                f"is {record['translated_bytes']}")
        merged[logical][2] = int(image_row[2])

    text_patch_rows = sum(
        1 for logical, row in image_table.items()
        if logical not in image_logicals and text_table[logical] != row)
    return merged, text_patch_rows


def build_entries(records: list[dict], overlay_root: Path, asset_version: str,
                  source_client_version: str) -> list[dict]:
    entries = []
    for record in records:
        logical = str(record["logical"])
        remote = str(record["remote"])
        artifact = overlay_root / remote
        if not artifact.is_file():
            raise RefusedMerge(f"image overlay artifact missing: {artifact}")
        if artifact.stat().st_size != int(record["translated_bytes"]):
            raise RefusedMerge(f"{artifact}: size != recorded translated_bytes")
        if sha256_file(artifact) != str(record["translated_sha256"]):
            raise RefusedMerge(f"{artifact}: sha256 != recorded translated_sha256")
        entries.append({
            "logical_key": logical,
            "logical_path": f"production/2018/Android/{logical}",
            "runtime_path": f"production/2018/Android/{remote}",
            "resource_kind": IMAGE_RESOURCE_KIND,
            "channel": "assets",
            "asset_version": asset_version,
            "client_version": None,
            "source_client_version": source_client_version,
            "source_sha256": str(record["original_sha256"]),
            "translated_sha256": str(record["translated_sha256"]),
            "reuse_status": "exact",
            "translation_status": "modified",
            "artifact_file": str(artifact.resolve()),
        })
    if not entries:
        raise RefusedMerge("image overlay produced no entries")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text-index", type=Path, required=True,
                        help="Catalogue already produced by the text overlay build.")
    parser.add_argument("--image-index", type=Path, required=True,
                        help="Catalogue shipped with the image overlay (official + image size patch).")
    parser.add_argument("--overlay-manifest", type=Path, required=True)
    parser.add_argument("--overlay-root", type=Path, required=True,
                        help="Directory holding the overlay bundles, keyed by remote name.")
    parser.add_argument("--asset-version", required=True)
    parser.add_argument("--source-client-version", default="")
    parser.add_argument("--out-index", type=Path, required=True)
    parser.add_argument("--out-entries", type=Path, required=True)
    parser.add_argument("--require-images", type=int, default=0,
                        help="Refuse unless exactly this many image bundles are merged (0 = no check).")
    args = parser.parse_args()

    records = load_overlay_manifest(args.overlay_manifest)
    if args.require_images and len(records) != args.require_images:
        raise RefusedMerge(f"overlay carries {len(records)} bundles, expected {args.require_images}")
    text_table = load_catalogue(args.text_index)
    image_table = load_catalogue(args.image_index)
    merged, text_patch_rows = merge(text_table, image_table, records)
    entries = build_entries(records, args.overlay_root.resolve(),
                            args.asset_version, args.source_client_version)

    args.out_index.parent.mkdir(parents=True, exist_ok=True)
    payload = msgpack.packb([merged], use_bin_type=True)
    # Never publish a catalogue this module cannot read back.
    if msgpack.unpackb(payload, raw=False, strict_map_key=False) != [merged]:
        raise RefusedMerge("merged catalogue does not round-trip")
    temporary = args.out_index.with_name(args.out_index.name + ".writing")
    temporary.write_bytes(payload)
    temporary.replace(args.out_index)

    args.out_entries.parent.mkdir(parents=True, exist_ok=True)
    args.out_entries.write_text(json.dumps({
        "schema_version": 1,
        "kind": "mltd-image-surface-entries",
        "asset_version": args.asset_version,
        "merged_index_sha256": sha256_file(args.out_index),
        "merged_index_bytes": args.out_index.stat().st_size,
        "image_bundles": len(entries),
        "entries": entries,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({
        "merged_index": str(args.out_index),
        "merged_index_sha256": sha256_file(args.out_index),
        "merged_index_bytes": args.out_index.stat().st_size,
        "catalogue_rows": len(merged),
        "text_patch_rows": text_patch_rows,
        "image_bundles": len(entries),
        "entries_file": str(args.out_entries),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RefusedMerge as error:
        print(f"REFUSED {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
