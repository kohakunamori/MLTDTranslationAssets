"""No-provider synthetic regression for build_translation_release_ledger.py."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from scripts import build_translation_release_ledger as mod
from scripts.mltd_translation_quality import evaluate_row, source_id

TRANSLATOR = {"provider": "https://translator.example/v1/responses", "model_id": "primary",
              "model": "t-model", "task": "DIALOGUE"}
FULL = {"semantic_accuracy": 5, "terminology": 5, "fluency": 5,
        "character_voice": 5, "context_consistency": 5}


def save(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def review(cand: dict, reviewer_id: str = "rev-A", scores: dict | None = None, **extra) -> dict:
    row = {"source_sha256": cand["source_sha256"], "source": cand["source"],
           "translation": cand["translation"], "verdict": "PASS", "scores": dict(scores or FULL),
           "blocking_errors": [], "notes": "", "reviewer_provenance": "review:openai-compatible",
           "reviewer_model": "r-model", "reviewer_id": reviewer_id}
    row.update(extra)
    return row


class Fixture:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.universe: list[dict] = []
        self.candidates: list[dict] = []
        self.reviews: list[dict] = []
        self.second: list[dict] = []
        self.risk: list[dict] = []
        self.glossary = tmp / "glossary.json"
        self.glossary.write_text('{"entries":{},"kana_allowlist":[]}', encoding="utf-8")

    def source(self, text: str) -> str:
        sid = source_id(text)
        self.universe.append({"source_sha256": sid, "source": text, "translation": "",
                              "status": "pending"})
        return sid

    def cand(self, text: str, translation: str, provenance=None, status="machine_translated",
             with_review=True, risk="low") -> dict:
        sid = self.source(text)
        row = {"source_sha256": sid, "source": text, "translation": translation,
               "status": status,
               "provenance": dict(TRANSLATOR) if provenance is None else provenance}
        self.candidates.append(row)
        if with_review:
            self.reviews.append(review(row))
        self.risk.append({"source_sha256": sid, "risk_level": risk})
        return row

    def write(self) -> dict[str, Path]:
        d = self.tmp / "in"
        glossary = {"entries": {}, "kana_allowlist": []}
        qa = [evaluate_row({"source_sha256": c["source_sha256"], "source": c["source"]},
                           c, glossary)
              for c in self.candidates]
        return {
            "universe": save(d / "universe.jsonl", self.universe),
            "candidates": save(d / "candidates.jsonl", self.candidates),
            "qa": save(d / "qa.jsonl", qa),
            "reviews": save(d / "reviews.jsonl", self.reviews),
            "second": save(d / "second.jsonl", self.second),
            "risk": save(d / "risk.jsonl", self.risk),
        }

    def argv(self, paths: dict[str, Path], *extra: str, out: Path | None = None,
             candidates: list[Path] | None = None) -> list[str]:
        argv = ["--surface", "nongtx", "--universe", str(paths["universe"]),
                "--expected-count", str(len(self.universe)), "--glossary", str(self.glossary),
                "--qa", str(paths["qa"]), "--reviews", str(paths["reviews"]),
                "--second-reviews", str(paths["second"]), "--risk", str(paths["risk"])]
        for c in candidates or [paths["candidates"]]:
            argv += ["--candidates", str(c)]
        if out is not None:
            argv += ["--out-dir", str(out)]
        return argv + list(extra)


def run(fx: Fixture, *extra: str, out: Path | None = None, **kw):
    paths = fx.write()
    out = out or fx.tmp / "out"
    code = mod.main(fx.argv(paths, *extra, out=out, candidates=kw.pop("candidates", None)), **kw)
    manifest_path = out / "release-ledger-nongtx.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    return code, out, manifest, paths


def by_sid(rows: list[dict]) -> dict[str, dict]:
    return {r["source_sha256"]: r for r in rows}


def test_complete_happy_path_needs_passing_audit(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.cand("こんにちは", "你好")
    fx.cand("ありがとう", "谢谢", risk="medium")
    code, out, manifest, paths = run(fx)
    assert code == 0
    assert manifest["schema"] == "mltd-translation-release-ledger/v1"
    assert manifest["frozen_identity"] == {"client": "9.0.200", "assets": "1077100"}
    assert manifest["complete"] is True and manifest["release_ready"] is False
    assert manifest["human_audit"]["status"] == "not_started"
    assert manifest["counts"]["accepted"] == 2
    assert manifest["counts"]["by_risk"] == {"low": 1, "medium": 1, "high": 0, "critical": 0}
    assert set(manifest["inputs"]) >= {"universe", "glossary", "candidates_1", "qa",
                                       "reviews", "second_reviews", "risk"}
    assert manifest["inputs"]["universe"]["rows"] == 2
    assert manifest["policy"]["reviewer_must_differ_from_translator"] is True
    assert len(manifest["policy"]["gate_sha256"]) == 64
    ledger = load(out / "release-ledger-nongtx.jsonl")
    row = by_sid(ledger)[a["source_sha256"]]
    assert row["release_gate"] == "accepted" and row["release_reasons"] == []
    assert row["qa_verdict"] == "PASS" and row["review"]["reviewer_id"] == "rev-A"
    assert mod.sha256_file(out / "release-ledger-nongtx.jsonl") == manifest["outputs"]["ledger"]["sha256"]
    sample = load(out / "audit-sample-nongtx.jsonl")
    assert len(sample) == 2 and manifest["human_audit"]["sampled"] == 2
    assert not list(out.glob(".ledger-staging-*"))

    results = save(tmp_path / "audit.jsonl",
                   [{"source_sha256": s["source_sha256"], "judgement": "ok"} for s in sample])
    code, _, manifest, _ = run(fx, "--audit-results", str(results))
    assert code == 0
    assert manifest["human_audit"]["status"] == "passed" and manifest["release_ready"] is True


def test_explicit_asset_identity_is_recorded_for_1077500(tmp_path):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好")
    code, _, manifest, _ = run(fx, "--client-version", "9.0.200", "--asset-version", "1077500")
    assert code == 0
    assert manifest["frozen_identity"] == {"client": "9.0.200", "assets": "1077500"}


def test_missing_candidate_and_qa_reject(tmp_path):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好")
    missing = fx.source("さようなら")
    bad = fx.cand("3人です", "4个人")
    code, out, manifest, _ = run(fx)
    assert code == 0 and manifest["complete"] is False
    assert manifest["counts"]["missing_candidate"] == 1 and manifest["counts"]["rejected"] == 1
    queue = by_sid(load(out / "needs-review-nongtx.jsonl"))
    assert queue[missing]["release_reasons"] == ["missing_candidate"]
    assert queue[missing]["source"] == "さようなら"
    assert queue[bad["source_sha256"]]["release_gate"] == "rejected"
    assert queue[bad["source_sha256"]]["release_reasons"] == ["deterministic_qa_reject"]


def test_missing_qa_row_rejected(tmp_path):
    fx = Fixture(tmp_path)
    c = fx.cand("こんにちは", "你好")
    paths = fx.write()
    save(paths["qa"], [])
    code = mod.main(fx.argv(paths, out=tmp_path / "out"))
    assert code == 0
    row = load(tmp_path / "out" / "needs-review-nongtx.jsonl")[0]
    assert row["source_sha256"] == c["source_sha256"]
    assert row["release_reasons"] == ["deterministic_qa_missing"]


@pytest.mark.parametrize("prov,rev_kw", [
    (dict(TRANSLATOR), {"reviewer_id": "https://translator.example/v1/responses:primary"}),
    (dict(TRANSLATOR), {"reviewer_id": "", "reviewer_provenance": "review:https://translator.example/v1/responses",
                        "reviewer_model": "t-model"}),
    (str(dict(TRANSLATOR)), {"reviewer_id": "https://translator.example/v1/responses|t-model"}),
])
def test_reviewer_equal_to_translator_demoted(tmp_path, prov, rev_kw):
    fx = Fixture(tmp_path)
    c = fx.cand("こんにちは", "你好", provenance=prov, with_review=False)
    fx.reviews.append(review(c, **rev_kw))
    code, out, manifest, _ = run(fx)
    assert code == 0 and manifest["counts"]["accepted"] == 0
    row = load(out / "needs-review-nongtx.jsonl")[0]
    assert row["release_gate"] == "needs_review"
    assert row["release_reasons"] == ["reviewer_not_independent_of_translator"]


def test_translator_identity_parsing():
    known, forms, prov = mod.translator_identity(
        {"provenance": "{'provider': 'https://x/v1', 'model_id': 'primary', 'model': None"})
    assert known and prov["provider"] == "https://x/v1" and "https://x/v1|primary" in forms
    assert mod.translator_identity({"provenance": "machine"})[0] is False
    assert mod.translator_identity(
        {"provenance": {"provider": "deterministic:authoritative_terms", "model_id": None}})[0]


@pytest.mark.parametrize("prov", ["", "gtx_machine", {"provider": "", "model": "x"}])
def test_unknown_translator_identity_demoted(tmp_path, prov):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好", provenance=prov)
    code, out, manifest, _ = run(fx)
    assert code == 0 and manifest["counts"]["accepted"] == 0
    assert load(out / "needs-review-nongtx.jsonl")[0]["release_reasons"] == ["translator_identity_unknown"]


def test_official_reviewed_row_exempt(tmp_path):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好", provenance="official_legacy_zhcn",
            status="official_legacy_zhcn_reviewed", with_review=False)
    seed = fx.cand("ありがとう", "谢谢", provenance="official_legacy_zhtw_seed",
                   status="official_legacy_zhtw", with_review=False)
    code, out, manifest, _ = run(fx)
    assert code == 0 and manifest["counts"]["accepted"] == 1
    queue = load(out / "needs-review-nongtx.jsonl")
    assert queue[0]["source_sha256"] == seed["source_sha256"]
    assert "independent_review_missing" in queue[0]["release_reasons"]


def test_critical_requires_distinct_second_reviewer(tmp_path):
    fx = Fixture(tmp_path)
    same = fx.cand("こんにちは", "你好", risk="critical")
    distinct = fx.cand("ありがとう", "谢谢", risk="critical")
    missing = fx.cand("おはよう", "早上好", risk="critical")
    fx.second.append(review(same, reviewer_id="rev-A"))
    fx.second.append(review(distinct, reviewer_id="rev-B", scores=dict(FULL)))
    code, out, manifest, _ = run(fx)
    assert code == 0
    assert [r["source_sha256"] for r in load(out / "release-ledger-nongtx.jsonl")] == [distinct["source_sha256"]]
    queue = by_sid(load(out / "needs-review-nongtx.jsonl"))
    assert queue[same["source_sha256"]]["release_reasons"] == ["second_review_not_independent"]
    assert queue[missing["source_sha256"]]["release_reasons"] == ["second_independent_review_missing"]


def test_stale_review_not_used(tmp_path):
    fx = Fixture(tmp_path)
    c = fx.cand("こんにちは", "你好", with_review=False)
    fx.reviews.append(dict(review(c), translation="您好"))
    code, out, manifest, _ = run(fx)
    row = load(out / "needs-review-nongtx.jsonl")[0]
    assert "independent_review_stale" in row["release_reasons"] and "review" not in row


def test_conflicting_duplicate_candidates_first_wins(tmp_path):
    fx = Fixture(tmp_path)
    c = fx.cand("こんにちは", "你好")
    paths = fx.write()
    second = save(tmp_path / "in" / "later.jsonl", [dict(c, translation="哈喽")])
    dup = save(tmp_path / "in" / "dup.jsonl", [dict(c)])
    out = tmp_path / "out"
    code = mod.main(fx.argv(paths, out=out, candidates=[paths["candidates"], second, dup]))
    manifest = json.loads((out / "release-ledger-nongtx.manifest.json").read_text(encoding="utf-8"))
    assert code == 0
    assert manifest["counts"]["candidate_conflicts"] == 1
    assert manifest["counts"]["candidate_duplicates_identical"] == 1
    assert load(out / "release-ledger-nongtx.jsonl")[0]["translation"] == "你好"


def test_input_changed_mid_build_aborts_without_outputs(tmp_path):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好")
    paths = fx.write()
    out = tmp_path / "out"

    def tamper():
        with paths["candidates"].open("a", encoding="utf-8") as handle:
            handle.write("\n")

    code = mod.main(fx.argv(paths, out=out), before_final_check=tamper)
    assert code == mod.EXIT_INPUT_CHANGED
    assert list(out.iterdir()) == []


def test_invalid_universe_count_fails(tmp_path):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好")
    paths = fx.write()
    argv = fx.argv(paths, out=tmp_path / "out")
    argv[argv.index("--expected-count") + 1] = "5"
    assert mod.main(argv) == mod.EXIT_INVALID
    assert not (tmp_path / "out").exists()


def test_out_dir_under_production_refused(tmp_path):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好")
    paths = fx.write()
    target = mod.PRODUCTION_DIR / "ledger-test-should-not-exist"
    assert mod.main(fx.argv(paths, out=target)) == mod.EXIT_INVALID
    assert not target.exists()


def test_coverage_only_writes_nothing(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好")
    fx.source("さようなら")
    paths = fx.write()
    before = sorted(p for p in tmp_path.rglob("*"))
    assert mod.main(fx.argv(paths, "--coverage-only")) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["counts"]["accepted"] == 1 and printed["counts"]["missing_candidate"] == 1
    assert printed["complete"] is False
    assert sorted(p for p in tmp_path.rglob("*")) == before


def _many(tmp_path: Path, n: int) -> tuple[Fixture, list[dict]]:
    fx = Fixture(tmp_path)
    rows = [fx.cand(f"テキスト番号{i:03d}", f"文本编号{i:03d}") for i in range(n)]
    return fx, rows


@pytest.mark.parametrize("minors,criticals,status", [
    (1, 0, "passed"),   # 1/40 = 2.5% <= 4%
    (2, 0, "failed"),   # 2/40 = 5% > 4%
    (0, 1, "failed"),   # any critical fails
])
def test_audit_thresholds(tmp_path, minors, criticals, status):
    fx, _ = _many(tmp_path, 50)
    code, out, manifest, _ = run(fx)
    sample = load(out / "audit-sample-nongtx.jsonl")
    assert code == 0 and len(sample) == 40 and manifest["human_audit"]["seed"] == mod.DEFAULT_AUDIT_SEED
    judgements = ["minor"] * minors + ["critical"] * criticals
    judgements += ["ok"] * (len(sample) - len(judgements))
    results = save(tmp_path / "audit.jsonl", [
        {"source_sha256": s["source_sha256"], "judgement": j} for s, j in zip(sample, judgements)])
    code, _, manifest, _ = run(fx, "--audit-results", str(results))
    assert code == 0
    assert manifest["human_audit"]["status"] == status
    assert manifest["human_audit"]["minor_errors"] == minors
    assert manifest["human_audit"]["critical_errors"] == criticals
    assert manifest["release_ready"] is (status == "passed")


def test_audit_minor_ratio_boundary():
    sample = [{"source_sha256": f"{i:064x}", "risk_level": "low"} for i in range(100)]
    inputs = mod.Inputs()
    with tempfile.TemporaryDirectory() as d:
        for minors, expected in ((4, "passed"), (5, "failed")):
            path = save(Path(d) / f"r{minors}.jsonl", [
                {"source_sha256": s["source_sha256"], "judgement": "minor" if i < minors else "ok"}
                for i, s in enumerate(sample)])
            assert mod.evaluate_audit(sample, path, inputs)["status"] == expected
        partial = save(Path(d) / "partial.jsonl", [{"source_sha256": sample[0]["source_sha256"],
                                                    "judgement": "ok"}])
        assert mod.evaluate_audit(sample, partial, inputs)["status"] == "in_progress"


def test_audit_sample_is_stratified_and_reproducible(tmp_path):
    fx = Fixture(tmp_path)
    for i in range(70):
        fx.cand(f"テキスト番号{i:03d}", f"文本编号{i:03d}", risk="high")
    disagree = fx.cand("こんにちは", "你好", risk="critical")
    fx.second.append(review(disagree, reviewer_id="rev-B"))
    fx.second[-1]["scores"] = dict(FULL, extra=1)
    code, out, manifest, _ = run(fx)
    first = load(out / "audit-sample-nongtx.jsonl")
    strata = [s["audit_stratum"] for s in first]
    assert strata.count("high") == 60 and strata.count("critical") == 1
    code, out2, _, _ = run(fx, out=tmp_path / "out2")
    assert load(out2 / "audit-sample-nongtx.jsonl") == first


def test_score_disagreement_rows_added_outside_quota(tmp_path):
    fx = Fixture(tmp_path)
    for i in range(65):
        c = fx.cand(f"テキスト番号{i:03d}", f"文本编号{i:03d}", risk="critical")
        fx.second.append(review(c, reviewer_id="rev-B",
                                scores=dict(FULL, extra=1) if i % 2 else dict(FULL)))
    code, out, manifest, _ = run(fx)
    sample = load(out / "audit-sample-nongtx.jsonl")
    strata = [s["audit_stratum"] for s in sample]
    assert strata.count("critical") == 60
    extra = [s for s in sample if s["audit_stratum"] == "score_disagreement"]
    assert extra and all(s["review_scores"] != s["second_review_scores"] for s in extra)
    assert len(sample) == manifest["human_audit"]["sampled"]


def test_verify_ledger_accepts_builder_output_and_rejects_tamper(tmp_path, capsys):
    fx = Fixture(tmp_path)
    fx.cand("こんにちは", "你好")
    fx.cand("ありがとう", "谢谢", risk="critical")
    fx.second.append(review(fx.candidates[-1], reviewer_id="rev-B"))
    fx.cand("おはよう", "早上好", provenance="official_legacy_zhcn",
            status="official_legacy_zhcn_reviewed", with_review=False)
    code, out, manifest, paths = run(fx)
    assert code == 0 and manifest["complete"] is True
    ledger = out / "release-ledger-nongtx.jsonl"
    base = ["--surface", "nongtx", "--universe", str(paths["universe"]), "--expected-count", "3",
            "--glossary", str(fx.glossary)]
    capsys.readouterr()
    assert mod.main(base + ["--verify-ledger", str(ledger)]) == 0
    assert capsys.readouterr().out.startswith("PASS")

    rows = load(ledger)
    rows[0]["translation"] = "您好"  # review payload no longer bound to the translation
    tampered = save(tmp_path / "tampered.jsonl", rows)
    assert mod.main(base + ["--verify-ledger", str(tampered)]) == mod.EXIT_VERIFY_FAIL
    assert capsys.readouterr().out.startswith("FAIL")

    rows = load(ledger)
    rows[0]["review"]["reviewer_id"] = "https://translator.example/v1/responses:primary"
    assert mod.main(base + ["--verify-ledger", str(save(tmp_path / "t2.jsonl", rows))]) == 1

    incomplete = save(tmp_path / "t3.jsonl", load(ledger)[:2])
    assert mod.main(base + ["--verify-ledger", str(incomplete)]) == 1
