import json
from pathlib import Path
import tempfile
import unittest

from pipelines.core.hashing import sha256_text
from scripts import build_authoritative_term_review_queue as queue


class AuthoritativeTermQueueTests(unittest.TestCase):
    def _root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "locales" / "master").mkdir(parents=True)
        (root / "lyrics" / "songs").mkdir(parents=True)
        return root

    def test_unmarked_rows_are_kept_separate_from_machine_rows(self):
        root = self._root()
        rows = [
            {"item_key": "name", "ja": "茜ちゃんです", "zh": "茜酱", "status": "accepted",
             "source_sha256": sha256_text("茜ちゃんです")},
            {"item_key": "machine", "ja": "茜ちゃんです", "zh": "茜酱", "status": "accepted",
             "translation_stage": "llm_translated", "source_sha256": sha256_text("茜ちゃんです")},
        ]
        (root / "locales" / "master" / "sample.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
        )
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


if __name__ == "__main__":
    unittest.main()
