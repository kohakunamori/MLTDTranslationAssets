#!/usr/bin/env python3
"""Restore one uploaded Chinese composite back into its 512x512 texture.

The portal's image studio uploads a translated composite; this script runs the
reverse transform (aspect check -> whole-image resize -> region split ->
inverse rotate -> paste into the pristine Texture2D, keeping the original
alpha geometry and every non-ROI pixel byte-identical).

Inputs come from the public asset bucket rather than the repository, so a
backfill job only needs the task id plus the uploaded composite:

  {ASSET_BASE}/images/task/<task_id>/task.json                 region map
  {ASSET_BASE}/images/original/<bundle>/<file>.png             pristine texture
  {ASSET_BASE}/images/composite/<task_id>/source-composite.png the pre-edit art

The result is written to `images/localized/<bundle>/<file>.png`, matching the
`localized.relative_path` field already used by
`manifests/images.manifest.json`.

Usage:
  python scripts/restore/restore_texture.py --task-id recon-0018... --model-file in.png
  python scripts/restore/restore_texture.py --batch work/batch.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_ASSET_BASE = os.environ.get(
    "MLTD_ASSET_BASE", "https://pub-mltd-assets.nyaneko.cn"
)


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "internal_texture_image25", Path(__file__).resolve().parent / "internal_texture_image25.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def display(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def fetch(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "mltd-restore/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        destination.write_bytes(response.read())


def materialize(task_id: str, base: str, tool) -> Path:
    """Download the task's region map and pristine texture into the layout the
    restore tool expects (work/image-localization-25/...)."""
    task_url = f"{base}/images/task/{task_id}/task.json"
    info_path = tool.OUT / task_id / "task.json"
    fetch(task_url, info_path)
    info = json.loads(info_path.read_text(encoding="utf-8"))

    original_rel = info["original"]
    original_path = tool.WORK / original_rel
    if not original_path.is_file():
        fetch(f"{base}/images/{original_rel}", original_path)

    composite = tool.WORK / info["prepared_image"]
    if not composite.is_file():
        fetch(f"{base}/images/composite/{task_id}/source-composite.png", composite)
    return info_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id")
    parser.add_argument("--model-file", type=Path, help="Uploaded Chinese composite")
    parser.add_argument("--model-url", help="Download the uploaded composite from a URL instead")
    parser.add_argument("--batch", type=Path, help="JSON list of {task_id, model_url|model_file}")
    parser.add_argument("--asset-base", default=DEFAULT_ASSET_BASE)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "images" / "localized")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing restored texture")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    items: list[dict] = []
    if args.batch:
        items = json.loads(args.batch.read_text(encoding="utf-8"))
    elif args.task_id:
        items = [{"task_id": args.task_id, "model_file": args.model_file, "model_url": args.model_url}]
    else:
        parser.error("provide --task-id or --batch")

    tool = load_tool()
    results = []
    for item in items:
        task_id = item["task_id"]
        job_id = item.get("job_id")
        model_path = Path(item["model_file"]) if item.get("model_file") else ROOT / "work" / "model" / f"{task_id}.png"
        if item.get("model_url"):
            fetch(item["model_url"], model_path)
        if args.dry_run:
            print(f"[dry-run] {task_id} <- {model_path}")
            continue
        try:
            materialize(task_id, args.asset_base, tool)
            info = tool.restore(task_id, model_path, force=args.force or True)
        except Exception as exc:
            print(f"    [x] {task_id}: {exc}", file=sys.stderr)
            results.append({"task_id": task_id, "job_id": job_id, "ok": False, "error": str(exc)})
            continue
        bundle = Path(info["original"]).parent.name
        target = args.out_dir / bundle / Path(info["original"]).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((tool.WORK / info["restored_image"]).read_bytes())
        print(f"    [ok] {task_id} -> {display(target)}")
        results.append(
            {
                "task_id": task_id,
                "job_id": job_id,
                "ok": True,
                "bundle": bundle,
                "output": display(target),
                "sha256": info["restored_sha256"],
                "source_sha256": info["source_sha256"],
            }
        )

    if args.dry_run:
        return 0
    summary = ROOT / "work" / "restore-summary.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = [r for r in results if not r["ok"]]
    print(f"[*] restored {len(results) - len(failures)}/{len(results)}; summary at {summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
