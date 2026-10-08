"""No-provider regression for rolling ETA estimation and safe control-code quarantine."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts.audit_mltd_numbered_controls import audit, scan_main
from scripts.localization_progress_eta import estimate, format_duration
from scripts.report_localization_progress import display_pct
from scripts.translate_gtx_queue import source_id


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                            for row in rows), encoding="utf-8")


def test_numbered_control_quarantine_isolated_and_context_preserved(tmp_path, monkeypatch):
    from scripts import audit_mltd_numbered_controls as tool

    invalid_source = "魔法みたいです\\17\\"
    sid = source_id(invalid_source)
    valid_source = "赤い\\03\\"
    good_sid = source_id(valid_source)
    main = tmp_path / "main.jsonl"
    context = tmp_path / "context.jsonl"
    _write(main, [
        {"source_sha256": sid, "source": invalid_source, "translation": "像魔法一样17",
         "status": "machine_translated"},
        {"source_sha256": good_sid, "source": valid_source, "translation": "红色\\03\\",
         "status": "machine_translated"},
    ])
    _write(context, [
        {"source_sha256": sid, "source": invalid_source, "context_examples": [{"context": []}]},
        {"source_sha256": good_sid, "source": valid_source},
    ])
    monkeypatch.setattr(tool, "_writer_is_active", lambda path: False)
    result = audit(main, context, tmp_path, apply_main=True)
    assert result["counts"]["unique_unresolved"] == 1
    assert result["counts"]["quarantined_rows"] == 1
    remaining = [json.loads(x) for x in main.read_text(encoding="utf-8").splitlines()]
    assert [row["source_sha256"] for row in remaining] == [good_sid]
    preserved = [json.loads(x) for x in
                 (tmp_path / "audits" / tool.BAD_NAME).read_text(encoding="utf-8").splitlines()]
    assert preserved[0]["translation"] == "像魔法一样17"
    staged = [json.loads(x) for x in
              (tmp_path / tool.REPAIR_NAME).read_text(encoding="utf-8").splitlines()]
    assert staged[0]["source_sha256"] == sid
    assert staged[0]["context_examples"] == [{"context": []}]
    assert staged[0]["previous_translation"] == "像魔法一样17"
    assert scan_main(main)[1]["unique_unresolved"] == 0
    again = audit(main, context, tmp_path)
    assert again["counts"]["unique_unresolved"] == 1
    assert again["counts"]["already_quarantined_unique"] == 1
    assert again["repair_source_ids"] == [sid]
    assert again["main_output_modified"] is True
    assert len((tmp_path / tool.REPAIR_NAME).read_text(encoding="utf-8").splitlines()) == 1
    second_apply = audit(main, context, tmp_path, apply_main=True)
    assert second_apply["counts"]["quarantined_rows"] == 0
    assert second_apply["counts"]["unique_unresolved"] == 1
    assert second_apply["repair_source_ids"] == [sid]
    # Once a repaired result is accepted in the original production output,
    # the unresolved audit/queue clears rather than resurrecting stale evidence.
    with main.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "source_sha256": sid, "source": invalid_source,
            "translation": "像魔法一样\\17\\", "status": "machine_translated",
        }, ensure_ascii=False) + "\n")
    healed = audit(main, context, tmp_path)
    assert healed["counts"]["unique_unresolved"] == 0
    assert healed["repair_source_ids"] == []
    assert (tmp_path / tool.REPAIR_NAME).read_text(encoding="utf-8") == ""


def test_quarantine_refuses_running_gtx_writer(tmp_path, monkeypatch):
    from scripts import audit_mltd_numbered_controls as tool

    output = tmp_path / "main.jsonl"
    output.write_text("", encoding="utf-8")
    monkeypatch.setattr(tool, "_writer_is_active", lambda path: True)
    with pytest.raises(RuntimeError, match="running writer"):
        audit(output, tmp_path / "context.jsonl", tmp_path, apply_main=True)


def test_eta_uses_previous_completed_run_then_rolling_observations(tmp_path):
    main, companion = tmp_path / "main.jsonl", tmp_path / "companion.jsonl"
    main.write_text("", encoding="utf-8")
    companion.write_text("x\n", encoding="utf-8")
    now = time.time()
    old = tmp_path / "previous.json"
    old.write_text(json.dumps({"elapsed_seconds": 1200, "accepted": 1200}), encoding="utf-8")
    # The completed summary must precede the current process.
    import os
    os.utime(old, (now - 150, now - 150))
    worker = {"stage": "companion", "pid": 999999, "started": now - 120}
    counts = {
        "gtx": {"done": 90, "pending": 10},
        "companion": {"done": 100, "pending": 900},
    }
    paths = {"gtx": main, "companion": companion}
    summaries = {"gtx": tmp_path / "no-summary.json", "companion": old}
    first = estimate(tmp_path, counts, paths, summaries, now=now, active=worker)
    assert first["companion"]["rate_per_minute"] == 60.0
    assert first["companion"]["eta_seconds"] == 900
    assert first["companion"]["basis"] == "previous_completed_run"
    second = estimate(
        tmp_path, {"gtx": counts["gtx"], "companion": {"done": 160, "pending": 840}},
        paths, summaries, now=now + 60, active=worker,
    )
    assert second["companion"]["basis"] == "recent_observations"
    assert second["companion"]["rate_per_minute"] == 60.0
    assert second["companion"]["eta_seconds"] == 840
    assert second["gtx"]["eta_seconds"] is None


def test_inactive_stage_estimates_work_not_finish_time(tmp_path):
    import os
    now = time.time()
    summary = tmp_path / "main.summary.json"
    summary.write_text(json.dumps({"elapsed_seconds": 600, "accepted": 600}), encoding="utf-8")
    os.utime(summary, (now - 50, now - 50))
    report = estimate(
        tmp_path,
        {"gtx": {"done": 100, "pending": 630},
         "companion": {"done": 100, "pending": 900}},
        {"gtx": tmp_path / "no-output", "companion": tmp_path / "no-output"},
        {"gtx": summary, "companion": tmp_path / "none"},
        now=now, active={"stage": "companion", "pid": 999, "started": now - 20},
        persist=False,
    )
    assert report["gtx"]["active"] is False
    assert report["gtx"]["basis"] == "previous_completed_run_inactive"
    assert report["gtx"]["eta_seconds"] == 630
    assert report["gtx"]["rate_per_minute"] == 60.0


def test_display_percent_never_rounds_pending_to_complete():
    assert display_pct(332438, 332451) == "99.9961%"
    assert display_pct(332451, 332451) == "100.00%"
    assert display_pct(0, 0) == "0.00%"
    assert display_pct(95, 100) == "95.00%"


def test_eta_refuses_stale_or_zero_rate(tmp_path):
    main = tmp_path / "main.jsonl"
    main.write_text("x\n", encoding="utf-8")
    counts = {"gtx": {"done": 5, "pending": 95}, "companion": {"done": 0, "pending": 10}}
    paths = {"gtx": main, "companion": tmp_path / "missing"}
    summaries = {"gtx": tmp_path / "missing", "companion": tmp_path / "missing"}
    now = time.time()
    import os
    os.utime(main, (now - 1000, now - 1000))
    worker = {"stage": "gtx", "pid": 1, "started": now - 1200}
    result = estimate(tmp_path, counts, paths, summaries, now=now, active=worker)
    assert result["gtx"]["basis"] == "no_recent_output"
    assert result["gtx"]["eta_seconds"] is None
    assert format_duration(900) == "15m"
    assert format_duration(6480) == "1h48m"
    assert format_duration(90_000) == "1d1h"
