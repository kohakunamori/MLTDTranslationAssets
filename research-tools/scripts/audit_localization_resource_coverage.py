#!/usr/bin/env python3
"""Report confirmed MLTD localization resource surfaces beyond the GTX catalogue.

This is an audit/reporting tool only. It never mutates translation queues or
assets. Textual surfaces are deduplicated by source_sha256 so the GTX, FontRender,
APK bootstrap, and non-GTX event-unit inventories can be compared without
double-counting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            yield row


def source_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_ids(path: Path) -> set[str]:
    result: set[str] = set()
    for row in iter_jsonl(path) or ():
        sid = str(row.get("source_sha256", "")).strip()
        source = str(row.get("source", ""))
        if not sid and source:
            sid = source_id(source)
        if sid:
            result.add(sid)
    return result


def build_report(workspace: Path) -> dict:
    main = source_ids(workspace / "translation-memory.jsonl")
    fontrender = source_ids(workspace / "fontrender-translation-queue.jsonl")
    apk_bi_missing_main = source_ids(workspace / "apk-bi-jp-translation-queue.jsonl")
    event_unit = source_ids(workspace / "event-unit-translation-queue.jsonl")
    mld_config = source_ids(workspace / "mld-translation-queue.jsonl")

    surfaces = {
        "gtx_main": main,
        "fontrender": fontrender,
        "apk_bootstrap_bi_missing_main": apk_bi_missing_main,
        "event_unit": event_unit,
        "mld_config": mld_config,
    }

    union: set[str] = set()
    sequential: dict[str, dict] = {}
    for name, ids in surfaces.items():
        new = ids - union
        sequential[name] = {
            "unique": len(ids),
            "new_vs_previous_union": len(new),
            "overlap_previous_union": len(ids) - len(new),
        }
        union.update(ids)

    overlap = {}
    names = list(surfaces)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            overlap[f"{left}__{right}"] = len(surfaces[left] & surfaces[right])

    apk_bi = read_json(workspace / "apk-bi-jp-coverage-audit.json")
    apk_mono = read_json(workspace / "apk-monobehaviour-jp-string-audit.json")
    apk_bmd = read_json(workspace / "apk-bmd-text-audit.json")
    event_summary = read_json(workspace / "event-unit-localization-summary.json")
    event_missing = read_json(workspace / "event-unit-missing-main-summary.json")
    baked_risk = read_json(workspace / "nongtx-image-risk-manifest-audit.json")
    baked_pair = read_json(workspace / "nongtx-texture-legacy-pair-audit.json")
    baked_uncovered = read_json(workspace / "nongtx-uncovered-object-classification.json")
    family_scan = read_json(workspace / "nongtx-textasset-family-scan.json")

    app_name = "ミリシタ"
    app_name_sid = source_id(app_name)

    return {
        "schema_version": 1,
        "kind": "mltd-localization-resource-coverage-audit",
        "workspace": str(workspace),
        "textual_surfaces": {
            "main_gtx_unique": len(main),
            "confirmed_union_unique": len(union),
            "confirmed_nonmain_unique": len(union - main),
            "apk_app_name": {
                "source": app_name,
                "source_sha256": app_name_sid,
                "in_main_gtx": app_name_sid in main,
                "classification": "branding_policy_required",
            },
            "surfaces": sequential,
            "pairwise_overlap": overlap,
        },
        "apk": {
            "bootstrap_bi": {
                "records": apk_bi.get("records"),
                "unique_sources": apk_bi.get("unique_source_values"),
                "overlap_main_unique": apk_bi.get("overlap_main_unique"),
                "missing_main_unique": apk_bi.get("missing_main_unique"),
            },
            "monobehaviour_scan": {
                "objects_scanned": apk_mono.get("objects_scanned"),
                "raw_candidates": apk_mono.get("raw_candidates"),
                "typetree_ok": apk_mono.get("typetree_ok"),
                "objects_with_jp_strings": apk_mono.get("objects_with_jp_strings"),
            },
            "bmd_textasset": {
                "jp_like_lines": apk_bmd.get("jp_like_lines"),
                "unique_jp_like_lines": apk_bmd.get("unique_jp_like_lines"),
            },
        },
        "event_unit": {
            "candidate_textassets": event_summary.get("candidate_textassets"),
            "occurrences": event_summary.get("occurrences"),
            "unique_sources": event_summary.get("unique_sources"),
            "new_vs_main": event_missing.get("new_unique"),
            "categories": event_missing.get("categories", {}),
            "errors": event_summary.get("errors"),
        },
        "baked_images": {
            "changed_remote_risk_overlap": baked_risk.get("changed_remote_risk_overlap"),
            "legacy_pair_rows": baked_pair.get("selected") or baked_pair.get("rows"),
            "previous_pair_audit_rows": len(baked_pair.get("rows", []))
            if isinstance(baked_pair.get("rows"), list)
            else None,
            "previously_uncovered_classified": baked_uncovered.get("candidate_count"),
            "classification_loaded": baked_uncovered.get("loaded"),
            "classification_errors": len(baked_uncovered.get("errors", []))
            if isinstance(baked_uncovered.get("errors"), list)
            else None,
            "classification_summary": baked_uncovered.get("summary", {}),
        },
        "nongtx_textasset_family_sampling": {
            "available": bool(family_scan),
            "family_groups": family_scan.get("family_groups"),
            "sample_rows": family_scan.get("sample_rows"),
            "samples_with_jp": family_scan.get("samples_with_jp"),
            "groups_with_jp": family_scan.get("groups_with_jp"),
            "errors": family_scan.get("errors"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workspace",
        type=Path,
        default=Path("build/localization-90200"),
    )
    ap.add_argument("--output", type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_report(args.workspace)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    t = report["textual_surfaces"]
    e = report["event_unit"]
    a = report["apk"]
    b = report["baked_images"]
    f = report["nongtx_textasset_family_sampling"]
    print("MLTD localization resource coverage")
    print(f"  GTX source universe:       {t['main_gtx_unique']:,}")
    print(f"  confirmed text union:      {t['confirmed_union_unique']:,}")
    print(f"  confirmed non-GTX unique:  {t['confirmed_nonmain_unique']:,}")
    for name, row in t["surfaces"].items():
        print(
            f"  {name:24s} {row['unique']:,} unique "
            f"(+{row['new_vs_previous_union']:,} new)"
        )
    print(
        f"  event_unit:               {e.get('occurrences', 0):,} occurrences / "
        f"{e.get('unique_sources', 0):,} unique / +{e.get('new_vs_main', 0):,} vs GTX"
    )
    print(
        f"  APK BI:                   {a['bootstrap_bi'].get('unique_sources', 0):,} unique / "
        f"+{a['bootstrap_bi'].get('missing_main_unique', 0):,} vs GTX"
    )
    print(
        f"  APK MonoBehaviour JP:     {a['monobehaviour_scan'].get('objects_with_jp_strings', 0)} "
        f"after {a['monobehaviour_scan'].get('objects_scanned', 0):,} objects"
    )
    print(
        f"  baked texture risk:       {b.get('changed_remote_risk_overlap', 0)} changed logicals; "
        f"{b.get('previously_uncovered_classified', 0)} formerly-uncovered classified"
    )
    if f["available"]:
        print(
            f"  TextAsset family sample:  {f.get('sample_rows', 0):,} rows / "
            f"{f.get('groups_with_jp', 0)} groups with JP"
        )
    else:
        print("  TextAsset family sample:  still running / not imported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
