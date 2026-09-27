#!/usr/bin/env python3
"""Build review-only side-by-side PNGs; never alters source or generated assets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work/image-localization-25"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def font(size: int):
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def put(canvas: Image.Image, image: Image.Image, x: int, y: int, side: int) -> None:
    image = ImageOps.contain(image.convert("RGBA"), (side, side), Image.Resampling.LANCZOS)
    w, h = image.size
    canvas.alpha_composite(image, (x + (side - w) // 2, y + (side - h) // 2))


def compare(row: dict, side: int, force: bool) -> dict | None:
    origin = WORK / row["original"]
    edited = WORK / row["edited"]
    if not edited.is_file():
        return None
    if not origin.is_file() or digest(origin) != row["original_sha256"]:
        raise ValueError(f"Source is missing or modified: {row['id']}")
    target = WORK / "review-pairs" / row["bundle"].removesuffix(".unity3d") / (
        Path(row["original"]).stem + "__compare.png"
    )
    if target.is_file() and not force and target.stat().st_mtime_ns >= edited.stat().st_mtime_ns:
        with Image.open(edited) as result:
            edit_size, edit_alpha = result.size, "A" in result.getbands()
        return {"id": row["id"], "comparison": target.relative_to(WORK).as_posix(),
                "source": row["original"], "edited": row["edited"],
                "size_changed": list(edit_size) != row["original_size"],
                "alpha_changed": edit_alpha != ("A" in row["original_mode"])}
    with Image.open(origin) as source, Image.open(edited) as result:
        original_size = source.size
        edited_size = result.size
        changed_size = original_size != edited_size
        changed_alpha = ("A" in source.getbands()) != ("A" in result.getbands())
        width = side * 2 + 48
        height = side + 160
        canvas = Image.new("RGBA", (width, height), (23, 30, 40, 255))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((12, 48, 16 + side, 52 + side), radius=5, fill=(65, 75, 88, 255))
        draw.rounded_rectangle((32 + side, 48, 36 + side * 2, 52 + side), radius=5, fill=(65, 75, 88, 255))
        put(canvas, source, 14, 50, side)
        put(canvas, result, side + 34, 50, side)
        draw.text((15, 12), f"ORIGINAL  {original_size[0]} x {original_size[1]}", font=font(21), fill="#ffffff")
        draw.text((side + 34, 12), f"GPT Image 2.5  {edited_size[0]} x {edited_size[1]}", font=font(21), fill="#ffffff")
        alert = []
        if changed_size:
            alert.append("SIZE CHANGED")
        if changed_alpha:
            alert.append("ALPHA CHANGED")
        draw.text((15, side + 62), row["id"], font=font(18), fill="#d9e3f2")
        draw.text((15, side + 102), " / ".join(alert) if alert else "UNREVIEWED - compare text and artwork", font=font(19),
                  fill="#ffb577" if alert else "#9de2cd")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".png.writing")
        canvas.convert("RGB").save(temp, format="PNG", optimize=True)
        temp.replace(target)
        return {"id": row["id"], "comparison": target.relative_to(WORK).as_posix(),
                "source": row["original"], "edited": row["edited"],
                "size_changed": changed_size, "alpha_changed": changed_alpha}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", type=int, default=650, help="Each visual column's maximum size")
    parser.add_argument("--force", action="store_true", help="Regenerate previously made comparisons")
    args = parser.parse_args()
    if not 256 <= args.side <= 1200:
        raise ValueError("--side must be between 256 and 1200")
    rows = [json.loads(line) for line in (WORK / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    done = []
    for row in rows:
        paired = compare(row, args.side, args.force)
        if paired is not None:
            done.append(paired)
    summary = {"inventory": len(rows), "ready_for_review": len(done),
               "size_changed": sum(x["size_changed"] for x in done),
               "alpha_changed": sum(x["alpha_changed"] for x in done),
               "comparison_root": str(WORK / "review-pairs")}
    (WORK / "review-pairs").mkdir(parents=True, exist_ok=True)
    (WORK / "review-pairs" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
