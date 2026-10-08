"""Frozen Event-unit QA cue refinements: explicit Japanese evidence, not review approval."""
from __future__ import annotations

import pytest
from scripts.mltd_translation_quality import evaluate_row, source_id


def codes(source: str, translation: str) -> set[str]:
    return {x["code"] for x in evaluate_row(
        {"source": source, "source_sha256": source_id(source)},
        {"source": source, "source_sha256": source_id(source),
         "translation": translation, "status": "machine_translated"},
        {"entries": {}, "kana_allowlist": []},
    )["issues"]}


@pytest.mark.parametrize("japanese", [
    "ワタシたち", "アタシたち", "ボクたち", "オレたち",
    "ワタシ達", "アタシ達", "ボク達", "オレ達",
])
def test_explicit_katakana_first_person_plural_is_not_invented(japanese):
    src = f"{japanese}が歌うよ♪"
    assert "unsupported_first_person_plural_addition" not in codes(
        src, "我们来唱歌哦♪"
    )


def test_unrelated_singular_first_person_is_still_reviewed():
    assert "unsupported_first_person_plural_addition" in codes(
        "ワタシが歌うよ♪", "我们来唱歌哦♪"
    )


def test_sentence_final_suiran_signals_adversative_without_forced_but():
    assert "sentence_final_adversative_missing" not in codes(
        "そうかもしれないけど……。", "虽然可能如此……。"
    )
    assert "sentence_final_adversative_missing" in codes(
        "そうかもしれないけど……。", "可能如此……。"
    )


@pytest.mark.parametrize("source,translation", [
    ("どんなアートを作りたいの？", "你想制作什么样的艺术品？"),
    ("どういう衣装が好き？", "你喜欢什么样的服装？"),
])
def test_what_kind_japanese_is_an_explicit_indefinite_cue(source, translation):
    assert "unsupported_indefinite_object_addition" not in codes(source, translation)


@pytest.mark.parametrize("source,translation", [
    ("どうして来たの？", "为什么来了？"),
    ("何も言ってないよ。", "不知道你为什么来。"),
])
def test_why_is_not_chinese_indefinite_object(source, translation):
    assert "unsupported_indefinite_object_addition" not in codes(source, translation)


def test_otherwise_unsupported_indefinite_still_reviewed():
    assert "unsupported_indefinite_object_addition" in codes(
        "まだ残ってる気がする。", "好像还剩下什么。"
    )
    assert "unsupported_indefinite_object_addition" in codes(
        "まだ残ってる気がする。", "为什么还剩下什么。"
    )



@pytest.mark.parametrize("japanese", [
    "自分たち", "自分達", "ウチら", "私ら", "僕ら", "俺ら",
    "うちら", "ワタシら", "ボクら", "オレら", "こっちのチーム",
])
def test_explicit_other_first_person_plural_forms_not_invented(japanese):
    src = f"{japanese}が優勝だ！"
    assert "unsupported_first_person_plural_addition" not in codes(
        src, "是我们夺冠啦！"
    )


def test_unrelated_singular_jibun_and_watashi_still_reviewed():
    assert "unsupported_first_person_plural_addition" in codes(
        "自分が優勝だ！", "是我们夺冠啦！"
    )
    assert "unsupported_first_person_plural_addition" in codes(
        "私は勝った！", "我们赢了！"
    )
