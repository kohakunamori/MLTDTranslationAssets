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


if __name__ == "__main__":
    unittest.main()
