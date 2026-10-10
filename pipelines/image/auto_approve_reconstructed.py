#!/usr/bin/env python3
"""Turn an audited reconstructed-image run into an installable manifest, unattended.

This is the automatic counterpart of ``stage_reviewed_images.py``.  The human
CSV sign-off is replaced by a gate on the independent audit report: the run is
refused unless the audit produced a candidate file with zero errors, zero
model-blocked tasks and zero failed tasks.  "Automatic" therefore never means
"unchecked" -- every candidate still carries the source SHA, archive SHA and
roundtrip evidence the audit verified, and ``inject_reviewed_textures.py``
re-checks all of it before a bundle is written.

Rows are written with ``review_status = AUTO_APPROVED_REVIEW_STATUS``, the only
provenance the unattended path may use.

Inputs are explicit and read-only:
  ``--audit-report``         ``audit_reconstructed_release.py`` report.json
  ``--verified-candidates``  the verified-candidates.jsonl that report names
  ``--work``                 pipeline work root (holds manifest.jsonl)
  ``--bundle-root``          directory holding the official source bundles
  ``--out``                  install manifest to write (JSONL)

``source_bundle`` and ``restored_png`` are written as absolute paths because the
injector refuses relative ones; ``original_png`` stays relative to the root the
injector is given.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inject_reviewed_textures import AUTO_APPROVED_REVIEW_STATUS, REQUIRED_ROW_KEYS

CONFIRMED_CANDIDATE_KIND = "mltd-audited-image-install-manifest"


class RefusedGate(ValueError):
    """The audit gate did not pass, so no manifest may be produced."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise RefusedGate(f"missing input: {path}")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise RefusedGate(f"{path}: expected a JSON object")
    return document


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise RefusedGate(f"missing input: {path}")
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise RefusedGate(f"{path}:{lineno}: row is not an object")
        rows.append(row)
    return rows


def gate(report: dict, candidates: list[dict]) -> None:
    """Refuse unless the audit itself says the cohort is clean.

    Only blocker-free runs pass.  A moderation-blocked or failed task means the
    cohort does not match the frozen queue, so nothing is published at all
    rather than publishing a partial surface.
    """
    if report.get("errors"):
        raise RefusedGate(f"audit reported {len(report['errors'])} error(s); refusing to approve")
    if report.get("model_blocked_unique"):
        raise RefusedGate(
            f"{report['model_blocked_unique']} image(s) were declined upstream; "
            "they need an explicit decision before this cohort can be installed")
    if report.get("failed_unique"):
        raise RefusedGate(f"{report['failed_unique']} image(s) failed generation")
    if not report.get("generated_unique"):
        raise RefusedGate("audit produced no generated candidates")
    if not candidates:
        raise RefusedGate("verified candidate file is empty")
    if len(candidates) != int(report.get("generated_original_texture_locators", len(candidates))):
        raise RefusedGate(
            "candidate locator count disagrees with the audit report "
            f"({len(candidates)} != {report.get('generated_original_texture_locators')})")


def build_rows(work: Path, bundle_root: Path, candidates: list[dict]) -> list[dict]:
    manifest_path = work / "manifest.jsonl"
    manifest = {}
    for row in load_jsonl(manifest_path):
        manifest[row["id"]] = row
    if not manifest:
        raise RefusedGate(f"{manifest_path} carries no source locators")

    rows = []
    for record in candidates:
        source_id = record.get("source_id")
        if source_id not in manifest:
            raise RefusedGate(f"candidate {source_id!r} is not in {manifest_path}")
        source = manifest[source_id]
        remote = str(record["remote"])
        bundle_path = bundle_root / remote
        restored = (work / str(record["restored_png"])).resolve()
        if not bundle_path.is_file():
            raise RefusedGate(f"official source bundle missing: {bundle_path}")
        if not restored.is_file():
            raise RefusedGate(f"restored image missing: {restored}")
        if sha256_file(restored) != record["restored_png_sha256"]:
            raise RefusedGate(f"{source_id}: restored image changed since the audit")
        row = {
            "archive_sha256": record["archive_sha256"],
            "bundle": record["bundle"],
            "original_mode": source.get("original_mode"),
            "original_png": record["original_png"],
            "original_png_sha256": record["original_png_sha256"],
            "original_size": source.get("original_size"),
            "region_map": record["region_map"],
            "remote": remote,
            "restored_png": str(restored),
            "restored_png_sha256": record["restored_png_sha256"],
            "review_status": AUTO_APPROVED_REVIEW_STATUS,
            "source_bundle": str(bundle_path.resolve()),
            "source_id": source_id,
            "task_id": record["task_id"],
            "texture_path_id": record["texture_path_id"],
        }
        missing = [key for key in REQUIRED_ROW_KEYS if row.get(key) in (None, "")]
        if missing:
            raise RefusedGate(f"{source_id}: reconstructed row lacks {', '.join(missing)}")
        rows.append(row)
    seen = set()
    for row in rows:
        key = (row["bundle"], int(row["texture_path_id"]))
        if key in seen:
            raise RefusedGate(f"duplicate locator {key[0]}:{key[1]} in the audited cohort")
        seen.add(key)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--verified-candidates", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True,
                        help="Directory holding the official source bundles, keyed by remote name.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--require-count", type=int, default=0,
                        help="Refuse unless exactly this many locators are approved (0 = no check).")
    args = parser.parse_args()

    report = load_json(args.audit_report)
    candidates = load_jsonl(args.verified_candidates)
    gate(report, candidates)
    rows = build_rows(args.work.resolve(), args.bundle_root.resolve(), candidates)
    if args.require_count and len(rows) != args.require_count:
        raise RefusedGate(f"approved {len(rows)} locators, expected {args.require_count}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_name(args.out.name + ".writing")
    temp.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8")
    temp.replace(args.out)
    print(json.dumps({
        "kind": CONFIRMED_CANDIDATE_KIND,
        "review_status": AUTO_APPROVED_REVIEW_STATUS,
        "locators": len(rows),
        "bundles": len({row["bundle"] for row in rows}),
        "manifest": str(args.out),
        "manifest_sha256": sha256_file(args.out),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RefusedGate as error:
        print(f"REFUSED {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
