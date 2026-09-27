#!/usr/bin/env python3
"""Make reviewable, Unity-sized copies of GPT Image outputs without changing the raw edits.

A normalised image is still unreviewed. For RGBA originals, preserve the
source alpha geometry. For opaque originals, remove any newly introduced alpha.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work/image-localization-25"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def normalize(row: dict, force: bool) -> dict | None:
    src = WORK / row["original"]
    raw = WORK / row["edited"]
    if not raw.is_file():
        return None
    if not src.is_file() or sha(src) != row["original_sha256"]:
        raise ValueError("Original file missing or modified: " + row["id"])
    final = WORK / "normalized" / Path(row["edited"]).relative_to("edited")
    if final.is_file() and not force and final.stat().st_mtime_ns >= raw.stat().st_mtime_ns:
        with Image.open(final) as im:
            if list(im.size) != row["original_size"]:
                raise ValueError("Existing normalised size mismatch: " + row["id"])
        return {"id": row["id"], "normalized": final.relative_to(WORK).as_posix(),
                "sha256": sha(final), "reused": True}
    with Image.open(src) as original, Image.open(raw) as edited:
        edited.load()
        native_size = original.size
        img = edited.convert("RGBA").resize(native_size, Image.Resampling.LANCZOS)
        alpha_preserved = False
        if "A" in original.getbands() or "transparency" in original.info:
            orig_alpha = original.convert("RGBA").getchannel("A")
            img.putalpha(orig_alpha)
            alpha_preserved = True
            save = img
        else:
            save = img.convert("RGB")
        final.parent.mkdir(parents=True, exist_ok=True)
        temp = final.with_suffix(".png.writing")
        save.save(temp, format="PNG")
        with Image.open(temp) as checked:
            checked.verify()
        temp.replace(final)
    return {"id": row["id"], "normalized": final.relative_to(WORK).as_posix(),
            "sha256": sha(final), "original_size": row["original_size"],
            "raw_size": list(edited.size), "size_resampled": list(edited.size) != row["original_size"],
            "alpha_preserved_from_original": alpha_preserved,
            "review_status": "unreviewed"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    rows = [json.loads(line) for line in (WORK / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    done = []
    for row in rows:
        result = normalize(row, args.force)
        if result is not None:
            done.append(result)
    summary = {"inventory": len(rows), "normalized_available": len(done), "review_status": "unreviewed",
               "normalization_note": "Visual consistency and text accuracy require human review; no Unity files modified."}
    p = WORK / "normalized" / "summary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
