"""Source-bound experiment: restore the source outside explicit text masks.

The reviewed mask below belongs only to the exact official comic sampled by
test_image_model.py. This is a manual experiment, not an image approval gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.core.hashing import sha256_file

COMIC = "ex4c00968.unity3d"
SOURCE_SHA = "b4e84b197c705d597ada2987d938c004b96370f9d4875a2f2979698c7d36a6f9"
REGIONS = [
    {"id": "title", "box": [36, 23, 605, 105]},
    {"id": "dialogue_1", "box": [45, 150, 278, 334]},
    {"id": "dialogue_2", "box": [462, 825, 620, 1048]},
    {"id": "dialogue_3", "box": [45, 1080, 213, 1315]},
    {"id": "annotation", "box": [27, 1460, 205, 1500]},
    {"id": "dialogue_4_right", "box": [278, 1538, 377, 1794]},
    {"id": "dialogue_4_center", "box": [180, 1610, 272, 1775]},
    {"id": "dialogue_4_left", "box": [37, 1820, 120, 1985]},
    {"id": "laugh", "box": [160, 1535, 293, 1612]},
    {"id": "footer", "box": [10, 2019, 160, 2050]},
]
PROTECTED = [
    [480, 397, 638, 463],  # 菊地 真 label
    [460, 475, 639, 589],  # English game logo
    [463, 1350, 639, 1495],  # 真壁 瑞希 label
    [0, 2051, 640, 2193],  # portraits and name labels
    [330, 630, 629, 815],  # second panel character faces
    [230, 1120, 639, 1350],  # third panel character face
    [390, 1595, 638, 1900],  # fourth panel character face
]


def text_mask(size, regions):
    mask = Image.new("L", size)
    draw = ImageDraw.Draw(mask)
    for region in regions:
        box = region["box"]
        if (len(box) != 4 or any(not isinstance(value, int) for value in box)
                or not 0 <= box[0] < box[2] <= size[0]
                or not 0 <= box[1] < box[3] <= size[1]):
            raise ValueError("invalid text region coordinates")
        draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=255)
    return mask


def verify_decoded_source(decoded, original):
    if decoded.size != original.size:
        raise ValueError("official source image size changed")
    deltas = [ImageChops.difference(a, b).getextrema()[1]
              for a, b in zip(decoded.convert("RGBA").split(), original.convert("RGBA").split())]
    # Unity texture decoder versions differ by one RGB level when rounding DXT
    # endpoints. Alpha and layout must be exact. Restoration itself still uses
    # the actual submitted PNG and demands zero changes outside the text mask.
    if max(deltas[:3]) > 1 or deltas[3] != 0:
        raise ValueError("original PNG differs from the verified official archive")
    return max(deltas[:3])


def restore(source, generated, canvas_size, regions, protected=()):
    if source.width != canvas_size[0] or source.height > canvas_size[1]:
        raise ValueError("source and canvas geometry disagree")
    error = abs(generated.width / generated.height / (canvas_size[0] / canvas_size[1]) - 1)
    if error > .01:
        raise ValueError("model changed the canvas aspect ratio")
    original = source.convert("RGBA")
    mask = text_mask(source.size, regions)
    for box in protected:
        if mask.crop(box).getbbox():
            raise ValueError("a text mask overlaps protected artwork")
    # Model alpha is not authoritative. Discard it before resampling so Pillow's
    # premultiplied-alpha resize cannot turn valid RGB lettering into black.
    candidate = generated.convert("RGB").resize(canvas_size, Image.Resampling.LANCZOS).crop((0, 0, *source.size)).convert("RGBA")
    candidate.putalpha(original.getchannel("A"))
    result = original.copy()
    result.paste(candidate, (0, 0), mask)
    outside = ImageChops.invert(mask)
    changed = Image.new("L", source.size)
    for left, right in zip(original.split(), result.split()):
        difference = ImageChops.difference(left, right)
        if ImageChops.multiply(difference, outside).getbbox():
            raise ValueError("non-text source pixels changed")
        changed = ImageChops.lighter(changed, difference)
    if ImageChops.difference(original.getchannel("A"), result.getchannel("A")).getbbox():
        raise ValueError("source alpha changed")
    for box in protected:
        if changed.crop(box).getbbox():
            raise ValueError("protected artwork changed")
    return result, {"outside_mask_changed_pixels": 0, "protected_regions_changed_pixels": 0,
                    "alpha_changed_pixels": 0, "text_mask_pixels": mask.histogram()[255],
                    "changed_pixels": sum(changed.histogram()[1:]), "aspect_ratio_error": error}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = json.loads((args.experiment / "summary.json").read_text(encoding="utf-8"))
    sample = next(row for row in summary["samples"] if row["logical"] == COMIC)
    if sample["source_bundle_sha256"] != SOURCE_SHA or sample["original_size"] != [640, 2193]:
        raise ValueError("text-region map does not describe this official source")
    folder = args.experiment / COMIC.removesuffix(".unity3d")
    archives = [path for path in (args.experiment / "bundles").glob("*.unity3d")
                if sha256_file(path) == SOURCE_SHA]
    if len(archives) != 1:
        raise ValueError("verified official source archive unavailable")
    if sha256_file(folder / "input.png") != sample["input_sha256"]:
        raise ValueError("experiment input PNG changed")
    if sha256_file(folder / "generated.png") != sample["output_sha256"]:
        raise ValueError("model output PNG changed")
    original = Image.open(folder / "original.png").convert("RGBA")
    from test_image_model import extract
    source_image, source_layout = extract(archives[0], COMIC, args.output / "source-parts")
    if source_layout != sample["layout"]:
        raise ValueError("texture layout differs from the verified archive")
    source_decoder_delta = verify_decoded_source(source_image, original)
    # Bind the comparison source to the actual submitted model input as pixels,
    # not PNG serialization, which can differ across Pillow versions.
    input_image = Image.open(folder / "input.png").convert("RGB").crop((0, 0, *original.size))
    flattened = Image.new("RGB", original.size, "white")
    flattened.paste(original, (0, 0), original.getchannel("A"))
    if any(ImageChops.difference(a, b).getbbox() for a, b in zip(flattened.split(), input_image.split())):
        raise ValueError("original PNG does not match the submitted input")
    result, metrics = restore(original, Image.open(folder / "generated.png"), tuple(sample["input_size"]), REGIONS, PROTECTED)
    args.output.mkdir(parents=True, exist_ok=True)
    result.save(args.output / "localized.png")
    mask = text_mask(original.size, REGIONS)
    mask.save(args.output / "text-mask.png")
    pair = Image.new("RGB", (original.width * 2, original.height), "white")
    pair.paste(original, (0, 0), original.getchannel("A"))
    pair.paste(result, (original.width, 0), result.getchannel("A"))
    pair.save(args.output / "comparison.png")
    parts = args.output / "parts"
    parts.mkdir(exist_ok=True)
    for region in sample["layout"]:
        result.crop(region["box"]).save(parts / f"{region['texture']}.png")
    report = {"status": "generated_unreviewed", "model": summary["model"],
              "source_archive_verified": True,
              "source_decoder_rgb_max_delta": source_decoder_delta,
              "source_bundle_sha256": SOURCE_SHA, "source_image_sha256": sha256_file(folder / "original.png"),
              "model_image_sha256": sample["output_sha256"], "output_sha256": sha256_file(args.output / "localized.png"),
              "regions": REGIONS, "protected": PROTECTED, **metrics}
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
