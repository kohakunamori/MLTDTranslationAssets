"""Manual, bounded provider experiment. Never approves or publishes images.

Unpack current official textures, assemble a comic, submit the PNG edit target
to the explicitly selected model and retain source/result pairs for inspection.
Credentials are read only from the workflow's existing secret environment.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import UnityPy
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
from build_generated_release import download, load_official_index, load_version_manifest
from pipelines.core.hashing import sha256_file

SAMPLES = ("ex4c00968.unity3d", "hitokoma_100.unity3d")
PROMPT = """Use case: text-localization.
The input PNG is the edit target: an official Japanese game comic, already
assembled in its original reading order. Translate ALL Japanese dialogue,
captions, character names and sound effects into fluent Simplified Chinese.
Use Chinese text inside the existing speech bubbles, with readable lettering
and the original reading order. Preserve the meaning, emotion and register.
Translate 菊地真 as 菊地真 and プロデューサー as 制作人.
Keep English branding, logos, copyright text, art style, character faces,
hair, costumes, hands, poses, props, colors, backgrounds, panel borders and
panel positions unchanged. Never add, remove or reorder panels or characters.
Replace Japanese lettering cleanly, retaining the surrounding artwork.
Preserve the entire canvas including its white padding, without cropping,
stretching, moving artwork, adding labels or inventing extra dialogue.
Return ONE edited image of the same composition, not an explanation.
"""
GUIDED_TEXT = {
    "ex4c00968.unity3d": """Mandatory approved text, use it verbatim rather than retranslating:
Title: 会动的毛绒玩偶
Panel 1 dialogue: 啊～要是小玩偶真能像舞台剧里那样动起来就好了呢～
Panel 2 dialogue: 菊地小姐，菊地小姐。小熊先生，小熊先生。
Panel 3 dialogue: 就算不在舞台剧里也能动哦！来，试试看！
Panel 3 annotation: （腹语术）
Panel 4 right dialogue: 谢谢！我也让它动起来了！
Panel 4 center dialogue: 是吧！
Panel 4 left dialogue: 瑞希真温柔呢。
Panel 4 laugh: 啊哈哈！
Character labels and footer: 菊地 真 / 真壁 瑞希 / 登场人物.
Preserve the English game logo and copyright exactly, letter for letter.
The word 小熊 must contain 熊, never 能. The name is 真壁, never 真璧.
""",
    "hitokoma_100.unity3d": """Mandatory approved text, use it verbatim rather than retranslating:
Right bubble: 迷、迷迭香、薄荷和巧克力的香味，可以帮助集中注意力……
Left bubble: 疲惫的时候，请试着用用看哦……♪
Replace the display sound effect ずらっ with 一排排.
Fit the Chinese only into the original text regions. Do not enlarge the
lettering over the character's face, hands, dress or bottles.
""",
}
STRICT_PROMPT = """Use case: text-localization. Perform a minimal text-only edit
of the provided PNG, not a redraw. The source image is the immutable master.
Priority 1: copy all artwork, character faces, eyes, mouths, expressions,
hands, costumes, hair, poses, toys, backgrounds, borders and panel geometry
exactly. A closed mouth MUST remain closed. Do not regenerate any illustration.
Priority 2: copy the English game logo and copyright pixel-for-pixel. Keep the
pink 菊地 真 and blue 真壁 瑞希 name labels exactly as in the original image;
do not redraw or typeset these labels. The English logo reads THE IDOLM@STER
MILLION LIVE! THEATER DAYS. Never rewrite these protected regions.
Priority 3: replace only Japanese title, speech-bubble text, the 腹話術 note,
laugh lettering and 登場人物 footer heading with the provided Chinese below.
Do not translate or alter any text outside these specified text regions.
Keep text inside its original bubble; shrinking the font is preferable to
moving, resizing or covering artwork. Preserve white padding and canvas ratio.
Typeset 小熊 with 熊 (U+718A), never 能 (U+80FD). Follow every supplied Chinese
sentence exactly, without paraphrasing, omissions, extra words or Japanese.
Return a single edited image. Keep all other source pixels untouched.
"""


def assemble(parts):
    if not parts:
        raise ValueError("no texture parts")
    if len({image.width for _, image in parts}) != 1:
        raise ValueError("comic part widths disagree")
    canvas = Image.new("RGBA", (parts[0][1].width, sum(image.height for _, image in parts)), "white")
    layout, y = [], 0
    for name, image in parts:
        canvas.paste(image, (0, y))
        layout.append({"texture": name, "box": [0, y, image.width, y + image.height]})
        y += image.height
    return canvas, layout


def extract(source, logical, output):
    parts = []
    output.mkdir(parents=True, exist_ok=True)
    for obj in UnityPy.load(str(source)).objects:
        if obj.type.name != "Texture2D":
            continue
        texture = obj.read()
        name = str(texture.m_Name)
        if not re.fullmatch(r"[A-Za-z0-9_+-]+", name):
            raise ValueError("unsafe texture filename")
        image = texture.image.convert("RGBA")
        image.save(output / f"{name}.png")
        parts.append((name, image))
    if logical.startswith("ex4c"):
        parts.sort(key=lambda part: int(part[0].rsplit("_", 1)[1]))
        if [int(name.rsplit("_", 1)[1]) for name, _ in parts] != list(range(6)):
            raise ValueError("comic does not have the expected six ordered parts")
    elif len(parts) != 1:
        raise ValueError("single-panel sample must have exactly one texture")
    return assemble(parts)


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def payload(prompt, source, aspect, size):
    return {"contents": [{"role": "user", "parts": [{"text": prompt},
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(source).decode("ascii")}}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                 "imageConfig": {"aspectRatio": aspect, "imageSize": size}}}


def decode_response(doc):
    images, text = [], []
    for candidate in doc.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("thought"):
                continue
            inline = part.get("inlineData", part.get("inline_data", {}))
            if inline.get("data"):
                mime = inline.get("mimeType", inline.get("mime_type", ""))
                if not mime.startswith("image/"):
                    raise ValueError("provider returned non-image inline data")
                images.append(base64.b64decode(inline["data"], validate=True))
            if part.get("text"):
                text.append(part["text"])
    if len(images) != 1:
        raise ValueError(f"expected one generated image, got {len(images)}")
    image = Image.open(io.BytesIO(images[0]))
    image.load()
    return image, "\n".join(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="imas-trans-image")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--guided", action="store_true", help="supply exact Chinese text to separate translation and rendering quality")
    parser.add_argument("--strict-preservation", action="store_true", help="test stronger protected artwork and label constraints on the stitched comic only")
    parser.add_argument("--index", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.model):
        raise ValueError("invalid model ID")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    version = load_version_manifest(ROOT / "manifests/asset-version.json")
    index_path = args.index or out / version["index_name"]
    if not args.index:
        download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    index = load_official_index(index_path)
    public = json.loads((ROOT / "configs/api-models.example.json").read_text(encoding="utf-8"))
    base = public["defaults"]["base_url"].removesuffix("/v1")
    endpoint = (base + '/v1/images/edits' if args.model == 'imas-trans-image' else
                f"{base}/v1beta/models/{args.model}:generateContent")
    key = os.environ.get("MLTD_LLM_API_KEY", "")
    if not args.prepare_only and not key:
        raise ValueError("existing MLTD_LLM_API_KEY secret is unavailable")
    summary = {"model": args.model, "provider": base, "endpoint": endpoint,
               "asset_version": version["asset_version"], "mode": "experiment_only",
               "guided_translation": args.guided or args.strict_preservation,
               "strict_preservation": args.strict_preservation, "samples": []}
    samples = SAMPLES[:1] if args.strict_preservation else SAMPLES
    for logical in samples:
        folder = out / logical.removesuffix(".unity3d")
        folder.mkdir(exist_ok=True)
        row = index[logical]
        source = (args.archive or out / "bundles") / row["remote"]
        download(f"{version['asset_root']}/{row['remote']}", source, row["declared_size"])
        original, layout = extract(source, logical, folder / "parts")
        original.save(folder / "original.png")
        # Pad rather than distort to a documented Gemini aspect ratio. The
        # original texture coordinates are retained for later split/review.
        aspect = "1:4" if logical.startswith("ex4c") else "4:3"
        height = original.width * 4 if aspect == "1:4" else (original.width * 3 + 3) // 4
        canvas = Image.new("RGB", (original.width, height), "white")
        canvas.paste(original, (0, 0), original.getchannel("A"))
        canvas.save(folder / "input.png")
        prompt = (STRICT_PROMPT if args.strict_preservation else PROMPT) + (GUIDED_TEXT[logical] if args.guided or args.strict_preservation else "")
        (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
        sample = {"logical": logical, "source_bundle_sha256": sha256_file(source),
                  "input_sha256": sha256_file(folder / "input.png"), "original_size": list(original.size),
                  "input_size": list(canvas.size), "aspect_ratio": aspect, "layout": layout,
                  "status": "prepared"}
        summary["samples"].append(sample)
        if not args.prepare_only:
            body = json.dumps(payload(prompt, png(canvas), aspect, "2K" if aspect == "1:4" else "1K")).encode("utf-8")
            request = urllib.request.Request(endpoint, data=body, method="POST",
                      headers={"Authorization": f"Bearer {key}", "x-goog-api-key": key, "Content-Type": "application/json"})
            started = time.monotonic()
            try:
                if args.model == 'imas-trans-image':
                    sys.path.insert(0, str(ROOT / 'pipelines/image'))
                    import provider_config
                    config = {k: dict(v) for k, v in provider_config.DEFAULTS.items()}
                    config['image_provider'].update(base_url=base + '/v1', model=args.model,
                        api_key_env='MLTD_LLM_API_KEY', require_api_key=True, min_request_interval_seconds=30)
                    raw, _ = provider_config.image_edit(config, png(canvas), prompt)
                    edited, caption, result = Image.open(io.BytesIO(raw)), '', {}
                else:
                    with urllib.request.urlopen(request, timeout=360) as response:
                        result = json.load(response)
                    edited, caption = decode_response(result)
                edited.save(folder / "generated.png")
                (folder / "response-text.txt").write_text(caption, encoding="utf-8")
                sample.update(status="generated", output_size=list(edited.size),
                              output_sha256=sha256_file(folder / "generated.png"),
                              model_version=result.get("modelVersion"), usage=result.get("usageMetadata"))
                ratio_error = abs(edited.width / edited.height / (canvas.width / canvas.height) - 1)
                sample["aspect_ratio_error"] = ratio_error
                if ratio_error <= .01:
                    normalized = edited.convert("RGB").resize(canvas.size, Image.Resampling.LANCZOS).crop((0, 0, *original.size))
                    normalized.save(folder / "localized-preview.png")
                    pair = Image.new("RGB", (original.width * 2, original.height), "white")
                    pair.paste(original, (0, 0), original.getchannel("A"))
                    pair.paste(normalized, (original.width, 0))
                    pair.save(folder / "comparison.png")
                else:
                    sample["geometry_warning"] = "model changed canvas ratio; no automatic crop or injection"
            except urllib.error.HTTPError as exc:
                sample.update(status="failed", http_status=exc.code,
                              error=exc.read(4096).decode("utf-8", "replace").replace(key, "[redacted]"))
            except Exception as exc:
                sample.update(status="failed", error=f"{type(exc).__name__}: {exc}".replace(key, "[redacted]"))
            sample["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in sample.items() if key not in ("layout", "error")}), flush=True)
    return 1 if any(sample["status"] == "failed" for sample in summary["samples"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
