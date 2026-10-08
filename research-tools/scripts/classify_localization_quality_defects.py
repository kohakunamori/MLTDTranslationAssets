#!/usr/bin/env python3
"""Classify MLTD localization quality defects and decide surface-level reflow.

Inputs: one or more human/model judgement JSONL files produced from a sample list
(``scripts/sample_localization_quality.py`` -> ``samples.jsonl``).  Each judgement
row carries ``source_sha256`` plus either a defect class or an explicit pass.

Fixed defect vocabulary (stable ASCII slug + Chinese label, never free text):

    semantic_mistranslation        语义错译
    terminology_inconsistency      术语不一致
    missing_glyph_tofu             字符缺失(豆腐块)
    layout_overflow                排版溢出
    language_residue               语言残留(繁中/日文)
    image_breakage                 画面破坏
    prompt_scope_violation         提示词越权改动

Rules (per surface, per defect class):

* rate = defective rows of that class / judged rows of that surface;
* any defect class seen **>= --systemic-threshold (3)** times in the sample sets
  ``systemic_defect: true`` and ``must_pause_surface: true``  -> the surface must
  pause and the pipeline (prompt / glossary / materializer) must be fixed before
  more rows are produced;
* any defect rate **> --rate-threshold (0.05)** sets ``promotion_blocked: true``
  -> the surface may not be promoted or published;
* each flagged class gets a ready-to-fill "pipeline change record"
  ``{defect_class, root_cause, pipeline_change_before, pipeline_change_after,
  rerun_scope, resample_result}`` under ``reflow_records`` in the output.

This tool writes only to ``--out-dir`` (never the production directory) and never
calls an API.  Exit codes: 0 clean, 1 promotion blocked, 2 invalid input,
4 surface must pause (systemic defect).  4 wins over 1 when both apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]

SCHEMA = "mltd-localization-quality-defects/v1"
DEFAULT_SYSTEMIC_THRESHOLD = 3
DEFAULT_RATE_THRESHOLD = 0.05
PRODUCTION_DIR = REPO / "build" / "localization-90200"

# Fixed vocabulary: ASCII slug -> Chinese label.  Order is stable and used in output.
DEFECT_CLASSES: dict[str, str] = {
    "semantic_mistranslation": "语义错译",
    "terminology_inconsistency": "术语不一致",
    "missing_glyph_tofu": "字符缺失(豆腐块)",
    "layout_overflow": "排版溢出",
    "language_residue": "语言残留(繁中/日文)",
    "image_breakage": "画面破坏",
    "prompt_scope_violation": "提示词越权改动",
}
DEFECT_SLUGS = tuple(DEFECT_CLASSES)
# Accepted spellings from human/model judgement rows -> slug.
DEFECT_ALIASES: dict[str, str] = {
    **{slug: slug for slug in DEFECT_SLUGS},
    "semantic_error": "semantic_mistranslation",
    "mistranslation": "semantic_mistranslation",
    "wrong_meaning": "semantic_mistranslation",
    "语义错译": "semantic_mistranslation",
    "terminology": "terminology_inconsistency",
    "term_inconsistency": "terminology_inconsistency",
    "inconsistent_terminology": "terminology_inconsistency",
    "术语不一致": "terminology_inconsistency",
    "tofu": "missing_glyph_tofu",
    "glyph_missing": "missing_glyph_tofu",
    "missing_glyph": "missing_glyph_tofu",
    "missing_char": "missing_glyph_tofu",
    "字符缺失": "missing_glyph_tofu",
    "豆腐块": "missing_glyph_tofu",
    "overflow": "layout_overflow",
    "text_overflow": "layout_overflow",
    "layout": "layout_overflow",
    "排版溢出": "layout_overflow",
    "traditional_chinese_residue": "language_residue",
    "japanese_residue": "language_residue",
    "residue": "language_residue",
    "语言残留": "language_residue",
    "image_damage": "image_breakage",
    "art_damage": "image_breakage",
    "broken_image": "image_breakage",
    "画面破坏": "image_breakage",
    "scope_violation": "prompt_scope_violation",
    "scope_creep": "prompt_scope_violation",
    "提示词越权改动": "prompt_scope_violation",
}
PASS_VALUES = {"ok", "pass", "passed", "clean", "none", "good", "无", "通过"}
DEFECT_FIELDS = ("defect_class", "defect_classes", "defects", "class", "classes", "defect")
JUDGEMENT_FIELDS = ("judgement", "judgment", "verdict", "result", "status")
SURFACE_FIELDS = ("surface", "surface_id", "panel_surface")
ID_FIELDS = ("source_sha256", "sample_id", "id", "composite_sha256")

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_INVALID = 2
EXIT_PAUSE = 4


class InvalidInput(ValueError):
    pass


# --------------------------------------------------------------------------- io

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO).as_posix()
    except ValueError:
        return str(resolved)


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InvalidInput(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise InvalidInput(f"{path}:{line_no}: expected a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def is_under_production(path: Path) -> bool:
    resolved, prod = path.resolve(), PRODUCTION_DIR.resolve()
    return resolved == prod or prod in resolved.parents


# ------------------------------------------------------------------- judgement

def judge_identity(row: dict, label: str, index: int) -> str:
    for field in ID_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise InvalidInput(f"{label}:{index}: judgement row without a source id "
                       f"(any of {', '.join(ID_FIELDS)})")


def normalize_class(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    slug = DEFECT_ALIASES.get(text.casefold()) or DEFECT_ALIASES.get(text)
    if slug is None:
        raise InvalidInput(
            f"unknown defect class {text!r}; use one of {', '.join(DEFECT_SLUGS)} "
            "or the Chinese label from DEFECT_CLASSES")
    return slug


def defect_classes_of(row: dict) -> list[str]:
    found: list[str] = []
    for field in DEFECT_FIELDS:
        if field not in row:
            continue
        value = row[field]
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                item = item.get("class") or item.get("defect_class") or item.get("label")
            slug = normalize_class(item)
            if slug and slug not in found:
                found.append(slug)
    # An explicit judgement of defect without a class is not silently a pass.
    judgement = None
    for field in JUDGEMENT_FIELDS:
        if field in row and row[field] is not None:
            judgement = str(row[field]).strip().casefold()
            break
    if judgement and judgement not in PASS_VALUES and not found:
        alias = DEFECT_ALIASES.get(judgement.casefold()) or DEFECT_ALIASES.get(judgement)
        if alias:
            found.append(alias)
        else:
            raise InvalidInput(
                f"judgement {judgement!r} is neither a pass ({'/'.join(sorted(PASS_VALUES))}) "
                f"nor a defect class ({', '.join(DEFECT_SLUGS)}).  The release ledger's coarse "
                "ok/minor/critical vocabulary is deliberately NOT accepted here: 'critical' "
                "spans several defect classes (meaning error / omission / control-code damage / "
                "wrong speaker), so a class must be stated explicitly instead of guessed")
    return found


def surface_of(row: dict, override: str | None, default: str) -> str:
    if override:
        return override
    for field in SURFACE_FIELDS:
        value = row.get(field)
        if value not in (None, ""):
            return str(value)
    return default


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judgements", type=Path, action="append", default=[],
                    help="judgement JSONL (human or model); repeatable")
    ap.add_argument("--sample", type=Path, action="append", default=[],
                    help="samples.jsonl from sample_localization_quality.py; repeatable. "
                         "Optional: used to report judgement coverage of the sample")
    ap.add_argument("--surface", default=None,
                    help="force one surface label when judgement rows carry none")
    ap.add_argument("--surface-map", type=Path, default=None,
                    help="JSON file mapping source id -> surface, for judgement rows without one")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--systemic-threshold", type=int, default=DEFAULT_SYSTEMIC_THRESHOLD)
    ap.add_argument("--rate-threshold", type=float, default=DEFAULT_RATE_THRESHOLD)
    ap.add_argument("--json", action="store_true", help="print the report JSON to stdout")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return run(args)
    except InvalidInput as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return EXIT_INVALID


def run(args: argparse.Namespace) -> int:
    if not args.judgements:
        raise InvalidInput("--judgements is required")
    out_dir: Path = args.out_dir
    if is_under_production(out_dir):
        raise InvalidInput(f"refusing to write under the production directory: {out_dir}")
    if args.systemic_threshold < 1:
        raise InvalidInput("--systemic-threshold must be >= 1")
    if args.rate_threshold <= 0:
        raise InvalidInput("--rate-threshold must be > 0")

    surface_map: dict[str, str] = {}
    if args.surface_map is not None:
        if not args.surface_map.is_file():
            raise InvalidInput(f"--surface-map missing: {args.surface_map}")
        raw_map = json.loads(args.surface_map.read_text(encoding="utf-8"))
        if not isinstance(raw_map, dict):
            raise InvalidInput("--surface-map must be a JSON object of source id -> surface")
        surface_map = {str(key): str(value) for key, value in raw_map.items()}

    inputs: dict[str, dict] = {}
    per_surface: dict[str, dict] = {}
    seen_ids: dict[str, str] = {}
    duplicate_rows = 0
    total_rows = 0

    for path in args.judgements:
        if not path.is_file():
            raise InvalidInput(f"judgement input missing: {path}")
        label = display_path(path)
        rows = read_jsonl(path)
        inputs.setdefault("judgements", [])
        inputs["judgements"] = list(inputs.get("judgements", [])) + [{
            "path": label, "sha256": sha256_file(path), "rows": len(rows)}]
        for index, row in enumerate(rows, 1):
            total_rows += 1
            sid = judge_identity(row, label, index)
            if sid in seen_ids:
                # Same sample judged twice: keep the first, count the duplicate.
                duplicate_rows += 1
                continue
            seen_ids[sid] = label
            default_surface = path.stem
            surface = surface_of(row, args.surface, surface_map.get(sid, default_surface))
            classes = defect_classes_of(row)
            bucket = per_surface.setdefault(surface, {
                "judged_rows": 0, "clean_rows": 0,
                "defect_counts": {slug: 0 for slug in DEFECT_SLUGS},
                "defect_counts_within_row": 0,
                "defective_rows": 0,
                "example_ids": {slug: [] for slug in DEFECT_SLUGS},
                "sources": []})
            if label not in bucket["sources"]:
                bucket["sources"].append(label)
            bucket["judged_rows"] += 1
            if classes:
                bucket["defective_rows"] += 1
                bucket["defect_counts_within_row"] += len(classes)
                for slug in classes:
                    bucket["defect_counts"][slug] += 1
                    if len(bucket["example_ids"][slug]) < 5:
                        bucket["example_ids"][slug].append(sid)
            else:
                bucket["clean_rows"] += 1

    sample_ids: set[str] = set()
    for path in args.sample:
        if not path.is_file():
            raise InvalidInput(f"--sample missing: {path}")
        label = display_path(path)
        rows = read_jsonl(path)
        inputs.setdefault("samples", [])
        inputs["samples"] = list(inputs.get("samples", [])) + [{
            "path": label, "sha256": sha256_file(path), "rows": len(rows)}]
        for row in rows:
            sid = str(row.get("source_sha256", "") or row.get("composite_sha256", "")).strip()
            if not sid:
                raise InvalidInput(f"{label}: sample row without source_sha256/composite_sha256")
            sample_ids.add(sid)
            surface = surface_of(row, args.surface, path.stem)
            bucket = per_surface.setdefault(surface, {
                "judged_rows": 0, "clean_rows": 0,
                "defect_counts": {slug: 0 for slug in DEFECT_SLUGS},
                "defect_counts_within_row": 0, "defective_rows": 0,
                "example_ids": {slug: [] for slug in DEFECT_SLUGS},
                "sources": []})
            bucket["sampled_rows"] = bucket.get("sampled_rows", 0) + 1

    if not seen_ids:
        raise InvalidInput("no judgement rows were read")

    surfaces: dict[str, dict] = {}
    systemic_pairs: list[dict] = []
    blocked_pairs: list[dict] = []
    reflow_records: list[dict] = []
    any_pause = False
    any_blocked = False

    for surface in sorted(per_surface):
        bucket = per_surface[surface]
        judged = bucket["judged_rows"]
        classes: dict[str, dict] = {}
        systemic = False
        blocked = False
        for slug in DEFECT_SLUGS:
            count = bucket["defect_counts"][slug]
            rate = (count / judged) if judged else 0.0
            is_systemic = count >= args.systemic_threshold
            over_rate = rate > args.rate_threshold
            if is_systemic:
                systemic = True
            if over_rate:
                blocked = True
            classes[slug] = {
                "label": DEFECT_CLASSES[slug],
                "count": count,
                "rate": round(rate, 6),
                "systemic_defect": is_systemic,
                "over_rate_threshold": over_rate,
                "example_ids": bucket["example_ids"][slug],
            }
            if is_systemic or over_rate:
                record = {
                    "surface": surface,
                    "defect_class": slug,
                    "defect_label": DEFECT_CLASSES[slug],
                    "defect_count": count,
                    "defect_rate": round(rate, 6),
                    "systemic_defect": is_systemic,
                    "promotion_blocked": over_rate,
                    "example_ids": bucket["example_ids"][slug],
                    "root_cause": None,
                    "pipeline_change_before": None,
                    "pipeline_change_after": None,
                    "rerun_scope": None,
                    "resample_result": None,
                    "resample_instruction": ("fix the pipeline, rerun sample_localization_quality.py "
                                             "with the SAME --seed, re-judge, then record the new "
                                             "count/rate here"),
                    "fields_required_before_rerun": [
                        "root_cause", "pipeline_change_before", "pipeline_change_after",
                        "rerun_scope"],
                    "next_step": ("pause this surface, fix the pipeline, then rerun the sampler "
                                  "with the same --seed and record resample_result"),
                }
                reflow_records.append(record)
            if is_systemic:
                systemic_pairs.append({"surface": surface, "defect_class": slug, "count": count})
            if over_rate:
                blocked_pairs.append({"surface": surface, "defect_class": slug,
                                      "rate": round(rate, 6)})

        sampled = bucket.get("sampled_rows")
        surfaces[surface] = {
            "judged_rows": judged,
            "clean_rows": bucket["clean_rows"],
            "defective_rows": bucket["defective_rows"],
            "overall_defect_rate": round(bucket["defective_rows"] / judged, 6) if judged else 0.0,
            "sampled_rows": sampled,
            "judgement_coverage": (round(judged / sampled, 6)
                                   if isinstance(sampled, int) and sampled else None),
            "sources": bucket["sources"],
            "classes": classes,
            "systemic_defect": systemic,
            "must_pause_surface": systemic,
            "promotion_blocked": blocked,
            "reflow_required": systemic or blocked,
        }
        any_pause = any_pause or systemic
        any_blocked = any_blocked or blocked

    judged_ids = set(seen_ids)
    sample_ids_all = set(sample_ids)
    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": {
            "defect_classes": {slug: DEFECT_CLASSES[slug] for slug in DEFECT_SLUGS},
            "systemic_threshold": args.systemic_threshold,
            "systemic_rule": ("a defect class seen >= systemic_threshold times in the sample "
                              "sets systemic_defect=true and must_pause_surface=true"),
            "rate_threshold": args.rate_threshold,
            "rate_rule": ("any defect rate > rate_threshold sets promotion_blocked=true for "
                          "that surface"),
            "pause_beats_block": "when a surface is both blocked and paused, pause is the "
                                 "binding state (exit 4)",
            "human_verdict_authority": ("the ledger's own human audit rules "
                                        "(critical=0 and minor<=4%) remain authoritative for "
                                        "release_ready; this report is the defect taxonomy and "
                                        "reflow trigger, not a replacement for it"),
            "defect_lines_are_source_bound": ("every judgement row must carry a sample source id; "
                                              "judgements are counted once per source id"),
            "zero_defects_is_not_a_pass": ("an unjudged sampled row is never counted as clean"),
        },
        "thresholds": {"systemic": args.systemic_threshold, "rate": args.rate_threshold},
        "inputs": inputs,
        "counts": {
            "judgement_rows_read": total_rows,
            "judgement_rows_used": total_rows - duplicate_rows,
            "duplicate_judgement_rows_ignored": duplicate_rows,
            "surfaces": len(surfaces),
            "sampled_ids_referenced": len(sample_ids_all),
        },
        "coverage": {
            "sample_rows_referenced": len(sample_ids_all),
            "sample_rows_judged": len(sample_ids_all & judged_ids),
            "sample_rows_without_judgement": len(sample_ids_all - judged_ids),
            "judged_rows_outside_sample": len(judged_ids - sample_ids_all) if sample_ids_all else 0,
            "rule": ("a sample row with no judgement is NOT a pass; the surface stays "
                     "unfinished until every sampled row has a judgement"),
            "unjudged_sample_ids": sorted(sample_ids_all - judged_ids)[:50],
        },
        "surfaces": surfaces,
        "systemic_defects": systemic_pairs,
        "promotion_blocks": blocked_pairs,
        "reflow_records": reflow_records,
        "summary": {
            "surfaces_judged": len(surfaces),
            "surfaces_paused": [name for name, block in surfaces.items()
                                if block["must_pause_surface"]],
            "surfaces_blocked": [name for name, block in surfaces.items()
                                 if block["promotion_blocked"]],
            "verdict": ("pause_surface_and_fix_pipeline" if any_pause
                        else ("promotion_blocked" if any_blocked else "clean")),
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "defect-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    reflow_path = out_dir / "reflow-records.jsonl"
    write_jsonl(reflow_path, reflow_records)
    report["outputs"] = {
        "report": {"path": display_path(report_path), "sha256": sha256_file(report_path)},
        "reflow_records": {"path": display_path(reflow_path),
                           "sha256": sha256_file(reflow_path), "rows": len(reflow_records)},
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"schema": report["schema"], "counts": report["counts"],
                          "summary": report["summary"],
                          "reflow_records": len(reflow_records),
                          "report": display_path(report_path)},
                         ensure_ascii=False, indent=2))
    if any_pause:
        return EXIT_PAUSE
    if any_blocked:
        return EXIT_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
