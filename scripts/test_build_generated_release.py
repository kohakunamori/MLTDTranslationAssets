#!/usr/bin/env python3
"""Offline contract tests for the public Assets generated-release entry point."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
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


def _fake_download(url: str, destination: Path, declared_size, content_key=None,
                   cache_root=None) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix == ".data":
        destination.write_bytes(msgpack.packb([{
            "story.gtx.unity3d": ["cat", "remote-hash.unity3d", 9]
        }], use_bin_type=True))
    else:
        destination.write_bytes(b"bundle-bytes")
    return "fetched"


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


class IncrementalReuseRegression(unittest.TestCase):
    """A second build must not fetch or rewrite a bundle whose inputs did not move.

    The whole point of the incremental path: one edited translation used to cost a
    full rebuild of every bundle, which on the real repository is ten thousand
    downloads and ten thousand rewrites for one changed row.
    """

    def _overlay_from_snapshot(self, snapshot, _archive, _ledger, output, *_args, **_kwargs):
        """A stand-in overlay that rewrites exactly the objects the snapshot names.

        Unlike ``_fabricate_overlay`` it reports the route counters the real
        overlay reports, which is what a release has to record before a later
        build is allowed to reuse anything from it.  It also refuses an empty
        snapshot exactly as the real overlay does (``localization_version_identity``
        rejects a snapshot with no objects), so a build that reuses everything
        must not call it at all.
        """
        requested = json.loads(Path(snapshot).read_text(encoding="utf-8"))["objects"]
        if not requested:
            raise AssertionError("the text overlay must not be invoked with an empty snapshot")
        # The real overlay creates its own output root before writing anything,
        # which matters when reuse leaves it nothing to rewrite.
        Path(output).mkdir(parents=True, exist_ok=True)
        self._guard_identity(Path(snapshot), Path(output), requested)
        rows = []
        for row in requested:
            target = Path(output) / "jp-android" / row["remote"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"translated:" + row["logical"].encode("utf-8"))
            rows.append({"logical": row["logical"], "remote": row["remote"],
                         "source_bundle_sha256": "a" * 64,
                         "output_plain_sha256": "b" * 64})
        (Path(output) / "localization-manifest.json").write_text(json.dumps({
            "source_candidates": len(rows), "resolved_exact": len(rows),
            "resolved_memory": 0, "stale_exact": 0, "bundles": rows}),
            encoding="utf-8")

    def _guard_identity(self, snapshot: Path, output: Path, requested: list) -> None:
        """Mirror the overlay's own refusal to reuse a build directory.

        ``cmd_build_overlay`` records the snapshot identity in the output root and
        refuses to write when a different snapshot already owns it.  A stub that
        skips this hides the fact that an incremental attempt followed by a full
        retry hands the overlay two different snapshots in one work directory.
        """
        identity_path = output / "version-identity.json"
        identity = {
            "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "snapshot_objects": len(requested),
        }
        if identity_path.is_file():
            if json.loads(identity_path.read_text(encoding="utf-8")) != identity:
                raise ValueError("overlay output root already belongs to another identity")
        else:
            identity_path.write_text(json.dumps(identity), encoding="utf-8")

    def _overlay_answering_from_memory(self, snapshot, _archive, _ledger, output,
                                       *_args, **_kwargs):
        """A subset run that answers one key from the memory table.

        The real overlay's ledger holds every accepted row of the bundles it was
        given, so a subset run can resolve a key that a full run resolves from the
        bundle's own row.  The builder must notice and rebuild everything.
        """
        requested = json.loads(Path(snapshot).read_text(encoding="utf-8"))["objects"]
        Path(output).mkdir(parents=True, exist_ok=True)
        self._guard_identity(Path(snapshot), Path(output), requested)
        rows = []
        for row in requested:
            target = Path(output) / "jp-android" / row["remote"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"translated:" + row["logical"].encode("utf-8"))
            rows.append({"logical": row["logical"], "remote": row["remote"],
                         "source_bundle_sha256": "a" * 64,
                         "output_plain_sha256": "b" * 64})
        from_memory = 1 if len(rows) == 1 else 0
        (Path(output) / "localization-manifest.json").write_text(json.dumps({
            "source_candidates": len(rows),
            "resolved_exact": len(rows) - from_memory,
            "resolved_memory": from_memory, "stale_exact": 0, "bundles": rows}),
            encoding="utf-8")

    def _downloader(self, downloads, index_rows):
        """A fake fetch that serves the catalogue rows this fixture publishes."""

        def counted_download(url, destination, *args, **kwargs):
            downloads.append(str(destination))
            destination = Path(destination)
            if destination.suffix == ".data":
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(msgpack.packb([{
                    logical: ["catalog-" + remote, remote, 9]
                    for logical, remote, _digest in index_rows}], use_bin_type=True))
                return "fetched"
            return _fake_download(url, destination, *args, **kwargs)

        return counted_download

    def _build_once(self, root: Path, work: Path, output: Path, commit: str,
                    index_rows=((("story.gtx.unity3d"), "remote-hash.unity3d", "a" * 64),)):
        from assets_generated_index import GeneratedStore
        argv = [
            "--version-manifest", str(root / "manifests" / "asset-version.json"),
            "--work-root", str(work), "--output-root", str(output),
            "--source-commit", commit, "--translation-commit", commit,
            "--generated-commit", commit, "--ci-run-id", "12345",
        ]
        stdout = io.StringIO()
        downloads: list[str] = []

        counted_download = self._downloader(downloads, index_rows)

        with mock.patch.object(build, "ROOT", root), \
                mock.patch.object(build, "download", side_effect=counted_download), \
                mock.patch.object(build, "run_overlay",
                                  side_effect=self._overlay_from_snapshot) as overlay, \
                mock.patch.object(build, "GeneratedStore",
                                  return_value=GeneratedStore(output)), \
                mock.patch.object(build, "read_gtx",
                                  return_value=("name", "k^原文", b"cipher")), \
                mock.patch.object(sys, "argv", ["build_generated_release.py", *argv]), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(build.main(), 0)
        return json.loads(stdout.getvalue()), downloads, overlay.call_count

    def _layout(self, base: Path, bundles=("story.gtx",)):
        root = base / "repo"
        _write_synthetic_root(root, images_complete=False)
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True,
                       capture_output=True)
        for key, value in (("user.email", "tests@example.invalid"),
                           ("user.name", "reuse tests"),
                           ("commit.gpgsign", "false")):
            subprocess.run(["git", "-C", str(root), "config", key, value],
                           check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "inputs"],
                       check=True, capture_output=True)
        for name in bundles[1:]:
            source = "原文"
            (root / "locales" / "story" / f"{name}.jsonl").write_text(json.dumps({
                "asset_version": "1077640", "client_version": None,
                "source_client_version": "9.0.200", "bundle": name,
                "item_key": "k",
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "ja": source, "zh": "译文", "status": "accepted",
            }, ensure_ascii=False) + "\n", encoding="utf-8")
        if len(bundles) > 1:
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "more inputs"],
                           check=True, capture_output=True)
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                check=True, capture_output=True,
                                text=True).stdout.strip()
        return root, commit

    def test_an_unchanged_bundle_is_neither_downloaded_nor_rewritten(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base)
            output = base / "out"
            first, first_downloads, first_overlays = self._build_once(root, base / "work-1", output, commit)
            self.assertEqual(first["reuse"]["mode"], "full")
            self.assertIn("remote-hash.unity3d", " ".join(first_downloads))

            # A fresh runner: same repository, same store, empty work directory.
            second, second_downloads, second_overlays = self._build_once(root, base / "work-2", output, commit)
            self.assertEqual(second["reuse"]["mode"], "incremental")
            self.assertEqual(second["reuse"]["reused_bundles"], 1)
            self.assertEqual([d for d in second_downloads if d.endswith(".unity3d")], [])
            # The overlay refuses an empty snapshot, so a build with nothing to
            # rewrite must not reach it at all.
            self.assertEqual(first_overlays, 1)
            self.assertEqual(second_overlays, 0)
            # The release still names every bundle; the reused one kept its bytes.
            manifest = json.loads(
                (output / "1077640" / "manifest.json").read_text(encoding="utf-8"))
            entries = {e["logical_key"]: e for e in manifest["entries"]}
            self.assertEqual(entries["story.gtx.unity3d"]["runtime_path"],
                             "production/2018/Android/remote-hash.unity3d")
            self.assertTrue((output / entries["story.gtx.unity3d"]["object_path"]).is_file())
            self.assertEqual(manifest["reuse"]["text_scope"], "regenerated")
            self.assertTrue(manifest["reuse"]["every_key_resolved_exactly"])

    def test_a_locale_file_may_spell_the_bundle_differently_from_the_catalogue(self):
        # The real repository has this: a locale file names `CD_jp.gtx` where the
        # official catalogue says `cd_jp.gtx`.  The rows must stay attached to the
        # catalogue's spelling, or the build dies looking them up.
        index_rows = (("cd_jp.gtx.unity3d", "cd-remote.unity3d", "a" * 64),)
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base)
            path = root / "locales/story/story.gtx.jsonl"
            row = json.loads(path.read_text(encoding="utf-8").strip())
            row["bundle"] = "CD_jp.gtx"
            path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

            report, downloads, overlays = self._build_once(
                root, base / "work-1", base / "out", commit, index_rows=index_rows)
            self.assertEqual(report["status"], "success")
            self.assertEqual([Path(d).name for d in downloads if d.endswith(".unity3d")],
                             ["cd-remote.unity3d"])
            manifest = json.loads(
                (base / "out" / "1077640" / "manifest.json").read_text(encoding="utf-8"))
            entries = {e["logical_key"]: e for e in manifest["entries"]}
            self.assertIn("cd_jp.gtx.unity3d", entries)
            self.assertEqual(entries["cd_jp.gtx.unity3d"]["runtime_path"],
                             "production/2018/Android/cd-remote.unity3d")

    def test_only_the_changed_bundle_is_fetched_and_rewritten_again(self):
        index_rows = (("story.gtx.unity3d", "remote-hash.unity3d", "a" * 64),
                      ("other.gtx.unity3d", "other-remote.unity3d", "b" * 64))
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base, bundles=("story.gtx", "other.gtx"))
            output = base / "out"
            first, first_downloads, first_overlays = self._build_once(root, base / "work-1", output, commit,
                                                      index_rows=index_rows)
            self.assertEqual(first["reuse"]["mode"], "full")
            self.assertEqual(len([d for d in first_downloads if d.endswith(".unity3d")]), 2)

            row = json.loads((root / "locales/story/story.gtx.jsonl")
                             .read_text(encoding="utf-8").strip())
            row["zh"] = "新译文"
            (root / "locales/story/story.gtx.jsonl").write_text(
                json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

            second, downloads, second_overlays = self._build_once(
                root, base / "work-2", output, commit, index_rows=index_rows)
            self.assertEqual(second["reuse"]["mode"], "incremental")
            self.assertEqual(second["reuse"]["reused_bundles"], 1)
            self.assertEqual(second["reuse"]["rebuilt_bundles"], 1)
            # One download: the bundle whose translation moved.  The other one is
            # published from the store, not fetched and not rewritten.
            self.assertEqual([Path(d).name for d in downloads if d.endswith(".unity3d")],
                             ["remote-hash.unity3d"])
            manifest = json.loads(
                (output / "1077640" / "manifest.json").read_text(encoding="utf-8"))
            entries = {e["logical_key"]: e for e in manifest["entries"]}
            self.assertEqual(entries["other.gtx.unity3d"]["runtime_path"],
                             "production/2018/Android/other-remote.unity3d")

    def test_a_subset_that_answered_from_memory_falls_back_to_a_full_rebuild(self):
        index_rows = (("story.gtx.unity3d", "remote-hash.unity3d", "a" * 64),
                      ("other.gtx.unity3d", "other-remote.unity3d", "b" * 64))
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base, bundles=("story.gtx", "other.gtx"))
            output = base / "out"
            self._build_once(root, base / "work-1", output, commit, index_rows=index_rows)

            row = json.loads((root / "locales/story/story.gtx.jsonl")
                             .read_text(encoding="utf-8").strip())
            row["zh"] = "新译文"
            (root / "locales/story/story.gtx.jsonl").write_text(
                json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

            from assets_generated_index import GeneratedStore
            argv = [
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--work-root", str(base / "work-2"), "--output-root", str(output),
                "--source-commit", commit, "--translation-commit", commit,
                "--generated-commit", commit, "--ci-run-id", "12345",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            downloads: list[str] = []
            with mock.patch.object(build, "ROOT", root), \
                    mock.patch.object(build, "download",
                                      side_effect=self._downloader(downloads, index_rows)), \
                    mock.patch.object(build, "run_overlay",
                                      side_effect=self._overlay_answering_from_memory
                                      ) as overlay, \
                    mock.patch.object(build, "GeneratedStore",
                                      return_value=GeneratedStore(output)), \
                    mock.patch.object(build, "read_gtx",
                                      return_value=("name", "k^原文", b"cipher")), \
                    mock.patch.object(sys, "argv", ["build_generated_release.py", *argv]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(build.main(), 0)
            # The risky subset run happened, was rejected, and the whole surface
            # ran again -- in the same work directory, which the overlay would
            # refuse if the builder handed it the previous attempt's directory.
            self.assertEqual(overlay.call_count, 2)
            self.assertIn("reuse_fallback", stderr.getvalue())
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["reuse"]["mode"], "full")
            self.assertEqual(report["reuse"]["reused_bundles"], 0)
            self.assertEqual(report["reuse"]["text_scope"], "release")
            manifest = json.loads(
                (output / "1077640" / "manifest.json").read_text(encoding="utf-8"))
            entries = {e["logical_key"]: e for e in manifest["entries"]}
            self.assertEqual(entries["story.gtx.unity3d"]["runtime_path"],
                             "production/2018/Android/remote-hash.unity3d")
            self.assertEqual(entries["other.gtx.unity3d"]["runtime_path"],
                             "production/2018/Android/other-remote.unity3d")
            self.assertTrue(manifest["reuse"]["every_key_resolved_exactly"])

    def test_a_changed_builder_disables_reuse_entirely(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base)
            output = base / "out"
            self._build_once(root, base / "work-1", output, commit)

            (root / "scripts").mkdir(exist_ok=True)
            (root / "scripts" / "build_generated_release.py").write_text(
                "# a changed writer must regenerate every bundle\n", encoding="utf-8")

            second, downloads, second_overlays = self._build_once(root, base / "work-2", output, commit)
            self.assertEqual(second["reuse"]["mode"], "full")
            self.assertIn("outside locales/", second["reuse"]["reason"])
            self.assertTrue([d for d in downloads if d.endswith(".unity3d")])


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


class OfficialObjectCache(unittest.TestCase):
    """A new asset version must reuse official bytes that did not change.

    The CDN renames every object on every version, so a cache keyed by remote
    name never survives a version bump.  These tests pin the replacement: the
    cache is keyed on the catalogue's own fingerprint of the bytes.
    """

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._buffer = io.BytesIO(payload)

        def read(self, size: int = -1) -> bytes:
            return self._buffer.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    def test_reuses_cached_bytes_without_any_request(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "official-by-content"
            cache.mkdir()
            payload = b"official-bytes"
            (cache / "fingerprint-a").write_bytes(payload)
            destination = root / "archive" / "renamed-for-this-version.unity3d"
            with mock.patch.object(build, "urlopen", side_effect=_forbid):
                outcome = build.download("https://cdn/a", destination, len(payload),
                                         "fingerprint-a", cache)
            self.assertEqual(outcome, "reused")
            self.assertEqual(destination.read_bytes(), payload)

    def test_cache_entry_of_the_wrong_size_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "official-by-content"
            cache.mkdir()
            (cache / "fingerprint-a").write_bytes(b"stale-bytes")
            payload = b"fresh-official-bytes"
            destination = root / "archive" / "bundle.unity3d"
            with mock.patch.object(build, "urlopen",
                                   return_value=self._Response(payload)) as opener:
                outcome = build.download("https://cdn/a", destination, len(payload),
                                         "fingerprint-a", cache)
            self.assertEqual(outcome, "fetched")
            opener.assert_called_once()
            self.assertEqual(destination.read_bytes(), payload)

    def test_a_fetch_feeds_the_next_version(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "official-by-content"
            payload = b"official-bytes"
            destination = root / "archive" / "bundle.unity3d"
            with mock.patch.object(build, "urlopen", return_value=self._Response(payload)):
                self.assertEqual(
                    build.download("https://cdn/a", destination, len(payload),
                                   "fingerprint-a", cache),
                    "fetched")
            self.assertEqual((cache / "fingerprint-a").read_bytes(), payload)
            # Next version: same bytes, same fingerprint, different remote name.
            renamed = root / "archive-next" / "new-remote-name.unity3d"
            with mock.patch.object(build, "urlopen", side_effect=_forbid):
                self.assertEqual(
                    build.download("https://cdn/b", renamed, len(payload),
                                   "fingerprint-a", cache),
                    "reused")
            self.assertEqual(renamed.read_bytes(), payload)

    def test_without_a_fingerprint_nothing_is_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = b"official-bytes"
            destination = root / "archive" / "bundle.unity3d"
            with mock.patch.object(build, "urlopen",
                                   return_value=self._Response(payload)) as opener:
                self.assertEqual(build.download("https://cdn/a", destination, len(payload)),
                                 "fetched")
            opener.assert_called_once()
            self.assertFalse((root / "official-by-content").exists())

    def test_a_short_response_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "archive" / "bundle.unity3d"
            with mock.patch.object(build, "urlopen", return_value=self._Response(b"short")):
                with self.assertRaises(ValueError):
                    build.download("https://cdn/a", destination, 999,
                                   "fingerprint-a", root / "official-by-content")
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
