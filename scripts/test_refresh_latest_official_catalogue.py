import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import refresh_latest_official_catalogue as tool


class RefreshCatalogueTests(unittest.TestCase):
    def test_existing_source_identity_is_not_added_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = root / "locales" / "master" / "old.jsonl"
            locale.parent.mkdir(parents=True)
            source = "新しい文"
            row = {"asset_version": "2", "bundle": "x.gtx", "item_key": "a", "source_sha256": "x", "ja": source}
            locale.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            with patch.object(tool, "ROOT", root):
                values = list(tool.locale_rows())
            self.assertEqual(len(values), 1)
            self.assertEqual(values[0][1]["bundle"], "x.gtx")


if __name__ == "__main__":
    unittest.main()
