"""Regression tests for Batch V2 production provider retry, without API access."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import pytest

from scripts import mltd_batch_v2_production as production
from scripts import translate_mltd_api_pool as legacy


def _row(text: str) -> dict:
    return {
        "source": text,
        "source_sha256": legacy.source_id(text),
        "task_hint": "UI",
        "examples": [{"key": "ld_menu"}],
    }


@pytest.mark.parametrize(
    ("error", "failures", "expected_attempts", "expected_retry_delays", "recover"),
    [
        (legacy.APIRequestError(429, "0", "shared capacity"), 1, 2, [5.0], True),
        (legacy.APIRequestError(503, "", "gateway busy"), 2, 3, [2.0, 4.0], True),
        (legacy.APIRequestError(503, "", "gateway busy"), 3, 3, [2.0, 4.0], False),
        (legacy.APIRequestError(429, "", "daily limit"), 1, 1, [], False),
        (legacy.APIRequestError(400, "", "unknown model"), 1, 1, [], False),
    ],
)
def test_batch_transport_retry_does_not_defer_after_one_transient_error(
    monkeypatch, error, failures, expected_attempts, expected_retry_delays, recover
):
    cfg = legacy.ModelConfig(
        id="mock", model="mock", api_protocol="responses",
        endpoint="https://example.invalid/responses", api_key_env="",
        concurrency=1, reasoning_effort="none", temperature=0,
        timeout=5, retries=2,
    )
    rows = [_row("お知らせ"), _row("プレゼント")]
    calls = []
    sleeps = []

    async def mock_request(*_args, **_kwargs):
        calls.append(1)
        if len(calls) <= failures:
            raise error
        return json.dumps({"translations": [
            {"id": r["source_sha256"], "translation": "公告"} for r in rows
        ]}), {"latency_seconds": 0.1, "usage": {}}

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(legacy, "request_translation", mock_request)
    monkeypatch.setattr(legacy, "validate_candidate",
                        lambda source_row, *_args: (
                            {"source_sha256": source_row["source_sha256"]}, ""
                        ))
    monkeypatch.setattr(production.asyncio, "sleep", record_sleep)

    stats = defaultdict(lambda: defaultdict(float))
    result = asyncio.run(production.translate_group(
        [legacy.WorkItem(row=r) for r in rows],
        None, cfg, None, "system", "batch-system", {},
        {}, stats, "dynamic", lambda: True,
    ))
    assert len(calls) == expected_attempts
    assert sleeps == expected_retry_delays
    assert stats["mock"]["batch_requests"] == expected_attempts
    assert stats["mock"]["requests"] == expected_attempts
    assert stats["mock"]["request_errors"] == min(failures, expected_attempts)
    assert stats["mock"]["retries"] == len(expected_retry_delays)
    if recover:
        assert all(candidate is not None and not reason for candidate, reason in result.values())
        assert stats["mock"]["batch_accepted"] == len(rows)
    else:
        assert all(candidate is None and production.provider_error(reason)
                   for candidate, reason in result.values())
        assert not stats["mock"]["batch_accepted"]
        if error.status_code == 429 and "daily" in error.detail:
            assert stats["mock"]["quota_exhausted_errors"] == 1


def test_item_size_uses_exact_masked_request_not_unused_evidence():
    row = _row("お知らせ123")
    row["context_examples"] = [
        {"context": [
            {"relative": -1, "source": "前の文"},
            {"relative": 0, "source": row["source"]},
            {"relative": 1, "source": "次の文"},
        ], "unused_occurrence_metadata": "冗長" * 2000}
    ]
    terms = {"お知らせ": "公告"}
    prepared = production.v2.prepare(row, terms)
    expected = {
        "id": prepared["sid"],
        "task": prepared["task"],
        "context": legacy.build_user_prompt(row, prepared["masked"]),
    }
    assert production.item_size(row, terms) == (
        len(json.dumps(expected, ensure_ascii=False)) + 2
    )
    assert "冗長" not in expected["context"]
    assert "__MLTD_TERM_" in expected["context"]
    assert "__MLTD_NUMBER_" in expected["context"]
    assert production.item_size(row, terms) < 500


def test_producer_packs_actual_context_but_keeps_speaker_partition(tmp_path):
    rows = []
    for i in range(12):
        row = _row(f"お知らせ{i}")
        row["task_hint"] = "DIALOGUE"
        row["usage_profile"] = {"speaker_codes": ["idol_a" if i < 6 else "idol_b"]}
        row["context_examples"] = [{"context": [
            {"relative": -1, "source": "前のセリフ"},
            {"relative": 0, "source": row["source"]},
            {"relative": 1, "source": "次のセリフ"},
        ], "unused": "context evidence" * 500}]
        rows.append(row)
    source = tmp_path / "pending.jsonl"
    source.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    async def produce():
        queue = asyncio.Queue(maxsize=32)
        stats = {}
        await production.producer_batch(
            source, queue, set(), 0, stats, "dynamic", {},
        )
        groups = []
        while not queue.empty():
            group = queue.get_nowait()
            groups.append(group)
            queue.task_done()
        return stats, groups

    stats, groups = asyncio.run(produce())
    assert stats["selected_pending"] == 12
    assert stats["batch_groups"] == 2
    # Each of the two speaker partitions ends at six items and is flushed
    # at EOF, not by an overflow from a seventh item in that partition.
    assert stats["batch_item_limit_flushes"] == 0
    assert stats["batch_context_budget_flushes"] == 0
    assert stats["batch_peak_estimated_item_chars"] <= production.MAX_BATCH_CONTEXT_CHARS
    assert sorted(len(g) for g in groups) == [6, 6]
    for group in groups:
        assert len({
            tuple(x.row["usage_profile"]["speaker_codes"]) for x in group
        }) == 1
        assert sum(production.item_size(item.row, {}) for item in group) <= (
            production.MAX_BATCH_CONTEXT_CHARS
        )


def test_batch_v2_numbered_control_token_must_survive_qa():
    source = r"魔法みたいです\17" + "\\"
    row = _row(source)
    row["task_hint"] = "TITLE"
    prepared = production.v2.prepare(row, {})
    assert prepared["tokens"] == [r"\17" + "\\"]
    assert "__MLTD_TOKEN_000__" in prepared["masked"]

    bad, reason = legacy.validate_candidate(
        row, "像魔法一样17", [], [], {},
    )
    assert bad is None
    assert "protected token mismatch" in reason

    good, reason = legacy.validate_candidate(
        row, "像魔法一样__MLTD_TOKEN_000__",
        prepared["tokens"], prepared["terms"], {}, prepared["numbers"],
    )
    assert reason == ""
    assert good is not None
    assert good["translation"] == "像魔法一样" + prepared["tokens"][0]


def test_producer_still_splits_real_oversize_payload(tmp_path):
    rows = [_row("あ" * 6500 + str(i)) for i in range(2)]
    src = tmp_path / "large.jsonl"
    src.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    async def produce():
        queue = asyncio.Queue(maxsize=32)
        stats = {}
        await production.producer_batch(src, queue, set(), 0, stats, "dynamic", {})
        return stats, [queue.get_nowait() for _ in range(queue.qsize())]

    stats, groups = asyncio.run(produce())
    assert stats["selected_pending"] == 2
    assert stats["batch_groups"] == 2
    assert [len(group) for group in groups] == [1, 1]
