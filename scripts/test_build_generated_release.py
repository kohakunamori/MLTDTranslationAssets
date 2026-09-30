#!/usr/bin/env python3
"""Offline contract tests for the public Assets generated-release entry point."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import msgpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_generated_release as build


class GeneratedReleaseContracts(unittest.TestCase):
    def test_official_index_is_normalized_and_rejects_unsafe_remote(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "index.data"
            path.write_bytes(msgpack.packb([{
                "foo.gtx.unity3d": ["catalog", "remote.unity3d", 12]
            }], use_bin_type=True))
            self.assertEqual(build.load_official_index(path)["foo.gtx.unity3d"]["declared_size"], 12)

            path.write_bytes(msgpack.packb([{
                "foo.gtx.unity3d": ["catalog", "../escape", 12]
            }], use_bin_type=True))
            with self.assertRaises(ValueError):
                build.load_official_index(path)

    def test_translation_rows_bind_source_hash_and_accept_cross_version_rows(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            locale = root / "locales" / "story"
            locale.mkdir(parents=True)
            source = "原文"
            row = {
                "asset_version": "1077500",
                "client_version": None,
                "source_client_version": "9.0.200",
                "bundle": "story.gtx",
                "item_key": "k",
                "source_sha256": build.sha256_text(source),
                "ja": source,
                "zh": "译文",
                "status": "accepted",
                "updated_at": "2026-09-30T00:00:00Z",
            }
            (locale / "story.gtx.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            grouped = build.read_translation_rows(root, "1077600")
            self.assertEqual(grouped["story.gtx.unity3d"][0]["translation"], "译文")

    def test_version_manifest_rejects_non_official_host(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "asset-version.json"
            path.write_text(json.dumps({
                "asset_version": 1077600,
                "client_version": "9.0.200",
                "asset_root": "https://example.invalid/{version}",
                "index_name": "index.data",
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                build.load_version_manifest(path)

    def test_same_key_different_source_is_selected_by_current_source(self):
        rows = [
            {"key": "k", "source": "旧文", "translation": "旧译"},
            {"key": "k", "source": "新文", "translation": "新译"},
        ]
        chosen = build.select_rows_for_current_sources(rows, {"k": "新文"})
        self.assertEqual([row["translation"] for row in chosen], ["新译"])

    def test_current_asset_version_wins_over_older_reused_translation(self):
        rows = [
            {"key": "k", "source": "同一原文", "translation": "旧译", "asset_version": "1077500"},
            {"key": "k", "source": "同一原文", "translation": "新译", "asset_version": "1077640"},
        ]
        chosen = build.select_rows_for_current_sources(
            rows, {"k": "同一原文"}, "1077640"
        )
        self.assertEqual([row["translation"] for row in chosen], ["新译"])

    def test_same_asset_version_conflict_is_rejected(self):
        rows = [
            {"key": "k", "source": "同一原文", "translation": "甲", "asset_version": "1077640"},
            {"key": "k", "source": "同一原文", "translation": "乙", "asset_version": "1077640"},
        ]
        with self.assertRaises(ValueError):
            build.select_rows_for_current_sources(rows, {"k": "同一原文"}, "1077640")


if __name__ == "__main__":
    unittest.main()
