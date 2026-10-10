"""Rules tests for lyric text, with no UnityPy dependency.

``pipelines/text/test_mltd_localize_scrobj.py`` covers the Unity reader and
therefore needs UnityPy installed; these three rules drive the translation
queue, the manifest's ``english_bypass_slots`` and 1,066 already-published rows,
so they are pinned where they can always run.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mltd_lyric_rules import is_english_bypass, is_localizable_line  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
