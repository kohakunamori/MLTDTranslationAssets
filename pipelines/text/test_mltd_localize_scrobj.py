import json
import sys
import tempfile
import unittest
from pathlib import Path

# ``pipelines/text`` is a package, so pytest puts ``pipelines/`` on sys.path and
# the sibling module would not resolve by bare name.  Insert the directory the
# way mltd_localization_pipeline.py does, so the test runs from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mltd_localize_scrobj import (  # noqa: E402
    LyricBundleError,
    LyricSlot,
    is_english_bypass,
    is_localizable_line,
    merge_slots,
    read_slots,
    read_song,
    rebuild_aggregate,
    slot_rows,
    song_names,
    write_song,
)


def slot(index: int, text: str, tick: int = 9600) -> LyricSlot:
    return LyricSlot(index=index, tick=tick, abs_time=index / 10, text=text)


class RulesAreReachableThroughThisModuleTests(unittest.TestCase):
    """The rules live in ``mltd_lyric_rules`` (dependency free) and are re-exported.

    ``scripts/llm_translate_untranslated.py`` must not pull in UnityPy just to
    ask whether a lyric line is English, so the definitions moved;
    ``pipelines/text/test_mltd_lyric_rules.py`` pins their behaviour without
    UnityPy.  This only pins the re-export so existing callers keep working.
    """

    def test_reexported_rules_are_the_shared_implementation(self):
        from mltd_lyric_rules import is_english_bypass as shared_bypass
        from mltd_lyric_rules import is_localizable_line as shared_localizable

        self.assertIs(is_english_bypass, shared_bypass)
        self.assertIs(is_localizable_line, shared_localizable)


class SlotRowTests(unittest.TestCase):
    def test_rows_match_the_existing_library_shape(self):
        rows = slot_rows("scrobj_x.unity3d", [slot(41, "一旦愛して♡")], "2026-10-10T00:00:00+00:00")
        self.assertEqual(
            list(rows[0]),
            ["bundle", "index", "tick", "abs_time", "source_sha256", "ja", "zh", "status", "updated_at"],
        )
        self.assertEqual(rows[0]["bundle"], "scrobj_x.unity3d")
        self.assertEqual(rows[0]["status"], "untranslated")
        self.assertEqual(rows[0]["zh"], "")
        self.assertEqual(len(rows[0]["source_sha256"]), 64)


class MergeTests(unittest.TestCase):
    """Re-extraction must never silently drop a translation that still applies."""

    def _existing(self):
        return [
            self._row(10, "あ", "甲"),
            self._row(20, "い", "乙"),
        ]

    def _row(self, index, text, zh, status="accepted", updated_at="old"):
        import hashlib
        return {
            "bundle": "scrobj_x.unity3d", "index": index, "tick": 1, "abs_time": 1.0,
            "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "ja": text, "zh": zh, "status": status, "updated_at": updated_at,
        }

    def _slot(self, index, text):
        return LyricSlot(index=index, tick=99, abs_time=9.9, text=text)

    def test_unchanged_lines_keep_their_translation(self):
        existing = self._existing()
        rows, stats = merge_slots("scrobj_x.unity3d", existing,
                                 [self._slot(10, "あ"), self._slot(20, "い")], "now")
        self.assertEqual(stats, {"preserved": 2, "reindexed": 0, "retranslated": 0, "dropped_accepted": 0})
        self.assertEqual([row["zh"] for row in rows], ["甲", "乙"])
        self.assertEqual([row["tick"] for row in rows], [99, 99])

    def test_a_moved_line_is_reindexed_not_retranslated(self):
        rows, stats = merge_slots("scrobj_x.unity3d", self._existing(),
                                  [self._slot(11, "あ"), self._slot(20, "い")], "now")
        self.assertEqual(stats["reindexed"], 1)
        self.assertEqual(rows[0]["index"], 11)
        self.assertEqual(rows[0]["zh"], "甲")

    def test_changed_source_text_becomes_untranslated(self):
        rows, stats = merge_slots("scrobj_x.unity3d", self._existing(),
                                  [self._slot(10, "ああ"), self._slot(20, "い")], "now")
        self.assertEqual(stats["retranslated"], 1)
        self.assertEqual(stats["dropped_accepted"], 1)
        self.assertEqual(rows[0]["zh"], "")
        self.assertEqual(rows[0]["status"], "untranslated")

    def test_inserted_line_is_added_without_touching_others(self):
        rows, stats = merge_slots("scrobj_x.unity3d", self._existing(),
                                  [self._slot(5, "う"), self._slot(10, "あ"), self._slot(20, "い")], "now")
        self.assertEqual(len(rows), 3)
        self.assertEqual(stats["retranslated"], 1)
        self.assertEqual(rows[0]["ja"], "う")

    def test_a_row_whose_hash_disagrees_with_its_text_aborts_the_merge(self):
        broken = self._existing()
        broken[0]["source_sha256"] = "0" * 64
        with self.assertRaises(LyricBundleError):
            merge_slots("scrobj_x.unity3d", broken, [self._slot(10, "あ")], "now")


class AggregateTests(unittest.TestCase):
    def _root(self, tmp):
        root = Path(tmp)
        (root / "songs").mkdir(parents=True)
        return root

    def test_counts_are_derived_from_the_song_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            write_song(root, "scrobj_a.unity3d", [
                {"bundle": "scrobj_a.unity3d", "index": 1, "ja": "あ", "zh": "甲", "status": "accepted"},
                {"bundle": "scrobj_a.unity3d", "index": 2, "ja": "Up we go", "zh": "", "status": "untranslated"},
            ])
            write_song(root, "scrobj_b.unity3d", [
                {"bundle": "scrobj_b.unity3d", "index": 1, "ja": "い", "zh": "", "status": "untranslated"},
            ])
            manifest = rebuild_aggregate(root)
            self.assertEqual(manifest["counts"], {
                "total_songs": 2, "total_slots": 3, "translated_slots": 1, "english_bypass_slots": 1,
            })
            self.assertEqual([song["bundle"] for song in manifest["songs"]],
                             ["scrobj_a.unity3d", "scrobj_b.unity3d"])
            self.assertEqual(song_names(root), ["scrobj_a.unity3d", "scrobj_b.unity3d"])

    def test_aggregate_is_deterministic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            write_song(root, "scrobj_a.unity3d", [
                {"bundle": "scrobj_a.unity3d", "index": 1, "ja": "あ", "zh": "甲", "status": "accepted"},
            ])
            rebuild_aggregate(root)
            first = (root / "all_lyrics.jsonl").read_text(encoding="utf-8")
            second = (root / "lyrics_manifest.json").read_text(encoding="utf-8")
            rebuild_aggregate(root)
            self.assertEqual((root / "all_lyrics.jsonl").read_text(encoding="utf-8"), first)
            self.assertEqual((root / "lyrics_manifest.json").read_text(encoding="utf-8"), second)
            self.assertEqual(len(first.strip().splitlines()), 1)
            self.assertEqual(read_song(root, "scrobj_a.unity3d")[0]["zh"], "甲")

    def test_manifest_is_readable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            rebuild_aggregate(root)
            document = json.loads((root / "lyrics_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(document["counts"]["total_songs"], 0)

    def test_unicode_line_separator_does_not_split_a_row(self):
        """U+2028 is legal inside JSON; str.splitlines() would break the row.

        ``lyrics/songs/scrobj_gf0000.unity3d.jsonl`` really ships such a row.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            write_song(root, "scrobj_a.unity3d", [
                {"bundle": "scrobj_a.unity3d", "index": 1, "ja": "モヤモヤするわ！\u2028",
                 "zh": "真让人心烦意乱！", "status": "accepted"},
                {"bundle": "scrobj_a.unity3d", "index": 2, "ja": "あ", "zh": "", "status": "untranslated"},
            ])
            rows = read_song(root, "scrobj_a.unity3d")
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]["ja"].endswith("\u2028"))
            manifest = rebuild_aggregate(root)
            self.assertEqual(manifest["counts"]["total_slots"], 2)
            self.assertEqual(manifest["counts"]["translated_slots"], 1)
            self.assertEqual(len((root / "all_lyrics.jsonl").read_text(encoding="utf-8").split("\n")) - 1, 2)


class ReaderContractTests(unittest.TestCase):
    def test_missing_bundle_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LyricBundleError):
                read_slots(Path(tmp) / "absent.unity3d")

    def test_unreadable_file_is_a_clear_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "garbage.unity3d"
            path.write_bytes(b"not a unity bundle at all")
            with self.assertRaises(LyricBundleError):
                read_slots(path)


if __name__ == "__main__":
    unittest.main()
