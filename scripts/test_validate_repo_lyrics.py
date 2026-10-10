import json
import tempfile
import unittest
from pathlib import Path

import validate_repo as tool


def row(bundle, index, text, *, zh="", status="untranslated"):
    return {
        "bundle": bundle, "index": index, "tick": index * 960, "abs_time": index / 10.0,
        "source_sha256": tool.sha256_text(text), "ja": text, "zh": zh,
        "status": status, "updated_at": "2026-01-01T00:00:00Z",
    }


class ValidateLyricsTests(unittest.TestCase):
    def _root(self, rows, bundle="scrobj_x.unity3d"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        songs = root / "lyrics" / "songs"
        songs.mkdir(parents=True)
        (songs / f"{bundle}.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8"
        )
        return root

    def test_a_well_formed_library_passes_and_is_counted(self):
        root = self._root([
            row("scrobj_x.unity3d", 1, "あ", zh="甲", status="accepted"),
            row("scrobj_x.unity3d", 2, "Up we go", status="untranslated"),
        ])
        counts = tool.validate_lyrics(root)
        self.assertEqual(counts, {"files": 1, "total_rows": 2, "accepted": 1, "pending": 0, "untranslated": 1})

    def test_the_missing_directory_is_only_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tool.validate_lyrics(Path(tmp))["files"], 0)

    def test_bundle_name_must_match_the_file(self):
        root = self._root([row("scrobj_other.unity3d", 1, "あ")])
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(root)

    def test_source_hash_must_match_the_japanese_line(self):
        broken = row("scrobj_x.unity3d", 1, "あ")
        broken["source_sha256"] = "0" * 64
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(self._root([broken]))

    def test_unknown_status_is_refused(self):
        broken = row("scrobj_x.unity3d", 1, "あ", zh="甲", status="reviewed")
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(self._root([broken]))

    def test_untranslated_row_may_not_carry_text(self):
        broken = row("scrobj_x.unity3d", 1, "あ", zh="甲", status="untranslated")
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(self._root([broken]))

    def test_accepted_row_needs_text(self):
        broken = row("scrobj_x.unity3d", 1, "あ", status="accepted")
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(self._root([broken]))

    def test_reserved_engine_delimiters_are_refused(self):
        broken = row("scrobj_x.unity3d", 1, "あ", zh="甲|乙", status="accepted")
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(self._root([broken]))

    def test_protected_tokens_must_survive(self):
        broken = row("scrobj_x.unity3d", 1, "こんにちは {$P$}", zh="你好", status="accepted")
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(self._root([broken]))

    def test_display_brackets_may_be_translated_but_not_dropped(self):
        kept = row("scrobj_x.unity3d", 1, "<いつの間にかこんなに>", zh="<不知不觉间已经如此>",
                   status="accepted")
        self.assertEqual(tool.validate_lyrics(self._root([kept]))["accepted"], 1)
        dropped = row("scrobj_x.unity3d", 1, "<いつの間にかこんなに>", zh="不知不觉间已经如此",
                      status="accepted")
        with self.assertRaises(SystemExit):
            tool.validate_lyrics(self._root([dropped]))

    def test_unicode_line_separator_inside_a_string_is_not_a_line_break(self):
        """U+2028 is legal inside JSON and must not split a row in two."""
        root = self._root([row("scrobj_x.unity3d", 1, "モヤモヤするわ！\u2028", zh="真让人心烦意乱！",
                               status="accepted")])
        self.assertEqual(tool.validate_lyrics(root)["total_rows"], 1)


if __name__ == "__main__":
    unittest.main()
