#!/usr/bin/env python3
"""Prepare one MLTD asset snapshot for reproducible Simplified-Chinese localization.

This is the version-independent orchestration layer around
mltd_localization_pipeline.py.  A new game release only needs a cached GTX
snapshot plus translation-memory inputs from earlier releases.  Exact Japanese
source values are reused automatically; only unresolved/new source values are
emitted into the machine-translation queue.

The official asset archive is never modified.  Localized UnityFS bundles are
written into an overlay root that can be supplied to start-local-arm64-stack.ps1
via -AssetOverlayRoot.

Exit codes:
  0: requested coverage gate met
  3: extraction/audit succeeded but coverage gate is not met yet
  2: invalid/missing input
"""
from __future__ import annotations

import argparse
import json
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.localization_version_identity import version_identity
from scripts.mltd_localization_pipeline import (
    cmd_audit,
    cmd_build_overlay,
    cmd_extract_snapshot,
    cmd_make_queue,
    load_snapshot,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.enrich_translation_context import (
    build_context_index,
    build_usage_profiles,
    enrich,
    load_speakers,
    write_jsonl_atomic,
)
from scripts.classify_translation_risk import classify_row, write_jsonl as write_risk_jsonl


def resolve(path: Path | None) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else REPO / path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--client-version", required=True)
    ap.add_argument("--asset-version", required=True)
    ap.add_argument("--archive-root", type=Path, required=True)
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument(
        "--translation",
        type=Path,
        action="append",
        default=[],
        help=(
            "accepted JSONL translation input; repeat for legacy official, "
            "reviewed, or prior-version machine translation memories"
        ),
    )
    ap.add_argument(
        "--catalogue",
        type=Path,
        help="reuse an already extracted full snapshot catalogue",
    )
    ap.add_argument(
        "--memory",
        type=Path,
        help="reuse the matching deduplicated source-memory JSONL",
    )
    ap.add_argument("--force-extract", action="store_true")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--example-count", type=int, default=3)
    ap.add_argument(
        "--required-coverage",
        type=float,
        default=1.0,
        help="coverage required for status=complete (default: 1.0)",
    )
    ap.add_argument(
        "--build-partial-overlay",
        action="store_true",
        help="write an overlay even when required coverage is not met",
    )
    ap.add_argument(
        "--no-overlay",
        action="store_true",
        help="audit/queue only; never materialize localized bundles",
    )
    ap.add_argument("--overlay-root", type=Path)
    ap.add_argument("--progress-every", type=int, default=1000)
    ap.add_argument("--audit-sample", type=int, default=50)
    ap.add_argument("--speaker-registry", type=Path, help="optional source-backed speaker evidence registry")
    ap.add_argument("--context-window", type=int, default=2)
    ap.add_argument("--context-examples", type=int, default=2)
    ap.add_argument("--no-quality-prep", action="store_true", help="skip context/risk generation (not recommended for production translation)")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if not 0.0 <= args.required_coverage <= 1.0:
        raise SystemExit("--required-coverage must be between 0 and 1")
    if args.workers <= 0:
        raise SystemExit("--workers must be > 0")
    if args.context_window < 0 or args.context_examples <= 0:
        raise SystemExit("--context-window must be >= 0 and --context-examples must be > 0")

    snapshot = resolve(args.snapshot)
    archive_root = resolve(args.archive_root)
    workspace = resolve(args.workspace)
    assert snapshot is not None and archive_root is not None and workspace is not None

    translations = [resolve(path) for path in args.translation]
    translation_paths = [path for path in translations if path is not None]
    catalogue = resolve(args.catalogue) if args.catalogue else workspace / "catalogue.jsonl"
    memory = resolve(args.memory) if args.memory else workspace / "translation-memory.jsonl"
    overlay_root = (
        resolve(args.overlay_root)
        if args.overlay_root
        else workspace / "overlay"
    )
    speaker_registry = resolve(args.speaker_registry) if args.speaker_registry else None
    assert catalogue is not None and memory is not None and overlay_root is not None

    summary_path = workspace / "prepare-localization-summary.json"
    extract_summary_path = workspace / "extract-summary.json"
    queue_path = workspace / "machine-translation-queue.jsonl"
    queue_summary_path = workspace / "machine-translation-queue-summary.json"
    context_queue_path = workspace / "machine-translation-queue-context.jsonl"
    context_summary_path = workspace / "context-enrichment-summary.json"
    risk_path = workspace / "translation-risk.jsonl"
    risk_summary_path = workspace / "translation-risk-summary.json"
    audit_path = workspace / "coverage-audit.json"

    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "mltd-snapshot-localization",
        "snapshot": str(snapshot),
        "archive_root": str(archive_root),
        "workspace": str(workspace),
        "translation_inputs": [str(path) for path in translation_paths],
        "required_coverage": args.required_coverage,
        "official_archive_mutated": False,
        "status": "initializing",
        "blockers": [],
        "paths": {
            "catalogue": str(catalogue),
            "memory": str(memory),
            "queue": str(queue_path),
            "context_queue": str(context_queue_path),
            "risk": str(risk_path),
            "audit": str(audit_path),
            "overlay_root": str(overlay_root),
        },
    }

    missing = []
    if not snapshot.is_file():
        missing.append(f"snapshot missing: {snapshot}")
    if not archive_root.is_dir():
        missing.append(f"archive root missing: {archive_root}")
    for path in translation_paths:
        if not path.is_file():
            missing.append(f"translation input missing: {path}")
    if speaker_registry is not None and not speaker_registry.is_file():
        missing.append(f"speaker registry missing: {speaker_registry}")
    if missing:
        result["status"] = "blocked-input-missing"
        result["blockers"] = missing
        workspace.mkdir(parents=True, exist_ok=True)
        write_json(summary_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    try:
        identity = version_identity(
            snapshot,
            client_version=args.client_version,
            asset_version=args.asset_version,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        result["status"] = "blocked-version-mismatch"
        result["blockers"].append(str(exc))
        write_json(summary_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    result["version_identity"] = identity
    snapshot_doc = load_snapshot(snapshot)
    result["snapshot_scope"] = snapshot_doc["scope"]
    result["snapshot_bundles"] = len(snapshot_doc["objects"])
    result["asset_index"] = snapshot_doc.get("asset_index")
    result["upstream_root"] = snapshot_doc.get("upstream_root")

    reuse_extracted = (
        not args.force_extract and catalogue.is_file() and memory.is_file()
    )
    if not reuse_extracted:
        workspace.mkdir(parents=True, exist_ok=True)
        extract_rc = cmd_extract_snapshot(
            Namespace(
                snapshot=snapshot,
                archive_root=archive_root,
                output=catalogue,
                memory=memory,
                summary=extract_summary_path,
                workers=args.workers,
                limit=0,
                example_count=args.example_count,
            )
        )
        if extract_rc != 0:
            result["status"] = "extract-failed"
            result["blockers"].append(f"extract-snapshot exited {extract_rc}")
            write_json(summary_path, result)
            return extract_rc
    result["extraction_reused"] = reuse_extracted
    if extract_summary_path.is_file():
        result["extraction"] = read_json(extract_summary_path)

    # No seeds is a valid first-run state.  Direct library invocation permits an
    # empty resolver even though the low-level CLI requires --translations.
    queue_rc = cmd_make_queue(
        Namespace(
            memory=memory,
            translations=translation_paths,
            output=queue_path,
            summary=queue_summary_path,
        )
    )
    if queue_rc != 0:
        result["status"] = "queue-failed"
        result["blockers"].append(f"make-queue exited {queue_rc}")
        write_json(summary_path, result)
        return queue_rc
    queue_summary = read_json(queue_summary_path)
    result["translation_queue"] = queue_summary

    if not args.no_quality_prep:
        catalogue_rows = read_jsonl(catalogue)
        queue_rows = read_jsonl(queue_path)
        speakers = load_speakers(speaker_registry)
        context_index = build_context_index(catalogue_rows, args.context_window, speakers)
        usage_profiles = build_usage_profiles(catalogue_rows, speakers)
        enriched_rows, context_summary = enrich(
            queue_rows,
            context_index,
            args.context_examples,
            speakers,
            usage_profiles,
        )
        context_summary.update({
            "schema_version": 1,
            "catalogue_rows": len(catalogue_rows),
            "context_identities": len(context_index),
            "window": args.context_window,
            "max_examples": args.context_examples,
            "speaker_registry": str(speaker_registry) if speaker_registry else None,
            "speaker_codes_loaded": len(speakers),
            "usage_profiles": len(usage_profiles),
            "multi_speaker_queue_rows": sum(
                bool(row.get("usage_profile", {}).get("multi_speaker"))
                for row in enriched_rows
            ),
            "multi_category_queue_rows": sum(
                bool(row.get("usage_profile", {}).get("multi_category"))
                for row in enriched_rows
            ),
            "cross_context_consistency_queue_rows": sum(
                bool(row.get("usage_profile", {}).get("requires_cross_context_consistency"))
                for row in enriched_rows
            ),
        })
        write_jsonl_atomic(context_queue_path, enriched_rows)
        write_json(context_summary_path, context_summary)

        risk_rows = [classify_row(row) for row in enriched_rows]
        write_risk_jsonl(risk_path, risk_rows)
        risk_levels = Counter(row["risk_level"] for row in risk_rows)
        risk_policies = Counter(row["review_policy"] for row in risk_rows)
        risk_reasons = Counter(
            reason for row in risk_rows for reason in row.get("risk_reasons", [])
        )
        risk_summary = {
            "schema_version": 1,
            "queue_rows": len(risk_rows),
            "risk_levels": dict(risk_levels),
            "review_policies": dict(risk_policies),
            "risk_reasons": dict(risk_reasons.most_common()),
            "critical_requires_second_independent_review": True,
            "high_requires_strict_scores": True,
        }
        write_json(risk_summary_path, risk_summary)
        result["quality_preparation"] = {
            "enabled": True,
            "context": context_summary,
            "risk": risk_summary,
        }
    else:
        result["quality_preparation"] = {
            "enabled": False,
            "warning": "context/risk generation skipped by --no-quality-prep",
        }

    audit_rc = cmd_audit(
        Namespace(
            catalogue=catalogue,
            translations=translation_paths,
            output=audit_path,
            sample=args.audit_sample,
        )
    )
    if audit_rc not in (0, 2):
        result["status"] = "audit-failed"
        result["blockers"].append(f"audit exited {audit_rc}")
        write_json(summary_path, result)
        return audit_rc
    audit = read_json(audit_path)
    coverage = float(audit.get("coverage", 0.0))
    result["coverage_audit"] = audit
    result["coverage"] = coverage
    gate_met = coverage + 1e-15 >= args.required_coverage
    result["coverage_gate_met"] = gate_met

    build_overlay = (
        not args.no_overlay
        and bool(translation_paths)
        and (gate_met or args.build_partial_overlay)
    )
    if build_overlay:
        overlay_rc = cmd_build_overlay(
            Namespace(
                snapshot=snapshot,
                client_version=args.client_version,
                asset_version=args.asset_version,
                archive_root=archive_root,
                translations=translation_paths,
                output_root=overlay_root,
                limit=0,
                progress_every=args.progress_every,
            )
        )
        if overlay_rc != 0:
            result["status"] = "overlay-build-failed"
            result["blockers"].append(f"build-overlay exited {overlay_rc}")
            write_json(summary_path, result)
            return overlay_rc
        manifest = overlay_root / "localization-manifest.json"
        result["overlay_built"] = True
        result["overlay_manifest"] = (
            read_json(manifest) if manifest.is_file() else None
        )
    else:
        result["overlay_built"] = False

    if gate_met:
        result["status"] = "complete"
        result["next_boundary"] = "static-and-runtime-localization-verification"
        exit_code = 0
    else:
        result["status"] = "translation-required"
        result["blockers"].append(
            f"coverage {coverage:.6%} is below required {args.required_coverage:.6%}"
        )
        result["next_boundary"] = (
            "translate machine-translation-queue.jsonl, pass the output back "
            "with another --translation, then rerun this same command"
        )
        exit_code = 3

    write_json(summary_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
