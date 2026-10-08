"""Safety regressions for QA-only event-unit text candidate selection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_candidate_overlay import source_bound_machine_candidates


def _file(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_machine_priority_and_legacy_is_never_machine_candidate(tmp_path: Path) -> None:
    source = "はーい！"
    sid = source_id(source)
    queue = {sid: {"source_sha256": sid, "source": source}}
    legacy = _file(tmp_path / "legacy.jsonl", [
        {"source_sha256": sid, "source": source, "translation": "是的！",
         "status": "official_legacy"},
    ])
    companion = _file(tmp_path / "companion.jsonl", [
        {"source_sha256": sid, "source": source, "translation": "好！",
         "status": "machine_translated"},
    ])
    main = _file(tmp_path / "main.jsonl", [
        {"source_sha256": sid, "source": source, "translation": "好呀！",
         "status": "agent_translated"},
    ])
    selected, counts = source_bound_machine_candidates(
        queue, [legacy, companion, main]
    )
    assert selected[sid]["translation"] == "好！"
    assert counts["cross_input_translation_differences"] == 1
    assert "chosen_from_legacy.jsonl" not in counts


def test_reject_stale_source_identity(tmp_path: Path) -> None:
    source = "おはよう"
    sid = source_id(source)
    machine = _file(tmp_path / "machine.jsonl", [
        {"source_sha256": sid, "source": "こんばんは",
         "translation": "晚上好", "status": "machine_translated"},
    ])
    with pytest.raises(ValueError, match="invalid candidate source identity"):
        source_bound_machine_candidates(
            {sid: {"source_sha256": sid, "source": source}}, [machine]
        )


def test_cannot_import_external_unknown_source_as_a_candidate(tmp_path: Path) -> None:
    source = "おはよう"
    sid = source_id(source)
    machine = _file(tmp_path / "machine.jsonl", [
        {"source_sha256": sid, "source": source, "translation": "早上好",
         "status": "machine_translated"},
    ])
    selected, counts = source_bound_machine_candidates({}, [machine])
    assert selected == {}
    assert counts == {}
