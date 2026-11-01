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
    translate_with_model,
)


# Keep the corruption explicit: Hangul JEU and Devanagari SA + AA.
CORRUPT_NAME = "\u3042\uC988\u0938\u093E"


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

    def test_bounded_retry_saves_clean_candidate_after_script_reject(self) -> None:
        source = "あずささん、今、鼻歌を歌っていましたか？"
        corrupt = CORRUPT_NAME + "小姐，您刚才是在哼歌吗？"
        clean = "梓小姐，您刚才是在哼歌吗？"
        requests: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            text = corrupt if len(requests) == 1 else clean
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": text}}]},
            )

        async def run():
            row = {
                "source": source,
                "source_sha256": sha256_text(source),
                "task_hint": "DIALOGUE",
            }
            stats = defaultdict(lambda: defaultdict(float))
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
                    {},
                    stats,
                )
            return candidate, error, stats

        candidate, error, stats = asyncio.run(run())
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


if __name__ == "__main__":
    unittest.main()
