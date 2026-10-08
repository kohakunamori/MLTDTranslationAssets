"""GTX overlay resolver rejects unsafe legacy tokens without weakening machine QA."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.mltd_localization_pipeline import build_resolver


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_invalid_legacy_row_excluded_and_source_memory_can_fallback(tmp_path: Path) -> None:
    # The lenient invalid-token path keys on legacy *provenance* (status prefix or
    # provenance), which is why it stays reachable after the gate became an allowlist.
    legacy = _write(tmp_path / "legacy.jsonl", [{
        "status": "official_legacy_opencc_t2s_owner_waived",
        "source": "问候\\01\\和\\17\\",
        "translation": "问候和\\17\\",
        "bundle": "MB_jp.gtx",
        "key": "example",
    }])
    machine = _write(tmp_path / "machine.jsonl", [{
        "status": "machine_translated",
        "source": "问候\\01\\和\\17\\",
        "translation": "招呼\\01\\与\\17\\",
    }])
    resolver = build_resolver([legacy, machine])
    assert resolver.rejected_invalid_legacy_rows == 1
    assert resolver.rejected_rows == 1
    assert not resolver.exact
    assert len(resolver.source) == 1


def test_raw_traditional_seed_status_is_no_longer_releasable(tmp_path: Path) -> None:
    """Owner ruling 2026-09-26: `official_legacy` is evidence, not a release status.

    Before the allowlist this row resolved as an exact hit and shipped Traditional
    Chinese to /cn/. It must now be dropped before the token check ever runs.
    """
    legacy = _write(tmp_path / "legacy.jsonl", [{
        "status": "official_legacy",
        "source": "受け取る",
        "translation": "領取",
        "bundle": "MB_jp.gtx",
        "key": "example",
    }])
    resolver = build_resolver([legacy])
    assert not resolver.exact
    assert not resolver.source
    assert resolver.rejected_rows == 1
    # Rejected by the status gate, NOT by the lenient invalid-token path.
    assert resolver.rejected_invalid_legacy_rows == 0


def test_unreviewed_draft_status_is_not_releasable(tmp_path: Path) -> None:
    """Un-reviewed agent drafts used to be accepted for not matching a blacklist."""
    draft = _write(tmp_path / "draft.jsonl", [{
        "status": "agent_draft_unreviewed",
        "source": "受け取る",
        "translation": "收下",
        "bundle": "MB_jp.gtx",
        "key": "example",
    }])
    resolver = build_resolver([draft])
    assert not resolver.exact
    assert resolver.rejected_rows == 1


def test_machine_fallback_resolves_conflicting_contextual_legacy(tmp_path: Path) -> None:
    legacy = _write(tmp_path / "legacy.jsonl", [
        {"status": "official_legacy_opencc_t2s_owner_waived", "source": "受け取る", "translation": "收下", "bundle": "A", "key": "one"},
        {"status": "official_legacy_opencc_t2s_owner_waived", "source": "受け取る", "translation": "领取", "bundle": "B", "key": "two"},
    ])
    machine = _write(tmp_path / "machine.jsonl", [
        {"status": "machine_translated", "source": "受け取る", "translation": "领取"},
    ])
    from scripts.mltd_localization_pipeline import resolve_translation
    resolver = build_resolver([legacy, machine])
    assert not resolver.ambiguous_sources
    assert resolve_translation(resolver, "A", "one", "受け取る") == ("收下", "exact")
    assert resolve_translation(resolver, "other", "three", "受け取る") == ("领取", "memory")


def test_conflicting_machine_fallback_still_unresolved(tmp_path: Path) -> None:
    machine = _write(tmp_path / "machine.jsonl", [
        {"status": "machine_translated", "source": "受け取る", "translation": "收下"},
        {"status": "agent_translated", "source": "受け取る", "translation": "领取"},
    ])
    from scripts.mltd_localization_pipeline import resolve_translation
    resolver = build_resolver([machine])
    assert len(resolver.ambiguous_sources) == 1
    assert resolve_translation(resolver, "other", "three", "受け取る") == (None, "unresolved")

def test_invalid_machine_row_remains_blocking(tmp_path: Path) -> None:
    machine = _write(tmp_path / "machine.jsonl", [{
        "status": "machine_translated",
        "source": "问候\\01\\和\\17\\",
        "translation": "问候和\\17\\",
    }])
    with pytest.raises(ValueError, match="protected token mismatch"):
        build_resolver([machine])
