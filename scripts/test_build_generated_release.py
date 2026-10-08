#!/usr/bin/env python3
"""Offline contract tests for the public Assets generated-release entry point."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import msgpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_generated_release as build


def _write_synthetic_root(root: Path, *, images_complete: bool, write_manifest: bool = True) -> None:
    """Minimal synthetic repo root. No real product locales/images/manifest are read."""
    (root / "manifests").mkdir(parents=True)
    (root / "locales" / "story").mkdir(parents=True)
    (root / "manifests" / "asset-version.json").write_text(json.dumps({
        "asset_version": 1077640,
        "client_version": "9.0.200",
        "asset_root": "https://td-assets.bn765.com/{version}/production/2018/Android",
        "index_name": "index.data",
    }), encoding="utf-8")
    source = "原文"
    row = {
        "asset_version": "1077640", "client_version": None,
        "source_client_version": "9.0.200", "bundle": "story.gtx",
        "item_key": "k", "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "ja": source, "zh": "译文", "status": "accepted",
    }
    (root / "locales" / "story" / "story.gtx.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    if not write_manifest:
        return
    images = []
    for name in ("a.png", "b.png"):
        rel = f"images/{name}"
        if images_complete:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"png")
        images.append({"id": name, "localized": {"relative_path": rel}})
    (root / "manifests" / "images.manifest.json").write_text(
        json.dumps({"images": images}), encoding="utf-8")


def _fabricate_overlay(_snapshot, _archive, _ledger, output, *_args, **_kwargs) -> None:
    """Stand-in for the overlay subprocess: no real producer runs.

    Synthesizes the overlay bytes + localization manifest that the real
    ``build_entries`` (still exercised, not mocked) consumes.
    """
    target = Path(output) / "jp-android" / "remote-hash.unity3d"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"translated-bytes")
    (Path(output) / "localization-manifest.json").write_text(json.dumps({"bundles": [{
        "logical": "story.gtx.unity3d",
        "remote": "remote-hash.unity3d",
        "source_bundle_sha256": "a" * 64,
        "output_plain_sha256": "b" * 64,
    }]}), encoding="utf-8")


def _fake_download(url: str, destination: Path, declared_size) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix == ".data":
        destination.write_bytes(msgpack.packb([{
            "story.gtx.unity3d": ["cat", "remote-hash.unity3d", 9]
        }], use_bin_type=True))
    else:
        destination.write_bytes(b"bundle-bytes")


def _forbid(*_args, **_kwargs):
    raise AssertionError("this step must not run")


class RequiredImagesFailClosed(unittest.TestCase):
    """--require-images must refuse before any side effect, for any input state."""

    def _run_main(self, root: Path, argv_extra, *, patch_probe=None):
        """Run build.main() against a synthetic root with downloads/overlay mocked.

        Returns (result, mocks) where result is either ("raised", exc) or
        ("returned", code, stdout).  Assertions on the mocks prove no side effect.
        """
        saved_stdout = sys.stdout
        stdout = io.StringIO()
        with mock.patch.object(build, "ROOT", root), \
                mock.patch.object(build, "download", side_effect=_fake_download) as download, \
                mock.patch.object(build, "run_overlay", side_effect=_fabricate_overlay) as overlay, \
                mock.patch.object(build, "GeneratedStore") as store, \
                mock.patch.object(build, "read_gtx",
                                  return_value=("name", "k^原文", b"cipher")):
            if patch_probe is not None:
                probe = mock.patch.object(build, "image_inputs_are_complete", patch_probe)
                probe.start()
            argv = ["build_generated_release.py", *argv_extra]
            try:
                with mock.patch.object(sys, "argv", argv), \
                        contextlib.redirect_stdout(stdout):
                    result = ("returned", build.main(), stdout.getvalue())
            except BaseException as exc:  # noqa: BLE001
                result = ("raised", exc)
            finally:
                if patch_probe is not None:
                    probe.stop()
                sys.stdout = saved_stdout
        return result, {"download": download, "overlay": overlay, "store": store}

    def _assert_no_side_effects(self, mocks, work: Path, output: Path) -> None:
        mocks["download"].assert_not_called()
        mocks["overlay"].assert_not_called()
        mocks["store"].assert_not_called()
        self.assertFalse(work.exists(), "work-root must not be created")
        self.assertFalse(output.exists(), "output-root must not be created")

    def test_require_images_refused_when_inputs_complete(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "repo"
            _write_synthetic_root(root, images_complete=True)
            work = base / "work"
            output = base / "out"
            argv = [
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--work-root", str(work), "--output-root", str(output),
                "--source-commit", "a" * 40, "--translation-commit", "b" * 40,
                "--generated-commit", "c" * 40, "--ci-run-id", "12345",
                "--require-images",
            ]
            result, mocks = self._run_main(root, argv)
            self.assertEqual(result[0], "raised")
            self.assertIsInstance(result[1], ValueError)
            self._assert_no_side_effects(mocks, work, output)

    def test_require_images_refused_when_inputs_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "repo"
            _write_synthetic_root(root, images_complete=False)
            work = base / "work"
            output = base / "out"
            argv = [
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--work-root", str(work), "--output-root", str(output),
                "--source-commit", "a" * 40, "--translation-commit", "b" * 40,
                "--generated-commit", "c" * 40, "--ci-run-id", "12345",
                "--require-images",
            ]
            result, mocks = self._run_main(root, argv)
            self.assertEqual(result[0], "raised")
            self.assertIsInstance(result[1], ValueError)
            self._assert_no_side_effects(mocks, work, output)

    def test_require_images_refused_when_manifest_absent(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "repo"
            _write_synthetic_root(root, images_complete=False, write_manifest=False)
            work = base / "work"
            output = base / "out"
            argv = [
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--work-root", str(work), "--output-root", str(output),
                "--source-commit", "a" * 40, "--translation-commit", "b" * 40,
                "--generated-commit", "c" * 40, "--ci-run-id", "12345",
                "--require-images",
            ]
            result, mocks = self._run_main(root, argv)
            self.assertEqual(result[0], "raised")
            self.assertIsInstance(result[1], ValueError)
            self._assert_no_side_effects(mocks, work, output)

    def test_require_images_guard_does_not_read_image_probe(self):
        """The early guard must not consult the image probe at all."""
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "repo"
            _write_synthetic_root(root, images_complete=True)
            work = base / "work"
            output = base / "out"
            argv = [
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--work-root", str(work), "--output-root", str(output),
                "--source-commit", "a" * 40, "--translation-commit", "b" * 40,
                "--generated-commit", "c" * 40, "--ci-run-id", "12345",
                "--require-images",
            ]
            for patch_probe in (
                mock.Mock(return_value=True),
                mock.Mock(return_value=False),
                mock.Mock(side_effect=AssertionError("probe must not be consulted")),
            ):
                result, mocks = self._run_main(root, argv, patch_probe=patch_probe)
                self.assertEqual(result[0], "raised")
                self.assertIsInstance(result[1], ValueError)
                patch_probe.assert_not_called()
                self._assert_no_side_effects(mocks, work, output)


class DefaultTextPathRegression(unittest.TestCase):
    """A full default (no --require-images) main() run still produces a text release."""

    def test_default_text_only_main_uses_flat_writer_and_keeps_runtime_path(self):
        from assets_generated_index import GeneratedStore
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "repo"
            _write_synthetic_root(root, images_complete=False)
            work = base / "work"
            output = base / "out"
            # Real product flat writer (CAS store), synthetic bytes only.
            real_store = GeneratedStore(output)
            argv = [
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--work-root", str(work), "--output-root", str(output),
                "--source-commit", "a" * 40, "--translation-commit", "b" * 40,
                "--generated-commit", "c" * 40, "--ci-run-id", "12345",
                "--max-bundles", "1",
            ]
            stdout = io.StringIO()
            with mock.patch.object(build, "ROOT", root), \
                    mock.patch.object(build, "download", side_effect=_fake_download), \
                    mock.patch.object(build, "run_overlay", side_effect=_fabricate_overlay), \
                    mock.patch.object(build, "GeneratedStore", return_value=real_store), \
                    mock.patch.object(build, "read_gtx",
                                       return_value=("name", "k^原文", b"cipher")), \
                    mock.patch.object(sys, "argv", ["build_generated_release.py", *argv]), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(build.main(), 0)
            report = json.loads(stdout.getvalue())
            # The default path never claims the image surface was produced.
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["image_surface"], "blocked_missing_reviewed_inputs")
            manifest = json.loads((output / "1077640" / "manifest.json").read_text(encoding="utf-8"))
            entries = {e["logical_key"]: e for e in manifest["entries"]}
            translated = entries["story.gtx.unity3d"]
            # runtime_path (hashed remote name) is preserved, and it maps to a real flat object.
            self.assertEqual(translated["runtime_path"],
                             "production/2018/Android/remote-hash.unity3d")
            self.assertTrue(translated["object_path"].startswith("objects/sha256/"))
            self.assertTrue((output / translated["object_path"]).is_file())
            catalog = entries["__official_asset_index__"]
            self.assertEqual(catalog["runtime_path"], "production/2018/Android/index.data")


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

    def test_generated_entries_keep_client_runtime_name_and_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            overlay = root / "overlay" / "jp-android"
            overlay.mkdir(parents=True)
            bundle = overlay / "remote-hash.unity3d"
            bundle.write_bytes(b"translated")
            index = root / "index.data"
            index.write_bytes(b"official-index")
            manifest = root / "localization-manifest.json"
            manifest.write_text(json.dumps({"bundles": [{
                "logical": "story.gtx.unity3d",
                "remote": "remote-hash.unity3d",
                "source_bundle_sha256": "a" * 64,
                "output_plain_sha256": "b" * 64,
            }]}), encoding="utf-8")
            entries = build.build_entries(
                root / "overlay", manifest, "1077640", "9.0.200",
                index_name="index.data", index_path=index,
            )
            translated = next(e for e in entries if e["logical_key"] == "story.gtx.unity3d")
            self.assertEqual(translated["runtime_path"],
                             "production/2018/Android/remote-hash.unity3d")
            catalog = next(e for e in entries if e["logical_key"] == "__official_asset_index__")
            self.assertEqual(catalog["runtime_path"], "production/2018/Android/index.data")
            self.assertEqual(catalog["translated_sha256"], build.sha256_file(index))


if __name__ == "__main__":
    unittest.main()
