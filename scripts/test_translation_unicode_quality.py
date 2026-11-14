#!/usr/bin/env python3
"""Unicode script-residue regressions for machine translation QA.

The event-unit audit found a corrupted given name in
``event_unit_talk_833 commands[14]``: the first four visible characters are
U+3042 HIRAGANA LETTER A, U+C988 HANGUL SYLLABLE JEU, U+0938 DEVANAGARI LETTER
SA, and U+093E DEVANAGARI VOWEL SIGN AA.  SOURCE is ``あずささん`` and the
project glossary maps ``三浦あずさ`` to ``三浦梓``, so the fragment must become
``梓`` without rewriting the rest of the line.

Hangul and Devanagari are legitimate wherever SOURCE uses them.  These tests
pin that distinction, protected-token masking, ordinary script/number
controls, and the existing bounded QA retry feedback path.

Private-use codepoints (category Co) are the same kind of residue: the leaked
term-marker triple U+E200/U+E202/U+E201 that reached published rows must be
rejected, and so must any other newly introduced private-use character in the
BMP or the two supplementary private-use planes, while a private-use character
the SOURCE itself carries is preserved.  The bounded retry correction keeps that
same split: it scopes removal to codepoints SOURCE does not carry and names a
source-present private-use character as content to keep.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.core.hashing import sha256_text  # noqa: E402
from pipelines.text.mltd_translation_quality import (  # noqa: E402
    evaluate_row,
    load_glossary,
)
from pipelines.text.translate_mltd_api_pool import (  # noqa: E402
    ModelConfig,
    mask_numeric_literals,
    restore_numeric_literals,
    translate_with_model,
    validate_candidate,
)


# Keep the corruption explicit: Hangul JEU and Devanagari SA + AA.
CORRUPT_NAME = "\u3042\uC988\u0938\u093E"


# The term-marker triple seen leaking into published rows: a private-use
# open/separator/close wrapper around an entity name or its JSON payload.
SENTINEL_OPEN, SENTINEL_SEPARATOR, SENTINEL_CLOSE = "\ue200", "\ue202", "\ue201"


def entity_sentinel(payload: str) -> str:
    return SENTINEL_OPEN + "entity" + SENTINEL_SEPARATOR + payload + SENTINEL_CLOSE


def retry_correction(user_prompt: str) -> str:
    """Return only the RETRY_CORRECTION block of a captured user prompt.

    The full message already carries ``SOURCE:``, the protected-token rule and
    the numeric-literal rule, so asserting those against the whole message can
    never fail; only this block proves what the retry itself asks the model to do.
    """
    lines = user_prompt.splitlines()
    try:
        start = lines.index("RETRY_CORRECTION:")
    except ValueError:
        raise AssertionError(f"no RETRY_CORRECTION block in {user_prompt!r}") from None
    return "\n".join(lines[start:])


def _config(**overrides: object) -> ModelConfig:
    values: dict[str, object] = {
        "id": "test",
        "model": "alias",
        "api_protocol": "chat_completions",
        "endpoint": "https://example.invalid/v1/chat/completions",
        "api_key_env": "",
        "concurrency": 1,
        "reasoning_effort": "high",
        "temperature": 0.1,
        "timeout": 10,
        "retries": 2,
        "min_reasoning_effort": "high",
        "cache_warmup": False,
    }
    values.update(overrides)
    return ModelConfig(**values)


class TranslationUnicodeQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.glossary = load_glossary(ROOT / "glossary/authoritative-terms.json")

    def evaluate(self, source: str, translation: str) -> dict:
        sid = sha256_text(source)
        return evaluate_row(
            {"source": source, "source_sha256": sid, "task_hint": "DIALOGUE"},
            {"source": source, "source_sha256": sid, "translation": translation},
            self.glossary,
        )

    def _translate_with_scripted_replies(
        self,
        source: str,
        replies: list[str],
        *,
        row_extra: dict | None = None,
        authoritative_terms: dict[str, str] | None = None,
    ) -> tuple[dict | None, str, defaultdict, list[dict]]:
        """Drive ``translate_with_model`` against scripted mock answers."""
        requests: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            index = min(len(requests) - 1, len(replies) - 1)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": replies[index]}}]}
            )

        async def run():
            row = {
                "source": source,
                "source_sha256": sha256_text(source),
                "task_hint": "DIALOGUE",
            }
            row.update(row_extra or {})
            stats: defaultdict = defaultdict(lambda: defaultdict(float))
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                candidate, error = await translate_with_model(
                    client,
                    _config(),
                    None,
                    "SYSTEM",
                    row,
                    self.glossary,
                    authoritative_terms or {},
                    stats,
                )
            return candidate, error, stats

        candidate, error, stats = asyncio.run(run())
        return candidate, error, stats, requests

    def test_corrupt_hangul_devanagari_name_is_rejected(self) -> None:
        entry = self.glossary["entries"]["三浦あずさ"]
        self.assertEqual(entry["target"], "三浦梓")
        self.assertEqual(entry["evidence"], "official_legacy_zhcn_exact_source")

        result = self.evaluate(
            "あずささん、今、鼻歌を歌っていましたか？",
            CORRUPT_NAME + "小姐，您刚才是在哼歌吗？",
        )
        self.assertEqual(result["qa_verdict"], "REJECT")
        issue = next(
            item
            for item in result["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertIn("HANGUL:U+C988", issue["detail"])
        self.assertIn("DEVANAGARI:U+0938", issue["detail"])
        self.assertIn("DEVANAGARI:U+093E", issue["detail"])

    def test_source_present_hangul_and_devanagari_are_allowed(self) -> None:
        result = self.evaluate(
            "歌は「안녕」と「सा」。",
            "歌曲是「안녕」和「सा」。",
        )
        codes = {item["code"] for item in result["issues"]}
        self.assertNotIn("introduced_unsupported_unicode", codes)
        self.assertNotEqual(result["qa_verdict"], "REJECT")

    def test_protected_token_contents_are_not_script_residue(self) -> None:
        result = self.evaluate(
            "{$P$}さん、こんにちは。",
            "{$P$}先生，{안녕}你好。",
        )
        codes = {item["code"] for item in result["issues"]}
        self.assertIn("protected_token_mismatch", codes)
        self.assertNotIn("introduced_unsupported_unicode", codes)

    def test_ordinary_scripts_and_numbers_are_not_script_rejections(self) -> None:
        cases = [
            ("また明日ね。", "明天见。"),
            ("曲名は『セカキュー』です。", "曲名是『セカキュー』。"),
            ("PVは2026年。", "PV是2026年。"),
        ]
        for source, translation in cases:
            with self.subTest(source=source):
                result = self.evaluate(source, translation)
                codes = {item["code"] for item in result["issues"]}
                self.assertNotIn("introduced_unsupported_unicode", codes)
                self.assertNotEqual(result["qa_verdict"], "REJECT")

    def test_introduced_invisible_format_codepoints_are_rejected(self) -> None:
        cases = [
            ("\u00a0", "U+00A0"),
            ("\u200b", "U+200B"),
            ("\u200f", "U+200F"),
            ("\ufeff", "U+FEFF"),
        ]
        for char, codepoint in cases:
            with self.subTest(codepoint=codepoint):
                result = self.evaluate("また明日ね。", "明天见。" + char)

                self.assertEqual(result["qa_verdict"], "REJECT")
                issue = next(
                    item
                    for item in result["issues"]
                    if item["code"] == "introduced_unsupported_unicode"
                )
                self.assertEqual(issue["detail"], f"format_codepoints={codepoint}")

    def test_source_present_invisible_format_codepoints_are_allowed(self) -> None:
        source = "前\u200b後\ufeff左\u00a0右\u200f。"
        translation = "前\u200b后\ufeff左\u00a0右\u200f。"

        result = self.evaluate(source, translation)

        self.assertNotIn(
            "introduced_unsupported_unicode",
            {item["code"] for item in result["issues"]},
        )
        self.assertNotEqual(result["qa_verdict"], "REJECT")

    def test_only_invisible_format_codepoint_missing_from_source_is_reported(self) -> None:
        source = "前\u200b後。"
        translation = "前\u200b后\ufeff。"

        result = self.evaluate(source, translation)

        issue = next(
            item
            for item in result["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertEqual(issue["detail"], "format_codepoints=U+FEFF")
        self.assertNotIn("U+200B", issue["detail"])

    def test_duplicated_invisible_format_codepoint_is_reported(self) -> None:
        source = "前\u200b後\ufeff左\u00a0右\u200f。"
        translation = "前\u200b\u200b后\ufeff左\u00a0右\u200f。"

        result = self.evaluate(source, translation)

        issue = next(
            item
            for item in result["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertEqual(issue["detail"], "format_codepoints=U+200B")
        self.assertNotIn("U+FEFF", issue["detail"])
        self.assertNotIn("U+00A0", issue["detail"])
        self.assertNotIn("U+200F", issue["detail"])

    def test_plain_whitespace_is_not_an_invisible_format_rejection(self) -> None:
        result = self.evaluate("また明日ね。", "明 天\n见\u3000。")

        self.assertNotIn(
            "introduced_unsupported_unicode",
            {item["code"] for item in result["issues"]},
        )
        self.assertNotEqual(result["qa_verdict"], "REJECT")

    def test_invisible_format_and_script_details_remain_distinct(self) -> None:
        format_result = self.evaluate("また明日ね。", "明天见。\u200b")
        format_issue = next(
            item
            for item in format_result["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertEqual(format_issue["detail"], "format_codepoints=U+200B")

        script_result = self.evaluate("また明日ね。", "明天见。\uc988")
        script_issue = next(
            item
            for item in script_result["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertEqual(script_issue["detail"], "scripts=HANGUL:U+C988")
        self.assertNotIn("format_codepoints", script_issue["detail"])

    def test_introduced_private_use_sentinel_is_rejected(self) -> None:
        source = "（……どうやらまつりには、俺の考えなんて\nお見通しだったみたいだな）"
        leaked = entity_sentinel("徳川茉莉")

        result = self.evaluate(source, "（……看来，" + leaked + "早就看穿我的想法了）")

        self.assertEqual(result["qa_verdict"], "REJECT")
        issue = next(
            item
            for item in result["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertIn("U+E200", issue["detail"])
        self.assertIn("U+E201", issue["detail"])
        self.assertIn("U+E202", issue["detail"])

    def test_introduced_private_use_is_rejected_in_every_private_use_plane(self) -> None:
        # U+E000 is the BMP private-use area, U+F0000 plane 15 and U+10FFFD
        # plane 16: the uniform category Co decides, not any single block.
        result = self.evaluate("また明日ね。", "明天见。\ue000\U000F0000\U0010FFFD")

        self.assertEqual(result["qa_verdict"], "REJECT")
        issue = next(
            item
            for item in result["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertIn("U+E000", issue["detail"])
        self.assertIn("U+F0000", issue["detail"])
        self.assertIn("U+10FFFD", issue["detail"])

    def test_source_present_private_use_is_kept_and_new_ones_are_still_rejected(self) -> None:
        source = (
            "（……どうやら"
            + entity_sentinel("徳川茉莉")
            + "には、俺の考えなんて\nお見通しだったみたいだな）"
        )
        kept = "（……看来，" + entity_sentinel("德川茉莉") + "早就看穿我的想法了）"

        preserved = self.evaluate(source, kept)
        self.assertNotIn(
            "introduced_unsupported_unicode",
            {item["code"] for item in preserved["issues"]},
        )
        self.assertNotEqual(preserved["qa_verdict"], "REJECT")

        # The source-present triple stays allowed, a different introduced
        # private-use codepoint does not, and the detail names only the new one.
        extra = self.evaluate(source, kept + "\ue000")
        issue = next(
            item
            for item in extra["issues"]
            if item["code"] == "introduced_unsupported_unicode"
        )
        self.assertIn("U+E000", issue["detail"])
        self.assertNotIn("U+E200", issue["detail"])

    def test_plain_entity_word_without_private_use_is_not_unicode_residue(self) -> None:
        source = "今日は、まつりが出演するドラマの撮影日だったけど、予定が飛んだ。"
        translation = (
            "今天原本是德川茉莉出演的电视剧拍摄日，可是 entity"
            '["people","德川茉莉"] 行程临时取消。'
        )

        codes = {item["code"] for item in self.evaluate(source, translation)["issues"]}
        self.assertNotIn("introduced_unsupported_unicode", codes)

    def test_introduced_pipeline_markers_are_blocked_case_insensitively(self) -> None:
        leaks = [
            "__MLTD_TERM_NNN__",
            "__mltd_TOKEN_NNN__",
            "__Mltd_NUMBER_nnn__",
        ]
        for marker in leaks:
            with self.subTest(marker=marker):
                result = self.evaluate("また明日ね。", "明天见。" + marker)
                issues = result["issues"]
                marker_issue = next(
                    item for item in issues if item["code"] == "introduced_pipeline_marker"
                )
                self.assertNotIn(
                    "introduced_unsupported_unicode",
                    {item["code"] for item in issues},
                )
                self.assertIn("added=", marker_issue["detail"])
                self.assertNotIn("U+", marker_issue["detail"])

    def test_pipeline_marker_lookalikes_are_not_blocked(self) -> None:
        translations = [
            "明天见。NNN",
            "明天见。__MLTD_FOO_NNN__",
            "明天见。__MLTD_TERM_NN__",
            "明天见。__MLTD_TERM_NNNN__",
            "明天见。__MLTD_TERM_NNN",
            "明天见。__MLTD_TERMN_NNN__",
            "明天见。__MLTD_TERM_ABC__",
        ]
        for translation in translations:
            with self.subTest(translation=translation):
                result = self.evaluate("また明日ね。", translation)
                self.assertNotIn(
                    "introduced_pipeline_marker",
                    {item["code"] for item in result["issues"]},
                )
                self.assertNotEqual(result["qa_verdict"], "REJECT")

    def test_source_pipeline_marker_multiset_is_preserved(self) -> None:
        markers = [
            "__MLTD_TERM_NNN__",
            "__MLTD_TOKEN_NNN__",
            "__MLTD_NUMBER_NNN__",
        ]
        for marker in markers:
            with self.subTest(marker=marker):
                source = "また明日ね。" + marker
                kept = self.evaluate(source, "明天见。" + marker)
                self.assertNotIn(
                    "introduced_pipeline_marker",
                    {item["code"] for item in kept["issues"]},
                )

                duplicated = self.evaluate(source, "明天见。" + marker + marker)
                duplicate_codes = {
                    item["code"] for item in duplicated["issues"]
                }
                self.assertIn("introduced_pipeline_marker", duplicate_codes)

                missing = self.evaluate(source, "明天见。")
                self.assertIn(
                    "introduced_pipeline_marker",
                    {item["code"] for item in missing["issues"]},
                )

    def test_source_pipeline_marker_order_is_preserved(self) -> None:
        source = "また明日ね。__MLTD_TERM_NNN____MLTD_TOKEN_NNN__"
        translation = "明天见。__MLTD_TOKEN_NNN____MLTD_TERM_NNN__"
        result = self.evaluate(source, translation)
        self.assertEqual(result["qa_verdict"], "REJECT")
        issue = next(
            item
            for item in result["issues"]
            if item["code"] == "introduced_pipeline_marker"
        )
        self.assertIn("order_changed", issue["detail"])
        self.assertNotIn("added=", issue["detail"])
        self.assertNotIn("missing=", issue["detail"])

    def test_numeric_marker_restore_remains_stable(self) -> None:
        source = "獲得数は2026年。"
        masked, targets = mask_numeric_literals(source)
        self.assertIn("__MLTD_NUMBER_000__", masked)
        self.assertEqual(restore_numeric_literals(masked, targets), source)
        for marker in (
            "__MLTD_NUMBER_000__",
            "__mltd_number_000__",
            "__MLTD_Number_000__",
        ):
            with self.subTest(marker=marker):
                self.assertEqual(mask_numeric_literals(marker), (marker, []))

    def test_source_template_survives_numeric_masking_round_trip(self) -> None:
        source = "獲得数は2026、__MLTD_NUMBER_NNN__枚です。"
        expected = "获得数是2026，__MLTD_NUMBER_NNN__枚。"
        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source,
            ["获得数是__MLTD_NUMBER_000__，__MLTD_NUMBER_NNN__枚。"],
        )

        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], expected)
        self.assertEqual(len(requests), 1)
        self.assertEqual(stats["test"]["accepted"], 1)

    def test_indexed_source_pipeline_marker_is_rejected_as_invalid(self) -> None:
        cases = [
            ("__MLTD_TERM_000__", "unexpected authoritative term marker"),
            ("__mltd_TERM_000__", "unexpected authoritative term marker"),
            ("__MLTD_term_000__", "unexpected authoritative term marker"),
            ("__MLTD_TOKEN_000__", "unknown protected-token marker"),
            ("__mltd_TOKEN_000__", "unknown protected-token marker"),
            ("__MLTD_token_000__", "unknown protected-token marker"),
            ("__MLTD_NUMBER_000__", "unknown numeric marker"),
            ("__mltd_number_000__", "unknown numeric marker"),
            ("__MLTD_Number_000__", "unknown numeric marker"),
        ]
        for marker, expected_error in cases:
            with self.subTest(marker=marker):
                source = "また明日ね。" + marker
                row = {
                    "source": source,
                    "source_sha256": sha256_text(source),
                    "task_hint": "DIALOGUE",
                }
                candidate, error = validate_candidate(
                    row, marker, [], [], self.glossary
                )
                self.assertIsNone(candidate)
                self.assertIn("structure:", error)
                self.assertIn(expected_error, error)

    def test_pipeline_marker_retry_then_clean_candidate(self) -> None:
        source = "また明日ね。"
        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source,
            ["明天见。__MLTD_TERM_NNN__", "明天见。"],
        )

        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], "明天见。")
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["requests"], 2)
        self.assertEqual(stats["test"]["qa_retries"], 1)
        self.assertEqual(stats["test"]["accepted"], 1)
        correction = retry_correction(requests[1]["messages"][-1]["content"])
        self.assertIn("introduced_pipeline_marker", correction)
        self.assertIn("Keep pipeline marker literals", correction)
        self.assertIn("Remove pipeline markers", correction)
        self.assertNotIn("U+", correction)

    def test_pipeline_marker_repeated_leak_is_not_accepted(self) -> None:
        source = "また明日ね。"
        leak = "明天见。__mltd_TOKEN_NNN__"
        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source,
            [leak, leak],
        )

        self.assertIsNone(candidate)
        self.assertIn("introduced_pipeline_marker", error)
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["requests"], 2)
        self.assertEqual(stats["test"]["qa_retries"], 2)
        self.assertEqual(stats["test"]["accepted"], 0)

    def test_indexed_marker_restore_errors_use_pipeline_feedback(self) -> None:
        source = "また明日ね。"
        for leak in ("__MLTD_TERM_000__", "__MLTD_NUMBER_000__"):
            with self.subTest(leak=leak):
                candidate, error, stats, requests = self._translate_with_scripted_replies(
                    source,
                    ["明天见。" + leak, "明天见。"],
                )

                self.assertEqual(error, "")
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate["translation"], "明天见。")
                self.assertEqual(len(requests), 2)
                self.assertEqual(stats["test"]["qa_retries"], 1)
                self.assertEqual(stats["test"]["accepted"], 1)
                correction = retry_correction(requests[1]["messages"][-1]["content"])
                self.assertIn("Keep pipeline marker literals", correction)
                self.assertIn("Remove pipeline markers", correction)
                self.assertIn("structure:", correction)
                self.assertNotIn("introduced_unsupported_unicode", correction)

    def test_authoritative_term_marker_count_errors_use_pipeline_feedback(self) -> None:
        source = "ジュリアは元気です。"
        terms = {"ジュリア": "茱莉亚"}
        clean = "__MLTD_TERM_000__很有精神。"
        leaks = [
            "她很有精神。",
            "__MLTD_TERM_000____MLTD_TERM_000__很有精神。",
        ]
        for leak in leaks:
            with self.subTest(leak=leak):
                candidate, error, stats, requests = self._translate_with_scripted_replies(
                    source,
                    [leak, clean],
                    authoritative_terms=terms,
                )

                self.assertEqual(error, "")
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate["translation"], "茱莉亚很有精神。")
                self.assertEqual(len(requests), 2)
                self.assertEqual(stats["test"]["qa_retries"], 1)
                self.assertEqual(stats["test"]["accepted"], 1)
                correction = retry_correction(requests[1]["messages"][-1]["content"])
                self.assertIn("authoritative term marker mismatch", correction)
                self.assertIn("Keep pipeline marker literals", correction)
                self.assertNotIn("introduced_unsupported_unicode", correction)

    def test_unknown_protected_token_marker_uses_pipeline_feedback(self) -> None:
        source = "また明日ね。"
        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source,
            ["明天见。__MLTD_TOKEN_000__", "明天见。"],
        )

        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], "明天见。")
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["qa_retries"], 1)
        correction = retry_correction(requests[1]["messages"][-1]["content"])
        self.assertIn("unknown protected-token marker", correction)
        self.assertIn("Keep pipeline marker literals", correction)
        self.assertNotIn("introduced_unsupported_unicode", correction)

    def test_pipeline_marker_feedback_combines_with_context_spill(self) -> None:
        source = "それも嘘なの？"
        leaky = "之前你说无法从噩梦里醒过来，__MLTD_TERM_NNN__都是谎言吗？"
        clean = "那也是谎言吗？"
        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source,
            [leaky, clean],
            row_extra={
                "task": "ASSETS_LYRICS",
                "context_examples": [
                    {"context": [{"relative": -1, "source": "一人で悪夢から目覚められない。"}]}
                ],
            },
        )

        self.assertEqual(error, "")
        self.assertEqual(candidate["translation"], clean)
        self.assertEqual(stats["test"]["qa_retries"], 1)
        correction = retry_correction(requests[1]["messages"][-1]["content"])
        self.assertIn("unexpectedly long", correction)
        self.assertIn("Re-translate SOURCE alone", correction)
        self.assertIn("Keep pipeline marker literals", correction)

    def test_unicode_residue_retry_keeps_unicode_feedback_only(self) -> None:
        source = "あずささん、今、鼻歌を歌っていましたか？"
        residues = ["\uC988", "\ue000"]
        for residue in residues:
            with self.subTest(residue=residue):
                candidate, error, stats, requests = self._translate_with_scripted_replies(
                    source,
                    ["梓小姐，您刚才是在哼歌吗？" + residue, "梓小姐，您刚才是在哼歌吗？"],
                )
                self.assertEqual(error, "")
                self.assertEqual(stats["test"]["qa_retries"], 1)
                correction = retry_correction(requests[1]["messages"][-1]["content"])
                self.assertIn("introduced_unsupported_unicode", correction)
                self.assertNotIn("introduced_pipeline_marker", correction)

    def test_invisible_format_rejection_uses_blocking_retry_feedback(self) -> None:
        source = "あずささん、今、鼻歌を歌っていましたか？"
        clean = "梓小姐，您刚才是在哼歌吗？"
        for residue in ("\u00a0", "\u200b", "\u200f", "\ufeff"):
            with self.subTest(residue=residue):
                leaked = clean[:2] + residue + clean[2:]
                candidate, error, stats, requests = self._translate_with_scripted_replies(
                    source,
                    [leaked, clean],
                )

                self.assertEqual(error, "")
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate["translation"], clean)
                self.assertEqual(stats["test"]["qa_retries"], 1)
                self.assertEqual(stats["test"]["accepted"], 1)
                correction = retry_correction(requests[1]["messages"][-1]["content"])
                self.assertIn("introduced_unsupported_unicode", correction)
                self.assertIn("format/space-control", correction)
                self.assertIn("zero-width space", correction)
                self.assertIn("byte-order mark", correction)
                self.assertIn("no-break space", correction)
                self.assertIn("right-to-left mark", correction)
                self.assertIn("do not occur in SOURCE", correction)

    def test_bounded_retry_saves_clean_candidate_after_script_reject(self) -> None:
        source = "あずささん、今、鼻歌を歌っていましたか？"
        corrupt = CORRUPT_NAME + "小姐，您刚才是在哼歌吗？"
        clean = "梓小姐，您刚才是在哼歌吗？"

        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source, [corrupt, clean]
        )

        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], clean)
        self.assertEqual(candidate["status"], "machine_translated")
        self.assertNotIn("qa", candidate)
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["requests"], 2)
        self.assertEqual(stats["test"]["qa_retries"], 1)
        self.assertEqual(stats["test"]["accepted"], 1)
        self.assertIn(
            "introduced_unsupported_unicode",
            requests[1]["messages"][-1]["content"],
        )

    def test_bounded_retry_recovers_from_private_use_sentinel_reject(self) -> None:
        source = "あずささん、今、鼻歌を歌っていましたか？"
        leaked = "梓小姐，" + entity_sentinel("三浦梓") + "您刚才是在哼歌吗？"
        clean = "梓小姐，您刚才是在哼歌吗？"

        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source, [leaked, clean]
        )

        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], clean)
        self.assertEqual(candidate["status"], "machine_translated")
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["qa_retries"], 1)
        self.assertEqual(stats["test"]["accepted"], 1)
        # The retry feedback is the narrow private-use correction, not the
        # generic code-only sentence: it asks for the introduced markers to be
        # removed while the bounded output contract and the numeric/token
        # preservation rules stay in force.  The codepoint detail itself stays
        # in the QA result and is deliberately not part of the prompt contract.
        correction = retry_correction(requests[1]["messages"][-1]["content"])
        self.assertIn("introduced_unsupported_unicode", correction)
        # The sentence about the markers must scope removal to codepoints SOURCE
        # does not carry; it must not read as "drop the private-use markers".
        self.assertIn("private-use/internal marker", correction)
        removal_clause = next(
            sentence
            for sentence in correction.split(".")
            if "private-use/internal marker" in sentence
        )
        self.assertIn("Remove only", removal_clause)
        self.assertIn("do not occur in SOURCE", removal_clause)
        self.assertIn("protected token", correction)
        self.assertIn("numeric literal", correction)
        self.assertNotIn("U+E200", correction)

    def test_retry_correction_keeps_private_use_the_source_carries(self) -> None:
        # The published rows that leaked the wrapper can also carry it in SOURCE.
        # The retry must ask only for the newly introduced codepoint to go.
        source = (
            "（……どうやら"
            + entity_sentinel("徳川茉莉")
            + "には、俺の考えなんて\nお見通しだったみたいだな）"
        )
        leaked = "（……看来，" + entity_sentinel("德川茉莉") + "\ue000早就看穿我的想法了）"
        clean = "（……看来，" + entity_sentinel("德川茉莉") + "早就看穿我的想法了）"

        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source, [leaked, clean]
        )

        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], clean)
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["qa_retries"], 1)
        correction = retry_correction(requests[1]["messages"][-1]["content"])
        # The feedback has to state both halves at once: markers absent from
        # SOURCE are removable, and the private-use characters SOURCE itself
        # carries are content that must be kept.
        self.assertIn("do not occur in SOURCE", correction)
        self.assertIn("SOURCE itself contains", correction)
        self.assertIn("must be kept", correction)
        self.assertIn("protected token", correction)
        self.assertIn("numeric literal", correction)
        self.assertNotIn("U+", correction)

    def test_lyric_spill_and_marker_leak_share_one_corrective_feedback(self) -> None:
        source = "それも嘘なの？"
        # Long enough for the spill ceiling (>26 characters) yet under the
        # lyric length rejection, so both blocking codes reach the retry.
        leaky = (
            "之前你说无法从噩梦里醒过来，"
            + entity_sentinel("徳川茉莉")
            + "都是谎言吗？"
        )
        clean = "那也是谎言吗？"

        candidate, error, stats, requests = self._translate_with_scripted_replies(
            source,
            [leaky, clean],
            row_extra={
                "task": "ASSETS_LYRICS",
                "context_examples": [
                    {"context": [{"relative": -1, "source": "一人で悪夢から目覚められない。"}]}
                ],
            },
        )

        # Both codes block the leaky draft, so the length-specific wording and
        # the marker correction must reach the retry together.
        self.assertEqual(error, "")
        self.assertEqual(candidate["translation"], clean)
        self.assertEqual(stats["test"]["qa_retries"], 1)
        feedback = requests[1]["messages"][-1]["content"]
        self.assertIn("Re-translate SOURCE alone", feedback)
        self.assertIn("private-use", feedback)
        self.assertIn("numeric literals", feedback)

    def test_provider_tos_error_banner_is_rejected_by_qa(self) -> None:
        source = "通信に失敗しました\r\n\r\nタイトルに戻ります"
        tos_banner = (
            "⚠ This service has been disabled in this account for violation of "
            "Terms of Service. Please submit an appeal to continue using this product."
        )
        qa = self.evaluate(source, tos_banner)
        self.assertEqual(qa["qa_verdict"], "REJECT")
        codes = {issue["code"] for issue in qa["issues"]}
        self.assertIn("provider_error_text", codes)

        row = {
            "source": source,
            "source_sha256": sha256_text(source),
            "task_hint": "UI",
        }
        candidate, error = validate_candidate(row, tos_banner, [], [], self.glossary)
        self.assertIsNone(candidate)
        self.assertIn("blocking_qa:provider_error_text", error)

    def test_http_200_provider_error_banner_retries_as_request_error_and_recovers(self) -> None:
        source = "連続お仕事を実行できませんでした\r\nタイトルに戻ります"
        tos_banner = (
            "⚠ This service has been disabled in this account for violation of "
            "Terms of Service. Please submit an appeal to continue using this product."
        )
        clean = "未能执行连续工作\r\n将返回标题界面"

        with mock.patch("asyncio.sleep", return_value=None):
            candidate, error, stats, requests = self._translate_with_scripted_replies(
                source,
                [tos_banner, clean],
            )

        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], clean)
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["request_errors"], 1)
        self.assertEqual(stats["test"]["qa_retries"], 0)
        self.assertEqual(stats["test"]["accepted"], 1)

    def test_http_200_provider_error_banner_never_accepts_on_repeated_failure(self) -> None:
        source = "連続お仕事を実行できませんでした\r\nタイトルに戻ります"
        tos_banner = (
            "⚠ This service has been disabled in this account for violation of "
            "Terms of Service. Please submit an appeal to continue using this product."
        )

        with mock.patch("asyncio.sleep", return_value=None):
            candidate, error, stats, requests = self._translate_with_scripted_replies(
                source,
                [tos_banner, tos_banner],
            )

        self.assertIsNone(candidate)
        self.assertIn("APIRequestError", error)
        self.assertIn("upstream provider error", error)
        self.assertEqual(len(requests), 2)
        self.assertEqual(stats["test"]["request_errors"], 2)
        self.assertEqual(stats["test"]["accepted"], 0)

    def test_targeted_retry_feedback_addresses_non_numeric_qa_failures(self) -> None:
        with mock.patch("asyncio.sleep", return_value=None):
            candidate, error, stats, requests = self._translate_with_scripted_replies(
                "白石紬とガシャ",
                ["白石紬和转蛋", "白石䌷和扭蛋"],
            )
        self.assertEqual(error, "")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"], "白石䌷和扭蛋")
        correction = retry_correction(requests[1]["messages"][-1]["content"])
        self.assertIn("白石䌷", correction)
        self.assertIn("扭蛋", correction)
        self.assertIn("non_mainland_forbidden_term", correction)


class ZhCnSurfaceQualityTests(unittest.TestCase):
    """Deterministic checks for traditional map coverage, blocked scripts, Ext-B, and TW terms."""

    def test_traditional_char_map_loads_full_table(self) -> None:
        from pipelines.text import mltd_translation_quality as tq

        self.assertTrue(tq.TRADITIONAL_CHAR_MAP_PATH.is_file())
        self.assertEqual(tq.TRADITIONAL_CHAR_MAP_PATH.name, "traditional-to-simplified-char-map.json")
        self.assertGreaterEqual(len(tq.TRADITIONAL_OUTPUT_CHARS), 2400)

    def test_introduced_traditional_chars_exceptions_and_catches(self) -> None:
        from pipelines.text.mltd_translation_quality import introduced_traditional_chars

        # Real Traditional bugs must be caught even next to 著名/著作 prefixes
        self.assertIn("著", introduced_traditional_chars("議論中", "讨论著"))
        self.assertIn("著", introduced_traditional_chars("セレブな感じ", "散发著名流气息"))
        self.assertIn("著", introduced_traditional_chars("メモを取るのに夢中", "顾著作笔记"))
        self.assertIn("過", introduced_traditional_chars("ガラス越しに", "透過玻璃"))
        self.assertIn("賽", introduced_traditional_chars("レース", "大奖賽"))
        # Legitimate Simplified compounds and authoritative idol name must pass
        self.assertEqual(introduced_traditional_chars("有名な作家", "著名作家"), {})
        self.assertEqual(introduced_traditional_chars("顕著な差", "显著差异"), {})
        self.assertEqual(introduced_traditional_chars("著作権", "著作权"), {})
        self.assertEqual(introduced_traditional_chars("可憐さん", "篠宫可怜"), {})

    def test_evaluate_row_and_surface_validator_block_scripts_extb_and_calques(self) -> None:
        from pipelines.text.mltd_translation_quality import (
            evaluate_row,
            validate_zhcn_surface_quality,
        )

        # Valid shared symbol/kaomoji cases from source must still pass
        validate_zhcn_surface_quality("٩(òωó)۶", "٩(òωó)۶")
        validate_zhcn_surface_quality("α波が出てる", "散发出了α波")
        validate_zhcn_surface_quality("アイドルの白石紬さん", "偶像白石䌷小姐")
        validate_zhcn_surface_quality("エミリーとガシャ", "艾米莉和扭蛋")

        bad_samples = [
            ("٩(òωó)۶", "٩(๑òωó๑)۶"),
            ("テスト", "测试ข้อความ"),
            ("テスト", "测试മലയാളം"),
            ("テスト", "测试ա"),
            ("テスト", "测试ગુ"),
            ("テスト", "测试ಕ"),
            ("テスト", "测试ა"),
            ("テスト", "测试א"),
            ("テスト", "真的吗؟"),
            ("テスト", "思考[::]回答"),
            ("テスト", "思考🫧结束"),
            ("テスト", "思考💭结束"),
            ("カンパチ", "红𫠐"),
            ("エミリー", "艾蜜莉"),
            ("エミリー　スチュアート", "艾米莉・斯图亚特"),
            ("エミリー　スチュアート", "艾米莉 斯图亚特"),
            ("白石紬", "白石紬小姐"),
            ("白石紬", "白石䋾"),
            ("徳川まつり", "德川小祭"),
            ("馬場このみ", "这位小美小姐"),
            ("スタイルドロップ獲得", "获得风格掉落物"),
            ("マスタースタイルドロップ", "大师造型水滴"),
            ("ネトスワで話題", "在网滑上引发热议"),
            ("ガシャを引く", "去抽转蛋吧"),
            ("プッシュ通知", "开启推播通知"),
            ("このメッセージ", "删除此讯息"),
            ("イベントランキング", "第活动排行榜第1名"),
            ("テスト", "adrayelejson?"),
            ("ブログ更新", "部落格更新"),
            ("ポップアップ表示", "显示弹跳视窗"),
            ("フォルダ分け", "分好资料夹"),
            ("アプリを起動", "启动应用程式"),
            ("キャッシュデータ", "快取资料"),
            ("ログインボーナス", "登入奖励"),
            ("獲得履歴", "获得履历"),
            ("全力でお届けするのです！", "まつり会竭尽全力！"),
            ("悪い意味じゃなくて", "没有别的意思"),
            ("バイクで来た", "骑自行车来的"),
            ("劇は盛り上がってます", "演出很热闹"),
        ]
        for ja, zh in bad_samples:
            with self.assertRaises(ValueError, msg=f"expected rejection for {(ja, zh)!r}"):
                validate_zhcn_surface_quality(ja, zh)

        # Resume-style 履歴書 may legitimately mention 履历表 without triggering UI 履歴 calque check
        validate_zhcn_surface_quality("履歴書を書く", "填写履历表")

        row_check = evaluate_row({"source": "カンパチ"}, {"translation": "红𫠐"}, {"entries": {}})
        self.assertEqual(row_check["qa_verdict"], "REJECT")
        self.assertEqual(row_check["issues"][0]["code"], "introduced_unsupported_unicode")

        # In-flight QA (evaluate_row + validate_candidate) rejects the exact lexicon rules too
        for ja, zh, code in (
            ("エミリー", "艾蜜莉", "non_mainland_forbidden_term"),
            ("エミリー　スチュアート", "艾米莉‧司徒亚特", "non_authoritative_idol_name"),
            ("エミリー　スチュアート", "艾米莉・斯图亚特", "non_authoritative_idol_name"),
            ("エミリー　スチュアート", "艾米莉 斯图亚特", "non_authoritative_idol_name"),
            ("白石紬", "白石紬小姐", "non_authoritative_idol_name"),
            ("白石紬", "白石䋾", "non_authoritative_idol_name"),
            ("つむぎちゃん", "阿纺酱", "non_authoritative_idol_name"),
            ("紬と一緒", "和纺织一起", "non_authoritative_idol_name"),
            ("篠宮可憐", "筿宫可怜", "non_authoritative_idol_name"),
            ("ソロコレの発売", "ソROCOレ发售", "non_authoritative_idol_name"),
            ("ソロコレの発売", "ROCO发售", "non_authoritative_idol_name"),
            ("松田亜利沙", "松田有栖", "non_authoritative_idol_name"),
            ("松田亜利沙", "松田阿莉莎", "non_authoritative_idol_name"),
            ("松田亜利沙", "松田亚利纱", "non_authoritative_idol_name"),
            ("三浦あずさ", "三浦秋月", "non_authoritative_idol_name"),
            ("三浦あずさ", "三浦静香", "non_authoritative_idol_name"),
            ("徳川まつり", "德川小毬", "non_authoritative_idol_name"),
            ("徳川まつり", "德川玛茜", "non_authoritative_idol_name"),
            ("徳川まつり", "德川小祭", "non_authoritative_idol_name"),
            ("馬場このみ", "马场木乃美", "non_authoritative_idol_name"),
            ("馬場このみ", "马场木下", "non_authoritative_idol_name"),
            ("馬場このみ", "这位小美", "non_authoritative_idol_name"),
            ("高槻やよい", "高槻やよ伊", "non_authoritative_idol_name"),
            ("スタイルドロップ獲得", "获得风格掉落物", "non_authoritative_idol_name"),
            ("マスタースタイルドロップ", "大师造型水滴", "non_authoritative_idol_name"),
            ("ネトスワで話題", "在网滑上引发热议", "non_authoritative_idol_name"),
            ("ネットスワイプス", "网络滑动", "non_authoritative_idol_name"),
            ("ログイン", "登入", "non_mainland_login_calque"),
            ("獲得履歴", "获得履历", "non_mainland_history_calque"),
            ("イベントランキング", "第活动排行榜第1名", "malformed_ranking_phrase"),
            ("テスト", "测试\n\n变成：\n\n测试", "translator_meta_text"),
            ("あずさの写真", "あず莎的照片", "non_authoritative_idol_name"),
            ("ジュニオールだ", "是ジュ尼奥尔？", "truncated_katakana_hybrid"),
            ("クララが手当", "要是クラ拉来照料", "truncated_katakana_hybrid"),
            ("ジュリアーノに聞いた", "问了茱莉亚ーノ", "truncated_katakana_hybrid"),
            ("クルリウタ プラチナマスター", "クル里乌塔 白金大师", "truncated_katakana_hybrid"),
            ("バンギャっぽい", "有邦ギャ风格", "truncated_katakana_hybrid"),
            ("わたくしのこと", "关于わたくし的事", "untranslated_japanese_fragment"),
            ("自覚ないのね～\n知ってるけど", "你还真是毫无自觉呢～\n知ってるけど", "untranslated_japanese_fragment"),
            ("わざと隙を見せる", "故意に露出破绽", "untranslated_japanese_fragment"),
            ("全力でお届けするのです！", "まつり会竭尽全力把笑容传达到的！", "non_authoritative_idol_name"),
            ("はいっ、まつりさん！", "好的，まつり小姐！", "non_authoritative_idol_name"),
            ("悪い意味じゃないんだけど……", "我其实是在称赞你……", "bad_meaning_semantics_missing"),
            ("悪い意味じゃなくて、頼りにしてるの", "没有别的意思，只是很信赖你", "bad_meaning_semantics_missing"),
            ("バイクで来たんだ", "是骑自行车来的", "motorbike_mistranslated_as_bicycle"),
            ("自転車じゃなくバイクを使うだろ", "不是自行车而是单车吧", "motorbike_mistranslated_as_bicycle"),
            ("劇はすっごく盛り上がってます", "演出变得特别热闹", "play_generalized_to_performance"),
        ):
            sid = sha256_text(ja)
            candidate, error = validate_candidate(
                {"source": ja, "source_sha256": sid, "task_hint": "UI"},
                zh,
                [],
                [],
                {"entries": {}},
            )
            self.assertIsNone(candidate, (ja, zh))
            self.assertIn(code, error, (ja, zh))

        # Legitimate co-occurrences and proper names must remain accepted.
        for ja, zh in (
            ("田所あずささんと三浦あずさ", "田所あずさ与三浦梓"),
            ("三浦あずさと最上静香と秋月律子", "三浦梓、最上静香与秋月律子"),
            ("三浦 あずさ", "三浦 梓"),
            ("徳川 まつり", "德川 茉莉"),
            ("馬場 このみ", "马场 木实"),
            ("馬場このみと木下ひなた", "马场木实与木下日向"),
            ("このみさんとこの景色", "木实和这美景"),
            ("まつり湯へようこそ", "欢迎光临まつり汤"),
            ("ロコのソロコレ発売", "ROCO的个人精选发售"),
            ("エミリー　スチュアート", "艾米莉·斯图亚特"),
            ("篠宮可憐と白石紬", "篠宫可怜与白石䌷"),
            ("紬を紡ぐ伝統", "纺织丝绸的传统"),
            ("マスタースタイルドロップを獲得", "获得大师风格水滴"),
            ("ネトスワで話題", "在NetSwipes上引发热议"),
            ("スタローコラボカフェ", "スタロー联动咖啡"),
            ("ミリシタカウントダウン2025", "ミリシタ倒计时2025"),
            ("恋の音色ライン", "《恋の音色ライ\nン》"),
            ("「わたくし」の秘密", "关于「わたくし」的秘密"),
            ("悪い意味合いじゃないです", "并不是坏事"),
            ("悪い意味じゃなくて", "不是说不好，只是觉得很自然"),
            ("悪い意味で捉えないで", "希望你别往坏处想"),
            ("マウンテンバイクが好き", "我喜欢山地自行车"),
            ("「スイム」「バイク」「ラン」の競技", "“游泳”“自行车”“跑步”三项竞技"),
            ("自転車じゃなくてバイクだよ", "不是自行车而是摩托车"),
            ("劇はすっごく盛り上がってます", "这出剧变得特别热闹"),
        ):
            validate_zhcn_surface_quality(ja, zh)
            sid = sha256_text(ja)
            candidate, error = validate_candidate(
                {"source": ja, "source_sha256": sid, "task_hint": "UI"},
                zh,
                [],
                [],
                {"entries": {}},
            )
            self.assertIsNotNone(candidate, f"unexpected rejection for {(ja, zh)!r}: {error}")

    def test_qa_retry_feedback_covers_new_idol_and_meta_guidance(self) -> None:
        from pipelines.text.translate_mltd_api_pool import _build_qa_retry_feedback

        idol_feedback = _build_qa_retry_feedback("non_authoritative_idol_name")
        self.assertIn("松田亚利沙", idol_feedback)
        self.assertIn("个人精选", idol_feedback)
        meta_feedback = _build_qa_retry_feedback("translator_meta_text")
        self.assertIn("变成：", meta_feedback)
        trunc_feedback = _build_qa_retry_feedback("truncated_katakana_hybrid")
        self.assertIn("ジュ尼奥尔", trunc_feedback)
        frag_feedback = _build_qa_retry_feedback("untranslated_japanese_fragment")
        self.assertIn("わたくし", frag_feedback)
        self.assertIn("没有别的意思", _build_qa_retry_feedback("bad_meaning_semantics_missing"))
        self.assertIn("摩托车", _build_qa_retry_feedback("motorbike_mistranslated_as_bicycle"))
        self.assertIn("演出", _build_qa_retry_feedback("play_generalized_to_performance"))


if __name__ == "__main__":
    unittest.main()
