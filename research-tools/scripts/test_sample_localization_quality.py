#!/usr/bin/env python3
"""Synthetic regression tests for the localization quality sampling toolchain.

No provider, no network, no production file: every fixture is generated under
``tmp_path``.  Covers the rules that must not silently degrade:

* a stratum smaller than its quota is taken in full;
* high/critical rows are covered 100% by default;
* the seed makes a draw reproducible (same seed identical, other seed different);
* the per-surface minimum sample size is enforced and its fill is accounted;
* score-disagreement rows join the sample outside the stratified quota;
* image mode reaches max(min_count, ceil(min_share * candidates)) and always adds
  rows carrying a priority label;
* a missing image label class is reported explicitly (and can be made fatal);
* defect rates > 5% block promotion, >= 3 occurrences pause the surface;
* unknown defect classes are rejected instead of being counted as clean.

Run: python -m pytest scripts/test_sample_localization_quality.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import scripts.classify_localization_quality_defects as defects  # noqa: E402
import scripts.sample_localization_quality as sampler  # noqa: E402

RISK_LEVELS = ("low", "medium", "high", "critical")


def sid(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class Fixture:
    """Builds a translations file, a risk index and the matching image inventory."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.translations: list[dict] = []
        self.risk: list[dict] = []

    def add(self, surface: str, level: str, index: int, **extra) -> dict:
        source = f"[{surface}] source line {index}"
        row = {"source_sha256": sid(source), "source": source,
               "translation": f"译文 {surface} {index}", "surface": surface}
        row.update(extra)
        self.translations.append(row)
        self.risk.append({"source_sha256": row["source_sha256"], "risk_level": level})
        return row

    def fill(self, surface: str, level: str, count: int, start: int = 0) -> None:
        for index in range(start, start + count):
            self.add(surface, level, index)

    def files(self) -> tuple[Path, Path]:
        translations = write_jsonl(self.tmp / "translations.jsonl", self.translations)
        risk = write_jsonl(self.tmp / "risk.jsonl", self.risk)
        return translations, risk

    def manual_risk(self, rows: list[tuple[str, str]]) -> Path:
        """Risk index from raw (source_text, level) pairs, for hand-built inputs."""
        return write_jsonl(self.tmp / "manual-risk.jsonl",
                           [{"source_sha256": sid(text), "risk_level": level}
                            for text, level in rows])


def sample_cli(fx: Fixture, out: Path, *extra: str) -> int:
    translations, risk = fx.files()
    argv = ["--translations", f"{translations}:surface", "--risk", str(risk),
            "--out-dir", str(out), *extra]
    return sampler.main(argv)


def manifest_of(out: Path) -> dict:
    return json.loads((out / "sample-manifest.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ text rules

def test_small_stratum_is_taken_in_full(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 5)
    fx.fill("gtx", "medium", 3, start=100)
    fx.fill("gtx", "high", 2, start=200)
    fx.fill("gtx", "critical", 1, start=300)
    out = tmp_path / "out"
    assert sample_cli(fx, out, "--seed", "11") == sampler.EXIT_OK
    capsys.readouterr()
    manifest = manifest_of(out)
    block = manifest["surfaces"]["gtx"]
    assert block["sampled_rows"] == 11
    assert block["rows_total"] == 11
    for level in RISK_LEVELS:
        stratum = block["strata"][level]
        assert stratum["selected"] == stratum["available"]
        assert stratum["dropped"] == 0
        assert "stratum_exhausted" in stratum["selection"] or "full_coverage" in stratum["selection"]
    rows = load(out / "samples.jsonl")
    assert {row["risk_level"] for row in rows} == set(RISK_LEVELS)
    assert all(row["seed"] == 11 for row in rows)


def test_high_and_critical_are_fully_covered(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 150)
    fx.fill("gtx", "medium", 150, start=1000)
    fx.fill("gtx", "high", 120, start=2000)
    fx.fill("gtx", "critical", 60, start=3000)
    out = tmp_path / "out"
    assert sample_cli(fx, out, "--seed", "5") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["surfaces"]["gtx"]
    assert block["high_critical_coverage"] == "full"
    assert block["high_critical_available"] == 180
    assert block["high_critical_selected"] == 180
    assert block["strata"]["high"]["selected"] == 120
    assert block["strata"]["critical"]["selected"] == 60
    # low/medium still obey the ledger quota
    assert block["strata"]["low"]["selected"] == 40
    assert block["strata"]["medium"]["selected"] == 40
    selected = load(out / "samples.jsonl")
    assert sum(1 for row in selected if row["risk_level"] in ("high", "critical")) == 180


def test_quota_mode_reproduces_the_ledger_literal_quota(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 150)
    fx.fill("gtx", "medium", 150, start=1000)
    fx.fill("gtx", "high", 120, start=2000)
    fx.fill("gtx", "critical", 60, start=3000)
    out = tmp_path / "out"
    assert sample_cli(fx, out, "--seed", "5", "--high-critical-coverage", "quota",
                      "--no-fill-to-minimum") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["surfaces"]["gtx"]
    assert block["sampled_rows"] == 200
    assert block["strata"]["high"]["selected"] == 60
    assert block["strata"]["critical"]["selected"] == 60


def test_seed_is_reproducible_and_seed_change_is_visible(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 300)
    fx.fill("gtx", "medium", 300, start=1000)
    fx.fill("gtx", "high", 80, start=2000)
    fx.fill("gtx", "critical", 40, start=3000)
    for name, seed in (("a", "17"), ("b", "17"), ("c", "18")):
        out = tmp_path / name
        assert sample_cli(fx, out, "--seed", seed) == sampler.EXIT_OK
        capsys.readouterr()
    text_a = (tmp_path / "a" / "samples.jsonl").read_text(encoding="utf-8")
    text_b = (tmp_path / "b" / "samples.jsonl").read_text(encoding="utf-8")
    text_c = (tmp_path / "c" / "samples.jsonl").read_text(encoding="utf-8")
    assert text_a == text_b
    assert text_a != text_c
    assert manifest_of(tmp_path / "a")["seed"] == 17
    assert manifest_of(tmp_path / "a")["outputs"]["samples"]["sha256"] == \
        hashlib.sha256(text_a.encode("utf-8")).hexdigest()


def test_minimum_rows_is_enforced_and_accounted(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 250)
    out = tmp_path / "out"
    assert sample_cli(fx, out, "--seed", "3") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["surfaces"]["gtx"]
    assert block["minimum_rows"] == 200
    assert block["minimum_rows_target"] == 200
    assert block["minimum_rows_met"] is True
    assert block["sampled_rows"] == 200
    assert block["minimum_rows_fill"] == 160
    assert block["strata"]["low"]["selected"] == 200
    assert block["strata"]["low"]["selection"][sampler.QUOTA_FILL_REASON] == 160
    assert block["sampled_rows"] + block["dropped_rows"] == block["rows_total"]


def test_surface_smaller_than_minimum_is_taken_in_full(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.fill("fontrender", "low", 12)
    out = tmp_path / "out"
    assert sample_cli(fx, out, "--seed", "3") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["surfaces"]["fontrender"]
    assert block["rows_total"] == 12
    assert block["sampled_rows"] == 12
    assert block["minimum_rows_target"] == 12
    assert block["minimum_rows_met"] is True


def test_score_disagreement_rows_join_outside_the_quota(tmp_path, capsys):
    """Mirrors build_translation_release_ledger: a disagreement row that wins a
    stratified seat keeps it; every other disagreement row is added on top and is
    the only kind whose ``audit_stratum`` becomes ``score_disagreement``."""
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 300)
    disagreeing = []
    for index in range(2):
        row = fx.add("gtx", "low", 500 + index)
        row["review"] = {"verdict": "PASS", "scores": {"semantic_accuracy": 5}}
        row["second_review"] = {"verdict": "PASS", "scores": {"semantic_accuracy": 4}}
        disagreeing.append(row["source_sha256"])
    out = tmp_path / "out"
    assert sample_cli(fx, out, "--seed", "23", "--no-fill-to-minimum") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["surfaces"]["gtx"]
    selected = load(out / "samples.jsonl")
    by_id = {row["source_sha256"]: row for row in selected}
    for sid_value in disagreeing:
        assert sid_value in by_id, "every disagreement row must be sampled"
        assert by_id[sid_value]["score_disagreement"] is True
    outside = [row for row in selected if row["selection_reason"] == "score_disagreement"]
    assert all(row["audit_stratum"] == "score_disagreement" for row in outside)
    assert block["strata"]["low"]["selection"]["quota"] == 40
    assert block["sampled_rows"] == 40 + len(outside)
    assert block["score_disagreement_sampled"] == 2
    assert block["score_disagreement_outside_quota"] == len(outside)


def test_score_disagreement_requires_both_reviews_to_pass(tmp_path):
    rows = [
        {"verdict": "PASS", "scores": {"semantic_accuracy": 5}},
        {"verdict": "FAIL", "scores": {"semantic_accuracy": 4}},
    ]
    assert sampler.score_disagreement({"review": rows[0], "second_review": rows[1]}) is False
    assert sampler.score_disagreement({"review": rows[0], "second_review": rows[0]}) is False
    assert sampler.score_disagreement({"review": rows[0], "second_review": rows[1],
                                       "score_disagreement": True}) is True
    assert sampler.score_disagreement({"review": rows[0]}) is False


def test_sampler_refuses_the_production_directory(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 10)
    assert sample_cli(fx, sampler.PRODUCTION_DIR / "nope", "--seed", "1") == sampler.EXIT_INVALID
    assert "production directory" in capsys.readouterr().err
    assert not (sampler.PRODUCTION_DIR / "nope").exists()


def test_sampler_derives_source_sha_when_only_source_is_present(tmp_path, capsys):
    rows = [{"source": "こんにちは", "translation": "你好", "surface": "gtx"}]
    translations = write_jsonl(tmp_path / "only-source.jsonl", rows)
    risk = write_jsonl(tmp_path / "risk.jsonl",
                       [{"source_sha256": sid("こんにちは"), "risk_level": "high"}])
    out = tmp_path / "out"
    code = sampler.main(["--translations", f"{translations}:surface", "--risk", str(risk),
                         "--out-dir", str(out), "--seed", "9"])
    capsys.readouterr()
    assert code == sampler.EXIT_OK
    sample = load(out / "samples.jsonl")[0]
    assert sample["sid_source"] == "derived_from_source"
    assert sample["risk_level"] == "high"


def test_sampler_rejects_a_tampered_source_sha(tmp_path, capsys):
    rows = [{"source_sha256": "0" * 64, "source": "さようなら", "translation": "再见",
             "surface": "gtx"}]
    translations = write_jsonl(tmp_path / "bad.jsonl", rows)
    risk = write_jsonl(tmp_path / "risk.jsonl", [])
    code = sampler.main(["--translations", f"{translations}:surface", "--risk", str(risk),
                         "--out-dir", str(tmp_path / "out"), "--seed", "9"])
    assert code == sampler.EXIT_INVALID
    assert "invalid input" in capsys.readouterr().err


def test_risk_index_for_another_surface_is_reported_loudly(tmp_path, capsys):
    """A risk index built from another universe silently routes everything as low."""
    fx = Fixture(tmp_path)
    fx.fill("gtx", "low", 30)
    translations = write_jsonl(tmp_path / "translations.jsonl", fx.translations)
    foreign = write_jsonl(tmp_path / "foreign-risk.jsonl",
                          [{"source_sha256": sid("totally different row"),
                            "risk_level": "critical"}])
    out = tmp_path / "out"
    code = sampler.main(["--translations", f"{translations}:surface", "--risk", str(foreign),
                         "--out-dir", str(out), "--seed", "9"])
    captured = capsys.readouterr()
    assert code == sampler.EXIT_OK
    assert "none of the 30 rows match any source in the risk index" in captured.err
    manifest = manifest_of(out)
    assert manifest["counts"]["sampled_rows"] == 30
    assert manifest["surfaces"]["gtx"]["strata"]["critical"]["available"] == 0
    assert manifest["surfaces"]["gtx"]["strata"]["low"]["available"] == 30
    assert all("none of the" in warning for warning in manifest["warnings"])


# ----------------------------------------------------------------- image rules

def image_row(surface: str, index: int, labels: list[str] | None = None,
              status: str = "reviewed_edited") -> dict:
    text = f"{surface}-image-{index}"
    row = {"composite_sha256": sid(text), "surface": surface, "review_status": status,
           "original": f"orig/{text}.png"}
    if labels:
        row["labels"] = labels
    return row


def run_image(inventory: Path, out: Path, *extra: str) -> int:
    argv = ["--image-inventory", str(inventory), "--image-surface-field", "surface",
            "--out-dir", str(out), *extra]
    return sampler.main(argv)


def test_image_sampling_reaches_count_and_share(tmp_path, capsys):
    inventory = write_jsonl(tmp_path / "inv.jsonl",
                            [image_row("event-comic", index) for index in range(1000)])
    out = tmp_path / "out"
    assert run_image(inventory, out, "--seed", "31", "--image-new-edit-field", "review_status",
                     "--image-new-edit-value", "reviewed_edited") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["image"]["surfaces"]["event-comic"]
    # max(30 rows, 5% of 1000) = 50, and never below the 20-row floor
    assert block["target"] == 50
    assert block["sampled_rows"] == 50
    assert block["share_sampled"] == pytest.approx(0.05)
    assert block["absolute_floor_met"] is True
    assert block["candidates_total"] == 1000


def test_image_small_surface_is_taken_in_full_but_floor_is_reported(tmp_path, capsys):
    inventory = write_jsonl(tmp_path / "inv.jsonl",
                            [image_row("event-comic", index) for index in range(8)])
    out = tmp_path / "out"
    assert run_image(inventory, out, "--seed", "31") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["image"]["surfaces"]["event-comic"]
    assert block["sampled_rows"] == 8
    assert block["candidates_total"] == 8
    assert block["selection"] == "all_candidates"
    # the inventory cannot supply 20 rows: the shortfall is reported, not hidden
    assert block["absolute_floor_required"] == 8
    assert block["absolute_floor_met"] is True


def test_image_label_priority_rows_are_always_included(tmp_path, capsys):
    rows = [image_row("event-comic", index, labels=["small_font_panel"])
            for index in range(6)]
    rows += [image_row("event-comic", 100 + index) for index in range(120)]
    inventory = write_jsonl(tmp_path / "inv.jsonl", rows)
    out = tmp_path / "out"
    assert run_image(inventory, out, "--seed", "41") == sampler.EXIT_OK
    capsys.readouterr()
    block = manifest_of(out)["image"]["surfaces"]["event-comic"]
    assert block["target"] == 30
    assert block["label_selected"]["small_font_panel"] == 6
    # the 30-row random draw may already hold some labelled rows; every remaining
    # labelled row is added on top, so the labelled set is complete and >= 30 rows
    extra = block["label_extra_beyond_random"]
    assert block["sampled_rows"] == 30 + extra
    assert block["sampled_rows"] >= 30
    selected = load(out / "samples.jsonl")
    labelled = [row for row in selected if "small_font_panel" in row["labels"]]
    assert len(labelled) == 6
    assert all(row["selection_reason"] == "label_priority" for row in labelled)
    unlabelled = [row for row in selected if not row["labels"]]
    assert len(unlabelled) == 30 - (6 - extra)


def test_image_missing_labels_are_reported_and_can_be_fatal(tmp_path, capsys):
    rows = [image_row("event-comic", index, labels=["longest_text_panel"])
            for index in range(40)]
    inventory = write_jsonl(tmp_path / "inv.jsonl", rows)
    out = tmp_path / "out"
    code = run_image(inventory, out, "--seed", "43", "--require-labels")
    captured = capsys.readouterr()
    assert code == sampler.EXIT_LABELS_MISSING
    report = manifest_of(out)["image"]["label_report"]
    assert report["status"] == "partial"
    assert report["observed_counts"]["longest_text_panel"] == 40
    assert "multipanel_first_panel" in report["missing_classes"]
    assert "multipanel_last_panel" in report["missing_classes"]
    assert "small_font_panel" in report["missing_classes"]
    assert "character_name_or_onomatopoeia" in report["missing_classes"]
    assert "cannot supply" in captured.err
    # the outputs are still written: the manifest is the report
    assert (out / "samples.jsonl").is_file()


def test_image_label_report_is_complete_when_all_classes_are_present(tmp_path, capsys):
    classes = list(sampler.LABEL_CLASSES)
    rows = [image_row("event-comic", index, labels=[classes[index % len(classes)]])
            for index in range(50)]
    inventory = write_jsonl(tmp_path / "inv.jsonl", rows)
    out = tmp_path / "out"
    assert run_image(inventory, out, "--seed", "47", "--require-labels") == sampler.EXIT_OK
    capsys.readouterr()
    report = manifest_of(out)["image"]["label_report"]
    assert report["status"] == "complete"
    assert report["missing_classes"] == []


def test_image_new_edit_filter_marks_untouched_rows_as_dropped(tmp_path, capsys):
    rows = ([image_row("event-comic", index) for index in range(40)]
            + [image_row("event-comic", 500 + index, status="unchanged") for index in range(10)])
    inventory = write_jsonl(tmp_path / "inv.jsonl", rows)
    out = tmp_path / "out"
    assert run_image(inventory, out, "--seed", "53", "--image-new-edit-field", "review_status",
                     "--image-new-edit-value", "reviewed_edited") == sampler.EXIT_OK
    capsys.readouterr()
    manifest = manifest_of(out)
    block = manifest["image"]["surfaces"]["event-comic"]
    assert block["candidates_total"] == 40
    # "dropped" is relative to the new-edit candidates, not to the untouched rows
    assert block["dropped_rows"] == 40 - block["sampled_rows"]
    assert block["sampled_rows"] + block["dropped_rows"] == block["candidates_total"]
    stats = manifest["image"]["inputs"][0]
    assert stats["rows_read"] == 50
    assert stats["rows_kept"] == 40
    assert stats["skipped_not_new_edit"] == 10
    assert stats["new_edit_selection"] == "review_status in ['reviewed_edited']"
    selected = load(out / "samples.jsonl")
    assert all(row["review_status"] == "reviewed_edited" for row in selected)


def test_image_row_without_sha_is_skipped_and_counted(tmp_path, capsys):
    inventory = write_jsonl(tmp_path / "inv.jsonl",
                            [image_row("event-comic", index) for index in range(30)]
                            + [{"surface": "event-comic", "original": "orig/unknown.png"}])
    out = tmp_path / "out"
    assert run_image(inventory, out, "--seed", "59") == sampler.EXIT_OK
    capsys.readouterr()
    stats = manifest_of(out)["image"]["inputs"][0]
    assert stats["skipped_without_sha"] == 1
    assert stats["rows_read"] == 31
    assert stats["rows_kept"] == 30


# ------------------------------------------------------------- defect taxonomy

def judgement_rows(count: int, surface: str = "gtx", defects: dict[str, int] | None = None,
                   prefix: str = "j") -> list[dict]:
    """``count`` rows, first assigning the given defect classes to leading rows."""
    rows: list[dict] = []
    remaining = dict(defects or {})
    for index in range(count):
        row = {"source_sha256": sid(f"{prefix}-{surface}-{index}"), "surface": surface}
        assigned = None
        for slug in list(remaining):
            if remaining[slug] > 0:
                remaining[slug] -= 1
                assigned = slug
                break
        if assigned:
            row["defect_class"] = assigned
            row["judgement"] = "defect"
        else:
            row["judgement"] = "ok"
        rows.append(row)
    assert not any(value > 0 for value in remaining.values()), "fixture defect counts too large"
    return rows


def run_defects(path: Path, out: Path, *extra: str) -> int:
    return defects.main(["--judgements", str(path), "--out-dir", str(out), *extra])


def test_defect_rate_above_threshold_blocks_promotion(tmp_path, capsys):
    # 2 defects in 30 judged rows = 6.7%: over the rate threshold, but below the
    # 3-occurrence systemic rule, so only promotion is blocked.
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       judgement_rows(30, defects={"layout_overflow": 2}))
    out = tmp_path / "out"
    assert run_defects(path, out) == defects.EXIT_BLOCKED
    capsys.readouterr()
    report = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))
    block = report["surfaces"]["gtx"]
    assert block["classes"]["layout_overflow"]["count"] == 2
    assert block["classes"]["layout_overflow"]["rate"] == pytest.approx(2 / 30, abs=1e-6)
    assert block["classes"]["layout_overflow"]["over_rate_threshold"] is True
    assert block["classes"]["layout_overflow"]["systemic_defect"] is False
    assert block["promotion_blocked"] is True
    assert block["must_pause_surface"] is False


def test_defect_rate_at_exactly_five_percent_does_not_block(tmp_path, capsys):
    # 2 defects in 40 judged rows = exactly 5%; the rule is "> 5%"
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       judgement_rows(40, defects={"layout_overflow": 2}))
    out = tmp_path / "out"
    assert run_defects(path, out) == defects.EXIT_OK
    capsys.readouterr()
    block = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))["surfaces"]["gtx"]
    assert block["classes"]["layout_overflow"]["rate"] == pytest.approx(0.05)
    assert block["classes"]["layout_overflow"]["over_rate_threshold"] is False
    assert block["promotion_blocked"] is False


def test_three_occurrences_of_one_class_pause_the_surface(tmp_path, capsys):
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       judgement_rows(200, defects={"semantic_mistranslation": 3}))
    out = tmp_path / "out"
    assert run_defects(path, out) == defects.EXIT_PAUSE
    capsys.readouterr()
    report = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))
    block = report["surfaces"]["gtx"]
    assert block["classes"]["semantic_mistranslation"]["count"] == 3
    assert block["classes"]["semantic_mistranslation"]["systemic_defect"] is True
    assert block["systemic_defect"] is True
    assert block["must_pause_surface"] is True
    # 1.5% is below the rate threshold, so the pause is the binding state
    assert block["promotion_blocked"] is False
    assert report["systemic_defects"] == [
        {"surface": "gtx", "defect_class": "semantic_mistranslation", "count": 3}]
    assert report["summary"]["verdict"] == "pause_surface_and_fix_pipeline"


def test_threshold_overrides_change_the_verdict(tmp_path, capsys):
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       judgement_rows(40, defects={"missing_glyph_tofu": 2}))
    out = tmp_path / "out"
    assert run_defects(path, out, "--systemic-threshold", "2",
                       "--rate-threshold", "0.01") == defects.EXIT_PAUSE
    capsys.readouterr()
    block = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))["surfaces"]["gtx"]
    assert block["classes"]["missing_glyph_tofu"]["systemic_defect"] is True
    assert block["classes"]["missing_glyph_tofu"]["over_rate_threshold"] is True


def test_two_classes_on_one_row_count_in_both(tmp_path, capsys):
    rows = judgement_rows(50)
    rows[0]["defect_classes"] = ["missing_glyph_tofu", "layout_overflow"]
    rows[0]["judgement"] = "defect"
    path = write_jsonl(tmp_path / "judgements.jsonl", rows)
    out = tmp_path / "out"
    assert run_defects(path, out) == defects.EXIT_OK
    capsys.readouterr()
    block = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))["surfaces"]["gtx"]
    assert block["defective_rows"] == 1
    assert block["classes"]["missing_glyph_tofu"]["count"] == 1
    assert block["classes"]["layout_overflow"]["count"] == 1


def test_reflow_record_shape_is_ready_to_fill(tmp_path, capsys):
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       judgement_rows(200, defects={"language_residue": 4}))
    out = tmp_path / "out"
    assert run_defects(path, out) == defects.EXIT_PAUSE
    capsys.readouterr()
    records = load(out / "reflow-records.jsonl")
    assert len(records) == 1
    record = records[0]
    for key in ("defect_class", "root_cause", "pipeline_change_before",
                "pipeline_change_after", "rerun_scope", "resample_result"):
        assert key in record
    assert record["defect_class"] == "language_residue"
    assert record["defect_label"] == "语言残留(繁中/日文)"
    assert record["root_cause"] is None
    assert record["resample_result"] is None
    assert record["systemic_defect"] is True
    report = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))
    assert report["reflow_records"] == records


def test_unknown_defect_class_is_rejected(tmp_path, capsys):
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       [{"source_sha256": sid("x"), "surface": "gtx", "defect_class": "oops"}])
    assert run_defects(path, tmp_path / "out") == defects.EXIT_INVALID
    assert "unknown defect class" in capsys.readouterr().err


def test_coarse_critical_judgement_is_rejected_with_a_hint(tmp_path, capsys):
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       [{"source_sha256": sid("x"), "surface": "gtx", "judgement": "critical"}])
    assert run_defects(path, tmp_path / "out") == defects.EXIT_INVALID
    error = capsys.readouterr().err
    assert "neither a pass" in error
    assert "class must be stated explicitly" in error


def test_chinese_labels_are_accepted(tmp_path, capsys):
    rows = judgement_rows(40)
    rows.append({"source_sha256": sid("cn"), "surface": "gtx", "defect_class": "画面破坏"})
    path = write_jsonl(tmp_path / "judgements.jsonl", rows)
    out = tmp_path / "out"
    assert run_defects(path, out) == defects.EXIT_OK
    capsys.readouterr()
    block = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))["surfaces"]["gtx"]
    assert block["classes"]["image_breakage"]["count"] == 1


def test_sample_coverage_counts_unjudged_rows_as_not_pass(tmp_path, capsys):
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       [{"source_sha256": sid("a"), "surface": "gtx", "judgement": "ok"}])
    sample = write_jsonl(tmp_path / "samples.jsonl",
                         [{"source_sha256": sid("a"), "surface": "gtx"},
                          {"source_sha256": sid("b"), "surface": "gtx"}])
    out = tmp_path / "out"
    assert defects.main(["--judgements", str(path), "--sample", str(sample),
                         "--out-dir", str(out)]) == defects.EXIT_OK
    capsys.readouterr()
    report = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))
    assert report["coverage"]["sample_rows_referenced"] == 2
    assert report["coverage"]["sample_rows_judged"] == 1
    assert report["coverage"]["sample_rows_without_judgement"] == 1
    assert report["surfaces"]["gtx"]["judgement_coverage"] == pytest.approx(0.5)


def test_duplicate_judgements_keep_the_first_verdict(tmp_path, capsys):
    first = {"source_sha256": sid("dup"), "surface": "gtx", "defect_class": "layout_overflow"}
    second = {"source_sha256": sid("dup"), "surface": "gtx", "judgement": "ok"}
    rows = judgement_rows(40) + [first, second]
    path = write_jsonl(tmp_path / "judgements.jsonl", rows)
    out = tmp_path / "out"
    assert run_defects(path, out) == defects.EXIT_OK
    capsys.readouterr()
    report = json.loads((out / "defect-report.json").read_text(encoding="utf-8"))
    assert report["counts"]["duplicate_judgement_rows_ignored"] == 1
    assert report["surfaces"]["gtx"]["classes"]["layout_overflow"]["count"] == 1


def test_classifier_refuses_the_production_directory(tmp_path, capsys):
    path = write_jsonl(tmp_path / "judgements.jsonl",
                       [{"source_sha256": sid("x"), "surface": "gtx", "judgement": "ok"}])
    assert run_defects(path, defects.PRODUCTION_DIR / "nope") == defects.EXIT_INVALID
    assert "production directory" in capsys.readouterr().err
    assert not (defects.PRODUCTION_DIR / "nope").exists()


# ------------------------------------------------------------------ contracts

def test_defect_vocabulary_is_fixed_and_bilingual():
    assert list(defects.DEFECT_CLASSES) == [
        "semantic_mistranslation", "terminology_inconsistency", "missing_glyph_tofu",
        "layout_overflow", "language_residue", "image_breakage", "prompt_scope_violation"]
    assert defects.DEFECT_CLASSES["semantic_mistranslation"] == "语义错译"
    assert defects.DEFECT_CLASSES["terminology_inconsistency"] == "术语不一致"
    assert defects.DEFECT_CLASSES["missing_glyph_tofu"] == "字符缺失(豆腐块)"
    assert defects.DEFECT_CLASSES["layout_overflow"] == "排版溢出"
    assert defects.DEFECT_CLASSES["language_residue"] == "语言残留(繁中/日文)"
    assert defects.DEFECT_CLASSES["image_breakage"] == "画面破坏"
    assert defects.DEFECT_CLASSES["prompt_scope_violation"] == "提示词越权改动"


def test_sampler_defaults_match_the_release_ledger_rule():
    assert sampler.DEFAULT_QUOTA == {"low": 40, "medium": 40, "high": 60, "critical": 60}
    assert sampler.DEFAULT_MIN_ROWS == 200
    assert sampler.FULL_COVERAGE_LEVELS == ("high", "critical")
    assert sampler.IMAGE_MIN_COUNT == 30
    assert sampler.IMAGE_MIN_SHARE == 0.05
    assert sampler.IMAGE_FLOOR == 20
    assert sampler.DEFAULT_SEED == 1077100
    assert defects.DEFAULT_SYSTEMIC_THRESHOLD == 3
    assert defects.DEFAULT_RATE_THRESHOLD == 0.05
