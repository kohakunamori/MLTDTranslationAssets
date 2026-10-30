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

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.text.mltd_lyric_rules import validate_lyric_tokens  # noqa: E402
from pipelines.text.mltd_translation_prompt import BASE  # noqa: E402
from pipelines.text.mltd_natural_prompt import NATURAL_POLICY  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
