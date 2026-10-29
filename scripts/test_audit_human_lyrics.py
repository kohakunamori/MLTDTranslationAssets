import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_human_lyrics import audit
from pipelines.core.hashing import sha256_text


class HumanLyricAuditTests(unittest.TestCase):
    def test_exact_joined_alignment_and_conflicts_stay_review_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs, cache = root / "lyrics/songs", root / "cache"
            songs.mkdir(parents=True)
            cache.mkdir()
            source = "この空の向こうへ\n夢を届けに行こう"
            rows = [{"ja": source, "zh": "把梦想带到天空彼端", "status": "accepted",
                     "source_sha256": sha256_text(source), "bundle": "song", "index": 1}]
            (songs / "song.jsonl").write_text(json.dumps(rows[0], ensure_ascii=False), encoding="utf-8")
            page = {"title": "Synthetic fixture", "url": "https://example.invalid/lyrics",
                    "revision_id": 1, "translator_status": "uncredited_or_unknown", "translators": [],
                    "lyrics": [{"original_lines": source.split("\n"),
                                "translated_lines": ["去天空另一侧", "传递梦想"], "matched_line_count": True}]}
            (cache / "a.json").write_text(json.dumps(page), encoding="utf-8")
            report = audit(root, cache)
            comparison, = report["comparisons"]
            self.assertFalse(comparison["gold_eligible"])
            self.assertEqual(comparison["references"][0]["reference_line_count"], 2)
            self.assertEqual(report["summary"]["matched_with_attribution"], 0)
            page["url"] = "https://example.invalid/other"
            page["lyrics"][0]["translated_lines"] = ["我们去太空", "拿到奖牌"]
            (cache / "b.json").write_text(json.dumps(page), encoding="utf-8")
            self.assertEqual(audit(root, cache)["comparisons"][0]["review_reason"], "conflicting_references")
            page["lyrics"][0]["translated_lines"] = ["错位的单行"]
            (cache / "b.json").write_text(json.dumps(page), encoding="utf-8")
            self.assertEqual(audit(root, cache)["reference_stats"]["excluded_unaligned_blocks"], 1)

    def test_missing_cache_is_an_error_not_a_successful_zero_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "no lyric collector"):
                audit(Path(tmp), Path(tmp))


if __name__ == "__main__":
    unittest.main()
