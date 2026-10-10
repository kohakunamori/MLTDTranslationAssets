import hashlib
import json
import msgpack
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discover_official_bundles as discovery


def entry(remote: str, size: int = 100) -> dict:
    return {"catalog_hash": "c" + remote, "remote": remote, "declared_size": size}


def registry():
    return {
        "schema_version": 1,
        "families": [
            {"id": "gtx_text", "pipeline": "gtx_text", "match": {"suffix": "_jp.gtx.unity3d"}},
            {"id": "song_lyrics", "pipeline": "song_lyrics", "match": {"prefix": "scrobj_"}},
        ],
        "exclude": [],
        "reviewed_unclassified": [],
    }


def write_locale(root: Path, bundle: str, key: str, source: str) -> None:
    path = root / "locales" / "master" / f"{bundle}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "asset_version": "1",
        "bundle": bundle,
        "item_key": key,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "ja": source,
        "zh": "译",
        "status": "accepted",
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")


class TrackedBundleTests(unittest.TestCase):
    def test_locale_rows_and_lyric_files_both_count_as_tracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_locale(root, "season_a_2023_001har_100_jp.gtx", "k", "あ")
            songs = root / "lyrics" / "songs"
            songs.mkdir(parents=True)
            (songs / "scrobj_aftspt.unity3d.jsonl").write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                discovery.tracked_bundle_names(root),
                {"season_a_2023_001har_100_jp.gtx.unity3d", "scrobj_aftspt.unity3d"},
            )

    def test_the_download_memo_counts_as_tracked(self):
        """A verified bundle with no rows of its own must not be reported forever.

        The official index ships a typo'd ``pecial_108_fc_01_jp.gtx`` whose rows
        are byte-identical to ``special_108_fc_01_jp.gtx``; source identity
        de-duplicates all 38 of them, so it never gets a ``locales/`` file and
        only the memo knows it was fetched.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifests").mkdir(parents=True)
            (root / "manifests" / "official-bundle-index.json").write_text(
                json.dumps({"schema_version": 1, "bundles": {"pecial_108_fc_01_jp.gtx.unity3d": "abc.unity3d"}}),
                encoding="utf-8",
            )
            self.assertIn("pecial_108_fc_01_jp.gtx.unity3d", discovery.tracked_bundle_names(root))

    def test_a_missing_or_broken_memo_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifests").mkdir(parents=True)
            (root / "manifests" / "official-bundle-index.json").write_text("not json", encoding="utf-8")
            self.assertEqual(discovery.memo_bundle_names(root), set())


class DiscoverTests(unittest.TestCase):
    def test_new_bundles_inside_known_families_are_found(self):
        index = {
            "event_0448_story_01_jp.gtx.unity3d": entry("a.unity3d", 7000),
            "scrobj_ittana.unity3d": entry("b.unity3d", 139470),
            "costume_icon_0001.unity3d": entry("c.unity3d", 500),
        }
        report = discovery.discover(index, registry(), {"event_0448_story_01_jp.gtx.unity3d"})
        self.assertEqual(report["localizable"]["new_bundles"], 1)
        self.assertEqual(report["localizable"]["bundles"][0]["logical"], "scrobj_ittana.unity3d")
        self.assertEqual(report["localizable"]["bundles"][0]["family"], "song_lyrics")
        self.assertEqual(report["localizable"]["new_bytes"], 139470)
        self.assertEqual(report["localizable"]["families"]["gtx_text"],
                         {"total": 1, "tracked": 1, "new": 0, "new_bytes": 0})

    def test_already_tracked_bundle_is_not_reported_again(self):
        index = {"md_jp.gtx.unity3d": entry("a.unity3d")}
        report = discovery.discover(index, registry(), {"md_jp.gtx.unity3d"})
        self.assertEqual(report["localizable"]["new_bundles"], 0)

    def test_unclassified_families_are_aggregated_not_downloaded(self):
        index = {
            "costume_icon_0001.unity3d": entry("a.unity3d", 100),
            "costume_icon_0002.unity3d": entry("b.unity3d", 200),
            "titlebg_0001.unity3d": entry("c.unity3d", 50),
        }
        report = discovery.discover(index, registry(), set())
        self.assertEqual(report["localizable"]["new_bundles"], 0)
        self.assertEqual(report["unclassified"]["bundles"], 3)
        self.assertEqual(report["unclassified"]["families"], 2)
        self.assertEqual(report["unclassified"]["new_family_count"], 2)

    def test_previously_inventoried_family_is_not_reported_as_new(self):
        index = {"costume_icon_0003.unity3d": entry("a.unity3d")}
        previous = {"costume_icon_#": 900}
        report = discovery.discover(index, registry(), set(), previous_inventory=previous)
        self.assertEqual(report["unclassified"]["bundles"], 1)
        self.assertEqual(report["unclassified"]["new_family_count"], 0)

    def test_reviewed_unclassified_family_is_suppressed(self):
        document = registry()
        document["reviewed_unclassified"] = ["costume_icon"]
        index = {"costume_icon_0001.unity3d": entry("a.unity3d"), "x_0001.unity3d": entry("b.unity3d")}
        report = discovery.discover(index, document, set())
        self.assertEqual(report["unclassified"]["bundles"], 1)
        self.assertEqual(report["unclassified"]["families"], 1)

    def test_bundle_cap_refuses_the_batch(self):
        index = {f"scrobj_s{i}.unity3d": entry(f"{i}.unity3d") for i in range(5)}
        report = discovery.discover(index, registry(), set(), max_new_bundles=4)
        self.assertIsNotNone(report["refused"])
        self.assertIn("5 new localizable bundles", report["refused"])

    def test_byte_cap_refuses_the_batch(self):
        index = {"scrobj_big.unity3d": entry("a.unity3d", 4096)}
        report = discovery.discover(index, registry(), set(), max_new_bytes=1024)
        self.assertIsNotNone(report["refused"])
        self.assertIn("4096 new bytes", report["refused"])

    def test_cap_of_zero_disables_the_bound(self):
        index = {f"scrobj_s{i}.unity3d": entry(f"{i}.unity3d") for i in range(5)}
        report = discovery.discover(index, registry(), set(), max_new_bundles=0, max_new_bytes=0)
        self.assertIsNone(report["refused"])
        self.assertEqual(report["localizable"]["new_bundles"], 5)

    def test_report_is_deterministic(self):
        index = {
            "scrobj_b.unity3d": entry("b.unity3d"),
            "scrobj_a.unity3d": entry("a.unity3d"),
            "event_0448_story_01_jp.gtx.unity3d": entry("c.unity3d"),
        }
        first = discovery.discover(index, registry(), set())
        second = discovery.discover(index, registry(), set())
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(
            [row["logical"] for row in first["localizable"]["bundles"]],
            ["event_0448_story_01_jp.gtx.unity3d", "scrobj_a.unity3d", "scrobj_b.unity3d"],
        )


class InventoryTests(unittest.TestCase):
    def test_inventory_round_trip_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifests" / "official-asset-inventory.json"
            counts = {"scrobj_#": 492, "costume_icon": 6827}
            discovery.write_inventory(path, "1077720", counts, "2026-10-09T00:00:00+00:00")
            first = path.read_text(encoding="utf-8")
            self.assertEqual(discovery.load_inventory(path), counts)
            discovery.write_inventory(path, "1077720", {"costume_icon": 6827, "scrobj_#": 492},
                                      "2026-10-09T00:00:00+00:00")
            self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_unusable_inventory_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inventory.json"
            self.assertEqual(discovery.load_inventory(path), {})
            path.write_text("{ not json", encoding="utf-8")
            self.assertEqual(discovery.load_inventory(path), {})
            path.write_text(json.dumps({"schema_version": 99, "families": {"a": 1}}), encoding="utf-8")
            self.assertEqual(discovery.load_inventory(path), {})
            path.write_text(json.dumps({"schema_version": 1, "families": {"a": 2, "b": "x"}}), encoding="utf-8")
            self.assertEqual(discovery.load_inventory(path), {"a": 2})

    def test_inventory_counts_signatures(self):
        index = {
            "event_0448_story_01_jp.gtx.unity3d": entry("a.unity3d"),
            "event_0450_story_01_jp.gtx.unity3d": entry("b.unity3d"),
            "md_jp.gtx.unity3d": entry("c.unity3d"),
        }
        self.assertEqual(
            discovery.inventory_of(index),
            {"event_#_story_#_jp.gtx": 2, "md_jp.gtx": 1},
        )


class MainFlowTests(unittest.TestCase):
    def _run(self, root: Path, index_rows: dict, extra: list):
        index_path = root / "work" / "idx.data"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_bytes(msgpack.packb([index_rows], use_bin_type=True))
        (root / "manifests").mkdir(parents=True, exist_ok=True)
        (root / "manifests" / "asset-version.json").write_text(json.dumps({
            "asset_version": "2", "client_version": "9.0.200",
            "asset_root": "https://td-assets.bn765.com/{version}/production/2018/Android",
            "index_name": "idx.data"}), encoding="utf-8")
        argv = ["discover_official_bundles.py",
                "--root", str(root), "--index", str(index_path),
                "--work-root", str(root / "work"),
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--inventory", str(root / "manifests" / "inventory.json"),
                "--report", str(root / "report.json"),
                "--no-inventory-write"] + extra
        with patch.object(sys, "argv", argv):
            return discovery.main()

    def test_main_reports_new_bundles_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = self._run(root, {"scrobj_ittana.unity3d": ["c", "b.unity3d", 139470]}, [])
            self.assertEqual(code, 0)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["localizable"]["new_bundles"], 1)
            self.assertEqual(report["unclassified"]["bundles"], 0)

    def test_main_exits_two_when_the_cap_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = self._run(root, {"scrobj_ittana.unity3d": ["c", "b.unity3d", 139470]},
                             ["--max-new-bundles", "0", "--max-new-bytes", "1"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
