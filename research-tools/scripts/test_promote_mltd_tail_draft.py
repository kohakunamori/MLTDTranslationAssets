"""No-provider regression for explicit last-nine source-bound tail promotion."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import promote_mltd_tail_draft as mod
from scripts.mltd_translation_quality import source_id


def save(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def fixtures(tmp_path: Path) -> tuple[Path, Path, dict[str, Path], Path, Path]:
    main = tmp_path / "machine-translations-api.jsonl"
    companion = tmp_path / "machine-translations-nongtx-api.jsonl"
    main.write_text("", encoding="utf-8")
    companion.write_text("", encoding="utf-8")
    source_g = r"質問\17" + "\\"
    source_c = "30パーセントです"
    rows = [
        {"source_sha256": source_id(source_g), "source": source_g, "errors": ["old_bad"]},
        {"source_sha256": source_id(source_c), "source": source_c, "errors": ["old_bad"]},
    ]
    save(tmp_path / "machine-translations-api.failed.jsonl", [rows[0]])
    save(tmp_path / "machine-translations-nongtx-api.failed.jsonl", [rows[1]])
    draft = tmp_path / "audits" / "draft.jsonl"
    save(draft, [
        {"source_sha256": rows[0]["source_sha256"],
         "translation": "问题" + "\\17\\"},
        {"source_sha256": rows[1]["source_sha256"],
         "translation": "是30%"},
    ])
    glossary = tmp_path / "glossary.json"
    glossary.write_text('{"entries":{},"kana_allowlist":[]}', encoding="utf-8")
    terms = tmp_path / "terms.json"
    terms.write_text('{"entries":{}}', encoding="utf-8")
    return tmp_path, draft, {"gtx": main, "companion": companion}, glossary, terms


def test_plan_has_no_production_side_effect_and_apply_is_bound(tmp_path, monkeypatch):
    args = fixtures(tmp_path)
    no_apply = mod.promote(*args)
    assert no_apply["source_count"] == 2
    assert no_apply["applied"] is False
    assert all(path.read_text(encoding="utf-8") == "" for path in args[2].values())
    monkeypatch.setattr(mod, "translator_active", lambda: False)
    result = mod.promote(*args, apply=True)
    assert result["applied"] is True
    assert result["by_stage_and_qa"]["PASS"] == 2
    for path in args[2].values():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["status"] == "agent_translated"
        assert rows[0]["provenance"]["not_human_verified"] is True
    with pytest.raises(ValueError, match="already accepted"):
        mod.promote(*args, apply=True)


def test_qa_rejects_corrupted_tokens_without_writing(tmp_path, monkeypatch):
    args = fixtures(tmp_path)
    draft = args[1]
    rows = [json.loads(line) for line in draft.read_text(encoding="utf-8").splitlines()]
    rows[0]["translation"] = "问题17"
    save(draft, rows)
    monkeypatch.setattr(mod, "translator_active", lambda: False)
    with pytest.raises(ValueError, match="blocking QA"):
        mod.promote(*args, apply=True)
    assert all(path.read_text(encoding="utf-8") == "" for path in args[2].values())


def test_active_worker_refuses_apply(tmp_path, monkeypatch):
    args = fixtures(tmp_path)
    monkeypatch.setattr(mod, "translator_active", lambda: True)
    with pytest.raises(RuntimeError, match="translator is running"):
        mod.promote(*args, apply=True)
    assert all(path.read_text(encoding="utf-8") == "" for path in args[2].values())
