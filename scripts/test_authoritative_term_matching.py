"""Regression tests for shared authoritative term matching."""
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import httpx

from pipelines.core.hashing import sha256_text
from pipelines.text.authoritative_term_matching import TermMatch, find_term_matches
from pipelines.text.build_authoritative_terms import _source_variant_entries
from pipelines.text.mltd_translation_quality import applicable_glossary, evaluate_row
from pipelines.text.translate_mltd_api_pool import (
    ModelConfig,
    load_authoritative_terms,
    mask_authoritative_terms,
    restore_authoritative_terms,
    translate_with_model,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_GLOSSARY_PATH = ROOT / "glossary/authoritative-terms.json"


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


class FullNameWhitespaceVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.glossary = json.loads(
            REAL_GLOSSARY_PATH.read_text(encoding="utf-8")
        )
        cls.terms = load_authoritative_terms(REAL_GLOSSARY_PATH)

    def test_real_glossary_variants_share_canonical_targets(self):
        entries = self.glossary["entries"]
        canonical_entries = {
            source: spec
            for source, spec in entries.items()
            if "source_variant_of" not in spec
        }
        derived = _source_variant_entries(canonical_entries)
        self.assertEqual(
            {variant: entries[variant] for variant in derived},
            derived,
        )
        counts = self.glossary["counts"]
        self.assertEqual(counts["fullname_source_variants"], 52)
        self.assertEqual(counts["entries"], len(entries))
        self.assertEqual(counts["entries"], 142)
        self.assertEqual(
            len({spec["source_variant_of"] for spec in derived.values()}),
            50,
        )
        evidence_counts = defaultdict(int)
        for spec in derived.values():
            evidence_counts[spec["evidence"]] += 1
        self.assertEqual(
            counts["fullname_source_variants_space"],
            evidence_counts[
                "observed_source_fullname_space_variant"
                "+canonical_authoritative_target"
            ],
        )
        self.assertEqual(
            counts["fullname_source_variants_separator"],
            evidence_counts[
                "observed_source_fullname_separator_variant"
                "+canonical_authoritative_target"
            ],
        )
        self.assertEqual(
            counts["fullname_source_variants"],
            sum(evidence_counts.values()),
        )
        # The recorded separator spelling inserts U+30FB while the owner target
        # keeps U+00B7, so inheriting the canonical row must not touch its bytes.
        dotted = derived["エミリー・スチュアート"]
        self.assertEqual(dotted["source_variant_of"], "エミリースチュアート")
        self.assertEqual(dotted["target"], "艾米莉\u00b7斯图亚特")
        self.assertNotIn("\u30fb", dotted["target"])
        for variant, spec in derived.items():
            with self.subTest(variant=variant):
                canonical = spec["source_variant_of"]
                self.assertEqual(spec["category"], "character_name")
                self.assertEqual(spec["target"], entries[canonical]["target"])

    def test_production_masking_and_glossary_rules_emit_canonical_targets(self):
        variants = {
            source: spec
            for source, spec in self.glossary["entries"].items()
            if "source_variant_of" in spec
        }
        for variant, spec in variants.items():
            target = spec["target"]
            source = f"{variant}と練習する"
            with self.subTest(source=source):
                masked, targets, applied = mask_authoritative_terms(
                    source, self.terms
                )
                self.assertEqual(applied, [variant])
                self.assertEqual(targets, [target])
                self.assertEqual(masked, "__MLTD_TERM_000__と練習する")
                self.assertEqual(
                    restore_authoritative_terms(masked, targets),
                    f"{target}と練習する",
                )
                match, = find_term_matches(source, self.terms)
                self.assertEqual(
                    (match.start, match.end, match.term),
                    (0, len(variant), variant),
                )
                self.assertEqual(
                    [
                        (rule["source_term"], rule["preferred"])
                        for rule in applicable_glossary(source, self.glossary)
                    ],
                    [(variant, target)],
                )
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
                        "translation": f"{target}と練習する",
                    },
                    self.glossary,
                )
                self.assertNotIn(
                    "preferred_term_missing",
                    {issue["code"] for issue in result["issues"]},
                )

    def test_separator_alias_requires_true_katakana_edges(self):
        variants = [
            "エミリー スチュアート",
            "エミリー　スチュアート",
            "エミリー・スチュアート",
        ]
        for variant in variants:
            for source in [f"{variant}マン", f"マン{variant}"]:
                with self.subTest(source=source):
                    masked, targets, applied = mask_authoritative_terms(
                        source, self.terms
                    )
                    self.assertEqual((masked, targets, applied), (source, [], []))
                    self.assertEqual(
                        applicable_glossary(source, self.glossary),
                        [],
                    )

        source = "🎵エミリー スチュアートと歌う"
        match, = find_term_matches(source, self.terms)
        self.assertEqual(
            (match.start, match.end, match.term),
            (1, 1 + len("エミリー スチュアート"), "エミリー スチュアート"),
        )
        masked, targets, applied = mask_authoritative_terms(
            source, self.terms
        )
        self.assertEqual(
            masked,
            "🎵__MLTD_TERM_000__と歌う",
        )
        self.assertEqual(applied, ["エミリー スチュアート"])
        self.assertEqual(
            restore_authoritative_terms(masked, targets),
            "🎵艾米莉·斯图亚特と歌う",
        )
        dotted = "🎵エミリー・スチュアートと歌う"
        masked, targets, applied = mask_authoritative_terms(
            dotted, self.terms
        )
        self.assertEqual(masked, "🎵__MLTD_TERM_000__と歌う")
        self.assertEqual(targets, ["艾米莉\u00b7斯图亚特"])
        self.assertEqual(applied, ["エミリー・スチュアート"])
        self.assertEqual(
            restore_authoritative_terms(masked, targets),
            "🎵艾米莉\u00b7斯图亚特と歌う",
        )

    def test_separator_alias_enforces_owner_middle_dot_in_qa(self):
        """VOICE-4-003: a U+30FB source spelling must not reach U+30FB output."""
        variant = "エミリー・スチュアート"
        target = "艾米莉\u00b7斯图亚特"
        source = f"新人の{variant}です"
        sid = sha256_text(source)
        masked, targets, applied = mask_authoritative_terms(source, self.terms)
        self.assertEqual(masked, "新人の__MLTD_TERM_000__です")
        self.assertEqual(applied, [variant])
        self.assertEqual(targets, [target])
        self.assertEqual(
            restore_authoritative_terms(masked, targets),
            f"新人の{target}です",
        )
        self.assertEqual(
            [
                (rule["source_term"], rule["preferred"])
                for rule in applicable_glossary(source, self.glossary)
            ],
            [(variant, target)],
        )
        result = evaluate_row(
            {"source": source, "source_sha256": sid, "task_hint": "DIALOGUE"},
            {
                "source": source,
                "source_sha256": sid,
                "translation": "新人の艾米莉\u30fb斯图亚特です",
            },
            self.glossary,
        )
        self.assertIn(
            "preferred_term_missing",
            {issue["code"] for issue in result["issues"]},
        )

    def test_reordered_and_ordinary_controls_remain_unmasked(self):
        for source in [
            "スチュアート エミリー",
            "島原 へ行く",
            "エミリースチュアートマン",
            "エミリー",
            "エミリーと歌う",
            "エミリー スチュアートマン",
            "マンエミリー スチュアート",
            "エミリー・スチュアートマン",
            "マンエミリー・スチュアート",
        ]:
            with self.subTest(source=source):
                masked, targets, applied = mask_authoritative_terms(
                    source, self.terms
                )
                self.assertEqual((masked, targets, applied), (source, [], []))
                self.assertEqual(
                    applicable_glossary(source, self.glossary),
                    [],
                )

    def test_variant_helper_derives_targets_from_synthetic_mappings(self):
        canonical = {
            "テストアイドル": {
                "target": "测试偶像",
                "category": "character_name",
            }
        }
        self.assertEqual(
            _source_variant_entries(
                canonical,
                {"テスト アイドル": "テストアイドル"},
            ),
            {
                "テスト アイドル": {
                    "target": "测试偶像",
                    "category": "character_name",
                    "evidence": (
                        "observed_source_fullname_space_variant"
                        "+canonical_authoritative_target"
                    ),
                    "source_variant_of": "テストアイドル",
                }
            },
        )
        self.assertEqual(
            _source_variant_entries(
                canonical,
                {"テスト・アイドル": "テストアイドル"},
            ),
            {
                "テスト・アイドル": {
                    "target": "测试偶像",
                    "category": "character_name",
                    "evidence": (
                        "observed_source_fullname_separator_variant"
                        "+canonical_authoritative_target"
                    ),
                    "source_variant_of": "テストアイドル",
                }
            },
        )
        with self.assertRaisesRegex(ValueError, "missing canonical"):
            _source_variant_entries(
                canonical,
                {"登録 なし": "登録なし"},
            )
        with self.assertRaisesRegex(ValueError, "not a separator spelling"):
            _source_variant_entries(
                canonical,
                {"テスト イドル": "テストアイドル"},
            )
        with self.assertRaisesRegex(ValueError, "unclassified source variant"):
            _source_variant_entries(
                canonical,
                {"テスト･アイドル": "テストアイドル"},
            )
        with self.assertRaisesRegex(ValueError, "edge separator"):
            _source_variant_entries(
                canonical,
                {" テストアイドル": "テストアイドル"},
            )


class AuthoritativeTermsGeneratorTests(unittest.TestCase):
    def test_cli_reproduces_glossary_and_counts(self):
        glossary = json.loads(REAL_GLOSSARY_PATH.read_text(encoding="utf-8"))
        exact_entries = {
            source: spec
            for source, spec in glossary["entries"].items()
            if spec.get("evidence") == "official_legacy_zhcn_exact_source"
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            evidence_path = temp / "character-evidence.json"
            zhcn_path = temp / "official-zhcn.jsonl"
            output_path = temp / "authoritative-terms.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "speakers": {
                            f"test_{index:03d}": {"name_jp": source}
                            for index, source in enumerate(exact_entries)
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with zhcn_path.open("w", encoding="utf-8", newline="\n") as handle:
                for source, spec in exact_entries.items():
                    row = {
                        "source": source,
                        "translation": spec["target"],
                    }
                    for _ in range(spec["evidence_count"]):
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "pipelines/text/build_authoritative_terms.py"),
                    "--character-evidence",
                    str(evidence_path),
                    "--official-zhcn",
                    str(zhcn_path),
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            generated = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(generated, glossary)
        self.assertEqual(generated["counts"]["entries"], 142)
        self.assertEqual(
            generated["counts"]["fullname_source_variants"],
            52,
        )
        self.assertEqual(
            generated["counts"]["fullname_source_variants_space"],
            51,
        )
        self.assertEqual(
            generated["counts"]["fullname_source_variants_separator"],
            1,
        )


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
