"""Check committed name evidence in the CI job that checks out locales/.

Keep this module stdlib-only so the validate job needs no test dependencies.
Prompt behavior belongs in test_given_name_hints.py, whose offline CI checkout
deliberately omits the locale trees.
"""

import json
import unittest
from pathlib import Path

from pipelines.text.mltd_given_name_hints import load_given_name_hints

ROOT = Path(__file__).resolve().parents[1]
HINTS_PATH = ROOT / "glossary" / "given-name-hints.json"
EXPECTED = {
    "このみ": ("木实", 39, "馬場このみ", "马场木实"),
    "やよい": ("弥生", 5, "高槻やよい", "高槻弥生"),
    "あずさ": ("梓", 10, "三浦あずさ", "三浦梓"),
    "まつり": ("茉莉", 21, "徳川まつり", "德川茉莉"),
}


class GivenNameHintAuthorityTests(unittest.TestCase):
    def test_authority_entries_match_glossary_and_roster(self):
        document = load_given_name_hints(HINTS_PATH)
        actual = {
            entry.source_form: (
                entry.target,
                entry.idol_id,
                entry.canonical_source,
                entry.canonical_target,
            )
            for entry in document.entries
        }
        self.assertEqual(actual, EXPECTED)
        self.assertEqual(document.entries[0].review_basis, "project_evidence+agent_review_not_human_reviewed")

        glossary = json.loads(
            (ROOT / "glossary" / "authoritative-terms.json").read_text(
                encoding="utf-8-sig"
            )
        )["entries"]
        roster = json.loads(
            (ROOT / "glossary" / "idols.json").read_text(encoding="utf-8-sig")
        )["idols"]
        by_id = {row["idol_id"]: row for row in roster}
        for entry in document.entries:
            self.assertEqual(
                glossary[entry.canonical_source]["target"],
                entry.canonical_target,
            )
            self.assertTrue(entry.canonical_source.endswith(entry.source_form))
            self.assertTrue(entry.canonical_target.endswith(entry.target))
            self.assertEqual(by_id[entry.idol_id]["name_jp"], entry.canonical_source)
            self.assertEqual(by_id[entry.idol_id]["name_zh"], entry.canonical_target)

        typed_keys = {entry.typed_record_key for entry in document.entries}
        typed_rows = {}
        with (ROOT / "locales" / "master" / "MD_jp.gtx.jsonl").open(
            encoding="utf-8-sig"
        ) as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("item_key") in typed_keys:
                    typed_rows[row["item_key"]] = (row.get("ja"), row.get("zh"))
        for entry in document.entries:
            self.assertEqual(
                typed_rows[entry.typed_record_key],
                (entry.source_form, entry.target),
            )


if __name__ == "__main__":
    unittest.main()
