import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_translate_untranslated as tool


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


if __name__ == "__main__":
    unittest.main()
