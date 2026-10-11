#!/usr/bin/env python3
"""Prove whether the frozen image cohort already has reviewed, intact outputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
APPROVED = {"user_approved_for_isolated_install_staging", "auto_approved_no_human_signoff"}


def locator(value: str) -> str:
    bundle, path_id = value.rsplit(":", 1)
    return f"{bundle.removesuffix('.unity3d')}:{int(path_id)}"


def image_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "images").resolve()):
        raise ValueError("image path escapes images/: " + relative)
    return path


def verify_png(root: Path, reference: dict, dimensions: dict) -> None:
    path = image_path(root, reference["relative_path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("image SHA mismatch: " + str(path))
    with Image.open(path) as image:
        if image.format != "PNG" or image.size != (dimensions["width"], dimensions["height"]):
            raise ValueError("image geometry/format mismatch: " + str(path))
        image.verify()


def inspect(root: Path, cohort: dict, manifest: dict) -> dict:
    if cohort.get("kind") != "mltd-frozen-image-cohort" or cohort.get("schema_version") != 1:
        raise ValueError("invalid frozen image cohort")
    sources = cohort["sources"]
    expected = cohort["expected_images"]
    if type(expected) is not int or expected <= 0 or len(sources) != expected:
        raise ValueError("frozen cohort count mismatch")
    by_hash = {row["sha256"]: {locator(x) for x in row["source_ids"]} for row in sources}
    dimensions_by_hash = {row["sha256"]: row["dimensions"] for row in sources}
    if len(by_hash) != expected:
        raise ValueError("duplicate frozen image source")
    if manifest.get("kind") != "mltd-images-manifest":
        raise ValueError("invalid image manifest")
    images = manifest["images"]
    if len(images) != expected or manifest["counts"]["total_images"] != expected:
        raise ValueError("image manifest does not describe the frozen cohort")
    seen = set()
    pending = []
    for row in images:
        source = row["original"]["sha256"]
        if source not in by_hash or source in seen or locator(row["id"]) not in by_hash[source]:
            raise ValueError("image locator/source differs from frozen cohort: " + row["id"])
        seen.add(source)
        if row["dimensions"] != dimensions_by_hash[source]:
            raise ValueError("image dimensions differ from frozen source: " + row["id"])
        # Official source PNGs are external binary inputs, not checked in.
        # Their hash and bundle/path ID are pinned by the frozen cohort above.
        # When present locally, also validate the original bytes.
        original_path = image_path(root, row["original"]["relative_path"])
        if original_path.exists():
            verify_png(root, row["original"], row["dimensions"])
        if row.get("review_status") not in APPROVED or not row.get("localized"):
            pending.append(row["id"])
        else:
            # An approved but broken output is an error, never a silent skip.
            verify_png(root, row["localized"], row["dimensions"])
    reviewed = len(images) - len(pending)
    if manifest["counts"]["reviewed_textures"] != reviewed:
        raise ValueError("reviewed image count does not match row provenance")
    return {"cohort_id": cohort["cohort_id"], "images": expected,
            "verified_reviewed_images": reviewed, "pending": pending,
            "needs_work": bool(pending),
            "status": "pending_generation" if pending else "reviewed_outputs_current"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--batch-progress", type=Path,
                        help="Report whether a resumed production batch is ready for audit")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.batch_progress:
        result = batch_status(json.loads(args.batch_progress.read_text(encoding="utf-8")))
    else:
        result = inspect(root,
                         json.loads((root / "configs/image-cohort.json").read_text(encoding="utf-8")),
                         json.loads((root / "manifests/images.manifest.json").read_text(encoding="utf-8")))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key in ("needs_work", "audit_ready"):
                if key in result:
                    output.write(f"{key}={str(result[key]).lower()}\n")
    print(json.dumps(result))
    return 0


def batch_status(progress: dict) -> dict:
    status = progress.get("status")
    if progress.get("failed", 0) or progress.get("geometry_review", 0):
        raise ValueError("production batch contains failed or invalid-geometry tasks")
    if status not in {"completed_run", "completed_bounded_run", "time_budget_exhausted"}:
        raise ValueError("production batch did not stop cleanly: " + str(status))
    return {"status": status, "audit_ready": status == "completed_run"}


if __name__ == "__main__":
    raise SystemExit(main())
