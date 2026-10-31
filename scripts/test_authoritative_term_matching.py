"""Regression tests for shared authoritative term matching."""
import asyncio
from collections import defaultdict
import json
import unittest

import httpx

from pipelines.core.hashing import sha256_text
from pipelines.text.authoritative_term_matching import TermMatch, find_term_matches
from pipelines.text.mltd_translation_quality import applicable_glossary, evaluate_row
from pipelines.text.translate_mltd_api_pool import (
    ModelConfig,
    mask_authoritative_terms,
    restore_authoritative_terms,
    translate_with_model,
)


def _matches(text, terms):
    return [
        (match.start, match.end, match.term)
        for match in find_term_matches(text, terms)
    ]


def _glossary():
    return {
        "entries": {
            "ロコ": {"target": "ROCO"},
            "ジュリア": {"target": "茱莉亚"},
        },
        "kana_allowlist": [],
    }


def _config():
    return ModelConfig(
        id="test",
        model="alias",
        api_protocol="chat_completions",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key_env="",
        concurrency=1,
        reasoning_effort="high",
        temperature=0.1,
        timeout=10,
        retries=1,
        min_reasoning_effort="high",
        cache_warmup=False,
    )


class TermMatcherTests(unittest.TestCase):
    def test_false_positive_katakana_embeddings_are_rejected(self):
        terms = ["ロコ", "ジュリア"]
        for text in ["ウロコ", "クロコダイル", "ジュリアン"]:
            self.assertEqual(find_term_matches(text, terms), [], text)

    def test_katakana_positive_controls_and_middle_dot(self):
        terms = ["ロコ", "ジュリア"]
        self.assertEqual(_matches("ロコちゃん", terms), [(0, 2, "ロコ")])
        self.assertEqual(_matches("ジュリアの歌", terms), [(0, 4, "ジュリア")])
        self.assertEqual(_matches("[衣装]ロコ", terms), [(4, 6, "ロコ")])
        self.assertEqual(_matches("ロコ・", terms), [(0, 2, "ロコ")])
        self.assertEqual(
            _matches("ロコ・ジュリア", terms),
            [(0, 2, "ロコ"), (3, 7, "ジュリア")],
        )

    def test_prolonged_sound_mark_blocks_embedded_katakana_term(self):
        self.assertEqual(find_term_matches("ロコー", ["ロコ"]), [])

    def test_halfwidth_katakana_boundaries_and_middle_dot(self):
        self.assertEqual(find_term_matches("ﾛロコ", ["ロコ"]), [])
        self.assertEqual(find_term_matches("ロコｰ", ["ロコ"]), [])
        self.assertEqual(_matches("ロコ･", ["ロコ"]), [(0, 2, "ロコ")])

    def test_kanji_and_mixed_terms_do_not_use_script_boundaries(self):
        self.assertEqual(
            _matches("如月千早誕生日", ["如月千早"]),
            [(0, 4, "如月千早")],
        )
        self.assertEqual(
            _matches("今日は千早さんと", ["千早さん"]),
            [(3, 7, "千早さん")],
        )

    def test_longest_overlapping_match_wins(self):
        self.assertEqual(
            _matches("如月千早誕生日", ["千早", "如月千早"]),
            [(0, 4, "如月千早")],
        )
        self.assertEqual(
            _matches("ABCDE", ["AB", "BCDE", "DE"]),
            [(1, 5, "BCDE")],
        )
        self.assertEqual(
            _matches("ABC", ["AB", "BC"]),
            [(0, 2, "AB")],
        )

    def test_repeated_terms_empty_terms_and_original_offsets(self):
        self.assertEqual(
            _matches("ロコとロコ", ["", "ロコ", "ロコ"]),
            [(0, 2, "ロコ"), (3, 5, "ロコ")],
        )
        match, = find_term_matches("🎵ロコ", ["ロコ"])
        self.assertIsInstance(match, TermMatch)
        self.assertEqual((match.start, match.end, match.term), (1, 3, "ロコ"))

    def test_mask_preserves_boundaries_repetitions_and_restoration(self):
        terms = {"ロコ": "ROCO", "ジュリア": "茱莉亚"}
        source = "ウロコ、ロコちゃん、クロコダイル、ジュリアン、ジュリアの歌"
        masked, targets, applied = mask_authoritative_terms(source, terms)
        self.assertEqual(
            masked,
            "ウロコ、__MLTD_TERM_000__ちゃん、クロコダイル、"
            "ジュリアン、__MLTD_TERM_001__の歌",
        )
        self.assertEqual(targets, ["ROCO", "茱莉亚"])
        self.assertEqual(applied, ["ロコ", "ジュリア"])
        self.assertEqual(
            restore_authoritative_terms(masked, targets),
            "ウロコ、ROCOちゃん、クロコダイル、ジュリアン、茱莉亚の歌",
        )

        repeated, repeated_targets, repeated_applied = mask_authoritative_terms(
            "ロコとロコ", terms
        )
        self.assertEqual(
            repeated, "__MLTD_TERM_000__と__MLTD_TERM_001__"
        )
        self.assertEqual(repeated_targets, ["ROCO", "ROCO"])
        self.assertEqual(repeated_applied, ["ロコ", "ロコ"])

        overlap_masked, overlap_targets, overlap_applied = (
            mask_authoritative_terms(
                "如月千早誕生日",
                {"千早": "SHORT", "如月千早": "LONG"},
            )
        )
        self.assertEqual(overlap_masked, "__MLTD_TERM_000__誕生日")
        self.assertEqual(overlap_targets, ["LONG"])
        self.assertEqual(overlap_applied, ["如月千早"])


class GlossaryQualityTests(unittest.TestCase):
    def test_applicable_glossary_ignores_embedded_katakana_words(self):
        glossary = _glossary()
        for source in ["ウロコ", "クロコダイル", "ジュリアン"]:
            self.assertEqual(applicable_glossary(source, glossary), [], source)

    def test_qa_no_longer_requires_names_for_ordinary_words(self):
        glossary = _glossary()
        for source in ["ウロコ", "クロコダイル", "ジュリアン"]:
            sid = sha256_text(source)
            result = evaluate_row(
                {
                    "source": source,
                    "source_sha256": sid,
                    "task_hint": "DIALOGUE",
                },
                {
                    "source": source,
                    "source_sha256": sid,
                    "translation": "合成译文。",
                },
                glossary,
            )
            codes = {issue["code"] for issue in result["issues"]}
            self.assertNotIn("preferred_term_missing", codes, source)

    def test_positive_glossary_rules_remain_and_are_deduplicated(self):
        glossary = _glossary()
        self.assertEqual(
            [
                rule["source_term"]
                for rule in applicable_glossary(
                    "ロコちゃんとジュリアの歌", glossary
                )
            ],
            ["ロコ", "ジュリア"],
        )
        self.assertEqual(
            [
                rule["source_term"]
                for rule in applicable_glossary("ロコとロコ", glossary)
            ],
            ["ロコ"],
        )

        source = "ロコちゃんとジュリアの歌"
        sid = sha256_text(source)
        result = evaluate_row(
            {"source": source, "source_sha256": sid, "task_hint": "DIALOGUE"},
            {
                "source": source,
                "source_sha256": sid,
                "translation": "小洛可酱和朱莉安的歌",
            },
            glossary,
        )
        missing = [
            issue
            for issue in result["issues"]
            if issue["code"] == "preferred_term_missing"
        ]
        self.assertEqual(len(missing), 2)


class AuthoritativeTranslationPipelineTests(unittest.TestCase):
    def test_exact_bypass_and_non_exact_model_path_use_consistent_matching(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "reported-backend",
                    "choices": [{"message": {"content": "鱼鳞"}}],
                    "usage": {"total_tokens": 1},
                },
            )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                cfg = _config()
                stats = defaultdict(lambda: defaultdict(float))
                glossary = _glossary()
                terms = {"ロコ": "ROCO"}

                exact_source = "ロコ"
                exact_sid = sha256_text(exact_source)
                exact, error = await translate_with_model(
                    client,
                    cfg,
                    None,
                    "SYSTEM",
                    {
                        "source": exact_source,
                        "source_sha256": exact_sid,
                        "task_hint": "DIALOGUE",
                    },
                    glossary,
                    terms,
                    stats,
                )
                self.assertEqual(error, "")
                self.assertEqual(exact["translation"], "ROCO")
                self.assertEqual(
                    exact["provenance"]["provider"],
                    "deterministic:authoritative_terms",
                )
                self.assertEqual(requests, [])

                ordinary_source = "ウロコ"
                ordinary_sid = sha256_text(ordinary_source)
                ordinary, error = await translate_with_model(
                    client,
                    cfg,
                    None,
                    "SYSTEM",
                    {
                        "source": ordinary_source,
                        "source_sha256": ordinary_sid,
                        "task_hint": "DIALOGUE",
                    },
                    glossary,
                    terms,
                    stats,
                )
                self.assertEqual(error, "")
                self.assertEqual(ordinary["translation"], "鱼鳞")
                self.assertEqual(len(requests), 1)
                self.assertNotIn(
                    "__MLTD_TERM_",
                    json.dumps(requests[0], ensure_ascii=False),
                )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
