import hashlib
import json
import msgpack
import subprocess
import sys
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

    def test_a_typo_named_official_package_cannot_duplicate_rows(self):
        """The official index really ships ``pecial_108_fc_01_jp.gtx`` (no ``s``).

        Extraction reports the bundle's *internal* name, which is spelled
        correctly, so all 38 of its rows collide with the rows already carried for
        ``special_108_fc_01_jp.gtx`` and are dropped instead of being appended as a
        second, conflicting copy.  Measured on asset 1077720: 1,570 extracted
        candidates - 38 duplicates = the 1,532 rows that were appended.
        """
        existing = {tool.source_identity("special_108_fc_01_jp.gtx", "special_108_fc_01_title", "sha1")}
        catalogue = [
            # what read_snapshot_bundle() reports for the typo'd logical name
            {"bundle": "special_108_fc_01_jp.gtx", "key": "special_108_fc_01_title",
             "source": "しゅわしゅわに弾けたら", "source_sha256": "sha1"},
            {"bundle": "special_108_fc_01_jp.gtx", "key": "special_108_fc_01_synopsis",
             "source": "野外ライブ本番前", "source_sha256": "sha2"},
        ]
        additions = tool.collect_new_rows(catalogue, existing, "3", "9.0.200", "now")
        self.assertEqual([row["item_key"] for row in additions], ["special_108_fc_01_synopsis"])

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


class SelectBundlesTests(unittest.TestCase):
    """Selection must look outward: a name nobody has seen still matches."""

    def setUp(self):
        self.registry = {
            "schema_version": 1,
            "families": [
                {"id": "gtx_text", "pipeline": "gtx_text", "match": {"suffix": "_jp.gtx.unity3d"}},
                {"id": "song_lyrics", "pipeline": "song_lyrics", "match": {"prefix": "scrobj_"}},
            ],
            "exclude": [],
            "reviewed_unclassified": [],
        }

    def test_unseen_text_bundle_is_selected_and_reported(self):
        index = {
            "event_0448_story_01_jp.gtx.unity3d": bundle("aaa.unity3d", 7000),
            "season_a_2023_001har_100_jp.gtx.unity3d": bundle("bbb.unity3d", 1100),
        }
        selected, discovered = tool.select_bundles(
            index, {"season_a_2023_001har_100_jp.gtx.unity3d".casefold()}, self.registry
        )
        self.assertEqual(sorted(selected), ["event_0448_story_01_jp.gtx.unity3d",
                                            "season_a_2023_001har_100_jp.gtx.unity3d"])
        self.assertEqual(discovered, ["event_0448_story_01_jp.gtx.unity3d"])

    def test_lyric_bundle_never_reaches_the_gtx_extractor(self):
        index = {"scrobj_ittana.unity3d": bundle("ccc.unity3d", 139470)}
        selected, discovered = tool.select_bundles(index, set(), self.registry)
        self.assertEqual(selected, {})
        self.assertEqual(discovered, [])

    def test_unrelated_resources_stay_unselected(self):
        index = {
            "costume_icon_0001.unity3d": bundle("ddd.unity3d", 100),
            "jacket_aftspt.unity3d": bundle("eee.unity3d", 100),
        }
        selected, discovered = tool.select_bundles(index, set(), self.registry)
        self.assertEqual(selected, {})
        self.assertEqual(discovered, [])

    def test_known_bundle_matching_is_case_insensitive(self):
        index = {"MD_jp.gtx.unity3d": bundle("fff.unity3d", 100)}
        selected, discovered = tool.select_bundles(index, {"md_jp.gtx.unity3d"}, self.registry)
        self.assertEqual(list(selected), ["MD_jp.gtx.unity3d"])
        self.assertEqual(discovered, [])


def bundle(remote: str, size: int = 10) -> dict:
    return {"catalog_hash": "c" + remote, "remote": remote, "declared_size": size}


class IncrementalDownloadTests(unittest.TestCase):
    """Only bundles whose content-addressed remote changed need downloading."""

    def test_only_changed_and_new_remotes_are_downloaded(self):
        index = {
            "a.gtx.unity3d": bundle("1.unity3d"),
            "b.gtx.unity3d": bundle("2.unity3d"),
            "c.gtx.unity3d": bundle("3.unity3d"),
        }
        verified = {"a.gtx.unity3d": "1.unity3d",     # unchanged -> reuse
                    "b.gtx.unity3d": "old.unity3d"}   # changed -> download
        to_download, reused = tool.plan_bundle_downloads(index, verified)
        self.assertEqual(list(reused), ["a.gtx.unity3d"])
        self.assertEqual(sorted(to_download), ["b.gtx.unity3d", "c.gtx.unity3d"])

    def test_full_rescan_ignores_the_memo(self):
        index = {"a.gtx.unity3d": bundle("1.unity3d")}
        verified = {"a.gtx.unity3d": "1.unity3d"}
        to_download, reused = tool.plan_bundle_downloads(index, verified, full_rescan=True)
        self.assertEqual(list(to_download), ["a.gtx.unity3d"])
        self.assertEqual(reused, {})

    def test_empty_memo_downloads_everything_once(self):
        index = {"a.gtx.unity3d": bundle("1.unity3d"), "b.gtx.unity3d": bundle("2.unity3d")}
        to_download, reused = tool.plan_bundle_downloads(index, {})
        self.assertEqual(len(to_download), 2)
        self.assertEqual(reused, {})

    def test_next_memo_keeps_unchanged_entries_and_drops_stale_ones(self):
        index = {
            "a.gtx.unity3d": bundle("1.unity3d"),   # unchanged, not downloaded
            "b.gtx.unity3d": bundle("2.unity3d"),   # changed, downloaded now
            "c.gtx.unity3d": bundle("3.unity3d"),   # removed upstream -> dropped
        }
        verified = {"a.gtx.unity3d": "1.unity3d",
                    "b.gtx.unity3d": "old.unity3d",
                    "gone.gtx.unity3d": "9.unity3d"}
        downloaded = {"b.gtx.unity3d": index["b.gtx.unity3d"]}
        self.assertEqual(
            tool.next_bundle_index(index, verified, downloaded),
            {"a.gtx.unity3d": "1.unity3d", "b.gtx.unity3d": "2.unity3d"},
        )

    def test_bundles_outside_a_truncated_selection_keep_their_memo(self):
        """--max-bundles must not throw away the memo of unexamined bundles."""
        index = {
            "a.gtx.unity3d": bundle("1.unity3d"),
            "b.gtx.unity3d": bundle("2.unity3d"),
            "c.gtx.unity3d": bundle("3.unity3d"),
        }
        verified = {key: row["remote"] for key, row in index.items()}
        selected = {"a.gtx.unity3d": index["a.gtx.unity3d"]}      # truncated run
        to_download, _reused = tool.plan_bundle_downloads(selected, verified)
        self.assertEqual(to_download, {})
        self.assertEqual(tool.next_bundle_index(index, verified, to_download), verified)

    def test_memo_round_trip_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifests" / "official-bundle-index.json"
            bundles = {"b.gtx.unity3d": "2.unity3d", "a.gtx.unity3d": "1.unity3d"}
            tool.write_bundle_index(path, bundles)
            first = path.read_text(encoding="utf-8")
            self.assertEqual(tool.load_bundle_index(path), bundles)
            tool.write_bundle_index(path, {"a.gtx.unity3d": "1.unity3d", "b.gtx.unity3d": "2.unity3d"})
            self.assertEqual(path.read_text(encoding="utf-8"), first)
            self.assertEqual(list(json.loads(first)["bundles"]), ["a.gtx.unity3d", "b.gtx.unity3d"])

    def test_unusable_memo_degrades_to_a_full_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "official-bundle-index.json"
            self.assertEqual(tool.load_bundle_index(path), {})       # cold start
            path.write_text("{ not json", encoding="utf-8")
            self.assertEqual(tool.load_bundle_index(path), {})       # corrupt
            path.write_text(json.dumps({"schema_version": 99, "bundles": {"a.gtx.unity3d": "1.unity3d"}}),
                            encoding="utf-8")
            self.assertEqual(tool.load_bundle_index(path), {})       # unknown schema
            path.write_text(json.dumps({"schema_version": 1,
                                        "bundles": {"a.gtx.unity3d": "1.unity3d", "b": 7, "c": "x.txt"}}),
                            encoding="utf-8")
            self.assertEqual(tool.load_bundle_index(path), {"a.gtx.unity3d": "1.unity3d"})


class MainFlowTests(unittest.TestCase):
    """End-to-end main() with the official CDN faked out."""

    def _run(self, root: Path, index_rows: dict, memo: Path, urls: list, extra: list):
        def fake_download(url, destination, declared_size):
            urls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if url.endswith(".data"):
                destination.write_bytes(msgpack.packb([index_rows], use_bin_type=True))
            else:
                destination.write_bytes(b"x" * max(1, declared_size or 1))

        def fake_extract(command, **kwargs):
            extra.append(command)
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps({
                "bundle": "x.gtx", "key": "k2", "source": "新文本",
                "source_sha256": hashlib.sha256("新文本".encode("utf-8")).hexdigest(),
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        argv = ["refresh_latest_official_catalogue.py",
                "--bundle-index", str(memo), "--work-root", str(root / "work")]
        with patch.object(tool, "ROOT", root), patch.object(tool, "download", fake_download), \
                patch.object(tool.subprocess, "run", fake_extract), \
                patch.object(sys, "argv", argv):
            return tool.main()

    def test_main_downloads_each_bundle_only_until_its_remote_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locales" / "master").mkdir(parents=True)
            (root / "manifests").mkdir(parents=True)
            (root / "manifests" / "asset-version.json").write_text(json.dumps({
                "asset_version": "2", "client_version": "9.0.200",
                "asset_root": "https://td-assets.bn765.com/{version}/production/2018/Android",
                "index_name": "idx.data"}), encoding="utf-8")
            (root / "locales" / "master" / "rows.jsonl").write_text(
                json.dumps({
                    "asset_version": "1", "bundle": "x.gtx", "item_key": "k1",
                    "source_sha256": hashlib.sha256("既存".encode("utf-8")).hexdigest(),
                    "ja": "既存"}, ensure_ascii=False) + "\n"
                + json.dumps({
                    "asset_version": "1", "bundle": "y.gtx", "item_key": "k9",
                    "source_sha256": hashlib.sha256("別".encode("utf-8")).hexdigest(),
                    "ja": "別"}, ensure_ascii=False) + "\n",
                encoding="utf-8")

            memo = root / "manifests" / "official-bundle-index.json"
            urls: list[str] = []
            extracted: list = []

            # Cold start: no memo, so every tracked bundle is verified once.
            self.assertEqual(self._run(root, {"x.gtx.unity3d": ["c", "aaa.unity3d", 5],
                                              "y.gtx.unity3d": ["c", "bbb.unity3d", 6]},
                                       memo, urls, extracted), 0)
            self.assertEqual([url.rsplit("/", 1)[-1] for url in urls],
                             ["idx.data", "aaa.unity3d", "bbb.unity3d"])
            self.assertEqual(len(extracted), 1)
            self.assertEqual(tool.load_bundle_index(memo),
                             {"x.gtx.unity3d": "aaa.unity3d", "y.gtx.unity3d": "bbb.unity3d"})
            untranslated = (root / "locales" / "master" / "official-2-untranslated.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(untranslated.strip().splitlines()), 1)

            # Warm memo, same official objects: nothing but the index is fetched.
            urls.clear()
            self.assertEqual(self._run(root, {"x.gtx.unity3d": ["c", "aaa.unity3d", 5],
                                              "y.gtx.unity3d": ["c", "bbb.unity3d", 6]},
                                       memo, urls, extracted), 0)
            self.assertEqual([url.rsplit("/", 1)[-1] for url in urls], ["idx.data"])
            self.assertEqual(len(extracted), 1)          # extraction is skipped entirely

            # One upstream object changed: only that bundle is re-downloaded.
            urls.clear()
            self.assertEqual(self._run(root, {"x.gtx.unity3d": ["c", "aaa.unity3d", 5],
                                              "y.gtx.unity3d": ["c", "ccc.unity3d", 7]},
                                       memo, urls, extracted), 0)
            self.assertEqual([url.rsplit("/", 1)[-1] for url in urls], ["idx.data", "ccc.unity3d"])
            self.assertEqual(tool.load_bundle_index(memo),
                             {"x.gtx.unity3d": "aaa.unity3d", "y.gtx.unity3d": "ccc.unity3d"})

            # A bundle dropped upstream leaves the memo instead of lingering.
            urls.clear()
            self.assertEqual(self._run(root, {"x.gtx.unity3d": ["c", "aaa.unity3d", 5]},
                                       memo, urls, extracted), 0)
            self.assertEqual(tool.load_bundle_index(memo), {"x.gtx.unity3d": "aaa.unity3d"})


if __name__ == "__main__":
    unittest.main()
