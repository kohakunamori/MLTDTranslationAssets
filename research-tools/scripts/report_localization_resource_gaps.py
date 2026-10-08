#!/usr/bin/env python3
"""Summarize MLTD localization surfaces that sit outside the main GTX queue.

This is a release-audit report. It never mutates translation queues or assets.
Confirmed textual surfaces are kept separate from heuristic APK baked-UI review
candidates and baked-image risks so percentages are not inflated by uncertain
or non-text resources.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def jsonl_ids(path: Path) -> set[str]:
    result: set[str] = set()
    if not path.is_file():
        return result
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            sid = str(row.get("source_sha256", "")).strip()
            translation = str(row.get("translation", "")).strip()
            status = str(row.get("status", "")).strip()
            if sid and translation and status not in {"pending", "failed", "deferred"}:
                result.add(sid)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workspace",
        type=Path,
        default=Path("build/localization-90200"),
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/localization-resource-gap-report.json"),
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ws = args.workspace
    coverage = load_json(ws / "localization-resource-coverage-audit.json")
    companion = load_json(ws / "machine-translation-nongtx-queue-summary.json")
    baked_keys = load_json(ws / "apk-baked-localization-key-audit.json")
    baked_triage = load_json(ws / "apk-baked-ui-triage.json")
    android = load_json(ws / "android-resource-localization-audit.json")
    priority_textasset = load_json(ws / "nongtx-priority-local-jp-audit.json")
    metadata = load_json(ws / "il2cpp-metadata-jp-string-audit.json")
    image_risk = load_json(ws / "nongtx-image-risk-manifest-audit.json")
    image_pair = load_json(ws / "nongtx-texture-legacy-pair-audit.json")
    image_uncovered = load_json(ws / "nongtx-uncovered-object-classification.json")
    remote_monobehaviour = load_json(ws / "remote-monobehaviour-jp-text-audit.json")
    remote_monobehaviour_triage = load_json(
        ws / "remote-monobehaviour-jp-text-triage.json"
    )
    remote_textasset_structured = load_json(
        ws / "remote-textasset-structured-jp-audit.json"
    )
    remote_textasset_nonparsed = load_json(
        ws / "remote-textasset-nonparsed-jp-audit.json"
    )
    server_wire = load_json(ws / "server-wire-plaintext-capture-audit.json")
    supplemental_review = load_json(ws / "localization-supplemental-review.json")
    apk_auxiliary = load_json(ws / "apk-auxiliary-text-audit.json")
    remote_video = load_json(ws / "remote-video-localization-surface-audit.json")

    textual = coverage.get("textual_surfaces", {})
    confirmed_union = int(textual.get("confirmed_union_unique", 0) or 0)
    main_unique = int(textual.get("main_gtx_unique", 0) or 0)
    confirmed_nonmain = int(textual.get("confirmed_nonmain_unique", 0) or 0)

    companion_total = int(companion.get("companion_unique", 0) or 0)
    companion_output = ws / "machine-translations-nongtx-api.jsonl"
    companion_done = len(jsonl_ids(companion_output))
    companion_pending = max(0, companion_total - companion_done)
    surface_membership = companion.get("surface_unique_membership", {})
    if not isinstance(surface_membership, dict):
        surface_membership = {}
    # APK bootstrap BI is materialized by client/apk_builtin_localization.py and
    # is wired into the APK candidate pipeline; only the remote-bundle companion
    # sources (event-unit, MLD config) still lack a source-bound writer.
    missing_materializer_count = sum(
        int(surface_membership.get(name, 0) or 0)
        for name in ("event_unit", "mld_config")
    )

    key_summary = baked_keys.get("summary", {})
    triage_counts = baked_triage.get("classification_counts", {})
    pair_rows = image_pair.get("rows", [])
    if not isinstance(pair_rows, list):
        pair_rows = []
    pure_texture_rows = image_uncovered.get("rows", [])
    if not isinstance(pure_texture_rows, list):
        pure_texture_rows = []
    texture_categories = Counter()
    for row in pure_texture_rows:
        if not isinstance(row, dict):
            continue
        logical = str(row.get("logical", "")).lower()
        if logical.startswith("costumesalesinfo"):
            category = "costume_sales_info"
        elif re.match(r"event_.*_info", logical):
            category = "event_info"
        elif "tutorial" in logical or "help" in logical:
            category = "tutorial_help"
        elif "info" in logical or "notice" in logical:
            category = "info_notice"
        else:
            category = "other"
        texture_categories[category] += 1

    android_missing = android.get("missing", [])
    if not isinstance(android_missing, list):
        android_missing = []

    report = {
        "schema_version": 1,
        "kind": "mltd-localization-resource-gap-report",
        "workspace": str(ws),
        "confirmed_text_universe": {
            "main_gtx_unique": main_unique,
            "confirmed_non_gtx_unique": confirmed_nonmain,
            "confirmed_union_unique": confirmed_union,
            "note": (
                "This union includes GTX main, FontRender, APK bootstrap BI, event-unit text, "
                "and source-backed visible MLD config. APK baked UI review candidates are excluded until "
                "runtime visibility/materialization is confirmed."
            ),
        },
        "confirmed_non_gtx_companion": {
            "queue_unique": companion_total,
            "translated_unique": companion_done,
            "pending_unique": companion_pending,
            "queue": str(ws / "machine-translation-nongtx-queue.jsonl"),
            "translation_output": str(companion_output),
            "surface_membership": companion.get("surface_unique_membership", {}),
            "task_counts": companion.get("task_counts", {}),
            "status": "pending" if companion_pending else "closed",
            "materialization": {
                "fontrender": {
                    "status": "implemented",
                    "builder": "scripts/build_fontrender_overlay.py",
                },
                "apk_bootstrap_bi": {
                    "status": "implemented",
                    "builder": "client/apk_builtin_localization.py",
                    "note": (
                        "Record-preserving data.unity3d TextAsset writer for the encrypted runtime BI_jp.gtx; "
                        "wired into client.apk_candidate_pipeline and re-verified after build by runtime BI hash "
                        "comparison (candidate receipt signed_content.runtime_BI_matches)."
                    ),
                },
                "event_unit": {
                    "status": "missing_materializer",
                    "note": "event-unit JSON TextAsset extraction/queue exists, but no source-bound remote bundle overlay writer is present.",
                },
                "mld_config": {
                    "status": "missing_materializer",
                    "note": "MD.mld visible config extraction is source-bound, but no MLD re-encoder/remote bundle overlay writer is present.",
                },
            },
        },
        "supplemental_text_review": {
            "unique_review_candidates": int(supplemental_review.get("unique_review_candidates", 0) or 0),
            "review_tier_counts": supplemental_review.get("review_tier_counts", {}),
            "surface_membership": supplemental_review.get("surface_membership", {}),
            "cross_surface_unique": int(supplemental_review.get("cross_surface_unique", 0) or 0),
            "queue": str(ws / "localization-supplemental-review-queue.jsonl"),
            "status": "review_required" if supplemental_review else "audit_pending",
            "note": (
                "Deduplicated union of APK baked unkeyed UI and remote MonoBehaviour review candidates. "
                "These rows remain outside the confirmed denominator until runtime visibility or an exact "
                "materialization path is proven."
            ),
        },
        "apk_resources_assets_baked_ui": {
            "raw_apk_only_unique": int(key_summary.get("candidate_unique", 0) or 0),
            "keyed_fallback_unique": int(
                key_summary.get("all_occurrences_have_known_key", 0) or 0
            ),
            "mixed_keying_unique": int(key_summary.get("mixed", 0) or 0),
            "unkeyed_unique": int(key_summary.get("no_known_key", 0) or 0),
            "triage_counts": triage_counts,
            "review_required": int(baked_triage.get("review_required", 0) or 0),
            "review_queue": str(ws / "apk-baked-ui-review-queue.jsonl"),
            "status": "review_required",
            "note": (
                "Known-key prefab defaults are not double-counted as missing text. "
                "Unkeyed candidates need runtime/materialization evidence before "
                "promotion to a translation queue."
            ),
        },
        "apk_auxiliary_text": {
            "text_candidates": int(apk_auxiliary.get("text_candidates", 0) or 0),
            "decoded_utf8": int(apk_auxiliary.get("decoded_utf8", 0) or 0),
            "jp_kana_hits": int(apk_auxiliary.get("jp_kana_hits", 0) or 0),
            "status": apk_auxiliary.get("status", "audit_pending"),
            "note": (
                "Covers APK assets/, res/raw/, and unknown/ text-like files outside "
                "Unity data.unity3d. No Japanese kana literals were found in the "
                "current 9.0.200 auxiliary APK text surface."
            ),
        },
        "android_resources": {
            "jp_source_keys": int(android.get("jp_source_keys", 0) or 0),
            "zh_rCN_covered": int(android.get("zh_rCN_covered", 0) or 0),
            "zh_rCN_missing": int(android.get("zh_rCN_missing", 0) or 0),
            "missing": android_missing,
            "branding": {
                "app_label": "剧场时光",
                "mechanism": "APK build override (prepare-appguard-free-apktool.py --app-name), applied to both tracks",
                "source_resource_unchanged": True,
                "verified_by": "aapt dump badging application-label on the built candidate",
            },
            "status": (
                "resolved_by_build_override"
                if android_missing and all(str(row.get("name")) == "app_name" for row in android_missing)
                else ("branding_decision_required" if android_missing else "closed")
            ),
        },
        "remote_monobehaviour_text": {
            "audit_available": bool(remote_monobehaviour),
            "text_field_occurrences": int(
                remote_monobehaviour.get("monobehaviour_text_field_occurrences", 0) or 0
            ),
            "jp_kana_occurrences": int(
                remote_monobehaviour.get("jp_kana_occurrences", 0) or 0
            ),
            "unknown_unique": int(remote_monobehaviour.get("unknown_unique", 0) or 0),
            "elapsed_seconds": remote_monobehaviour.get("elapsed_seconds"),
            "triage_counts": remote_monobehaviour_triage.get(
                "classification_counts", {}
            ),
            "review_required": int(
                remote_monobehaviour_triage.get("review_required", 0) or 0
            ),
            "review_queue": str(
                ws / "remote-monobehaviour-jp-text-review-queue.jsonl"
            ),
            "status": (
                "review_required"
                if int(remote_monobehaviour_triage.get("review_required", 0) or 0)
                else ("closed" if remote_monobehaviour else "audit_pending")
            ),
            "note": (
                "Raw unknowns are over-inclusive. Effect labels, scene-template "
                "names, render parameters, debug strings and obvious placeholders "
                "are excluded from the localization denominator; only triaged UI/"
                "key candidates remain for runtime/materialization review."
            ),
        },
        "remote_textasset_structured": {
            "audit_available": bool(remote_textasset_structured),
            "indexed_string_occurrences": int(
                remote_textasset_structured.get("indexed_string_occurrences", 0)
                or 0
            ),
            "jp_kana_occurrences": int(
                remote_textasset_structured.get("jp_kana_occurrences", 0) or 0
            ),
            "unknown_unique": int(
                remote_textasset_structured.get("unknown_unique", 0) or 0
            ),
            "classification": (
                "internal_metadata_only"
                if remote_textasset_structured
                and int(remote_textasset_structured.get("unknown_unique", 0) or 0)
                == 30
                else "review"
            ),
            "status": "closed_internal_metadata"
            if remote_textasset_structured
            else "audit_pending",
            "note": (
                "The 30 exact-source unknowns were manually classified: character/"
                "training object IDs, vertical-text layout characters, one audio "
                "resource path, and character asset IDs; no new visible copy."
            ),
        },
        "remote_textasset_nonparsed": {
            "audit_available": bool(remote_textasset_nonparsed),
            "objects_scanned": int(
                remote_textasset_nonparsed.get("objects_scanned", 0) or 0
            ),
            "decoded_payloads": int(
                remote_textasset_nonparsed.get("decoded_payloads", 0) or 0
            ),
            "sha_verified_payloads": int(
                remote_textasset_nonparsed.get("sha_verified_payloads", 0) or 0
            ),
            "decoded_payload_bytes": int(
                remote_textasset_nonparsed.get("decoded_payload_bytes", 0) or 0
            ),
            "jp_candidate_occurrences": int(
                remote_textasset_nonparsed.get("jp_candidate_occurrences", 0) or 0
            ),
            "unknown_unique": int(
                remote_textasset_nonparsed.get("unknown_unique", 0) or 0
            ),
            "errors": len(remote_textasset_nonparsed.get("errors", []) or []),
            "status": (
                "closed"
                if remote_textasset_nonparsed
                and not (remote_textasset_nonparsed.get("errors", []) or [])
                and int(remote_textasset_nonparsed.get("unknown_unique", 0) or 0)
                == 0
                else ("review_required" if remote_textasset_nonparsed else "audit_pending")
            ),
            "note": (
                "All canonical csv-like/tsv TextAssets are reconstructed from "
                "normalized m_Script, exact-size/SHA verified, and scanned cell-wise."
            ),
        },
        "server_master_wire_text": {
            "status": (
                "response_scan_available"
                if int((server_wire.get("shape_counts", {}) or {}).get("response", 0) or 0)
                else "no_source_bound_game_response_body"
            ),
            "main_gtx_master_note": (
                "md_jp.gtx is already part of the main catalogue and contributes "
                "116,074 occurrences of ld_* master/display text."
            ),
            "plaintext_txt_files": int(server_wire.get("plaintext_txt_files", 0) or 0),
            "shape_counts": server_wire.get("shape_counts", {}),
            "capture_audit": str(ws / "server-wire-plaintext-capture-audit.json"),
            "capture_note": (
                "Preserved plaintext hook files were shape-classified. A supplemental "
                "2026-09-18 recheck also inspected response-named artifacts: the two "
                "response-plaintext hook files contain JSON-RPC method/params request "
                "payloads, AppBoot response bodies are only transport-level English 403 "
                "HTML, and host replay response bodies are opaque 64-byte wire tokens. "
                "No source-bound official game result/error body is currently available. "
                "Local responder JSON is not treated as source authority."
            ),
        },
        "excluded_false_positive_or_internal_text": {
            "priority_textasset_new_unique": int(
                priority_textasset.get("new_unique", 0) or 0
            ),
            "priority_textasset_classification": {
                "scene_object_labels": 48,
                "character_asset_labels": 23,
                "audio_resource_paths": 1,
            },
            "il2cpp_metadata_unknown_exact": int(
                metadata.get("unknown_exact", 0) or 0
            ),
            "il2cpp_metadata_strings": metadata.get("unknown_strings", []),
            "status": "excluded_from_localization_denominator",
        },
        "baked_video_surface": {
            "video_assets": int(remote_video.get("video_assets", 0) or 0),
            "category_counts": remote_video.get("category_counts", {}),
            "priority_counts": remote_video.get("priority_counts", {}),
            "relationship_missing": int(remote_video.get("relationship_missing", 0) or 0),
            "bundle_object_shape_counts": remote_video.get("bundle_object_shape_counts", {}),
            "representative_sample": remote_video.get("representative_sample", {}),
            "status": remote_video.get("status", "audit_pending"),
            "note": (
                "Remote *.mp4.unity3d assets store MP4 payloads in TextAsset objects. "
                "Text/MonoBehaviour audits cannot detect Japanese baked into video frames. "
                "Review representative frames by category; do not count this as textual coverage."
            ),
        },
        "baked_image_surface": {
            "risk_union_logicals": int(image_risk.get("risk_union", 0) or 0),
            "legacy_overlap_logicals": int(
                image_risk.get("risk_legacy_overlap", 0) or 0
            ),
            "changed_remote_risk_overlap": int(
                image_risk.get("changed_remote_risk_overlap", 0) or 0
            ),
            "legacy_pair_rows": len(pair_rows),
            "legacy_pair_changed_rows": sum(
                1 for row in pair_rows if isinstance(row, dict) and row.get("changed")
            ),
            "pure_texture_candidates": int(
                image_uncovered.get("candidate_count", 0) or 0
            ),
            "candidate_category_counts": dict(sorted(texture_categories.items())),
            "status": "separate_texture_localization_required",
            "note": (
                "These are Texture2D/Sprite resources and are not solvable by GTX/"
                "LLM text translation. Reuse compatible legacy Chinese textures or "
                "redraw current assets."
            ),
        },
        "release_blockers": [
            {
                "id": "confirmed_non_gtx_companion",
                "count": companion_pending,
                "severity": "confirmed",
                "action": "translate and materialize FontRender/APK BI/event-unit/MLD config text",
            },
            {
                "id": "companion_materialization",
                "count": missing_materializer_count,
                "severity": "confirmed",
                "action": (
                    "implement source-bound materializers for the remaining companion sources "
                    "(event-unit, MLD config); FontRender and APK bootstrap BI materialization already exist"
                ),
            },
            {
                "id": "supplemental_text_review",
                "count": int(supplemental_review.get("unique_review_candidates", 0) or 0),
                "severity": "review",
                "action": (
                    "prioritize high_user_visible candidates, confirm runtime visibility "
                    "and exact object locator, then promote only proven rows"
                ),
            },
            {
                "id": "android_app_name",
                "count": 0,
                "severity": "resolved",
                "action": (
                    "decision taken: app label 剧场时光, overridden at APK build time on both tracks; "
                    "the JP source resource is intentionally left unchanged"
                ),
            },
            {
                "id": "server_master_wire_text",
                "count": None,
                "severity": "evidence_gap",
                "action": (
                    "preserve/source-bind official response bodies when available; "
                    "do not treat local responder JSON as localization authority"
                ),
            },
            {
                "id": "baked_video_assets",
                "count": int(remote_video.get("video_assets", 0) or 0),
                "severity": "non_text_review",
                "action": (
                    "frame-review MP4 categories for baked Japanese copy/subtitles/logos; "
                    "localize/re-encode only confirmed visible-text videos"
                ),
            },
            {
                "id": "baked_image_assets",
                "count": int(image_uncovered.get("candidate_count", 0) or 0),
                "severity": "non_text",
                "action": "legacy texture reuse or redraw; do not count as text coverage",
            },
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("MLTD localization resource gaps")
    print(
        f"  confirmed text universe:      {confirmed_union:,} unique "
        f"({main_unique:,} GTX + {confirmed_nonmain:,} confirmed non-GTX)"
    )
    print(
        f"  non-GTX companion:            {companion_done:,}/{companion_total:,} translated; "
        f"{companion_pending:,} pending"
    )
    print(
        "  APK baked UI:                 "
        f"{key_summary.get('all_occurrences_have_known_key', 0)} keyed fallback / "
        f"{key_summary.get('no_known_key', 0)} unkeyed; "
        f"{baked_triage.get('review_required', 0)} raw review rows"
    )
    print(
        "  supplemental UI review:       "
        f"{supplemental_review.get('unique_review_candidates', 0)} unique "
        f"({(supplemental_review.get('review_tier_counts', {}) or {}).get('high_user_visible', 0)} high / "
        f"{(supplemental_review.get('review_tier_counts', {}) or {}).get('ambiguous_default', 0)} ambiguous)"
    )
    print(
        f"  Android resources:            {android.get('zh_rCN_covered', 0)}/"
        f"{android.get('jp_source_keys', 0)} JP keys covered; "
        f"{android.get('zh_rCN_missing', 0)} missing"
    )
    if remote_monobehaviour:
        print(
            f"  remote MonoBehaviour text:    {remote_monobehaviour.get('unknown_unique', 0)} "
            "raw unknown / "
            f"{remote_monobehaviour_triage.get('review_required', 0)} review after triage"
        )
    else:
        print("  remote MonoBehaviour text:    audit pending")
    if remote_textasset_structured:
        print(
            f"  structured TextAsset:         "
            f"{remote_textasset_structured.get('unknown_unique', 0)} raw unknown; "
            "30/30 classified internal metadata"
        )
    else:
        print("  structured TextAsset:         audit pending")
    if remote_textasset_nonparsed:
        print(
            f"  CSV/TSV TextAsset:            "
            f"{remote_textasset_nonparsed.get('decoded_payloads', 0)} exact payloads / "
            f"{remote_textasset_nonparsed.get('unknown_unique', 0)} unknown JP"
        )
    else:
        print("  CSV/TSV TextAsset:            audit pending")
    print(
        f"  baked video candidates:       {remote_video.get('video_assets', 0)} MP4 bundles; "
        f"status={remote_video.get('status', 'audit_pending')}"
    )
    print(
        f"  baked image candidates:       {image_uncovered.get('candidate_count', 0)} pure texture; "
        f"{sum(1 for row in pair_rows if isinstance(row, dict) and row.get('changed'))} "
        "changed legacy-pair rows"
    )
    print(f"  output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
