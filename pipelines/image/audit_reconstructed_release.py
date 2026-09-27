#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed image release PREPARATION, not Unity materialization.

Validate frozen reconstructed-image identities and all generated PNGs, including
each duplicate Texture2D locator, source SHA, restored alpha and outside-ROI
pixels. Emit an unreviewed QA inventory and explicit manual-review blockers.
Never promote model output to accepted assets and never call the image API.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORK = ROOT / "work/image-localization-25"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def within(root: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel or "\\" in rel:
        raise ValueError("invalid workset relative path")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("path escapes source workset")
    return target


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def lines(path: Path) -> list[dict]:
    return [json.loads(s) for s in path.read_text(encoding="utf-8-sig").splitlines()
            if s.strip()]


def dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def dump_jsonl(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    with tmp.open("w", encoding="utf-8", newline="\n") as out:
        for row in data:
            out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def png_check(original: Path, restored: Path, task: dict) -> tuple[bool, bool]:
    """Return (alpha and outside-ROI intact, changed inside ROI)."""
    with Image.open(original) as src, Image.open(restored) as dst:
        if src.format != "PNG" or dst.format != "PNG":
            raise ValueError("non-PNG source/restored image")
        src.load()
        dst.load()
        if src.mode != task["original_mode"] or dst.mode != src.mode:
            raise ValueError("Texture2D mode changed")
        if list(src.size) != task["original_size"] or dst.size != src.size:
            raise ValueError("Texture2D dimensions changed")
        if src.mode not in {"RGB", "RGBA"}:
            raise ValueError("unsupported Texture2D color mode")
        if src.mode == "RGBA":
            if ImageChops.difference(src.getchannel("A"), dst.getchannel("A")).getbbox():
                raise ValueError("Texture2D alpha geometry changed")
        outside = Image.new("L", src.size, 255)
        painter = ImageDraw.Draw(outside)
        for region in task["region_map"]:
            rect = region["rect"]
            if not isinstance(rect, list) or len(rect) != 4:
                raise ValueError("invalid ROI geometry")
            x, y, w, h = rect
            if not all(type(v) is int for v in rect) or min(x, y) < 0 or min(w, h) <= 0:
                raise ValueError("invalid ROI boundary")
            if x + w > src.width or y + h > src.height:
                raise ValueError("ROI outside Texture2D")
            painter.rectangle((x, y, x + w - 1, y + h - 1), fill=0)
        inside = ImageChops.invert(outside)
        changed = False
        for old, new in zip(src.split(), dst.split()):
            difference = ImageChops.difference(old, new)
            if ImageChops.multiply(difference, outside).getbbox():
                raise ValueError("non-ROI pixels changed")
            if ImageChops.multiply(difference, inside).getbbox():
                changed = True
        return True, changed


def verify(work: Path, dest: Path) -> dict:
    pre = work / "reconstructed-preprocess"
    image_root = work / "internal-composites"
    progress = load_json(pre / "batch-progress.json")
    summary = load_json(pre / "summary.json")
    image_queue = pre / "image-edit-queue.jsonl"
    queue = lines(image_queue)
    original_rows = lines(work / "manifest.jsonl")
    original_by_id = {r["id"]: r for r in original_rows}
    review_report = load_json(pre / "preprocess-report.json")
    classifications = {r["task_id"]: r for r in review_report["rows"]}
    sha_cache: dict[str, str] = {}
    errors: list[dict] = []
    statuses: Counter = Counter()
    expanded: Counter = Counter()
    records: list[dict] = []
    manual: list[dict] = []
    used_ids: set[str] = set()

    def checkhash(path: Path, expected: str) -> None:
        if not path.is_file():
            raise ValueError("missing source-bound file: " + str(path))
        key = str(path.resolve())
        if key not in sha_cache:
            sha_cache[key] = sha(path)
        if sha_cache[key].lower() != expected.lower():
            raise ValueError("source-bound file SHA mismatch: " + str(path))

    if (len(queue) != summary["needs_edit_unique"] or
        sum(len(row["source_ids"]) for row in queue) != summary["needs_edit_objects"] or
        len(original_rows) != summary["total_original_textures"] or
        len(original_by_id) != len(original_rows)):
        raise ValueError("frozen source counts or manifest identities changed")
    if sha(image_queue) != progress["source_queue_sha256"]:
        raise ValueError("image queue changed since completed production run")
    if progress["status"] != "completed_run":
        raise ValueError("image generation still active or stopped; audit after completion")
    if summary["needs_edit_unique"] != 937:
        raise ValueError("unexpected source-bound image baseline")

    for queued in queue:
        tid = queued["task_id"]
        entry = {"task_id": tid, "source_ids": queued["source_ids"]}
        try:
            if not tid.startswith("recon-") or len(set(queued["source_ids"])) != len(queued["source_ids"]):
                raise ValueError("invalid task ID or duplicate source IDs")
            bound = classifications[tid]
            if (bound["status"] != "needs_image_edit" or
                bound["raw_sha256"] != queued["source_sha256"] or
                bound["source_sha256"] != queued["composite_sha256"] or
                sorted(bound["ids"]) != sorted(queued["source_ids"])):
                raise ValueError("frozen classifier vs image queue mismatch")
            if any(identifier in used_ids for identifier in queued["source_ids"]):
                raise ValueError("same original Texture2D belongs to multiple image tasks")
            used_ids.update(queued["source_ids"])
            meta = load_json(image_root / tid / "task.json")
            if (meta["task_id"] != tid or meta["texture_id"] != queued["texture_id"] or
                meta["source_sha256"] != queued["source_sha256"] or
                meta["prepared_sha256"] != queued["composite_sha256"]):
                raise ValueError("task.json differs from frozen image queue")
            checkhash(within(work, meta["original"]), queued["source_sha256"])
            checkhash(within(work, meta["prepared_image"]), queued["composite_sha256"])
            members = [original_by_id[identifier] for identifier in queued["source_ids"]]
            if queued["texture_id"] not in queued["source_ids"]:
                raise ValueError("representative missing from member IDs")
            for member in members:
                if member["original_sha256"] != queued["source_sha256"]:
                    raise ValueError("duplicate Texture2D has different raw source SHA")
                checkhash(within(work, member["original"]), queued["source_sha256"])
            blocked = image_root / tid / "moderation-blocked.json"
            restored = image_root / tid / "restored-texture.png"
            if blocked.is_file():
                b = load_json(blocked)
                if (b["status"] != "moderation_blocked_manual_review" or
                    b["task_id"] != tid or
                    b["source_sha256"] != queued["source_sha256"] or
                    b["composite_sha256"] != queued["composite_sha256"] or
                    sorted(b["source_ids"]) != sorted(queued["source_ids"])):
                    raise ValueError("moderation record not source-bound")
                if restored.is_file():
                    raise ValueError("task both blocked and restored")
                status = "moderation_blocked_manual_review"
                manual.append({**entry, "status": status, "reason": "provider moderation blocked"})
            elif meta["status"] == "generated_unreviewed" and restored.is_file():
                checkhash(restored, meta["restored_sha256"])
                checkhash(within(work, meta["restored_image"]), meta["restored_sha256"])
                if (meta["restored_image"] != restored.relative_to(work).as_posix() or
                    sorted(meta["region_map"], key=lambda r: r["order"]) != meta["region_map"]):
                    raise ValueError("restored path/region order changed")
                if not Path(meta["model_image"]).is_file():
                    raise ValueError("native model image missing")
                checkhash(Path(meta["model_image"]), meta["model_sha256"])
                _, altered = png_check(within(work, meta["original"]), restored, meta)
                if not altered:
                    raise ValueError("generated result has no edited ROI pixels")
                status = "generated_unreviewed"
                for member in members:
                    records.append({
                        "review_status": "unreviewed_not_installable",
                        "task_id": tid,
                        "source_id": member["id"],
                        "bundle": member["bundle"],
                        "remote": member["remote"],
                        "archive_sha256": member["archive_sha256"],
                        "texture_path_id": member["texture_path_id"],
                        "original_png": member["original"],
                        "original_png_sha256": member["original_sha256"],
                        "restored_png": meta["restored_image"],
                        "restored_png_sha256": meta["restored_sha256"],
                        "composite_png_sha256": meta["prepared_sha256"],
                        "region_map": meta["region_map"],
                    })
            else:
                status = "failed_generation_manual_review"
                failure = image_root / tid / "production-error.json"
                note = load_json(failure).get("error", "") if failure.is_file() else "not generated"
                manual.append({**entry, "status": status, "reason": note})
            statuses[status] += 1
            expanded[status] += len(members)
        except (ValueError, KeyError, TypeError, OSError, IndexError) as exc:
            errors.append({**entry, "error": type(exc).__name__ + ": " + str(exc)[:500]})

    for entry in review_report["rows"]:
        if entry["status"] == "manual_review":
            manual.append({
                "task_id": entry["task_id"], "source_ids": entry["ids"],
                "status": "uncertain_japanese_manual_review",
                "reason": "reconstructed image visual classifier uncertain",
            })
    for entry in summary["manual_review_missing_source"]:
        manual.append({
            "task_id": None, "source_ids": entry["member_ids"],
            "status": "unmapped_sprite_manual_review", "reason": entry["reason"],
        })

    if (statuses["generated_unreviewed"] != progress["completed_total"] or
        statuses["moderation_blocked_manual_review"] != progress["moderation_blocked_total"] or
        sum(statuses.values()) != len(queue)):
        errors.append({"error": "actual image statuses disagree with production progress"})
    count_by_id = Counter(row["source_id"] for row in records)
    if any(count != 1 for count in count_by_id.values()):
        errors.append({"error": "duplicate release-prep Texture2D locator"})
    if not errors:
        dump_jsonl(dest / "verified-candidates.jsonl", records)
    report = {
        "schema_version": 1,
        "kind": "mltd-reconstructed-image-release-prep",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_queue_sha256": progress["source_queue_sha256"],
        "generated_unique": statuses["generated_unreviewed"],
        "generated_original_texture_locators": expanded["generated_unreviewed"],
        "model_blocked_unique": statuses["moderation_blocked_manual_review"],
        "model_blocked_original_texture_locators": expanded["moderation_blocked_manual_review"],
        "failed_unique": statuses["failed_generation_manual_review"],
        "uncertain_unique": summary["uncertain_unique"],
        "unmapped_unique": summary["unable_to_reconstruct_unique"],
        "automatically_skipped_no_japanese_unique": summary["automatic_no_japanese_skip_unique"],
        "verified_candidate_file": str(dest / "verified-candidates.jsonl") if not errors else None,
        "reviewed_and_approved": 0,
        "unity_bundles_modified": 0,
        "ready_to_install": False,
        "errors": errors,
        "manual_review": manual,
        "notes": [
            "Hashes and per-pixel geometry are verified, but Japanese-to-Chinese visual quality is not.",
            "Candidate PNGs must pass a source-SHA-bound human review before any Unity mutation.",
            "Duplicate original SHA expands into independent bundle + Texture2D path_id locators.",
            "Source archive SHA is recorded but original Unity bundle bytes are not rewritten here.",
        ],
    }
    dump(dest / "report.json", report)
    print(json.dumps({k: report[k] for k in (
        "generated_unique", "generated_original_texture_locators",
        "model_blocked_unique", "model_blocked_original_texture_locators",
        "failed_unique", "uncertain_unique", "unmapped_unique",
        "reviewed_and_approved", "unity_bundles_modified")}, ensure_ascii=False))
    print("QA_ERRORS", len(errors), "REPORT", dest / "report.json")
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work", type=Path, default=DEFAULT_WORK)
    p.add_argument("--output-dir", type=Path, default=None)
    args = p.parse_args()
    work = args.work.resolve()
    dest = (args.output_dir or work / "release-prep").resolve()
    report = verify(work, dest)
    return 0 if not report["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
