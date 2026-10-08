#!/usr/bin/env python3
"""Complementary conformance tests for the LANDED scripts/build_translation_release_ledger.py.

The landed tool (written by a concurrent session) ships its own regression file
``scripts/test_build_translation_release_ledger.py`` (25 tests, green).  This file deliberately
does NOT duplicate that suite: it covers only the behaviours the author's suite does not
assert, plus the three audit boundaries recorded in
``work/agents/text-localization/ledger-builder-20260923/HANDOFF.md``:

* high-risk rows need maximum strict scores (spec page);
* an undeterminable REVIEWER identity goes to needs-review (author tests only the translator side);
* a reviewer naming the translator MODEL through another provider is currently accepted
  (audit note; pins the landed boundary so a later change is visible);
* score-disagreement rows (both reviews PASS, scores differ) are sampled on top of the 200-row
  quota and never consume a stratified seat — audit GAP (a), fixed 2026-09-23;
* ``--risk`` is required (fail closed) unless ``--allow-missing-risk`` is passed explicitly, so a
  missing risk index can no longer silently route every row as low — audit GAP (e), fixed 2026-09-23;
* nothing is written outside ``--out-dir`` and no staging directory is left behind.

Run: python -m pytest scripts/test_build_translation_release_ledger_conformance.py -q -p no:cacheprovider
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

import scripts.build_translation_release_ledger as ledger  # noqa: E402
from scripts.mltd_localize_gtx import read_jsonl  # noqa: E402

FULL_SCORES = {
    "semantic_accuracy": 5,
    "terminology": 5,
    "fluency": 5,
    "character_voice": 5,
    "context_consistency": 5,
}
TRANSLATOR = {
    "provider": "https://translator.example/v1",
    "model_id": "primary",
    "model": "t-model",
    "task": "DIALOGUE",
}
GLOSSARY = {"entries": {}, "kana_allowlist": []}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def text_pair(index: int) -> tuple[str, str]:
    """QA-clean pair: kana only in the source, plain Simplified Chinese as the translation."""
    return f"テスト原文{index}のセリフ", f"测试原文{index}的台词。"


class Fixture:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.in_dir = tmp_path / "in"
        self.in_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = tmp_path / "out"
        self.universe: list[dict] = []
        self.candidates: list[dict] = []
        self.qa: list[dict] = []
        self.risk: list[dict] = []
        self.reviews: list[dict] = []
        self.second: list[dict] = []
        self.glossary = self.in_dir / "glossary.json"
        self.glossary.write_text(json.dumps(GLOSSARY), encoding="utf-8")

    def add(self, source: str, translation: str, *, risk: str | None = "low",
            qa: str | None = "PASS", review: dict | None = None, second_review: dict | None = None,
            provenance: object = TRANSLATOR, status: str = "machine_translated",
            candidate: bool = True) -> str:
        sid = sha256_text(source)
        self.universe.append({"source_sha256": sid, "source": source, "translation": "",
                              "status": "pending"})
        if candidate:
            self.candidates.append({"source_sha256": sid, "source": source,
                                    "translation": translation, "status": status,
                                    "provenance": provenance})
        if qa is not None:
            self.qa.append({"source_sha256": sid, "source": source, "translation": translation,
                            "qa_verdict": qa, "issues": []})
        if risk is not None:
            self.risk.append({"source_sha256": sid, "risk_level": risk, "risk_score": 0,
                              "risk_reasons": [], "review_policy": "standard_independent_review"})
        if review is not None:
            self.reviews.append(self.review_row(source, translation, review))
        if second_review is not None:
            self.second.append(self.review_row(source, translation, second_review))
        return sid

    def review_row(self, source: str, translation: str, spec: dict) -> dict:
        row = {
            "source_sha256": sha256_text(source),
            "source": source,
            "translation": translation,
            "verdict": spec.get("verdict", "PASS"),
            "scores": dict(spec.get("scores", FULL_SCORES)),
            "blocking_errors": [],
            "notes": "",
            "reviewer_provenance": spec.get("reviewer_provenance", "review-endpoint"),
            "reviewer_model": spec.get("reviewer_model", "r-model"),
        }
        reviewer_id = spec.get("reviewer_id", "independent-reviewer-1")
        if reviewer_id is not None:
            row["reviewer_id"] = reviewer_id
        return row

    def pair(self, index: int, **kwargs) -> str:
        source, translation = text_pair(index)
        return self.add(source, translation, **kwargs)

    def write(self) -> dict:
        paths = {
            "universe": write_jsonl(self.in_dir / "universe.jsonl", self.universe),
            "candidates": write_jsonl(self.in_dir / "candidates.jsonl", self.candidates),
            "qa": write_jsonl(self.in_dir / "qa.jsonl", self.qa),
            "risk": write_jsonl(self.in_dir / "risk.jsonl", self.risk),
            "reviews": write_jsonl(self.in_dir / "reviews.jsonl", self.reviews),
            "second": write_jsonl(self.in_dir / "second.jsonl", self.second),
        }
        self.paths = paths
        return paths

    def argv(self, *, out_dir: Path | None = None, include_reviews: bool = True,
             include_second: bool = True, include_risk: bool = True, seed: int | None = None,
             run_id: str = "conformance") -> list[str]:
        argv = [
            "--surface", "nongtx",
            "--universe", str(self.paths["universe"]),
            "--expected-count", str(len(self.universe)),
            "--glossary", str(self.glossary),
            "--candidates", str(self.paths["candidates"]),
            "--qa", str(self.paths["qa"]),
            "--out-dir", str(out_dir or self.out_dir),
            "--run-id", run_id,
        ]
        if include_risk:
            argv += ["--risk", str(self.paths["risk"])]
        if include_reviews:
            argv += ["--reviews", str(self.paths["reviews"])]
        if include_second:
            argv += ["--second-reviews", str(self.paths["second"])]
        if seed is not None:
            argv += ["--audit-sample-seed", str(seed)]
        return argv

    def manifest(self, out_dir: Path | None = None) -> dict:
        path = (out_dir or self.out_dir) / "release-ledger-nongtx.manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def ledger_rows(self, out_dir: Path | None = None) -> list[dict]:
        return read_jsonl((out_dir or self.out_dir) / "release-ledger-nongtx.jsonl")

    def queue_rows(self, out_dir: Path | None = None) -> list[dict]:
        return read_jsonl((out_dir or self.out_dir) / "needs-review-nongtx.jsonl")

    def sample_rows(self, out_dir: Path | None = None) -> list[dict]:
        return read_jsonl((out_dir or self.out_dir) / "audit-sample-nongtx.jsonl")


def test_high_risk_rows_need_maximum_strict_scores(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.pair(1, risk="high", review={"scores": dict(FULL_SCORES, terminology=4)})
    fixture.pair(2, risk="high", review={})
    fixture.write()

    assert ledger.main(fixture.argv()) == ledger.EXIT_OK

    assert [row["source"] for row in fixture.ledger_rows()] == [text_pair(2)[0]]
    queue = {row["source"]: row for row in fixture.queue_rows()}
    assert queue[text_pair(1)[0]]["release_reasons"] == ["strict_review_terminology_not_maximum"]


def test_unknown_reviewer_identity_goes_to_needs_review(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.pair(3, review={"reviewer_id": None, "reviewer_provenance": "review-endpoint",
                            "reviewer_model": ""})
    fixture.write()

    assert ledger.main(fixture.argv()) == ledger.EXIT_OK

    assert fixture.ledger_rows() == []
    assert fixture.queue_rows()[0]["release_reasons"] == ["reviewer_identity_unknown"]


def test_reviewer_naming_the_translator_model_on_another_endpoint_is_accepted(tmp_path):
    """Pins the landed boundary from audit note (b): only provider+model equality is rejected.

    A reviewer that reuses the translator MODEL behind a different provider string is treated as
    independent, because the landed rule compares the provider+model pair.  If the owner tightens
    this, the test must be updated together with the audit note.
    """
    fixture = Fixture(tmp_path)
    fixture.pair(4, review={"reviewer_id": None, "reviewer_provenance": "other-endpoint",
                            "reviewer_model": TRANSLATOR["model"]})
    fixture.write()

    assert ledger.main(fixture.argv()) == ledger.EXIT_OK

    rows = fixture.ledger_rows()
    assert len(rows) == 1
    assert rows[0]["review"]["reviewer_model"] == TRANSLATOR["model"]


def test_score_disagreement_rows_are_always_sampled_and_flagged(tmp_path):
    """Spec: both reviews PASS with differing scores ⇒ always audited, never competing for the quota."""
    fixture = Fixture(tmp_path)
    for index in range(10, 50):  # exactly the low quota (40) of non-disagreeing accepted rows
        fixture.pair(index, risk="low", review={})
    disagreeing_ids = []
    for index in range(50, 53):
        source, translation = text_pair(index)
        disagreeing_ids.append(fixture.add(
            source, translation, risk="low",
            review={"reviewer_id": "reviewer-a", "scores": dict(FULL_SCORES)},
            second_review={"reviewer_id": "reviewer-b", "scores": dict(FULL_SCORES, fluency=4)}))
    fixture.write()

    assert ledger.main(fixture.argv(seed=11)) == ledger.EXIT_OK

    sample = fixture.sample_rows()
    by_id = {row["source_sha256"]: row for row in sample}
    for sid in disagreeing_ids:
        assert sid in by_id, "every score-disagreement row must be sampled"
        assert by_id[sid]["score_disagreement"] is True
    outside = [sid for sid in disagreeing_ids
               if by_id[sid]["audit_stratum"] == "score_disagreement"]
    audit = fixture.manifest()["human_audit"]
    assert audit["score_disagreement_rows"] == len(disagreeing_ids)
    assert audit["score_disagreement_outside_quota"] == len(outside)
    assert len(sample) == 40 + len(outside), "disagreement rows must not consume a stratified seat"


def test_score_disagreement_requires_two_passing_verdicts(tmp_path):
    """Differing scores with a non-PASS verdict is not a spec disagreement row."""
    fixture = Fixture(tmp_path)
    source, translation = text_pair(5)
    sid = fixture.add(source, translation, risk="low",
                      review={"reviewer_id": "reviewer-a", "scores": dict(FULL_SCORES)},
                      second_review={"reviewer_id": "reviewer-b", "verdict": "REVIEW",
                                     "scores": dict(FULL_SCORES, fluency=4)})
    fixture.write()

    assert ledger.main(fixture.argv(seed=11)) == ledger.EXIT_OK

    sample = fixture.sample_rows()
    assert [row["source_sha256"] for row in sample] == [sid]
    assert "score_disagreement" not in sample[0]
    assert fixture.manifest()["human_audit"]["score_disagreement_rows"] == 0


def test_missing_risk_input_is_refused_unless_explicitly_allowed(tmp_path):
    """Fail closed: without --risk every row would silently route as low (audit GAP (e))."""
    fixture = Fixture(tmp_path)
    fixture.pair(6, risk="high", review={})
    fixture.write()

    with pytest.raises(SystemExit) as excinfo:
        ledger.main(fixture.argv(include_risk=False))
    assert excinfo.value.code == ledger.EXIT_INVALID

    argv = fixture.argv(include_risk=False) + ["--allow-missing-risk"]
    assert ledger.main(argv) == ledger.EXIT_OK

    manifest = fixture.manifest()
    assert manifest["counts"]["accepted"] == 1
    assert manifest["counts"]["accepted_without_risk_row"] == 1
    assert manifest["counts"]["by_risk"] == {"low": 1, "medium": 0, "high": 0, "critical": 0}
    assert manifest["policy"]["risk_authority"]["missing_risk_allowed"] is True


def test_writes_only_inside_the_out_dir_and_leaves_no_staging(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.pair(7, review={})
    fixture.write()
    before = {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()}
    input_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in fixture.paths.values()}

    assert ledger.main(fixture.argv()) == ledger.EXIT_OK

    created = {path.relative_to(tmp_path).as_posix()
               for path in tmp_path.rglob("*") if path.is_file()} - before
    assert created
    prefix = fixture.out_dir.relative_to(tmp_path).as_posix() + "/"
    assert all(name.startswith(prefix) for name in created), sorted(created)
    assert not list(fixture.out_dir.glob(".ledger-staging-*"))
    for path, digest in input_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, path
    for entry in fixture.manifest()["outputs"].values():
        recorded = Path(entry["path"])
        if not recorded.is_absolute():
            recorded = REPO / recorded
        assert recorded.resolve().is_relative_to(fixture.out_dir.resolve())
