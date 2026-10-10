"""Rules tests for lyric text, with no UnityPy dependency.

``pipelines/text/test_mltd_localize_scrobj.py`` covers the Unity reader and
therefore needs UnityPy installed; these rules drive the translation
queue, the manifest's ``english_bypass_slots``, 1,066 already-published rows and
every lyric string that reaches a device, so they are pinned where they can
always run.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mltd_lyric_rules import (  # noqa: E402
    is_english_bypass,
    is_localizable_line,
    validate_lyric_tokens,
)


class LocalizableLineTests(unittest.TestCase):
    """The published rule: Latin script and no kana/kanji stays English."""

    def test_japanese_lines_need_translation(self):
        for text in ("一旦愛して♡", "ちょうだい", "涙には意味がある", "【譲】 キュート"):
            self.assertTrue(is_localizable_line(text), text)
            self.assertFalse(is_english_bypass(text), text)

    def test_ascii_lines_are_left_alone(self):
        for text in ("I Love You", "Up we go!", "Crazy now"):
            self.assertFalse(is_localizable_line(text), text)
            self.assertTrue(is_english_bypass(text), text)

    def test_full_width_punctuation_does_not_make_a_line_japanese(self):
        """These 236 rows exist in the library and must stay English.

        A full-width ``！`` or ``～`` is not kana and not an ideograph, so the
        English rule wins; treating them as Japanese would queue 236 already
        settled rows for translation and change the published bypass count.
        """
        for text in ("SAY HALLO！", "『MILLION』", "Ha～", "（Thank you... Fooooo!!）",
                     "YES！WE WERE BORN ON DREAM！\n", "「I miss you…」 "):
            self.assertTrue(is_english_bypass(text), text)
            self.assertFalse(is_localizable_line(text), text)

    def test_symbols_and_digits_are_not_an_english_bypass(self):
        for text in ("♪", "5 4 3 2 1 0", "（♪）", ""):
            self.assertFalse(is_english_bypass(text), text)

    def test_latin_with_a_single_kana_is_translated(self):
        self.assertTrue(is_localizable_line("Wow そう"))
        self.assertFalse(is_localizable_line("Wow"))

    def test_half_width_katakana_counts_as_japanese(self):
        self.assertTrue(is_localizable_line("ﾃｽﾄ"))

    def test_full_width_latin_is_latin(self):
        self.assertTrue(is_english_bypass("ＨＥＬＬＯ"))


class LyricTokenTests(unittest.TestCase):
    """What may be stored as a lyric translation, measured against the library."""

    def test_an_ordinary_line_passes(self):
        validate_lyric_tokens("一旦愛して♡", "先爱一下♡")
        validate_lyric_tokens("涙には意味がある", "眼泪亦有意义")

    def test_emphasis_text_may_be_translated_but_brackets_must_balance(self):
        validate_lyric_tokens("<いつの間にかこんなに>", "<不知不觉间已经如此>")
        with self.assertRaises(ValueError) as caught:
            validate_lyric_tokens("<いつの間にかこんなに>", "不知不觉间已经如此")
        self.assertIn("angle bracket", str(caught.exception))

    def test_an_engine_tag_is_never_dropped(self):
        validate_lyric_tokens("<size=24>やあ", "<size=24>你好")
        with self.assertRaises(ValueError):
            validate_lyric_tokens("<size=24>やあ", "你好")

    def test_placeholders_survive_verbatim(self):
        validate_lyric_tokens("{0}よ", "{0}呀")
        with self.assertRaises(ValueError) as caught:
            validate_lyric_tokens("{0}よ", "呀")
        self.assertIn("protected token", str(caught.exception))

    def test_a_second_line_is_refused(self):
        """One slot is one line on the device; no committed row carries a newline."""
        with self.assertRaises(ValueError) as caught:
            validate_lyric_tokens("空っぽのステージ", "空荡荡的舞台\n（附注）")
        self.assertIn("one line", str(caught.exception))

    def test_an_explanation_instead_of_a_translation_is_refused(self):
        """The blob that failed run 38058410546, on one line: 10x the source."""
        blob = ("思考过程：空っぽのステージ指的是空无一人的舞台，这里应当译作空荡荡的舞台，"
                "因为歌词强调舞台的空旷；备选译法还有空落落的舞台、无人登台的舞台，"
                "综合上下句的语气，最终采用空荡荡的舞台。")
        with self.assertRaises(ValueError) as caught:
            validate_lyric_tokens("空っぽのステージ", blob)
        self.assertIn("ceiling", str(caught.exception))

    def test_the_length_ceiling_leaves_the_library_room(self):
        """The longest committed translation is 2.67x its source, so 4x is safe."""
        source = "Wow" * 10
        validate_lyric_tokens(source, "哇" * (2 * len(source)))
        with self.assertRaises(ValueError):
            validate_lyric_tokens(source, "哇" * (5 * len(source)))

    def test_a_short_line_may_expand_beyond_the_multiple(self):
        """The floor keeps a three-character line natural rather than literal."""
        validate_lyric_tokens("Wow", "哇哦，真是太好了呀")  # 9 characters, multiple would be 12
        self.assertLess(len("哇哦，真是太好了呀"), 40)


if __name__ == "__main__":
    unittest.main()
