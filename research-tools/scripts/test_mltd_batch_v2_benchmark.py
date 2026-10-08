"""Batch V2 isolated contract/regression tests: no real gateway calls."""
import argparse
import asyncio
import json

import pytest

from scripts import mltd_batch_v2_benchmark as v2
from scripts import translate_mltd_api_pool as legacy


def row(text: str, task="UI", key="ld_menu", speaker=None):
    return {"source": text, "source_sha256": legacy.source_id(text),
            "task_hint": task, "examples": [{"key": key}],
            "usage_profile": {"speaker_codes": speaker or []}}


def test_json_id_contract_rejects_duplicates_missing_extra_and_wrong_types():
    a, b = row("お知らせ")["source_sha256"], row("プレゼント")["source_sha256"]
    valid = json.dumps({"translations": [
        {"id": b, "translation": "礼物"}, {"id": a, "translation": "公告"}]})
    assert v2.parse_batch(valid, {a, b}) == {a: "公告", b: "礼物"}
    bad = [
        '{"translations":[]}', '{"translations":[{"id":"bad","translation":"错误"}]}',
        json.dumps({"translations": [{"id": a, "translation": "1"},
                                     {"id": a, "translation": "2"}]}),
        json.dumps({"translations": [{"id": a, "translation": "1"},
                                     {"id": b, "translation": ""}]}),
        json.dumps({"translations": [{"id": a, "translation": "1"},
                                     {"id": b, "translation": "2", "source": "x"}]}),
        ("X" * 3) + valid,
    ]
    for raw in bad:
        with pytest.raises((ValueError, json.JSONDecodeError)):
            v2.parse_batch(raw, {a, b})


def test_batch_prompt_replaces_original_single_line_contract():
    original = ("HEADER\nOUTPUT CONTRACT\n"
                "- Return ONLY the final Simplified Chinese translation of SOURCE. "
                "No JSON, labels.\n\nKeep terms.\n")
    updated = v2.batch_system_prompt(original)
    assert "Return ONLY one JSON object" in updated
    assert "No JSON, labels" not in updated
    assert "Keep terms." in updated
    with pytest.raises(ValueError):
        v2.batch_system_prompt("no contract")


def test_sample_verification_rejects_mutated_identity():
    data = [row("一"), row("二")]
    v2.verify_sample(data, 2)
    with pytest.raises(ValueError):
        v2.verify_sample(data * 2, 4)
    with pytest.raises(ValueError):
        v2.verify_sample([dict(data[0], source="fake"), data[1]], 2)


def test_batch_partition_protects_speaker_and_task_and_caps():
    rows = [row(f"選択{i}", task="UI", key="ld_menu_a") for i in range(18)]
    rows += [row("あかり", "DIALOGUE", "story_1", ["a"])]
    rows += [row("あおい", "DIALOGUE", "story_1", ["b"])]
    batch = v2.batches(rows, "8")
    assert sorted(len(x) for x in batch) == [1, 1, 2, 8, 8]
    assert all(len(set(v2.legacy.classify_task(r) for r in g)) == 1 for g in batch)
    assert [len(x) for x in v2.batches(rows[:18], "dynamic")] == [18]


def test_individual_marker_restoration_does_not_cross_rows():
    a = row("報酬15個 __MLTD_TOKEN_000__")
    b = row("報酬25個 __MLTD_TOKEN_000__")
    prepared = [v2.prepare(r, {}) for r in (a, b)]
    assert "__MLTD_NUMBER_000__" in prepared[0]["masked"]
    assert "__MLTD_NUMBER_000__" in prepared[1]["masked"]
    assert prepared[0]["numbers"] != prepared[1]["numbers"]


def test_mock_run_resumes_idempotently_and_only_writes_isolated(tmp_path):
    r = [row("お知らせ"), row("プレゼント")]
    mock = argparse.Namespace(live=False, max_requests=0)
    system = "X\nOUTPUT CONTRACT\n- single only.\n\nRemaining."
    glossary = {"kana_allowlist": []}
    first = asyncio.run(v2.run_arm(r, "8", mock, None, system, {},
                                   glossary, tmp_path))
    assert first["accepted_total"] == 2
    assert first["requests_this_run"] == 1
    second = asyncio.run(v2.run_arm(r, "8", mock, None, system, {},
                                    glossary, tmp_path))
    assert second["accepted_total"] == 2
    assert second["requests_this_run"] == 0
    assert len(v2.load_jsonl(tmp_path / "8" / "translations.jsonl")) == 2


def test_live_guard_before_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(v2, "production_active", lambda: True)
    args = v2.parser().parse_args(["--live", "--sample", str(tmp_path / "no-sample")])
    with pytest.raises(RuntimeError, match="production translator"):
        asyncio.run(v2.async_main(args))


def test_split_failed_batch_salvages_correct_rows(tmp_path, monkeypatch):
    r = [row("お知らせ"), row("プレゼント"), row("ミッション"), row("ライブ開始")]
    mock = argparse.Namespace(live=False, max_requests=0)
    system = "X\nOUTPUT CONTRACT\n- single only.\n\nRemaining."
    original = v2.parse_batch
    calls = {"n": 0}

    def failing_once(raw, expected, *, allow_missing=False):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ValueError("malformed batch")
        return original(raw, expected, allow_missing=allow_missing)
    monkeypatch.setattr(v2, "parse_batch", failing_once)
    result = asyncio.run(v2.run_arm(r, "8", mock, None, system, {},
                                    {"kana_allowlist": []}, tmp_path))
    assert result["accepted_total"] == 4
    assert result["requests_this_run"] == 3
    assert result["details"]["split_retries"] == 1

def test_terminal_mock_failure_is_durable_not_replayed(tmp_path):
    rows = [row("茜ちゃん")]
    args = argparse.Namespace(live=False, max_requests=0)
    system = "X\nOUTPUT CONTRACT\n- single only.\n\nRemaining."
    first = asyncio.run(v2.run_arm(rows, "single", args, None, system, {},
                                   {"kana_allowlist": []}, tmp_path))
    assert first["terminal_failed_total"] == 1
    assert first["requests_total"] == 1
    second = asyncio.run(v2.run_arm(rows, "single", args, None, system, {},
                                    {"kana_allowlist": []}, tmp_path))
    assert second["requests_this_run"] == 0
    assert second["requests_total"] == 1
    assert len(v2.load_jsonl(tmp_path / "single" / "failed.jsonl")) == 1


def test_partial_batch_salvages_known_id_then_retries_missing_only(tmp_path, monkeypatch):
    rows = [row("お知らせ"), row("プレゼント")]
    args = argparse.Namespace(live=False, max_requests=0)
    system = "X\nOUTPUT CONTRACT\n- single only.\n\nRemaining."
    original = v2.parse_batch

    def remove_last(raw, expected, *, allow_missing=False):
        data = json.loads(raw)
        if len(data["translations"]) > 1:
            data["translations"] = data["translations"][:-1]
        return original(json.dumps(data), expected, allow_missing=allow_missing)

    monkeypatch.setattr(v2, "parse_batch", remove_last)
    result = asyncio.run(v2.run_arm(rows, "8", args, None, system, {},
                                    {"kana_allowlist": []}, tmp_path))
    assert result["accepted_total"] == 2
    assert result["requests_total"] == 2
    assert result["details"]["partial_batch_salvaged"] == 1


def test_live_max_requests_applies_inside_split(tmp_path, monkeypatch):
    rows = [row("お知らせ"), row("プレゼント")]
    args = argparse.Namespace(live=False, max_requests=1)
    system = "X\nOUTPUT CONTRACT\n- single only.\n\nRemaining."

    def always_fail(*args, **kwargs):
        raise ValueError("fake corrupt batch")
    monkeypatch.setattr(v2, "parse_batch", always_fail)
    result = asyncio.run(v2.run_arm(rows, "8", args, None, system, {},
                                    {"kana_allowlist": []}, tmp_path))
    assert result["requests_total"] == 1
    assert result["completed_total"] == 0
    assert result["terminal_failed_total"] == 0




def test_paired_review_same_source_identity_and_missing_rows(tmp_path):
    import hashlib
    from scripts import mltd_batch_v2_compare as compare
    from scripts.mltd_batch_v2_benchmark import save_jsonl
    sample = tmp_path / "sample.jsonl"
    sample_rows = [row("お知らせ"), row("プレゼント")]
    save_jsonl(sample, sample_rows)
    live = tmp_path / "live"
    live.mkdir()
    (live / "manifest.json").write_text(
        json.dumps({"sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest()}),
        encoding="utf8")
    for arm in ("single", "dynamic"):
        sub = live / arm
        sub.mkdir()
        (sub / "summary.json").write_text(
            json.dumps({"kind": "live", "requests_total": 2,
                        "prompt_tokens": 120, "cached_tokens": 20,
                        "completion_tokens": 10, "reasoning_tokens": 2}),
            encoding="utf8")
        save_jsonl(sub / "translations.jsonl", [
            {"source_sha256": sample_rows[0]["source_sha256"],
             "translation": "公告" if arm == "single" else "通知"},
        ])
        save_jsonl(sub / "failed.jsonl", [])
    result = compare.review(sample, live, "single", "dynamic")
    assert result["pair_counts"]["different_pairs"] == 1
    assert result["pair_counts"]["incomplete_pairs"] == 1
    assert result["all_sources_accounted_for"] is False
    assert result["eligible_for_full_AB_savings"] is False
    assert result["request_first_objective"]["request_reduction_percent"] is None
    assert result["request_first_objective"]["eligible_for_production_auto_merge"] is False
    assert result["metrics"]["single"]["uncached_prompt_tokens"] == 100
    assert (live / "human-review-single-vs-dynamic.csv").is_file()
    poisoned = live / "single" / "summary.json"
    bad = json.loads(poisoned.read_text(encoding="utf8"))
    bad["eligible_for_fair_AB_comparison"] = False
    poisoned.write_text(json.dumps(bad), encoding="utf8")
    with pytest.raises(ValueError, match="contaminated"):
        compare.review(sample, live, "single", "dynamic")



def test_live_transport_retries_same_batch_without_splitting(tmp_path, monkeypatch):
    import httpx
    rows = [row("お知らせ"), row("プレゼント")]
    args = argparse.Namespace(live=True, max_requests=0)
    cfg = legacy.ModelConfig(
        id="codex", model="fake", api_protocol="responses",
        endpoint="http://not-called.invalid", api_key_env="",
        concurrency=1, reasoning_effort="none", temperature=0,
        timeout=3, retries=1,
        request_params_by_task={"UI": {"max_tokens": 256}},
    )
    calls = []

    async def fake_request(client, model, system, user, params):
        calls.append(json.loads(user))
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("simulated transport disconnect")
        ids = [r["id"] for r in calls[-1]["items"]]
        return (json.dumps({"translations": [
            {"id": ids[0], "translation": "公告"},
            {"id": ids[1], "translation": "礼物"},
        ]}), {"usage": {"prompt_tokens": 45, "completion_tokens": 20,
                        "cached_tokens": 0, "reasoning_tokens": 0}})
    monkeypatch.setattr(legacy, "request_translation", fake_request)
    result = asyncio.run(v2.run_arm(
        rows, "8", args, cfg, "X\nOUTPUT CONTRACT\n- single.\n\nMore.", {},
        {"kana_allowlist": []}, tmp_path))
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert result["requests_total"] == 2
    assert result["accepted_total"] == 2
    assert result["details"]["transient_transport_attempts"] == 1
    assert result["details"].get("split_retries", 0) == 0



def test_model_not_found_aborts_single_and_keeps_pending(tmp_path, monkeypatch):
    """A gateway HTTP 400 must not become hundreds of terminal source failures."""
    rows = [row("お知らせ"), row("プレゼント")]
    args = argparse.Namespace(live=True, max_requests=0)
    cfg = legacy.ModelConfig(
        id="codex", model="fake", api_protocol="responses",
        endpoint="http://not-called.invalid", api_key_env="",
        concurrency=1, reasoning_effort="none", temperature=0,
        timeout=3, retries=1,
    )
    calls = []

    async def failing_translator(client, model, limiter, system, row_, glossary,
                                 terms, stats):
        calls.append(row_["source_sha256"])
        stats[model.id]["requests"] += 1
        return None, ('APIRequestError:HTTP 400 retry_after=\'\': '
                      '{"error":{"message":"unknown provider for model fake"}}')
    monkeypatch.setattr(legacy, "translate_with_model", failing_translator)
    with pytest.raises(v2.ProviderUnavailable, match="pending"):
        asyncio.run(v2.run_arm(rows, "single", args, cfg,
                               "X\nOUTPUT CONTRACT\n- single.\n\nMore.",
                               {}, {"kana_allowlist": []}, tmp_path))
    assert len(calls) == 1, "must halt immediately after provider routing failure"
    assert not v2.load_jsonl(tmp_path / "single" / "failed.jsonl")
    assert not v2.load_jsonl(tmp_path / "single" / "translations.jsonl")
    summary = json.loads((tmp_path / "single" / "summary.json").read_text())
    assert summary["status"] == "blocked_provider"
    assert summary["requests_total"] == 1
    assert summary["completed_total"] == 0


def test_provider_error_classifier_covers_unavailable_and_ignores_qa():
    assert v2.is_provider_failure("APIRequestError:HTTP 400")
    assert v2.is_provider_failure("quota_exhausted:HTTP 429")
    assert v2.is_provider_failure("RemoteProtocolError:broken stream")
    assert not v2.is_provider_failure("blocking_qa:preferred_term_missing")
    assert not v2.is_provider_failure("empty_translation")



def test_model_catalog_preflight_blocks_unadvertised_luna_without_post(monkeypatch):
    from types import SimpleNamespace

    class Catalog:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"data": [
                                       {"id": "gemini-3.8-flash-high"}]})
    monkeypatch.setattr(v2.httpx, "AsyncClient", lambda **kwargs: Catalog())
    cfg = legacy.ModelConfig(
        id="codex", model="gpt-5.6-luna", api_protocol="responses",
        endpoint="https://example.invalid/v1/responses", api_key_env="",
        concurrency=1, reasoning_effort="none", temperature=0,
        timeout=3, retries=1,
    )
    with pytest.raises(v2.ProviderUnavailable, match="not advertised"):
        asyncio.run(v2.check_model_catalog(cfg))


def test_model_catalog_preflight_accepts_advertised_model(monkeypatch):
    from types import SimpleNamespace

    class Catalog:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, url, **_kwargs):
            assert url == "https://example.invalid/v1/models"
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"data": [
                                       {"id": "gpt-5.6-luna"}]})
    monkeypatch.setattr(v2.httpx, "AsyncClient", lambda **kwargs: Catalog())
    cfg = legacy.ModelConfig(
        id="codex", model="gpt-5.6-luna", api_protocol="responses",
        endpoint="https://example.invalid/v1/responses", api_key_env="",
        concurrency=1, reasoning_effort="none", temperature=0,
        timeout=3, retries=1,
    )
    asyncio.run(v2.check_model_catalog(cfg))



def test_single_live_caps_internal_retries_without_changing_concurrency(
        tmp_path, monkeypatch):
    rows = [row("お知らせ"), row("プレゼント")]
    cfg = legacy.ModelConfig(
        id="codex", model="fake", api_protocol="responses",
        endpoint="http://not-called.invalid", api_key_env="",
        concurrency=1, reasoning_effort="none", temperature=0,
        timeout=3, retries=5)
    args = argparse.Namespace(live=True, max_requests=1)
    received = []

    async def translate(_client, model, _limiter, _system, source, _glossary,
                        _terms, stats):
        received.append((model.retries, model.concurrency, source["source_sha256"]))
        for _ in range(model.retries):
            stats[model.id]["requests"] += 1
        return {"source_sha256": source["source_sha256"], "source": source["source"],
                "translation": "公告", "status": "machine_translated"}, ""

    monkeypatch.setattr(legacy, "translate_with_model", translate)
    result = asyncio.run(v2.run_arm(
        rows, "single", args, cfg, "X\nOUTPUT CONTRACT\n- single.\n\nMore.",
        {}, {"kana_allowlist": []}, tmp_path))
    assert result["requests_total"] == 1
    assert result["accepted_total"] == 1
    assert len(received) == 1
    assert received[0][:2] == (1, 1)
    assert cfg.retries == 5 and cfg.concurrency == 1


def test_transport_retry_does_not_exceed_one_request_cap(tmp_path, monkeypatch):
    import httpx
    rows = [row("お知らせ"), row("プレゼント")]
    args = argparse.Namespace(live=True, max_requests=1)
    cfg = legacy.ModelConfig(
        id="codex", model="fake", api_protocol="responses",
        endpoint="http://not-called.invalid", api_key_env="",
        concurrency=1, reasoning_effort="none", temperature=0,
        timeout=3, retries=5, request_params_by_task={"UI":{"max_tokens":256}})
    calls = []

    async def fake_request(*_args, **_kwargs):
        calls.append(1)
        raise httpx.RemoteProtocolError("simulated disconnect")

    monkeypatch.setattr(legacy, "request_translation", fake_request)
    with pytest.raises(v2.ProviderUnavailable, match="request cap"):
        asyncio.run(v2.run_arm(
            rows, "8", args, cfg, "X\nOUTPUT CONTRACT\n- single.\n\nMore.",
            {}, {"kana_allowlist": []}, tmp_path))
    assert len(calls) == 1
    assert json.loads((tmp_path / "8" / "summary.json").read_text())[
        "requests_total"] == 1
    assert not v2.load_jsonl(tmp_path / "8" / "failed.jsonl")



def test_readonly_full_sample_audit_distinguishes_pending_and_quarantined(tmp_path):
    import hashlib
    from scripts import mltd_batch_v2_audit as audit_module
    sample = tmp_path / "sample.jsonl"
    sample_rows = [row("お知らせ"), row("プレゼント")]
    v2.save_jsonl(sample, sample_rows)
    live = tmp_path / "live"
    live.mkdir()
    (live / "manifest.json").write_text(json.dumps({
        "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
        "count": 2, "model": "gpt-5.6-luna"}), encoding="utf8")
    single = live / "single"
    single.mkdir()
    v2.save_jsonl(single / "translations.jsonl", [{
        "source_sha256": sample_rows[0]["source_sha256"],
        "source": sample_rows[0]["source"], "translation": "公告",
        "benchmark": {"mock": False}}])
    v2.save_jsonl(single / "failed.jsonl", [])
    initial = audit_module.audit(sample, live, ("single", "dynamic"))
    assert initial["arms"]["single"]["accepted"] == 1
    assert initial["arms"]["single"]["pending"] == 1
    assert initial["all_arms_complete_and_fair"] is False
    v2.save_jsonl(single / "provider-route-400-quarantine-20260920.jsonl",
                  [{"source_sha256": sample_rows[1]["source_sha256"],
                    "error": "unknown provider"}])
    (single / "summary.json").write_text(json.dumps({
        "accepted_total": 1, "terminal_failed_total": 0,
        "eligible_for_fair_AB_comparison": False,
        "status": "partial_provider_route_rejections_quarantined"}), encoding="utf8")
    second = audit_module.audit(sample, live, ("single",))
    assert second["arms"]["single"]["provider_rejections_quarantined"] == 1
    assert second["arms"]["single"]["pending"] == 1
    assert second["arms"]["single"]["eligible_for_fair_AB_comparison"] is False


def test_readonly_audit_rejects_duplicate_ids(tmp_path):
    import hashlib
    from scripts import mltd_batch_v2_audit as audit_module
    sample = tmp_path / "sample.jsonl"
    sample_rows = [row("お知らせ"), row("プレゼント")]
    v2.save_jsonl(sample, sample_rows)
    live = tmp_path / "live"
    live.mkdir()
    (live / "manifest.json").write_text(json.dumps({
        "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
        "count": 2, "model": "gpt-5.6-luna"}), encoding="utf8")
    single = live / "single"
    single.mkdir()
    dup = {"source_sha256": sample_rows[0]["source_sha256"],
           "source": sample_rows[0]["source"], "translation": "公告",
           "benchmark": {"mock": False}}
    v2.save_jsonl(single / "translations.jsonl", [dup, dup])
    with pytest.raises(ValueError, match="duplicate"):
        audit_module.audit(sample, live, ("single",))



def test_request_first_report_requires_complete_paired_quality(tmp_path):
    import hashlib
    from scripts import mltd_batch_v2_compare as compare
    sample = tmp_path / "sample.jsonl"
    sources = [row("お知らせ"), row("プレゼント")]
    v2.save_jsonl(sample, sources)
    live = tmp_path / "live"
    live.mkdir()
    (live / "manifest.json").write_text(json.dumps({
        "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
        "count": 2, "model": "gpt-5.6-luna"}), encoding="utf8")
    for arm, requests, seconds in (("single", 12, 120), ("dynamic", 6, 60)):
        folder = live / arm
        folder.mkdir()
        v2.save_jsonl(folder / "translations.jsonl", [
            {"source_sha256": src["source_sha256"], "source": src["source"],
             "translation": "公告", "benchmark": {"mock": False}} for src in sources])
        v2.save_jsonl(folder / "failed.jsonl", [])
        (folder / "summary.json").write_text(json.dumps({
            "kind": "live", "requests_total": requests, "elapsed_seconds": seconds,
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
            "reasoning_tokens": 0, "accepted_total": 2,
            "terminal_failed_total": 0}), encoding="utf8")
    report = compare.review(sample, live, "single", "dynamic")
    goal = report["request_first_objective"]
    assert report["eligible_for_full_AB_savings"] is True
    assert goal["request_reduction_percent"] == 50.0
    assert goal["accepted_throughput_gain_percent"] == 100.0
    assert goal["wall_time_reduction_percent"] == 50.0
    assert goal["observed_request_target_met"] is True
    assert goal["observed_quality_gate_met"] is True
    assert goal["human_review_pending"] is True
    assert goal["eligible_for_production_auto_merge"] is False

    # Re-running paired summaries after more source IDs arrive must never
    # silently erase a human translator's completed notes.
    import csv
    output = live / "human-review-single-vs-dynamic.csv"
    with output.open(encoding="utf-8-sig", newline="") as reader:
        records = list(csv.DictReader(reader))
        columns = list(records[0])
    records[0]["human_review"] = "approved"
    records[0]["human_notes"] = "explicit manual correction kept"
    with output.open("w", encoding="utf-8-sig", newline="") as writer_file:
        writer = csv.DictWriter(writer_file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
    compare.review(sample, live, "single", "dynamic")
    with output.open(encoding="utf-8-sig", newline="") as reader:
        reread = list(csv.DictReader(reader))
    assert reread[0]["human_review"] == "approved"
    assert reread[0]["human_notes"] == "explicit manual correction kept"


def test_opt_in_mixed_speaker_batches_keep_per_item_context_and_are_fewer():
    rows = [
        row(f"セリフ{i}", task="DIALOGUE", key=f"story_{i}",
            speaker=["speaker_a" if i % 2 == 0 else "speaker_b"])
        for i in range(12)
    ]
    strict = v2.batches(rows, "dynamic")
    mixed = v2.batches(rows, "dynamic-mixed")
    assert len(strict) == 2
    assert len(mixed) == 2  # cap six even when speakers differ
    assert [len(x) for x in mixed] == [6, 6]
    assert len({legacy.speaker_info(x)[0][0] for x in strict[0]}) == 1
    assert len({legacy.speaker_info(x)[0][0] for x in mixed[0]}) == 2
    assert {x["source_sha256"] for group in mixed for x in group} == {
        x["source_sha256"] for x in rows}
    for group in mixed:
        for source in group:
            prompt = legacy.build_user_prompt(source, source["source"])
            code = legacy.speaker_info(source)[0][0]
            assert "SPEAKER=" + code in prompt
            assert source["source"] in prompt


def test_opt_in_mixed_preserves_split_qa_and_source_ids(tmp_path):
    rows = [
        row("一つ" + str(i), "DIALOGUE", "story_" + str(i),
            ["speaker_a" if i % 2 else "speaker_b"])
        for i in range(8)
    ]
    mock = argparse.Namespace(live=False, max_requests=0)
    system = "X\nOUTPUT CONTRACT\n- one source only.\n\nRemain."
    report = asyncio.run(v2.run_arm(
        rows, "dynamic-mixed", mock, None, system, {},
        {"kana_allowlist": []}, tmp_path))
    assert report["completed_total"] == 8
    assert report["terminal_failed_total"] + report["accepted_total"] == 8
    assert len({x["source_sha256"] for x in v2.load_jsonl(
        tmp_path / "dynamic-mixed" / "translations.jsonl")}) == report[
        "accepted_total"]

def test_full_live_requires_explicit_authorization(monkeypatch, tmp_path):
    monkeypatch.setattr(v2, "production_active", lambda: False)
    args = v2.parser().parse_args(["--live", "--sample", str(tmp_path / "no-sample")])
    with pytest.raises(ValueError, match="max-requests"):
        asyncio.run(v2.async_main(args))

