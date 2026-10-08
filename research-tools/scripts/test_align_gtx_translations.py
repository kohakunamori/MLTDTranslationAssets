from __future__ import annotations

import hashlib

import pytest

from scripts.align_gtx_translations import AlignmentError, align_catalogue


def sid(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_exact_source_hash_reuse_and_pending_fallback() -> None:
    catalogue = [
        {"bundle": "MD_jp.gtx", "key": "a", "source": "こんにちは"},
        {"bundle": "MD_jp.gtx", "key": "b", "source": "未翻訳"},
    ]
    translations = [
        {"source_sha256": sid("こんにちは"), "source": "こんにちは", "translation": "你好", "status": "machine_translated"},
    ]
    candidate, unresolved, report = align_catalogue(catalogue, translations, source_label="fixture")
    assert candidate[0]["translation"] == "你好"
    assert candidate[1]["translation"] == ""
    assert unresolved[0]["key"] == "b"
    assert report["counts"]["translated"] == 1
    assert report["counts"]["unresolved"] == 1


def test_same_as_source_is_pending() -> None:
    source = "春香"
    candidate, unresolved, report = align_catalogue(
        [{"bundle": "CD_jp.gtx", "key": "name", "source": source}],
        [{"source_sha256": sid(source), "source": source, "translation": source, "status": "machine_translated"}],
        source_label="fixture",
    )
    assert candidate[0]["status"] == "pending"
    assert unresolved[0]["match_method"] == "source_sha256_same_as_source"
    assert report["counts"]["same_as_source"] == 1


def test_nonaccepted_and_empty_rows_do_not_reuse() -> None:
    source = "テスト"
    candidate, unresolved, report = align_catalogue(
        [{"bundle": "MD_jp.gtx", "key": "x", "source": source}],
        [{"source_sha256": sid(source), "source": source, "translation": "测试", "status": "pending"}],
        source_label="fixture",
    )
    assert candidate[0]["translation"] == ""
    assert unresolved[0]["match_method"] == "source_sha256_nonaccepted_status"
    assert report["counts"]["nonaccepted_status"] == 1


def test_hash_mismatch_fails_closed() -> None:
    with pytest.raises(AlignmentError, match="source_sha256 mismatch"):
        align_catalogue(
            [{"bundle": "MD_jp.gtx", "key": "x", "source": "テスト"}],
            [{"source_sha256": "0" * 64, "source": "テスト", "translation": "测试", "status": "machine_translated"}],
            source_label="fixture",
        )


def test_conflicting_translation_for_same_hash_fails_closed() -> None:
    source = "テスト"
    with pytest.raises(AlignmentError, match="conflicting translation"):
        align_catalogue(
            [{"bundle": "MD_jp.gtx", "key": "x", "source": source}],
            [
                {"source_sha256": sid(source), "source": source, "translation": "测试", "status": "machine_translated"},
                {"source_sha256": sid(source), "source": source, "translation": "试验", "status": "machine_translated"},
            ],
            source_label="fixture",
        )
