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


class SweepTests(unittest.TestCase):
    """Rows merged before the diff helper existed still need promoting."""

    def make_tree(self, root: Path) -> Path:
        path = root / "locales" / "master" / "CM_jp.gtx.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            self.row("reviewed", status="pending", zh="人工译文"),
            self.row("draft", status="pending", zh="机器译文", stage="llm_translated"),
            self.row("empty", status="untranslated", zh=""),
            self.row("done", status="accepted", zh="已发布"),
        ]
        path.write_text(
            "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in rows),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def row(key: str, *, status: str, zh: str, stage: str | None = None, sha: str | None = None):
        source = f"原文-{key}"
        row = {
            "asset_version": "1077500",
            "client_version": None,
            "source_client_version": "9.0.200",
            "bundle": "CM_jp.gtx",
            "item_key": key,
            "source_sha256": sha or hashlib.sha256(source.encode()).hexdigest(),
            "ja": source,
            "zh": zh,
            "status": status,
            "updated_at": "2026-09-27T00:00:00Z",
        }
        if stage is not None:
            row["translation_stage"] = stage
        return row

    def test_sweep_promotes_reviewed_rows_and_leaves_machine_drafts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = self.make_tree(root)
            result = promote.sweep(root)
            self.assertEqual(result["promoted"], 1)
            self.assertEqual(result["machine_drafts_left_pending"], 1)
            self.assertEqual(result["scanned_files"], 1)
            rows = {row["item_key"]: row for row in
                    (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())}
            self.assertEqual(rows["reviewed"]["status"], "accepted")
            self.assertEqual(rows["draft"]["status"], "pending")
            self.assertEqual(rows["draft"]["translation_stage"], "llm_translated")
            self.assertEqual(rows["empty"]["status"], "untranslated")
            self.assertEqual(rows["done"]["status"], "accepted")

    def test_sweep_refuses_a_row_whose_source_hash_moved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = self.make_tree(root)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[0]["source_sha256"] = "1" * 64
            path.write_text(
                "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in rows),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                promote.sweep(root)
            # Nothing was rewritten before the refusal.
            self.assertIn('"status": "pending"', path.read_text(encoding="utf-8"))

    def test_sweep_leaves_rows_that_would_fail_the_repo_gate_pending(self):
        """A translation that drops a protected token must not be published."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "locales" / "story" / "story.jsonl"
            path.parent.mkdir(parents=True)
            rows = [
                self.row("ok", status="pending", zh="{$P$}你好"),
                self.row("token_lost", status="pending", zh="你好"),
            ]
            for row in rows:
                row["ja"] = "{$P$}こんにちは"
                row["source_sha256"] = hashlib.sha256(
                    row["ja"].encode("utf-8")
                ).hexdigest()
            path.write_text(
                "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in rows),
                encoding="utf-8",
            )
            result = promote.sweep(root)
            self.assertEqual(result["promoted"], 1)
            self.assertEqual(result["protected_token_mismatch_left_pending"], 1)
            stored = {row["item_key"]: row for row in
                      (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())}
            self.assertEqual(stored["ok"]["status"], "accepted")
            self.assertEqual(stored["token_lost"]["status"], "pending")

    def test_sweep_dry_run_reports_without_editing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = self.make_tree(root)
            original = path.read_text(encoding="utf-8")
            result = promote.sweep(root, dry_run=True)
            self.assertEqual(result["promoted"], 1)
            self.assertTrue(result["dry_run"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_allow_machine_drafts_promotes_them_too(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = self.make_tree(root)
            result = promote.sweep(root, allow_machine=True)
            self.assertEqual(result["promoted"], 2)
            self.assertEqual(result["machine_drafts_left_pending"], 0)
            rows = {row["item_key"]: row for row in
                    (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())}
            self.assertEqual(rows["draft"]["status"], "accepted")
            self.assertEqual(rows["draft"]["translation_stage"], "human_translated")


if __name__ == "__main__":
    unittest.main()
