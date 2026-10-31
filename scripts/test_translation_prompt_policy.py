#!/usr/bin/env python3
"""The system prompt must agree with the validator that judges its output.

Runs #26 and #27 of 2026-10-12 both returned the same two-line draft for five
lyric rows -- ``わたしはもういないよ\\n(重なっていくよ Precious tone)`` became
``我已经不在了哦\\n(重叠起来吧 Precious tone)`` -- and
:func:`validate_lyric_tokens` refused all five with "lyric translation must stay
on one line".  The model was not disobedient: the output contract told it to
*"Preserve meaningful line breaks"*, and the lyric task was not named in TASK
BEHAVIOR at all, so nothing in the prompt could override that.

A rule the prompt asks for and the validator refuses is a policy nobody can
satisfy.  These tests pin the two files to the same answer.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.text.mltd_lyric_rules import validate_lyric_tokens  # noqa: E402
from pipelines.text.mltd_translation_prompt import (  # noqa: E402
    BASE,
    compile_prompt_bundle,
    snapshot_status,
    write_prompt_snapshot,
)
from pipelines.text.mltd_natural_prompt import NATURAL_POLICY  # noqa: E402
from pipelines.text.mltd_translation_quality import applicable_glossary  # noqa: E402


class LyricLineBreakPolicyTests(unittest.TestCase):
    """The prompt's line-break policy and the lyric validator say the same thing."""

    def test_the_lyric_task_is_named_in_task_behavior(self):
        self.assertIn("ASSETS_LYRICS:", BASE)
        lyric_line = next(line for line in BASE.splitlines() if line.startswith("- ASSETS_LYRICS:"))
        self.assertIn("no line break", lyric_line)
        # Joining, not dropping: the second half of the Japanese line still has to
        # appear in the answer, which is what makes the break a space.
        self.assertIn("join the parts into one line", lyric_line)

    def test_the_output_contract_scopes_the_break_rule_to_surfaces_that_can_show_one(self):
        # The bare unconditional instruction is what produced the rejected drafts:
        # "Preserve meaningful line breaks and every numeric literal ..." with no
        # exception for the one surface that renders a single line per slot.
        self.assertNotIn("Preserve meaningful line breaks and every numeric literal", BASE)
        contract = next(line for line in BASE.splitlines() if "numeric literal exactly as written" in line)
        self.assertIn("Preserve meaningful line breaks", contract)
        self.assertIn("where the surface can show them", contract)
        self.assertIn("ASSETS_LYRICS", contract)

    def test_the_final_check_is_task_aware(self):
        check = next(line for line in BASE.splitlines() if "protected-marker counts" in line)
        self.assertIn("line-break policy", check)
        self.assertIn("lyrics are one line", check)

    def test_the_validator_still_refuses_exactly_what_the_prompt_now_forbids(self):
        source = "わたしはもういないよ\n(重なっていくよ Precious tone)"
        with self.assertRaises(ValueError) as caught:
            validate_lyric_tokens(source, "我已经不在了哦\n(重叠起来吧 Precious tone)")
        self.assertIn("one line", str(caught.exception))
        # The joined form the prompt asks for is the form the library accepted.
        validate_lyric_tokens(source, "我已经不复存在 （交叠相织吧 Precious tone）")

    def test_natural_policy_protects_names_catchphrases_and_special_terms(self):
        self.assertIn("只能依据 GLOSSARY", NATURAL_POLICY)
        self.assertIn("不要按汉字字面、日语读音或当前场景临时改名", NATURAL_POLICY)
        self.assertIn("不要因为说话者身份自行添加", NATURAL_POLICY)
        self.assertIn("特殊术语在相邻句中随意换名", NATURAL_POLICY)


class CompiledGlossaryMappingTests(unittest.TestCase):
    """The real compiler must carry authoritative ``target`` glossary rules.

    Production ``--glossary`` defaults to ``glossary/authoritative-terms.json``,
    whose evidence-backed field is ``target``.  A compiler that only read the
    legacy ``preferred`` field emitted 90 bare source terms and zero mappings
    for the live 90-entry file, so the compiled GLOSSARY section lost every
    target rule (the separate masking path still enforced those targets).
    """

    def _workspace(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _compile(self, glossary_doc, *, policy_style="natural"):
        tmp = self._workspace()
        glossary_path = tmp / "glossary.json"
        evidence_path = tmp / "evidence.json"
        glossary_path.write_text(json.dumps(glossary_doc, ensure_ascii=False), encoding="utf-8")
        evidence_path.write_text(
            json.dumps({"speakers": {"001har": {"name_jp": "haru"}}}), encoding="utf-8"
        )
        return compile_prompt_bundle(
            glossary_path, evidence_path, policy_style=policy_style
        )

    def _glossary_lines(self, bundle):
        # The standalone section header is "\nGLOSSARY\n"; the policy text only
        # ever mentions "GLOSSARY" inline (e.g. "GLOSSARY 是本项目的术语依据"),
        # so anchoring on the newlines isolates the compiled section.
        section = bundle.text.split("\nGLOSSARY\n", 1)[1].split("\n\n", 1)[0]
        return [line for line in section.splitlines() if line]

    def test_authoritative_target_entries_reach_the_compiled_prompt(self):
        bundle = self._compile(
            {
                "entries": {
                    "ハイスコア ランキング": {"target": "最高分排行榜", "category": "ranking_label"},
                    "茜ちゃん": {"target": "小茜"},
                    "キミさけ": {"target": "キミさけ"},
                    "謎の未訳語": {"category": "pending"},  # no target at all
                }
            }
        )
        lines = self._glossary_lines(bundle)
        self.assertIn("茜ちゃん=>小茜", lines)
        self.assertIn("ハイスコア ランキング=>最高分排行榜", lines)
        self.assertIn("キミさけ=>キミさけ", lines)
        # No target means a bare term, never a fabricated ``src=>`` mapping.
        self.assertIn("謎の未訳語", lines)
        self.assertNotIn("謎の未訳語=>", bundle.text)
        # Production default must be the natural policy, not legacy.
        self.assertEqual(bundle.manifest["policy_style"], "natural")
        self.assertEqual(bundle.manifest["prompt_version"], "mltd-zhcn-natural-v2")

    def test_legacy_preferred_and_string_specs_still_compile(self):
        bundle = self._compile(
            {
                "entries": {
                    "ロコ": {"preferred": "ROCO"},
                    "ジュリア": "茱莉亚",  # bare string spec
                    "でぃあ": {"preferred": "Dearly", "forbidden": ["迪亚", "  "]},
                }
            },
            policy_style="legacy",
        )
        lines = self._glossary_lines(bundle)
        self.assertIn("ロコ=>ROCO", lines)
        self.assertIn("ジュリア=>茱莉亚", lines)
        # ``forbidden`` must survive; blank entries are dropped, real ones kept.
        self.assertIn("でぃあ=>Dearly; avoid=迪亚", lines)

    def test_non_empty_preferred_wins_over_target(self):
        bundle = self._compile(
            {
                "entries": {
                    "茜ちゃん": {"preferred": "阿茜", "target": "小茜"},
                    "シーズン": {"target": "赛季"},  # target-only still works
                }
            }
        )
        lines = self._glossary_lines(bundle)
        self.assertIn("茜ちゃん=>阿茜", lines)
        self.assertNotIn("茜ちゃん=>小茜", bundle.text)
        self.assertIn("シーズン=>赛季", lines)

    def test_compiled_mapping_agrees_with_quality_applicable_glossary(self):
        entries = {
            "ハイスコア ランキング": {"target": "最高分排行榜"},
            "茜ちゃん": {"target": "小茜"},
            "自作の語": {"target": "自造语"},
        }
        bundle = self._compile({"entries": entries})
        lines = set(self._glossary_lines(bundle))
        source = "茜ちゃんのハイスコア ランキング"
        for rule in applicable_glossary(source, {"entries": entries}):
            self.assertIn(f"{rule['source_term']}=>{rule['preferred']}", lines)
        # A term absent from SOURCE stays compiled but is never "applicable".
        self.assertIn("自作の語=>自造语", lines)
        self.assertNotIn("自作の語", {r["source_term"] for r in applicable_glossary(source, {"entries": entries})})

    def test_hashes_and_snapshot_round_trip_track_the_compiled_text(self):
        bundle = self._compile({"entries": {"茜ちゃん": {"target": "小茜"}}})
        data = bundle.text.encode("utf-8")
        self.assertEqual(bundle.manifest["prompt_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(bundle.manifest["prompt_bytes"], len(data))

        tmp = self._workspace()
        prompt_path = tmp / "prompt.md"
        manifest_path = tmp / "prompt.manifest.json"
        self.assertEqual(snapshot_status(bundle, prompt_path, manifest_path)["status"], "stale")
        write_prompt_snapshot(bundle, prompt_path, manifest_path)
        status = snapshot_status(bundle, prompt_path, manifest_path)
        self.assertEqual(status["status"], "current")
        self.assertEqual(status["snapshot_prompt_sha256"], bundle.manifest["prompt_sha256"])

        # A prompt edited outside the compiler is detected as stale again.
        prompt_path.write_text(bundle.text + "\nDRIFT", encoding="utf-8")
        self.assertEqual(snapshot_status(bundle, prompt_path, manifest_path)["status"], "stale")


if __name__ == "__main__":
    unittest.main()
