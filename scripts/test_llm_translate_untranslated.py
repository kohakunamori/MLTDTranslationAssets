import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_translate_untranslated as tool


def _row(source, *, item_key="a", zh="", status="untranslated", stage=None, version="1"):
    row = {
        "asset_version": version, "client_version": None,
        "source_client_version": "9.0.200", "bundle": "x.gtx",
        "item_key": item_key, "source_sha256": tool.sha256_text(source),
        "ja": source, "zh": zh, "status": status,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    if stage is not None:
        row["translation_stage"] = stage
    return row


def _write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class LlmDraftWorkflowTests(unittest.TestCase):
    def test_apply_marks_only_untranslated_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = root / "locales" / "story" / "x.jsonl"
            locale.parent.mkdir(parents=True)
            source = "こんにちは {$P$}"
            row = {
                "asset_version": "1", "client_version": None,
                "source_client_version": "9.0.200", "bundle": "x.gtx",
                "item_key": "a", "source_sha256": tool.sha256_text(source),
                "ja": source, "zh": "", "status": "untranslated",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            accepted = dict(row, item_key="b", zh="旧译文", status="accepted")
            locale.write_text(json.dumps(row, ensure_ascii=False) + "\n" + json.dumps(accepted, ensure_ascii=False) + "\n", encoding="utf-8")
            draft = root / "draft.jsonl"
            draft.write_text(json.dumps({"source": source, "source_sha256": tool.sha256_text(source), "translation": "你好 {$P$}"}) + "\n", encoding="utf-8")
            with patch.object(tool, "ROOT", root):
                self.assertEqual(tool.apply(type("Args", (), {"draft": draft})()), 0)
            values = [json.loads(line) for line in locale.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(values[0]["status"], "pending")
            self.assertEqual(values[0]["translation_stage"], "llm_translated")
            self.assertEqual(values[1]["status"], "accepted")


class LlmPublishTests(unittest.TestCase):
    """`publish` admits machine drafts without claiming a human reviewed them."""

    def _fixture(self, root: Path) -> Path:
        locale = root / "locales" / "master" / "official-2-untranslated.jsonl"
        locale.parent.mkdir(parents=True)
        _write(locale, [
            _row("こんにちは {$P$}", item_key="llm", zh="你好 {$P$}", status="pending",
                 stage="llm_translated", version="2"),
            _row("さようなら {$P$}", item_key="human", zh="再见 {$P$}", status="accepted",
                 stage="human_translated", version="2"),
            _row("おはよう {$P$}", item_key="empty", zh="", status="untranslated",
                 stage="untranslated", version="2"),
        ])
        return locale

    def _publish(self, root: Path, **kwargs):
        args = type("Args", (), {"draft": kwargs.get("draft"), "dry_run": kwargs.get("dry_run", False)})()
        with patch.object(tool, "ROOT", root):
            return tool.publish(args)

    def test_publish_admits_only_machine_rows_and_keeps_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            untouched = locale.read_text(encoding="utf-8").splitlines()[1]
            self.assertEqual(self._publish(root), 0)
            rows = _read(locale)
            self.assertEqual([row["status"] for row in rows], ["accepted", "accepted", "untranslated"])
            # Provenance survives: the admitted row is still machine output.
            self.assertEqual(rows[0]["translation_stage"], "llm_translated")
            self.assertEqual(rows[1]["translation_stage"], "human_translated")
            # The rows publish did not touch keep their exact bytes.
            self.assertEqual(locale.read_text(encoding="utf-8").splitlines()[1], untouched)

    def test_publish_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            self._publish(root)
            first = locale.read_text(encoding="utf-8")
            self._publish(root)
            self.assertEqual(locale.read_text(encoding="utf-8"), first)

    def test_publish_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            before = locale.read_text(encoding="utf-8")
            self._publish(root, dry_run=True)
            self.assertEqual(locale.read_text(encoding="utf-8"), before)

    def test_draft_scope_limits_the_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            other = _row("こんばんは {$P$}", item_key="other", zh="晚上好 {$P$}",
                         status="pending", stage="llm_translated", version="2")
            _write(locale, _read(locale) + [other])
            draft = root / "draft.jsonl"
            _write(draft, [{"source": "こんばんは {$P$}",
                            "source_sha256": tool.sha256_text("こんばんは {$P$}"),
                            "translation": "晚上好 {$P$}"}])
            self._publish(root, draft=draft)
            rows = {row["item_key"]: row for row in _read(locale)}
            self.assertEqual(rows["other"]["status"], "accepted")
            self.assertEqual(rows["llm"]["status"], "pending")

    def test_publish_refuses_a_row_whose_source_hash_drifted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            rows = _read(locale)
            rows[0]["source_sha256"] = "0" * 64
            _write(locale, rows)
            with self.assertRaises(SystemExit):
                self._publish(root)


if __name__ == "__main__":
    unittest.main()
