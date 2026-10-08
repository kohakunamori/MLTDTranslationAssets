#!/usr/bin/env python3
"""Unified reproducible quality sampler for MLTD localization surfaces (text + image).

Purpose: every surface must "sample first, then be allowed to promote", so the
sample has to be reproducible, source-bound and risk-stratified.  This tool only
produces the *sample list*; it never judges quality and never calls any API.

Text mode (``--translations <path>:<surface-field>``, repeatable):

* per surface at least ``--min-rows`` (default 200) rows; a surface smaller than
  that is taken in full;
* risk strata with the same quota as ``docs/LOCALIZATION_RELEASE_LEDGER.md``
  (low 40 / medium 40 / high 60 / critical 60); a stratum smaller than its quota
  is taken in full;
* ``high`` / ``critical`` are covered 100% by default (``--high-critical-coverage
  full``); pass ``quota`` to fall back to the ledger's literal 60-row quota;
* every row whose *two* independent reviews both PASS with differing scores is
  added on top of the quota and never consumes a stratified seat;
* a fixed ``--seed`` (default ``1077100``) makes the draw reproducible; the seed
  and the full sample list are written to ``--out-dir``.

Image mode (``--image-inventory <jsonl>``):

* per surface sample ``>= --image-min-count`` (30) rows **and** ``>= --image-
  min-share`` (5%) of the new edits, never fewer than ``--image-floor`` (20) when
  the surface has that many; surfaces smaller than the target are taken in full;
* label-priority coverage for longest-text panel, first/last panel of a
  multi-panel comic, small-font panel and panels with character names /
  onomatopoeia (see ``LABEL_CLASSES``); rows carrying any priority label are
  always included, so coverage cannot silently degrade;
* when the inventory cannot supply a priority label the tool **reports which
  label is missing** (``sample-manifest.json`` -> ``image.label_report``, plus a
  stderr warning) instead of silently dropping the rule.  ``--require-labels``
  turns that report into a non-zero exit code.

Outputs (always inside ``--out-dir``, never the production directory):
``samples.jsonl`` (per row: ``surface`` / ``stratum`` / ``risk`` / ``seed`` /
``source_sha256``) and ``sample-manifest.json`` (seed, per-stratum counts,
coverage, dropped items, label report).

Exit codes: 0 ok, 2 invalid input, 3 ``--require-labels`` was passed and a
required image label class is missing (outputs are still written: they are the
report).  The production directory ``build/localization-90200`` is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]

SCHEMA = "mltd-localization-quality-sample/v1"
DEFAULT_SEED = 1077100
DEFAULT_SURFACE_FIELD = "surface"
DEFAULT_RISK_FILE = "build/localization-90200/translation-risk.jsonl"
PRODUCTION_DIR = REPO / "build" / "localization-90200"

RISK_LEVELS = ("low", "medium", "high", "critical")
RISK_ORDER = {level: index for index, level in enumerate(RISK_LEVELS)}
DEFAULT_QUOTA = {"low": 40, "medium": 40, "high": 60, "critical": 60}
FULL_COVERAGE_LEVELS = ("high", "critical")
DEFAULT_MIN_ROWS = 200
QUOTA_FILL_REASON = "fill_to_minimum"

IMAGE_MIN_COUNT = 30
IMAGE_MIN_SHARE = 0.05
IMAGE_FLOOR = 20
# Priority label classes required by the audit rule (stable ASCII slugs).
LABEL_CLASSES = (
    "longest_text_panel",
    "multipanel_first_panel",
    "multipanel_last_panel",
    "small_font_panel",
    "character_name_or_onomatopoeia",
)
# Accepted spelling of a label, normalised to the slug above.
LABEL_ALIASES = {
    "longest_text_panel": "longest_text_panel",
    "longest_text": "longest_text_panel",
    "longest-panel": "longest_text_panel",
    "longest_panel": "longest_text_panel",
    "text_heavy": "longest_text_panel",
    "most_text": "longest_text_panel",
    "multipanel_first_panel": "multipanel_first_panel",
    "first_panel": "multipanel_first_panel",
    "panel_first": "multipanel_first_panel",
    "panel_0": "multipanel_first_panel",
    "multipanel_last_panel": "multipanel_last_panel",
    "last_panel": "multipanel_last_panel",
    "panel_last": "multipanel_last_panel",
    "multipanel": "multipanel_last_panel",
    "small_font_panel": "small_font_panel",
    "small_font": "small_font_panel",
    "small_text": "small_font_panel",
    "tiny_font": "small_font_panel",
    "character_name_or_onomatopoeia": "character_name_or_onomatopoeia",
    "character_name": "character_name_or_onomatopoeia",
    "charaname": "character_name_or_onomatopoeia",
    "onomatopoeia": "character_name_or_onomatopoeia",
    "sfx": "character_name_or_onomatopoeia",
    "onomatope": "character_name_or_onomatopoeia",
}
# Panel-flag booleans accepted from an inventory's ``--label-flags-field`` object.
LABEL_FLAG_KEYS = {
    "longest_text": "longest_text_panel",
    "longest_text_panel": "longest_text_panel",
    "first_panel": "multipanel_first_panel",
    "is_first_panel": "multipanel_first_panel",
    "last_panel": "multipanel_last_panel",
    "is_last_panel": "multipanel_last_panel",
    "small_font": "small_font_panel",
    "small_font_panel": "small_font_panel",
    "character_name": "character_name_or_onomatopoeia",
    "onomatopoeia": "character_name_or_onomatopoeia",
}
# Locator keys copied verbatim from an image inventory row for the human reviewer.
IMAGE_LOCATOR_KEYS = (
    "original", "original_png", "edited", "edited_png", "restored_png", "composite_png",
    "bundle", "remote", "texture_id", "texture_path_id", "review_status", "size",
    "region_map", "classification", "source_ids", "task_id",
)
SHA_FIELD_PREFERENCE = ("source_sha256", "composite_sha256", "restored_png_sha256", "sha256")

EXIT_OK = 0
EXIT_LABELS_MISSING = 3
EXIT_INVALID = 2


class InvalidInput(ValueError):
    pass


# --------------------------------------------------------------------------- io

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


# ------------------------------------------------------------------------ setup

def parse_translation_spec(spec: str) -> tuple[Path, str]:
    """``<path>[:<surface-field>]``; a Windows drive letter is never a separator."""
    if ":" in spec:
        head, tail = spec.rsplit(":", 1)
        if tail and not any(ch in tail for ch in "\\/"):
            return Path(head), tail
    return Path(spec), DEFAULT_SURFACE_FIELD


def parse_quota(text: str) -> dict[str, int]:
    quota = dict(DEFAULT_QUOTA)
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise InvalidInput(f"--quota entry must be <level>=<count>: {part!r}")
        level, count = (item.strip() for item in part.split("=", 1))
        if level not in DEFAULT_QUOTA:
            raise InvalidInput(f"unknown risk level in --quota: {level!r}")
        try:
            value = int(count)
        except ValueError as exc:
            raise InvalidInput(f"invalid --quota count for {level}: {count!r}") from exc
        if value < 0:
            raise InvalidInput(f"--quota count must be >= 0 for {level}")
        quota[level] = value
    return quota


def derive_surface_seed(seed: int, surface: str, mode: str) -> int:
    """Process-stable per-surface RNG seed (never Python's salted str hash)."""
    material = f"{seed}|{mode}|{surface}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def load_risk_index(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_file():
        raise InvalidInput(f"risk index missing: {path}")
    index: dict[str, str] = {}
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", ""))
        level = str(row.get("risk_level", "")).strip().lower()
        if not sid:
            raise InvalidInput(f"{path}: risk row without source_sha256")
        if level not in RISK_LEVELS:
            raise InvalidInput(f"{path}: invalid risk_level for {sid}: {level!r}")
        if sid in index:
            raise InvalidInput(f"{path}: duplicate risk source_sha256 {sid}")
        index[sid] = level
    return index


def row_identity(row: dict, label: str) -> tuple[str, str, str]:
    """(source_sha256, sid_source, source) with the ledger's source-bound rule."""
    source = row.get("source")
    source = str(source) if isinstance(source, str) else ""
    sid = str(row.get("source_sha256", "") or "").strip()
    if sid and source:
        if sha256_text(source) != sid:
            raise InvalidInput(f"{label}: source_sha256 is not the SHA-256 of source: {sid}")
        return sid, "row", source
    if sid:
        return sid, "row", source
    if source:
        return sha256_text(source), "derived_from_source", source
    raise InvalidInput(f"{label}: row has neither source_sha256 nor source")


def row_risk(row: dict, risk_index: dict[str, str], sid: str) -> tuple[str, str]:
    """(risk_level, risk_source).  Queue-authority index wins over an inline field."""
    if sid in risk_index:
        return risk_index[sid], "index"
    inline = row.get("risk")
    if isinstance(inline, dict):
        level = str(inline.get("risk_level", "")).strip().lower()
        if level in RISK_LEVELS:
            return level, "row"
        return "low", "default"
    if isinstance(inline, str) and inline.strip().lower() in RISK_LEVELS:
        return inline.strip().lower(), "row"
    return "low", "default"


def score_disagreement(row: dict) -> bool:
    """Rows whose two independent reviews both PASS but score differently.

    Mirrors ``build_translation_release_ledger.score_disagreement``; also accepts a
    producer-set ``score_disagreement: true`` flag on the row itself.
    """
    if row.get("score_disagreement") is True:
        return True
    first, second = row.get("review"), row.get("second_review")
    if not (isinstance(first, dict) and isinstance(second, dict)):
        return False
    for review in (first, second):
        if str(review.get("verdict", "")).upper() != "PASS":
            return False
    return first.get("scores") != second.get("scores")


# ----------------------------------------------------------------- text sample

def sample_text_surface(surface: str, rows: list[dict], quota: dict[str, int], seed: int,
                        min_rows: int, coverage: str) -> tuple[list[dict], dict]:
    """Return (sample rows for this surface, accounting block)."""
    rng = random.Random(derive_surface_seed(seed, surface, "text"))
    ordered = sorted(rows, key=lambda item: item["source_sha256"])
    strata: dict[str, list[dict]] = {level: [] for level in RISK_LEVELS}
    for row in ordered:
        strata[row["risk_level"]].append(row)

    picked: list[dict] = []
    seen: set[str] = set()
    accounting: dict[str, dict] = {}
    for level in RISK_LEVELS:
        pool = strata[level]
        selection: dict[str, int] = {}
        chosen: list[dict] = []
        if level in FULL_COVERAGE_LEVELS and coverage == "full":
            chosen = list(pool)
            selection["full_coverage"] = len(chosen)
        elif len(pool) <= quota[level]:
            chosen = list(pool)
            selection["stratum_exhausted"] = len(chosen)
        else:
            chosen = rng.sample(pool, quota[level])
            selection["quota"] = len(chosen)
        for row in chosen:
            picked.append({"row": row, "stratum": level, "reason": next(iter(selection))})
            seen.add(row["source_sha256"])
        accounting[level] = {
            "available": len(pool),
            "selected": len(chosen),
            "dropped": len(pool) - len(chosen),
            "selection": selection,
            "full_coverage_required": level in FULL_COVERAGE_LEVELS and coverage == "full",
        }

    # Per-surface minimum row count: top up from the rows still unselected, highest
    # risk level first, so a short stratum can never shrink the audit below the floor.
    fill_selected = 0
    if min_rows and len(picked) < min_rows:
        need = min_rows - len(picked)
        remaining: dict[str, list[dict]] = {level: [] for level in RISK_LEVELS}
        for row in ordered:
            if row["source_sha256"] not in seen:
                remaining[row["risk_level"]].append(row)
        for level in reversed(RISK_LEVELS):
            if need <= 0:
                break
            pool = remaining[level]
            if not pool:
                continue
            shuffled = list(pool)
            rng.shuffle(shuffled)
            take = shuffled[:need]
            if take:
                picked.extend({"row": row, "stratum": level, "reason": QUOTA_FILL_REASON}
                              for row in take)
                seen.update(row["source_sha256"] for row in take)
                accounting[level]["selection"][QUOTA_FILL_REASON] = len(take)
                accounting[level]["selected"] += len(take)
                accounting[level]["dropped"] -= len(take)
                fill_selected += len(take)
                need -= len(take)

    # Sample-size guarantee: at least min(rows_total, min_rows) rows for this surface.
    target = min(len(ordered), min_rows) if min_rows else 0
    minimum_rows_met = len(picked) >= target

    # Score-disagreement rows join on top of the quota and never consume a seat.
    disagreement = [row for row in ordered
                    if row["source_sha256"] not in seen and row["score_disagreement"]]
    for row in disagreement:
        picked.append({"row": row, "stratum": "score_disagreement",
                       "reason": "score_disagreement"})
        seen.add(row["source_sha256"])
        level = row["risk_level"]
        accounting[level]["selection"]["score_disagreement"] = (
            accounting[level]["selection"].get("score_disagreement", 0) + 1)
        accounting[level]["selected"] += 1
        accounting[level]["dropped"] -= 1

    high_critical_available = sum(accounting[level]["available"]
                                  for level in FULL_COVERAGE_LEVELS)
    high_critical_selected = sum(accounting[level]["selected"]
                                 for level in FULL_COVERAGE_LEVELS)
    if coverage == "full" and high_critical_selected != high_critical_available:
        raise AssertionError(f"{surface}: high/critical coverage is not 100%")

    samples = [{
        "mode": "text",
        "surface": surface,
        "stratum": item["stratum"],
        "audit_stratum": item["stratum"],  # ledger builder's sample key
        "risk": item["row"]["risk_level"],
        "risk_level": item["row"]["risk_level"],
        "risk_source": item["row"]["risk_source"],
        "seed": seed,
        "selection_reason": item["reason"],
        "source_sha256": item["row"]["source_sha256"],
        "source": item["row"].get("source"),
        "translation": item["row"].get("translation"),
        "sid_source": item["row"]["sid_source"],
        "input": item["row"]["input"],
        "score_disagreement": bool(item["row"]["score_disagreement"]),
    } for item in picked]

    block = {
        "mode": "text",
        "rows_total": len(ordered),
        "sampled_rows": len(samples),
        "coverage_ratio": round(len(samples) / len(ordered), 6) if ordered else 0.0,
        "minimum_rows": min_rows,
        "minimum_rows_target": target,
        "minimum_rows_met": minimum_rows_met,
        "minimum_rows_fill": fill_selected,
        "strata": accounting,
        "high_critical_coverage": coverage,
        "high_critical_available": high_critical_available,
        "high_critical_selected": high_critical_selected,
        "score_disagreement_sampled": sum(1 for item in samples if item["score_disagreement"]),
        "score_disagreement_outside_quota": sum(
            1 for item in samples if item["selection_reason"] == "score_disagreement"),
        "dropped_rows": len(ordered) - len(samples),
        "risk_source_counts": _counts(item["row"]["risk_source"] for item in picked),
        "risk_level_counts": _counts(item["row"]["risk_level"] for item in picked),
    }
    return samples, block


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- image sample

def normalise_labels(row: dict, label_field: str, flags_field: str) -> set[str]:
    labels: set[str] = set()
    raw = row.get(label_field)
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    for item in items:
        if isinstance(item, dict):
            item = item.get("class") or item.get("label") or item.get("name")
        if item is None:
            continue
        slug = LABEL_ALIASES.get(str(item).strip().casefold())
        if slug:
            labels.add(slug)
    flags = row.get(flags_field)
    if isinstance(flags, dict):
        for key, value in flags.items():
            if value and LABEL_FLAG_KEYS.get(str(key).strip().casefold()):
                labels.add(LABEL_FLAG_KEYS[str(key).strip().casefold()])
    # Panel position is derivable when the inventory states the panel geometry.
    panel_count = row.get("panel_count")
    panel_index = row.get("panel_index")
    if isinstance(panel_count, int) and isinstance(panel_index, int) and panel_count >= 2:
        if panel_index == 0:
            labels.add("multipanel_first_panel")
        if panel_index == panel_count - 1:
            labels.add("multipanel_last_panel")
    return labels


def find_sha_field(row: dict) -> str | None:
    for field in SHA_FIELD_PREFERENCE:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return field
    return None


def image_surface_of(row: dict, field: str | None, fallback: str) -> str:
    if field:
        value = row.get(field)
        if value not in (None, ""):
            return str(value)
    for candidate in ("surface", "bundle"):
        value = row.get(candidate)
        if value not in (None, ""):
            return str(value)
    return fallback


def sample_image_surface(surface: str, rows: list[dict], seed: int, min_count: int,
                         min_share: float, floor: int) -> tuple[list[dict], dict]:
    rng = random.Random(derive_surface_seed(seed, surface, "image"))
    available = len(rows)
    ordered = sorted(rows, key=lambda item: str(item["source_sha256"]))
    target = max(min_count, math.ceil(min_share * available)) if available else 0
    if available <= target:
        chosen = list(ordered)
        selection = "all_candidates"
    else:
        chosen = rng.sample(ordered, target)
        selection = "random_to_target"

    # Label-priority coverage: every row carrying a priority label is included, so a
    # required class can never be missed by the random draw alone.
    label_rows = {cls: [row for row in ordered if cls in row["labels"]] for cls in LABEL_CLASSES}
    already = {item["source_sha256"] for item in chosen}
    label_extra = 0
    for cls in LABEL_CLASSES:
        for row in label_rows[cls]:
            if row["source_sha256"] not in already:
                chosen.append(row)
                already.add(row["source_sha256"])
                label_extra += 1
    selection = ("random_plus_label_priority" if selection == "random_to_target" and label_extra
                 else selection)

    chosen.sort(key=lambda item: item["source_sha256"])
    label_counts = {cls: len(label_rows[cls]) for cls in LABEL_CLASSES}
    label_selected = {cls: sum(1 for row in chosen if cls in row["labels"])
                      for cls in LABEL_CLASSES}
    missing = [cls for cls in LABEL_CLASSES if label_counts[cls] == 0]

    samples = []
    for row in chosen:
        labels = sorted(row["labels"])
        stratum = (f"label:{labels[0]}" if labels else "random")
        item = {
            "mode": "image",
            "surface": surface,
            "stratum": stratum,
            "audit_stratum": stratum,
            "risk": row["risk_level"],
            "risk_level": row["risk_level"],
            "risk_source": row["risk_level_source"],
            "seed": seed,
            "selection_reason": ("label_priority" if labels else selection),
            "source_sha256": row["source_sha256"],
            "sha_field": row["sha_field"],
            "labels": labels,
        }
        for key in IMAGE_LOCATOR_KEYS:
            if key in row["raw"]:
                item[key] = row["raw"][key]
        item["input"] = row["input"]
        samples.append(item)

    floor_required = min(floor, available)
    block = {
        "mode": "image",
        "candidates_total": available,
        "sampled_rows": len(samples),
        "target": target,
        "selection": selection,
        "min_count": min_count,
        "min_share": min_share,
        "absolute_floor": floor,
        "absolute_floor_required": floor_required,
        "absolute_floor_met": len(samples) >= floor_required,
        "share_sampled": round(len(samples) / available, 6) if available else 0.0,
        "coverage_ratio": round(len(samples) / available, 6) if available else 0.0,
        "label_counts": label_counts,
        "label_selected": label_selected,
        "label_extra_beyond_random": label_extra,
        "label_classes_available": [cls for cls in LABEL_CLASSES if label_counts[cls]],
        "label_classes_missing": missing,
        "dropped_rows": available - len(samples),
        "risk_source_counts": _counts(row["risk_level_source"] for row in chosen),
    }
    return samples, block


def build_image_candidates(rows: list[dict], surface_field: str | None, label_field: str,
                           flags_field: str, new_edit_field: str | None,
                           new_edit_values: list[str], risk_index: dict[str, str],
                           label: str) -> tuple[dict[str, list[dict]], dict]:
    """Group inventory rows per surface; returns ({surface: [candidate]}, stats)."""
    kept, skipped_no_sha, skipped_not_new = 0, 0, 0
    grouped: dict[str, list[dict]] = {}
    new_edit_selection = "all_rows"
    if new_edit_field:
        new_edit_selection = f"{new_edit_field} in {sorted(set(new_edit_values))}"
    for index, raw in enumerate(rows, 1):
        if new_edit_field and new_edit_values:
            value = raw.get(new_edit_field)
            if str(value) not in new_edit_values:
                skipped_not_new += 1
                continue
        sha_field = find_sha_field(raw)
        if sha_field is None:
            skipped_no_sha += 1
            continue
        sid = str(raw[sha_field]).strip()
        surface = image_surface_of(raw, surface_field, label)
        level = risk_index.get(sid, "unknown")
        grouped.setdefault(surface, []).append({
            "source_sha256": sid,
            "sha_field": sha_field,
            "surface": surface,
            "labels": normalise_labels(raw, label_field, flags_field),
            "risk_level": level,
            "risk_level_source": "index" if sid in risk_index else "unknown",
            "raw": raw,
            "input": label,
        })
        kept += 1
    stats = {
        "rows_read": len(rows),
        "rows_kept": kept,
        "skipped_not_new_edit": skipped_not_new,
        "skipped_without_sha": skipped_no_sha,
        "new_edit_selection": new_edit_selection,
        "new_edit_field": new_edit_field,
        "new_edit_values": sorted(set(new_edit_values)),
        "input": label,
        "row_index_note": "rows are 1-indexed in source order; no row is dropped silently",
    }
    return grouped, stats


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--translations", action="append", default=[], metavar="PATH[:FIELD]",
                    help="translation/ledger JSONL; repeatable. FIELD names the row field "
                         "carrying the surface label (default: surface, then the file stem)")
    ap.add_argument("--risk", type=Path, default=None,
                    help=f"risk index JSONL (queue authority: {DEFAULT_RISK_FILE})")
    ap.add_argument("--surface", default=None,
                    help="surface label for single-input runs whose rows carry no surface field")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--quota", default=",".join(f"{k}={v}" for k, v in DEFAULT_QUOTA.items()))
    ap.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS,
                    help="per-surface minimum sample size (default 200)")
    ap.add_argument("--no-fill-to-minimum", action="store_true",
                    help="do not top up to --min-rows; reproduces the ledger's literal quotas")
    ap.add_argument("--high-critical-coverage", choices=("full", "quota"), default="full",
                    help="full (default): every high/critical row enters the sample; "
                         "quota: the ledger's literal 60-row quota")
    ap.add_argument("--image-inventory", type=Path, action="append", default=[],
                    help="image inventory JSONL (new edits); repeatable")
    ap.add_argument("--image-surface-field", default=None,
                    help="surface label field for image rows (default: surface, then bundle, "
                         "then the file stem)")
    ap.add_argument("--image-label-field", default="labels",
                    help="list field holding panel labels (default: labels)")
    ap.add_argument("--image-label-flags-field", default="label_flags",
                    help="object field holding boolean panel flags (default: label_flags)")
    ap.add_argument("--image-new-edit-field", default=None,
                    help="field marking a newly edited item, e.g. review_status")
    ap.add_argument("--image-new-edit-value", action="append", default=[],
                    help="accepted value of --image-new-edit-field; repeatable")
    ap.add_argument("--image-min-count", type=int, default=IMAGE_MIN_COUNT)
    ap.add_argument("--image-min-share", type=float, default=IMAGE_MIN_SHARE)
    ap.add_argument("--image-floor", type=int, default=IMAGE_FLOOR)
    ap.add_argument("--require-labels", action="store_true",
                    help="exit non-zero when a required image label class is missing")
    ap.add_argument("--write-dropped", action="store_true",
                    help="also write dropped.jsonl with the unsampled source ids")
    ap.add_argument("--json", action="store_true", help="print the summary JSON to stdout")
    return ap


def run(args: argparse.Namespace) -> tuple[int, dict]:
    out_dir: Path = args.out_dir
    if is_under_production(out_dir):
        raise InvalidInput(f"refusing to write under the production directory: {out_dir}")
    if args.seed is None:
        raise InvalidInput("--seed is required")
    quota = parse_quota(args.quota)
    if args.min_rows < 0:
        raise InvalidInput("--min-rows must be >= 0")
    if args.image_min_count <= 0 or args.image_min_share < 0 or args.image_floor <= 0:
        raise InvalidInput("image sampling bounds must be positive")

    risk_path = args.risk if args.risk is not None else (
        REPO / DEFAULT_RISK_FILE if (REPO / DEFAULT_RISK_FILE).is_file() else None)
    risk_index = load_risk_index(risk_path)

    inputs: dict[str, dict] = {}
    if risk_path is not None:
        inputs["risk"] = {"path": display_path(risk_path), "sha256": sha256_file(risk_path),
                          "rows": len(risk_index)}
    warnings: list[str] = []
    if risk_index and not any(level in ("high", "critical")
                              for level in risk_index.values()):
        warnings.append(
            "risk index contains no high/critical rows; machine sources are expected to use the "
            f"queue-based authority {DEFAULT_RISK_FILE}")

    samples: list[dict] = []
    surface_blocks: dict[str, dict] = {}
    dropped_rows: list[dict] = []
    min_rows = 0 if args.no_fill_to_minimum else args.min_rows

    for spec in args.translations:
        path, field = parse_translation_spec(spec)
        if not path.is_file():
            raise InvalidInput(f"translations input missing: {path}")
        label = display_path(path)
        inputs.setdefault("translations", [])
        inputs["translations"] = list(inputs.get("translations", [])) + [{
            "path": label, "surface_field": field, "sha256": sha256_file(path)}]
        rows = read_jsonl(path)
        prepared: dict[str, list[dict]] = {}
        matched_risk = 0
        for index, row in enumerate(rows, 1):
            sid, sid_source, source = row_identity(row, f"{label}:{index}")
            surface = row.get(field)
            surface = str(surface) if surface not in (None, "") else (args.surface or path.stem)
            level, level_source = row_risk(row, risk_index, sid)
            matched_risk += 1 if level_source == "index" else 0
            prepared.setdefault(surface, []).append({
                "source_sha256": sid, "sid_source": sid_source, "source": source,
                "risk_level": level, "risk_source": level_source,
                "score_disagreement": score_disagreement(row),
                "translation": row.get("translation"),
                "raw": row, "input": label,
            })
        # A risk index for one surface silently gives every other surface risk_level=low,
        # which switches the strict / dual-review policies off.  Report it loudly.
        if risk_index and rows and matched_risk == 0:
            warnings.append(
                f"{label}: none of the {len(rows)} rows match any source in the risk index "
                f"({display_path(risk_path) if risk_path else 'none'}); every row therefore falls "
                "back to risk_level=low and the high/critical policies are skipped.  Pass the "
                "risk index built from the SAME queue/universe as this file")
        elif risk_index and rows and matched_risk < len(rows):
            warnings.append(
                f"{label}: risk index covers {matched_risk}/{len(rows)} rows; the rest fall back "
                "to risk_level=low")
        for surface, surface_rows in sorted(prepared.items()):
            if surface in surface_blocks and surface_blocks[surface].get("sources"):
                raise InvalidInput(
                    f"surface {surface!r} appears in more than one --translations file; "
                    "sample one file per surface or rename the surface field value")
            picked, block = sample_text_surface(
                surface, surface_rows, quota, args.seed, min_rows,
                "full" if args.high_critical_coverage == "full" else "quota")
            block["sources"] = [label]
            surface_blocks[surface] = block
            samples.extend(picked)
            if args.write_dropped:
                selected = {item["source_sha256"] for item in picked}
                for row in surface_rows:
                    if row["source_sha256"] not in selected:
                        dropped_rows.append({"mode": "text", "surface": surface,
                                             "stratum": row["risk_level"],
                                             "source_sha256": row["source_sha256"]})

    image_blocks: dict[str, dict] = {}
    image_stats: list[dict] = []
    image_label_report: dict = {}
    for spec in args.image_inventory:
        path = spec
        if not path.is_file():
            raise InvalidInput(f"image inventory missing: {path}")
        label = display_path(path)
        inputs.setdefault("image_inventory", [])
        inputs["image_inventory"] = list(inputs.get("image_inventory", [])) + [{
            "path": label, "sha256": sha256_file(path)}]
        rows = read_jsonl(path)
        grouped, stats = build_image_candidates(
            rows, args.image_surface_field, args.image_label_field,
            args.image_label_flags_field, args.image_new_edit_field,
            args.image_new_edit_value, risk_index, label)
        stats["path"] = label
        image_stats.append(stats)
        if not stats["new_edit_field"]:
            warnings.append(
                f"{label}: no --image-new-edit-field was given, so every inventory row was "
                "treated as a new edit; record the real filter before using this sample as "
                "release evidence")
        for surface, candidates in sorted(grouped.items()):
            picked, block = sample_image_surface(
                surface, candidates, args.seed, args.image_min_count,
                args.image_min_share, args.image_floor)
            image_blocks[surface] = block
            samples.extend(picked)
            if args.write_dropped:
                selected = {item["source_sha256"] for item in picked}
                for row in candidates:
                    if row["source_sha256"] not in selected:
                        dropped_rows.append({"mode": "image", "surface": surface,
                                             "stratum": sorted(row["labels"])[0] if row["labels"]
                                             else "random",
                                             "source_sha256": row["source_sha256"]})

    if args.image_inventory:
        present: dict[str, int] = {}
        for block in image_blocks.values():
            for cls, count in block["label_counts"].items():
                present[cls] = present.get(cls, 0) + count
        missing = [cls for cls in LABEL_CLASSES if not present.get(cls)]
        image_label_report = {
            "required_classes": list(LABEL_CLASSES),
            "observed_counts": present,
            "missing_classes": missing,
            "status": "complete" if not missing else ("partial" if present else "none"),
            "note": "a missing class means the inventory cannot supply that label; the rule is "
                    "reported here and never silently dropped",
        }
        if missing:
            warnings.append(
                "image inventory cannot supply these required label classes: "
                + ", ".join(missing)
                + "; report the gap and fix the inventory before treating the image sample as "
                  "complete")

    if not samples:
        raise InvalidInput("no rows were sampled: check --translations/--image-inventory inputs")

    # dropped accounting must be exactly verifiable: selected + dropped == total
    for name, block in list(surface_blocks.items()) + list(image_blocks.items()):
        total = block.get("rows_total", block.get("candidates_total"))
        picked_rows = block["sampled_rows"]
        if name in surface_blocks:
            accounted = sum(item["selected"] for item in block["strata"].values())
        else:
            accounted = picked_rows
        if accounted != picked_rows or picked_rows + block["dropped_rows"] != total:
            raise AssertionError(
                f"{name}: sample accounting mismatch ({accounted} != {picked_rows} or "
                f"{picked_rows} + {block['dropped_rows']} != {total})")

    samples.sort(key=lambda item: (item["mode"], item["surface"], item["source_sha256"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_path = out_dir / "samples.jsonl"
    write_jsonl(samples_path, samples)
    dropped_path = None
    if args.write_dropped:
        dropped_path = out_dir / "dropped.jsonl"
        write_jsonl(dropped_path, dropped_rows)

    manifest = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": args.seed,
        "determinism": {
            "engine": "random.Random(derived integer seed per surface and mode)",
            "seed_derivation": "int.from_bytes(sha256('<seed>|<mode>|<surface>')[:8], 'big')",
            "row_order": "sorted by source_sha256 before any draw",
            "reproducible_command": "rerun with the same inputs and --seed; samples.jsonl and "
                                    "the per-stratum counts must be byte-identical",
        },
        "policy": {
            "text": {
                "min_rows_per_surface": args.min_rows,
                "min_rows_effective": min_rows,
                "fill_to_minimum": not args.no_fill_to_minimum,
                "quota": quota,
                "high_critical_coverage": args.high_critical_coverage,
                "score_disagreement_rows_outside_quota": True,
                "score_disagreement_requires_two_passing_reviews": True,
                "small_stratum": "taken in full",
            },
            "image": {
                "min_count": args.image_min_count,
                "min_share": args.image_min_share,
                "absolute_floor": args.image_floor,
                "interpretation": "target per surface = max(min_count, ceil(min_share * "
                                  "new_edit_candidates)); a surface with fewer candidates than "
                                  "the target is taken in full, which honours the absolute floor "
                                  "whenever the surface holds that many rows",
                "label_classes": list(LABEL_CLASSES),
                "label_rule": "rows carrying any priority label are always included",
                "missing_label_behaviour": "explicitly reported, never silently degraded",
                "new_edit_selection": [
                    {"path": stats["path"], "field": stats["new_edit_field"],
                     "values": stats["new_edit_values"],
                     "rule": stats["new_edit_selection"]} for stats in image_stats],
            },
        },
        "inputs": inputs,
        "surfaces": surface_blocks,
        "image": {"surfaces": image_blocks, "inputs": image_stats,
                  "label_report": image_label_report},
        "counts": {
            "sampled_rows": len(samples),
            "text_rows": sum(1 for item in samples if item["mode"] == "text"),
            "image_rows": sum(1 for item in samples if item["mode"] == "image"),
            "dropped_rows": sum(block["dropped_rows"]
                                for block in list(surface_blocks.values())
                                + list(image_blocks.values())),
        },
        "dropped": {
            "policy": "dropped = rows_total - sampled_rows, per stratum in surfaces[*].strata; "
                      "full list only with --write-dropped",
            "path": display_path(dropped_path) if dropped_path else None,
            "rows": len(dropped_rows) if args.write_dropped else None,
            "per_surface": {name: block["dropped_rows"] for name, block in
                            list(surface_blocks.items()) + list(image_blocks.items())},
        },
        "outputs": {"samples": {"path": display_path(samples_path),
                                "sha256": sha256_file(samples_path),
                                "rows": len(samples)}},
        "warnings": warnings,
    }
    manifest_path = out_dir / "sample-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    code = EXIT_OK
    if args.require_labels and image_label_report.get("missing_classes"):
        code = EXIT_LABELS_MISSING
    return code, manifest


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code, manifest = run(args)
    except InvalidInput as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return EXIT_INVALID
    summary = {
        "schema": manifest["schema"],
        "seed": manifest["seed"],
        "counts": manifest["counts"],
        "surfaces": {name: {"sampled": block.get("sampled_rows"),
                            "total": block.get("rows_total", block.get("candidates_total")),
                            "coverage": block.get("coverage_ratio"),
                            "minimum_met": block.get("minimum_rows_met"),
                            "high_critical_coverage": block.get("high_critical_coverage")}
                     for name, block in list(manifest["surfaces"].items())
                     + list(manifest["image"]["surfaces"].items())},
        "image_missing_labels": manifest["image"]["label_report"].get("missing_classes", []),
        "out": display_path(args.out_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
