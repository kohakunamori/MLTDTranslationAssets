#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promote_merged_locales as promote


class PromoteMergedLocalesTests(unittest.TestCase):
    def make_repo(self, root: Path) -> tuple[str, str, Path]:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        path = root / "locales" / "story" / "story.jsonl"
        path.parent.mkdir(parents=True)
        source = "日文原文"
        row = {
            "asset_version": "1077500",
            "client_version": None,
            "source_client_version": "9.0.200",
            "bundle": "story.gtx",
            "item_key": "k",
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "ja": source,
            "zh": "",
            "status": "untranslated",
            "translation_stage": "untranslated",
            "updated_at": "2026-09-30T00:00:00Z",
        }
        untouched = dict(row, item_key="untouched", zh="已有译文", status="pending")
        path.write_text(
            "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in (row, untouched)),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        row["zh"] = "中文译文"
        path.write_text(
            "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in (row, untouched)),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "translation"], cwd=root, check=True)
        after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        return before, after, path

    def test_only_changed_nonempty_rows_are_promoted(self):
        with tempfile.TemporaryDirectory() as raw:
            before, after, path = self.make_repo(Path(raw))
            result = promote.promote(Path(raw), before, after)
            self.assertEqual(result["promoted"], 1)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["status"], "accepted")
            self.assertEqual(rows[0]["translation_stage"], "human_translated")
            self.assertEqual(rows[1]["status"], "pending")

    def test_dry_run_does_not_edit(self):
        with tempfile.TemporaryDirectory() as raw:
            before, after, path = self.make_repo(Path(raw))
            original = path.read_text(encoding="utf-8")
            result = promote.promote(Path(raw), before, after, dry_run=True)
            self.assertEqual(result["promoted"], 1)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_source_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            before, after, path = self.make_repo(Path(raw))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            row = rows[0]
            row["source_sha256"] = "0" * 64
            path.write_text(
                "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in (row, rows[1])),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                promote.promote(Path(raw), before, after)


if __name__ == "__main__":
    unittest.main()
