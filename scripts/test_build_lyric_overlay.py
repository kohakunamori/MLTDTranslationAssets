"""Tests for packaging accepted lyrics into the release overlay.

The UnityPy round-trip itself is verified against real official bundles outside
CI (the repository does not carry official assets), but every decision this step
makes — which songs are eligible, which are refused, how the manifest grows and
where the caps stop the run — is pinned here.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipelines" / "text"))

import build_lyric_overlay as overlay  # noqa: E402


def row(bundle, index, ja, zh, status="accepted"):
    import hashlib

    return {
        "bundle": bundle,
        "index": index,
        "tick": index * 9600,
        "abs_time": index / 10,
        "source_sha256": hashlib.sha256(ja.encode("utf-8")).hexdigest(),
        "ja": ja,
        "zh": zh,
        "status": status,
        "updated_at": "2026-10-10T00:00:00+00:00",
    }


def write_song(root: Path, bundle: str, rows) -> None:
    songs = root / "songs"
    songs.mkdir(parents=True, exist_ok=True)
    (songs / f"{bundle}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


class SongBundlesTests(unittest.TestCase):
    def test_names_are_sorted_and_keep_the_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_song(root, "scrobj_b.unity3d", [row("scrobj_b.unity3d", 1, "あ", "啊")])
            write_song(root, "scrobj_a.unity3d", [row("scrobj_a.unity3d", 1, "い", "呀")])
            self.assertEqual(overlay.song_bundles(root), ["scrobj_a.unity3d", "scrobj_b.unity3d"])

    def test_missing_library_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(overlay.song_bundles(Path(tmp)), [])


class RunTests(unittest.TestCase):
    """``run`` is driven with a fake downloader and a fake patcher."""

    def setUp(self):
        self.index = {
            "scrobj_a.unity3d": {"remote": "aaa.unity3d", "declared_size": 100, "catalog_hash": "ha"},
            "scrobj_b.unity3d": {"remote": "bbb.unity3d", "declared_size": 200, "catalog_hash": "hb"},
        }

    def _workspace(self, tmp):
        root = Path(tmp)
        lyrics = root / "lyrics"
        write_song(lyrics, "scrobj_a.unity3d", [
            row("scrobj_a.unity3d", 1, "あ", "啊"),
            row("scrobj_a.unity3d", 2, "う", "呜"),
        ])
        write_song(lyrics, "scrobj_b.unity3d", [row("scrobj_b.unity3d", 5, "え", "诶")])
        return lyrics, root / "archive", root / "overlay"

    def _run(self, lyrics, archive, over, *, matcher=None, **kwargs):
        """Drive ``run`` with a fake downloader and a fake patcher.

        ``matcher`` stands in for "does this bundle still contain these lines":
        returning 0 makes the fake patcher report a bundle whose lines moved or
        changed upstream.
        """
        self.downloads = []

        def fake_download(url, destination, declared, content_key, cache_root):
            self.downloads.append((url, str(destination), declared, content_key, cache_root))
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"official")
            return "fetched"

        def fake_save(source, output, translations):
            changed = matcher(source, translations) if matcher else len(translations)
            if not changed:
                return {"bundle": Path(source).name, "changed": 0, "written": False,
                        "slots_total": 0, "unmatched_texts": sorted(translations.source_texts()),
                        "output_bytes": 0}
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"patched")
            return {
                "bundle": Path(source).name,
                "source_path": str(source),
                "output_path": str(output),
                "source_bundle_sha256": "s" * 64,
                "output_bundle_sha256": "o" * 64,
                "output_plain_sha256": "p" * 64,
                "output_bytes": 7,
                "changed": changed,
                "slots_total": len(translations) + 1,
                "unmatched_texts": [],
                "written": True,
            }

        with patch.object(overlay, "save_localized_bundle", fake_save):
            return overlay.run(
                index=self.index, archive_root=archive, overlay_root=over,
                lyrics_root=lyrics, asset_version="1077741",
                upstream_root="https://cdn/1077741/production/2018/Android",
                downloader=fake_download, cache_root=None, **kwargs)

    def test_every_translated_song_is_patched_and_downloaded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            summary = self._run(lyrics, archive, over)
            self.assertEqual(summary["bundles_patched"], 2)
            self.assertEqual(summary["slots_patched"], 3)
            self.assertEqual(summary["declared_bytes"], 300)
            self.assertEqual(len(self.downloads), 2)
            self.assertEqual(
                sorted(url for url, *_ in self.downloads),
                ["https://cdn/1077741/production/2018/Android/aaa.unity3d",
                 "https://cdn/1077741/production/2018/Android/bbb.unity3d"])
            self.assertEqual(self.downloads[0][2], 100)          # declared size is verified
            self.assertEqual(self.downloads[0][3], "ha")         # content fingerprint drives the cache

    def test_manifest_row_carries_what_the_release_builder_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            self._run(lyrics, archive, over)
            document = json.loads((over / "localization-manifest.json").read_text(encoding="utf-8"))
            rows = document["bundles"]
            self.assertEqual([r["logical"] for r in rows],
                             ["scrobj_a.unity3d", "scrobj_b.unity3d"])
            for entry in rows:
                for key in ("logical", "remote", "source_bundle_sha256", "output_plain_sha256"):
                    self.assertIn(key, entry)
            self.assertEqual(document["lyrics"]["bundles"], 2)
            self.assertEqual(document["lyrics"]["slots_patched"], 3)

    def test_existing_text_rows_survive_the_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            over.mkdir(parents=True, exist_ok=True)
            (over / "localization-manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "bundles_written": 11,
                "bundles": [{"logical": "MD_jp.gtx", "remote": "md.unity3d"}],
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            self._run(lyrics, archive, over)
            document = json.loads((over / "localization-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(document["bundles_written"], 11)
            self.assertEqual([r["logical"] for r in document["bundles"]],
                             ["MD_jp.gtx", "scrobj_a.unity3d", "scrobj_b.unity3d"])

    def test_a_duplicate_manifest_row_is_refused(self):
        manifest = Path("manifest.json")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "localization-manifest.json"
            path.write_text(json.dumps({"bundles": [{"logical": "x.unity3d"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                overlay.append_manifest(path, [{"logical": "x.unity3d"}],
                                        {"slots_patched": 0, "songs_with_translation": 1,
                                         "songs_without_translation": 0}, "1")

    def test_song_without_translation_is_skipped_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            write_song(lyrics, "scrobj_c.unity3d", [row("scrobj_c.unity3d", 1, "お", "")])
            summary = self._run(lyrics, archive, over)
            self.assertEqual(summary["songs_without_translation"], 1)
            self.assertEqual(summary["bundles_patched"], 2)

    def test_song_absent_from_the_catalogue_is_reported_not_patched(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            write_song(lyrics, "scrobj_gone.unity3d", [row("scrobj_gone.unity3d", 1, "か", "卡")])
            summary = self._run(lyrics, archive, over)
            self.assertEqual(summary["songs_absent_from_catalogue"], ["scrobj_gone.unity3d"])
            self.assertEqual(summary["bundles_patched"], 2)

    def test_song_whose_lines_changed_upstream_is_reported_not_patched(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            summary = self._run(lyrics, archive, over, matcher=lambda source, translations: 0)
            self.assertEqual(sorted(summary["songs_without_matching_lines"]),
                             ["scrobj_a.unity3d", "scrobj_b.unity3d"])
            self.assertEqual(summary["bundles_patched"], 0)
            self.assertEqual(summary["accepted_rows_not_applied"], 3)

    def test_cap_refuses_before_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            with self.assertRaises(ValueError) as caught:
                self._run(lyrics, archive, over, max_bundles=1)
            self.assertIn("cap 1", str(caught.exception))
            self.assertEqual(self.downloads, [])

    def test_byte_cap_refuses_before_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            lyrics, archive, over = self._workspace(tmp)
            with self.assertRaises(ValueError) as caught:
                self._run(lyrics, archive, over, max_bytes=250)
            self.assertIn("cap 250", str(caught.exception))
            self.assertEqual(self.downloads, [])


class ReleaseBuilderWiringTests(unittest.TestCase):
    """The lyrics only reach a client if the release build actually calls this.

    A dropped call would not crash anything: the release would simply keep
    shipping text-only bundles, and the phone would keep showing Japanese lyrics.
    Wiring is therefore asserted, the way the workflow steps are, instead of
    assumed.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parent / "build_generated_release.py").read_text(
            encoding="utf-8")

    def test_the_release_build_runs_the_lyric_overlay(self):
        self.assertIn("build_lyric_overlay.run(", self.source)

    def test_it_hands_over_what_the_overlay_needs(self):
        start = self.source.index("build_lyric_overlay.run(")
        call = self.source[start: start + 900]
        for keyword in ("index=", "archive_root=", "overlay_root=", "lyrics_root=",
                        "downloader=download", "asset_version=", "upstream_root="):
            self.assertIn(keyword, call, f"the lyric overlay is not given {keyword}")

    def test_the_release_report_says_what_the_lyrics_did(self):
        self.assertIn('"lyrics":', self.source)

    def test_the_overlay_runs_before_the_entries_are_built(self):
        """Publishing entries before patching would ship the unpatched overlay."""
        self.assertLess(self.source.index("build_lyric_overlay.run("),
                        self.source.index("entries = build_entries("))


if __name__ == "__main__":
    unittest.main()
