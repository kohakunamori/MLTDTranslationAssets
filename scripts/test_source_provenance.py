"""Where a release's client version comes from now: the rows, not a field.

The field used to live in `manifests/asset-version.json` as a hand-maintained
"9.0.200", which made it look like a version to keep in step with the game.  It
is not: the axes are independent, the release identity is `asset_version` alone,
and the client version is provenance for the text the release carries -- which
every locale row already records.  On 2026-10-10 client 9.0.300 shipped while all
395,673 rows still said 9.0.200, and the field looked stale when it was right.

These tests pin the replacement: the library answers, a leftover field is only a
fallback for an empty library, and having neither is a named error rather than a
silently invented value.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_generated_release as builder  # noqa: E402
import source_provenance  # noqa: E402


def rows(*values: str) -> list[dict]:
    return [{"asset_version": "1077741", "client_version": None,
             "source_client_version": value} for value in values]


class ProvenanceVoteTests(unittest.TestCase):
    def test_the_majority_wins(self):
        value, counts = source_provenance.from_rows(rows(*(["9.0.200"] * 3 + ["9.0.100"] * 1)))
        self.assertEqual(value, "9.0.200")
        self.assertEqual(counts["9.0.100"], 1)

    def test_a_tie_goes_to_the_newer_capture(self):
        value, _ = source_provenance.from_rows(rows("9.0.100", "9.0.200"))
        self.assertEqual(value, "9.0.200")

    def test_missing_and_empty_values_do_not_vote(self):
        value, counts = source_provenance.from_rows(
            [{"source_client_version": None}, {"source_client_version": ""}, {}])
        self.assertIsNone(value)
        self.assertEqual(sum(counts.values()), 0)

    def test_the_library_is_read_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locales" / "story").mkdir(parents=True)
            (root / "locales" / "story" / "a.jsonl").write_text(
                json.dumps(rows("9.0.200")[0]) + "\n" + json.dumps(rows("9.0.200")[0]) + "\n",
                encoding="utf-8")
            (root / "locales" / "card").mkdir()
            (root / "locales" / "card" / "b.jsonl").write_text(
                json.dumps(rows("9.0.100")[0]) + "\n", encoding="utf-8")
            value, counts = source_provenance.from_library(root)
            self.assertEqual(value, "9.0.200")
            self.assertEqual(dict(counts), {"9.0.200": 2, "9.0.100": 1})


class ReleaseBuilderProvenanceTests(unittest.TestCase):
    def test_the_version_manifest_no_longer_needs_a_client_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset-version.json"
            path.write_text(json.dumps({
                "asset_version": 1077741,
                "asset_root": "https://td-assets.bn765.com/{version}/production/2018/Android",
                "index_name": "f34f019c5ddf91173e6755cb76545da9c27b74c9.data",
            }), encoding="utf-8")
            version = builder.load_version_manifest(path)
            self.assertEqual(version["asset_version"], "1077741")
            self.assertEqual(version["legacy_client_version"], "")

    def test_the_rows_answer_and_a_leftover_field_does_not_override_them(self):
        version = {"legacy_client_version": "9.0.100"}
        self.assertEqual(builder.release_provenance(version, rows("9.0.200")), "9.0.200")

    def test_a_leftover_field_still_serves_an_empty_library(self):
        version = {"legacy_client_version": "9.0.200"}
        self.assertEqual(builder.release_provenance(version, []), "9.0.200")

    def test_no_provenance_anywhere_is_a_named_error(self):
        with self.assertRaises(ValueError) as caught:
            builder.release_provenance({"legacy_client_version": ""}, [])
        self.assertIn("source_client_version", str(caught.exception))
        self.assertIn("client_version", str(caught.exception))

    def test_the_builder_does_not_read_the_field_it_deleted(self):
        source = Path(builder.__file__).read_text(encoding="utf-8")
        self.assertNotIn('version["client_version"]', source)
        self.assertIn("release_provenance(version", source)


if __name__ == "__main__":
    unittest.main()
