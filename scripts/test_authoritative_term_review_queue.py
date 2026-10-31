import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipelines.core.hashing import sha256_text  # noqa: E402
from scripts import build_authoritative_term_review_queue as queue  # noqa: E402


class AuthoritativeTermQueueTests(unittest.TestCase):
    def _root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "locales" / "master").mkdir(parents=True)
        (root / "lyrics" / "songs").mkdir(parents=True)
        return root

    def _write_locale(self, root, rows):
        (root / "locales" / "master" / "sample.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

    def _row(self, source, translation, **extra):
        row = {
            "ja": source,
            "zh": translation,
            "status": "accepted",
            "source_sha256": sha256_text(source),
        }
        row.update(extra)
        return row

    def test_unmarked_rows_are_kept_separate_from_machine_rows(self):
        root = self._root()
        rows = [
            {"item_key": "name", "ja": "茜ちゃんです", "zh": "茜酱", "status": "accepted",
             "source_sha256": sha256_text("茜ちゃんです")},
            {"item_key": "machine", "ja": "茜ちゃんです", "zh": "茜酱", "status": "accepted",
             "translation_stage": "llm_translated", "source_sha256": sha256_text("茜ちゃんです")},
        ]
        self._write_locale(root, rows)
        report = queue.build_queue(root, {"茜ちゃん": "小茜"})
        self.assertEqual(report["summary"]["rows"], 2)
        self.assertEqual({row["translation_stage"] for row in report["rows"]}, {None, "llm_translated"})
        machine = queue.build_queue(root, {"茜ちゃん": "小茜"}, "llm_translated")
        self.assertEqual(machine["summary"]["rows"], 1)
        unmarked = queue.build_queue(root, {"茜ちゃん": "小茜"}, "unmarked")
        self.assertEqual(unmarked["summary"]["rows"], 1)

    def test_output_inside_checkout_is_refused(self):
        with self.assertRaisesRegex(ValueError, "outside the source checkout"):
            queue.outside_repo(queue.ROOT / "review.json")

    def test_lyrics_rows_are_scanned_and_source_identity_is_checked(self):
        root = self._root()
        source = "茜ちゃん"
        row = {"index": 4, "ja": source, "zh": "茜酱", "status": "accepted",
               "source_sha256": sha256_text(source), "translation_stage": "llm_translated"}
        (root / "lyrics" / "songs" / "song.jsonl").write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        report = queue.build_queue(root, {source: "小茜"})
        self.assertEqual(report["rows"][0]["kind"], "lyrics")
        self.assertEqual(report["rows"][0]["index"], 4)

    def test_mismatched_source_identity_still_raises(self):
        root = self._root()
        self._write_locale(root, [{
            "item_key": "bad", "ja": "茜ちゃん", "zh": "茜酱", "status": "accepted",
            "source_sha256": sha256_text("違う文章"),
        }])
        with self.assertRaisesRegex(ValueError, "source identity mismatch"):
            queue.build_queue(root, {"茜ちゃん": "小茜"})

    def test_character_name_conflicts_are_high_priority(self):
        root = self._root()
        source = "如月千早"
        self._write_locale(root, [self._row(source, "星井美希", item_key="name")])
        report = queue.build_queue(
            root,
            {source: source},
            name_terms={"如月千早": "如月千早", "星井美希": "星井美希"},
        )
        high_rows = [row for row in report["rows"] if row["priority"] == "high"]
        self.assertEqual(len(high_rows), 1)
        self.assertIn("星井美希", high_rows[0]["character_name_conflicts"])
        self.assertEqual(report["summary"]["high_priority_name_conflicts"], len(high_rows))
        self.assertEqual(report["rows"][0]["priority"], "high")

    def test_name_conflict_without_term_gap_still_emits_a_row(self):
        root = self._root()
        source = "如月千早"
        translation = "如月千早と星井美希"
        self._write_locale(root, [self._row(source, translation, item_key="name")])
        report = queue.build_queue(
            root,
            {source: source},
            name_terms={"如月千早": "如月千早", "星井美希": "星井美希"},
        )
        self.assertEqual(len(report["rows"]), 1)
        item = report["rows"][0]
        self.assertEqual(item["review_reason"], "character_name_conflict")
        self.assertEqual(item["priority"], "high")
        self.assertEqual(item["source"], source)
        self.assertEqual(item["translation"], translation)
        self.assertIn("星井美希", item["character_name_conflicts"])
        self.assertEqual(report["summary"]["high_priority_name_conflicts"], 1)
        self.assertEqual(report["summary"]["issue_records"], 1)

    def test_summary_counts_agree_with_emitted_rows(self):
        root = self._root()
        self._write_locale(root, [
            self._row("如月千早", "星井美希", item_key="a"),
            self._row("星井美希", "如月千早", item_key="b"),
        ])
        report = queue.build_queue(
            root,
            {"如月千早": "如月千早"},
            name_terms={"如月千早": "如月千早", "星井美希": "星井美希"},
        )
        summary = report["summary"]
        self.assertEqual(summary["issue_records"], len(report["rows"]))
        self.assertEqual(summary["rows"], len(report["rows"]))
        self.assertEqual(
            summary["high_priority_name_conflicts"],
            sum(1 for row in report["rows"] if row["priority"] == "high"),
        )
        self.assertEqual(summary["unique_catalogue_rows"], 2)

    def test_two_terms_on_one_catalogue_row_are_two_issue_records(self):
        root = self._root()
        self._write_locale(root, [
            self._row("茜ちゃんと如月千早", "无人", item_key="row-1"),
        ])
        report = queue.build_queue(root, {"茜ちゃん": "小茜", "如月千早": "如月千早"})
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual(report["summary"]["issue_records"], 2)
        self.assertEqual(report["summary"]["unique_catalogue_rows"], 1)
        self.assertEqual(
            {row["source_term"] for row in report["rows"]}, {"茜ちゃん", "如月千早"}
        )

    def test_katakana_substrings_do_not_create_name_conflicts(self):
        root = self._root()
        self._write_locale(root, [
            self._row("ジュリアン", "星井美希", item_key="julian"),
            self._row("ウロコ", "星井美希", item_key="uroko"),
            self._row("クロコダイル", "星井美希", item_key="crocodile"),
        ])
        report = queue.build_queue(
            root,
            {"ジュリア": "茱莉亚", "ロコ": "ROCO"},
            name_terms={"ジュリア": "茱莉亚", "ロコ": "ROCO", "星井美希": "星井美希"},
        )
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["summary"]["high_priority_name_conflicts"], 0)

    def test_katakana_substring_does_not_pollute_expected_names(self):
        root = self._root()
        self._write_locale(root, [
            self._row("星井美希とウロコ", "星井美希", item_key="mixed"),
        ])
        report = queue.build_queue(
            root,
            {"ロコ": "ROCO"},
            name_terms={"ロコ": "ROCO", "星井美希": "星井美希"},
        )
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["summary"]["issue_records"], 0)

    def test_standalone_katakana_name_still_flags_a_conflict(self):
        root = self._root()
        self._write_locale(root, [self._row("ジュリア", "星井美希", item_key="julia")])
        report = queue.build_queue(
            root, {}, name_terms={"ジュリア": "茱莉亚", "星井美希": "星井美希"}
        )
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["priority"], "high")
        self.assertIn("星井美希", report["rows"][0]["character_name_conflicts"])

    def test_kanji_name_inside_ordinary_prose_still_matches(self):
        root = self._root()
        self._write_locale(root, [
            self._row("如月千早誕生日", "星井美希生日", item_key="prose"),
        ])
        report = queue.build_queue(
            root, {}, name_terms={"如月千早": "如月千早", "星井美希": "星井美希"}
        )
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["priority"], "high")
        self.assertIn("星井美希", report["rows"][0]["character_name_conflicts"])
        # A kanji name sitting inside prose is a real match, so the correct
        # translation raises nothing at all.
        correct = self._root()
        self._write_locale(correct, [
            self._row("如月千早誕生日", "如月千早的生日", item_key="prose-ok"),
        ])
        ok = queue.build_queue(
            correct,
            {"如月千早": "如月千早"},
            name_terms={"如月千早": "如月千早", "星井美希": "星井美希"},
        )
        self.assertEqual(ok["rows"], [])

    def test_newline_or_space_split_name_is_not_a_conflict(self):
        name_terms = {"佐竹美奈子": "佐竹美奈子", "星井美希": "星井美希"}
        for source, translation in (
            ("佐竹\n美奈子", "佐竹美奈子"),
            ("佐竹 美奈子", "佐竹美奈子"),
            ("佐竹\n  美奈子", "佐竹美奈子"),
            ("佐竹美奈子", "佐竹美奈子"),
            ("佐竹美奈子", "佐竹\n美奈子"),
            ("佐竹美奈子", "佐竹 美奈子"),
        ):
            with self.subTest(source=source, translation=translation):
                root = self._root()
                self._write_locale(root, [
                    self._row(source, translation, item_key="split"),
                ])
                report = queue.build_queue(
                    root, {"佐竹美奈子": "佐竹美奈子"}, name_terms=name_terms
                )
                self.assertEqual(report["rows"], [])
                self.assertEqual(report["summary"]["high_priority_name_conflicts"], 0)

    def test_high_priority_rows_sort_first(self):
        root = self._root()
        self._write_locale(root, [
            self._row("如月千早", "星井美希", item_key="z-unmarked"),
            self._row("如月千早", "星井美希", item_key="z-machine",
                      translation_stage="llm_translated"),
            self._row("茜ちゃん", "阿茜", item_key="a-unmarked"),
            self._row("茜ちゃん", "阿茜", item_key="a-machine",
                      translation_stage="llm_translated"),
        ])
        report = queue.build_queue(
            root,
            {"茜ちゃん": "小茜"},
            name_terms={"如月千早": "如月千早", "星井美希": "星井美希"},
        )
        self.assertEqual(
            [(row["priority"], row["translation_stage"]) for row in report["rows"]],
            [("high", "llm_translated"), ("high", None),
             ("normal", "llm_translated"), ("normal", None)],
        )
        self.assertEqual(
            [row["item_key"] for row in report["rows"]],
            ["z-machine", "z-unmarked", "a-machine", "a-unmarked"],
        )


if __name__ == "__main__":
    unittest.main()
