import hashlib
import json
import msgpack
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import refresh_lyrics_catalogue as tool
from mltd_localize_scrobj import LyricSlot


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


def entry(remote: str, size: int = 1000) -> dict:
    """Index row as ``load_official_index`` returns it (used by pure helpers)."""
    return {"catalog_hash": "c" + remote, "remote": remote, "declared_size": size}


def raw_entry(remote: str, size: int = 1000) -> list:
    """Index row as it is stored in the official msgpack table."""
    return ["c" + remote, remote, size]


def slot(index: int, text: str) -> LyricSlot:
    return LyricSlot(index=index, tick=index * 960, abs_time=index / 10, text=text)


class SelectSongsTests(unittest.TestCase):
    def test_only_lyric_families_are_selected(self):
        index = {
            "scrobj_ittana.unity3d": entry("a.unity3d"),
            "scrobj_aftspt.unity3d": entry("b.unity3d"),
            "event_0448_story_01_jp.gtx.unity3d": entry("c.unity3d"),
            "costume_icon_0001.unity3d": entry("d.unity3d"),
        }
        missing, known = tool.select_songs(index, registry(), {"scrobj_aftspt.unity3d"})
        self.assertEqual(missing, ["scrobj_ittana.unity3d"])
        self.assertEqual(known, ["scrobj_aftspt.unity3d"])

    def test_case_is_preserved_but_matching_is_case_insensitive(self):
        index = {"SCROBJ_X.unity3d": entry("a.unity3d")}
        missing, known = tool.select_songs(index, registry(), set())
        self.assertEqual(missing, ["SCROBJ_X.unity3d"])
        self.assertEqual(known, [])


class ChangedSongsTests(unittest.TestCase):
    def test_only_moved_remotes_are_reported(self):
        index = {
            "scrobj_a.unity3d": entry("same.unity3d"),
            "scrobj_b.unity3d": entry("new.unity3d"),
            "scrobj_c.unity3d": entry("unseen.unity3d"),
        }
        memo = {"scrobj_a.unity3d": "same.unity3d", "scrobj_b.unity3d": "old.unity3d"}
        self.assertEqual(tool.changed_songs(index, memo), ["scrobj_b.unity3d"])


class SourceMemoTests(unittest.TestCase):
    def test_round_trip_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lyrics" / "source-bundle-index.json"
            tool.write_source_memo(path, {"scrobj_b.unity3d": "2", "scrobj_a.unity3d": "1"})
            first = path.read_text(encoding="utf-8")
            self.assertEqual(tool.load_source_memo(path),
                             {"scrobj_a.unity3d": "1", "scrobj_b.unity3d": "2"})
            tool.write_source_memo(path, {"scrobj_a.unity3d": "1", "scrobj_b.unity3d": "2"})
            self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_unusable_memo_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memo.json"
            self.assertEqual(tool.load_source_memo(path), {})
            path.write_text("{ not json", encoding="utf-8")
            self.assertEqual(tool.load_source_memo(path), {})
            path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            self.assertEqual(tool.load_source_memo(path), {})


class MainFlowTests(unittest.TestCase):
    def _setup(self, tmp: Path, index_rows: dict):
        work = tmp / "work"
        work.mkdir(parents=True, exist_ok=True)
        (work / "idx.data").write_bytes(msgpack.packb([index_rows], use_bin_type=True))
        manifests = tmp / "manifests"
        manifests.mkdir(parents=True, exist_ok=True)
        (manifests / "asset-version.json").write_text(json.dumps({
            "asset_version": "1077720", "client_version": "9.0.200",
            "asset_root": "https://td-assets.bn765.com/{version}/production/2018/Android",
            "index_name": "idx.data"}), encoding="utf-8")
        return work, manifests

    def _argv(self, tmp: Path, work: Path, manifests: Path, extra=None):
        return ["refresh_lyrics_catalogue.py",
                "--work-root", str(work),
                "--index", str(work / "idx.data"),
                "--version-manifest", str(manifests / "asset-version.json"),
                "--lyrics-root", str(tmp / "lyrics"),
                "--source-memo", str(tmp / "lyrics" / "source-bundle-index.json")] + list(extra or [])

    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            work, manifests = self._setup(tmp, {"scrobj_ittana.unity3d": raw_entry("a.unity3d", 139470)})
            argv = self._argv(tmp, work, manifests, ["--dry-run"])
            with patch.object(sys, "argv", argv):
                self.assertEqual(tool.main(), 0)
            self.assertFalse((tmp / "lyrics" / "songs").exists())
            self.assertFalse((tmp / "lyrics" / "source-bundle-index.json").exists())

    def test_cap_trips_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            work, manifests = self._setup(tmp, {"scrobj_ittana.unity3d": raw_entry("a.unity3d", 139470)})
            argv = self._argv(tmp, work, manifests, ["--max-new-bundles", "0", "--max-new-bytes", "1"])
            with patch.object(sys, "argv", argv):
                self.assertEqual(tool.main(), 2)
            self.assertFalse((tmp / "lyrics" / "songs").exists())

    def test_new_song_is_extracted_and_aggregated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            work, manifests = self._setup(tmp, {"scrobj_ittana.unity3d": raw_entry("a.unity3d", 139470)})

            def fake_download(url, destination, declared_size):
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"bundle")

            with patch("build_generated_release.download", side_effect=fake_download), \
                 patch.object(tool, "read_slots", return_value=[slot(41, "一旦愛して♡"), slot(64, "ちょうだい")]), \
                 patch.object(sys, "argv", self._argv(tmp, work, manifests)):
                self.assertEqual(tool.main(), 0)

            song = tmp / "lyrics" / "songs" / "scrobj_ittana.unity3d.jsonl"
            rows = [json.loads(line) for line in song.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["ja"] for row in rows], ["一旦愛して♡", "ちょうだい"])
            self.assertTrue(all(row["status"] == "untranslated" for row in rows))
            manifest = json.loads((tmp / "lyrics" / "lyrics_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["total_songs"], 1)
            self.assertEqual(manifest["counts"]["total_slots"], 2)

            # Second run: the song is already there and unchanged -> nothing to do.
            with patch("build_generated_release.download", side_effect=AssertionError("must not download")), \
                 patch.object(tool, "read_slots", side_effect=AssertionError("must not extract")), \
                 patch.object(sys, "argv", self._argv(tmp, work, manifests)):
                self.assertEqual(tool.main(), 0)

    def test_changed_song_keeps_its_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            work, manifests = self._setup(tmp, {"scrobj_a.unity3d": raw_entry("v2.unity3d")})
            lyrics = tmp / "lyrics"
            songs = lyrics / "songs"
            songs.mkdir(parents=True)
            old_text = "あ"
            songs.joinpath("scrobj_a.unity3d.jsonl").write_text(json.dumps({
                "bundle": "scrobj_a.unity3d", "index": 10, "tick": 1, "abs_time": 1.0,
                "source_sha256": hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
                "ja": old_text, "zh": "甲", "status": "accepted",
                "updated_at": "old"}, ensure_ascii=False) + "\n", encoding="utf-8")
            tool.write_source_memo(lyrics / "source-bundle-index.json", {"scrobj_a.unity3d": "v1.unity3d"})

            def fake_download(url, destination, declared_size):
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"bundle")

            with patch("build_generated_release.download", side_effect=fake_download), \
                 patch.object(tool, "read_slots", return_value=[slot(10, old_text), slot(11, "い")]), \
                 patch.object(sys, "argv", self._argv(tmp, work, manifests)):
                self.assertEqual(tool.main(), 0)

            rows = [json.loads(line) for line in songs.joinpath("scrobj_a.unity3d.jsonl")
                    .read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["zh"] for row in rows], ["甲", ""])
            self.assertEqual(tool.load_source_memo(lyrics / "source-bundle-index.json"),
                             {"scrobj_a.unity3d": "v2.unity3d"})


if __name__ == "__main__":
    unittest.main()
