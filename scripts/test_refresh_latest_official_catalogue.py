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

    def test_version_bump_alone_does_not_add_rows(self):
        """The 2026-10-01 regression: a source already present in ANY asset
        version is not new when the official version advances."""
        existing = {tool.source_identity("x.gtx", "a", "sha1")}
        catalogue = [
            {"bundle": "x.gtx", "key": "a", "source": "文A", "source_sha256": "sha1"},
            {"bundle": "x.gtx", "key": "b", "source": "文B", "source_sha256": "sha2"},
        ]
        additions = tool.collect_new_rows(catalogue, existing, "3", "9.0.200", "now")
        self.assertEqual([row["item_key"] for row in additions], ["b"])
        self.assertEqual(additions[0]["asset_version"], "3")

    def test_duplicate_rows_inside_catalogue_are_kept_once(self):
        catalogue = [
            {"bundle": "x.gtx", "key": "a", "source": "文A", "source_sha256": "sha1"},
            {"bundle": "x.gtx", "key": "a", "source": "文A", "source_sha256": "sha1"},
        ]
        additions = tool.collect_new_rows(catalogue, set(), "3", "9.0.200", "now")
        self.assertEqual(len(additions), 1)

    def test_large_append_is_refused_by_default(self):
        catalogue = [
            {"bundle": "x.gtx", "key": f"k{i}", "source": f"文{i}", "source_sha256": f"s{i}"}
            for i in range(10)
        ]
        additions = tool.collect_new_rows(catalogue, set(), "3", "9.0.200", "now")
        with self.assertRaises(SystemExit):
            tool.enforce_new_row_cap(additions, 5)
        tool.enforce_new_row_cap(additions, 10)          # exactly at the cap: allowed
        tool.enforce_new_row_cap(additions, 0)           # 0 disables the cap


if __name__ == "__main__":
    unittest.main()
