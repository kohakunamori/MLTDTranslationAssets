#!/usr/bin/env python3
"""Offline contract tests for the public Assets generated-release entry point."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import msgpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_generated_release as build


class RealPipelineEntrypointTests(unittest.TestCase):
    """Exercise the child CLI without inheriting pytest's import path."""

    def run_cli(self, script: str, *args: str, cwd: Path):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, "-P", str(build.ROOT / script), *args],
            cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=30,
        )

    def test_generated_overlay_cli_starts_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_cli(
                "pipelines/text/mltd_localization_pipeline.py", "--help", cwd=Path(raw),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("build-overlay", result.stdout)

    def test_version_identity_resolves_relative_index_from_repository_root(self):
        # Keep the fixture on the repository's drive on Windows.
        with tempfile.TemporaryDirectory(dir=build.ROOT) as raw:
            temp = Path(raw)
            index = temp / "index.data"
            index.write_bytes(b"source-bound-index")
            snapshot = temp / "snapshot.json"
            snapshot.write_text(json.dumps({
                "complete": True, "scope": "jp-android",
                "upstream_root": "https://td-assets.bn765.com/1077741/production/2018/Android",
                "asset_index": os.path.relpath(index, build.ROOT),
                "objects": [{"logical": "story.gtx.unity3d"}],
            }), encoding="utf-8")
            result = self.run_cli(
                "pipelines/text/localization_version_identity.py",
                "--snapshot", str(snapshot), "--client-version", "9.0.200",
                "--asset-version", "1077741", cwd=temp,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["asset_index_sha256"],
                         hashlib.sha256(b"source-bound-index").hexdigest())


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
        "source_bundle_sha256": hashlib.sha256(
            (Path(_archive) / "jp-android/remote-hash.unity3d").read_bytes()).hexdigest(),
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
                         "source_bundle_sha256": hashlib.sha256(
                             (Path(_archive) / "jp-android" / row["remote"]).read_bytes()).hexdigest(),
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

        A memory route is legitimate in a subset: its ledger still contains
        all unchanged bundles, so it must not force a second full overlay pass.
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
                         "source_bundle_sha256": hashlib.sha256(
                             (Path(_archive) / "jp-android" / row["remote"]).read_bytes()).hexdigest(),
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
                    logical: [content_hash, remote, len(logical.encode("utf-8"))]
                    for logical, remote, content_hash in index_rows}], use_bin_type=True))
                return "fetched"
            logical = next(name for name, remote, _hash in index_rows if remote == destination.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(logical.encode("utf-8"))
            return "fetched"

        return counted_download

    def _build_once(self, root: Path, work: Path, output: Path, commit: str,
                    index_rows=((("story.gtx.unity3d"), "remote-hash.unity3d", "a" * 64),),
                    lyric_writer=None, force=False):
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
        lyric_context = (mock.patch.object(build.build_lyric_overlay, "save_localized_bundle",
                                          side_effect=lyric_writer)
                         if lyric_writer else contextlib.nullcontext())
        if force:
            argv.append("--no-reuse")

        with mock.patch.object(build, "ROOT", root), \
                mock.patch.object(build, "download", side_effect=counted_download), \
                mock.patch.object(build, "run_overlay",
                                  side_effect=self._overlay_from_snapshot) as overlay, \
                mock.patch.object(build, "GeneratedStore",
                                  return_value=GeneratedStore(output)), \
                mock.patch.object(build, "read_gtx", side_effect=lambda source: (
                    next(name[:-len(".unity3d")] for name, remote, _hash in index_rows
                         if remote == Path(source).name), "k^原文", b"cipher")), \
                mock.patch.object(sys, "argv", ["build_generated_release.py", *argv]), \
                contextlib.redirect_stdout(stdout), lyric_context:
            self.assertEqual(build.main(), 0)
        return json.loads(stdout.getvalue()), downloads, overlay.call_count

    def _layout(self, base: Path, bundles=("story.gtx",)):
        root = base / "repo"
        _write_synthetic_root(root, images_complete=False)
        for name in bundles[1:]:
            source = "原文"
            (root / "locales" / "story" / f"{name}.jsonl").write_text(json.dumps({
                "asset_version": "1077640", "client_version": None,
                "source_client_version": "9.0.200", "bundle": name,
                "item_key": "k",
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "ja": source, "zh": "译文", "status": "accepted",
            }, ensure_ascii=False) + "\n", encoding="utf-8")
        # Package fingerprints read the actual inputs; Git history is no longer
        # part of planning. Publication still needs a syntactically valid input
        # commit, just like the other synthetic store fixtures in this module.
        commit = "f" * 40
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
            self.assertEqual(manifest["reuse"]["text_scope"], "release")
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

    def test_the_published_catalogue_declares_the_bytes_the_release_serves(self):
        """Every released object, not only the ones this run rewrote.

        The client requires the bytes it downloads to have the length the
        catalogue declares.  An incremental build publishes one rebuilt bundle
        and carries the other one over from the store, so this pins both halves of
        the entry list: aligning only the bundles a surface produced would leave
        every reused bundle contradicting the catalogue.
        """
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

            report, _downloads, overlays = self._build_once(
                root, base / "work-2", output, commit, index_rows=index_rows)
            self.assertEqual(overlays, 1)
            self.assertEqual(report["reuse"]["reused_bundles"], 1)
            self.assertEqual(report["index_alignment"],
                             {"published_index": str(base / "work-2" / "catalogue" / "index.data"),
                              "published_index_sha256":
                                  build.sha256_file(base / "work-2" / "catalogue" / "index.data"),
                              "aligned_from": str(base / "work-2" / "index.data"),
                              "catalogue_records": 2, "covered_records": 2,
                              "changed_records": 2, "uncovered_records": 0})

            manifest = json.loads(
                (output / "1077640" / "manifest.json").read_text(encoding="utf-8"))
            entries = {e["logical_key"]: e for e in manifest["entries"]}
            catalog_entry = entries["__official_asset_index__"]
            pristine = base / "work-2" / "index.data"
            self.assertEqual(catalog_entry["source_sha256"], build.sha256_file(pristine))
            self.assertEqual(catalog_entry["translated_sha256"],
                             catalog_entry["artifact_sha256"])
            self.assertNotEqual(catalog_entry["translated_sha256"],
                                catalog_entry["source_sha256"])
            self.assertEqual(catalog_entry["translation_status"], "modified")

            # The object the gateway serves at the client's catalogue path is the
            # aligned copy: same records, sizes from the released bytes.
            catalog = msgpack.unpackb(
                (output / catalog_entry["object_path"]).read_bytes(),
                raw=False, strict_map_key=False)[0]
            self.assertEqual(list(catalog), ["story.gtx.unity3d", "other.gtx.unity3d"])
            for logical, remote in (("story.gtx.unity3d", "remote-hash.unity3d"),
                                    ("other.gtx.unity3d", "other-remote.unity3d")):
                entry = entries[logical]
                self.assertEqual(catalog[logical][1], remote)
                self.assertEqual(catalog[logical][2],
                                 (output / entry["object_path"]).stat().st_size)
                # The fixture's official object is the logical name's length, which
                # no translated bundle of this release has.
                self.assertNotEqual(catalog[logical][2], len(logical))

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

    def test_a_subset_uses_the_complete_ledger_without_full_rebuild(self):
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
            # A memory route no longer forces a second overlay pass: unchanged
            # bundles remain in the complete source-bound resolver ledger.
            self.assertEqual(overlay.call_count, 1)
            self.assertNotIn("reuse_fallback", stderr.getvalue())
            ledger_rows = [json.loads(line) for line in
                           (base / "work-2/all-translations.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual({r["bundle"] for r in ledger_rows}, {"story.gtx", "other.gtx"})
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["reuse"]["mode"], "incremental")
            self.assertEqual(report["reuse"]["reused_bundles"], 1)
            self.assertEqual(report["reuse"]["text_scope"], "regenerated")
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
            self.assertIn("per-bundle", second["reuse"]["reason"])
            self.assertTrue([d for d in downloads if d.endswith(".unity3d")])


    def test_one_row_in_a_shared_file_rebuilds_one_bundle(self):
        index_rows = (("story.gtx.unity3d", "a.unity3d", "a" * 64),
                      ("other.gtx.unity3d", "b.unity3d", "b" * 64))
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base, bundles=("story.gtx", "other.gtx"))
            paths = sorted((root / "locales/story").glob("*.jsonl"))
            shared = root / "locales/story/shared.jsonl"
            shared.write_text("".join(p.read_text(encoding="utf-8") for p in paths), encoding="utf-8")
            for p in paths:
                p.unlink()
            self._build_once(root, base / "work-1", base / "out", commit, index_rows=index_rows)
            rows = [json.loads(line) for line in shared.read_text(encoding="utf-8").splitlines()]
            rows[0]["zh"] = "修改单个包"
            shared.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                              encoding="utf-8")
            report, downloads, overlays = self._build_once(
                root, base / "work-2", base / "out", commit, index_rows=index_rows)
            self.assertEqual(report["reuse"]["rebuilt_bundles"], 1)
            self.assertEqual(report["reuse"]["reused_bundles"], 1)
            self.assertEqual(overlays, 1)
            self.assertEqual(len([p for p in downloads if p.endswith(".unity3d")]), 1)

    def test_version_rename_only_republishes_existing_objects(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base)
            self._build_once(root, base / "work-1", base / "out", commit)
            path = root / "manifests/asset-version.json"
            version = json.loads(path.read_text(encoding="utf-8"))
            version["asset_version"] = 1077650
            path.write_text(json.dumps(version), encoding="utf-8")
            report, downloads, overlays = self._build_once(
                root, base / "work-2", base / "out", commit,
                index_rows=(("story.gtx.unity3d", "new-remote.unity3d", "a" * 64),))
            self.assertEqual(report["reuse"]["reused_bundles"], 1)
            self.assertEqual(overlays, 0)
            self.assertFalse([p for p in downloads if p.endswith(".unity3d")])
            manifest = json.loads((base / "out/1077650/manifest.json").read_text(encoding="utf-8"))
            entry = next(e for e in manifest["entries"] if e["resource_kind"] == "bundle")
            self.assertEqual(entry["runtime_path"], "production/2018/Android/new-remote.unity3d")
            self.assertEqual(entry["asset_version"], "1077650")

    def test_only_edited_song_is_built_and_forced_build_matches(self):
        index_rows = (("story.gtx.unity3d", "text.unity3d", "a" * 64),
                      ("scrobj_a.unity3d", "song-a.unity3d", "b" * 64),
                      ("scrobj_b.unity3d", "song-b.unity3d", "c" * 64))
        def lyric_writer(source, output, translations):
            output.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(sorted(translations.by_position.values())).encode()
            output.write_bytes(payload)
            return {"changed": len(translations), "output_bytes": len(payload),
                    "source_bundle_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "output_plain_sha256": hashlib.sha256(payload).hexdigest(),
                    "slots_total": len(translations), "unmatched_texts": []}
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base)
            songs = root / "lyrics/songs"
            songs.mkdir(parents=True)
            for name in ("scrobj_a.unity3d", "scrobj_b.unity3d"):
                (songs / f"{name}.jsonl").write_text(json.dumps({
                    "index": 0, "ja": "歌詞", "zh": "歌词", "status": "accepted",
                    "source_sha256": hashlib.sha256("歌詞".encode()).hexdigest(),
                }, ensure_ascii=False) + "\n", encoding="utf-8")
            self._build_once(root, base / "work-1", base / "out", commit,
                             index_rows=index_rows, lyric_writer=lyric_writer)
            edited = songs / "scrobj_a.unity3d.jsonl"
            row = json.loads(edited.read_text(encoding="utf-8"))
            row["zh"] = "新歌词"
            edited.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            report, downloads, overlays = self._build_once(
                root, base / "work-2", base / "out", commit,
                index_rows=index_rows, lyric_writer=lyric_writer)
            self.assertEqual(overlays, 0)
            self.assertEqual(report["reuse"]["lyric_rebuilt_bundles"], 1)
            self.assertEqual(report["reuse"]["lyric_reused_bundles"], 1)
            self.assertEqual([Path(p).name for p in downloads if p.endswith(".unity3d")],
                             ["song-a.unity3d"])
            def outputs():
                manifest = json.loads((base / "out/1077640/manifest.json").read_text(encoding="utf-8"))
                return {e["logical_key"]: e["artifact_sha256"] for e in manifest["entries"]}
            incremental_outputs = outputs()
            self._build_once(root, base / "work-3", base / "out", commit,
                             index_rows=index_rows, lyric_writer=lyric_writer, force=True)
            self.assertEqual(outputs(), incremental_outputs)

    def test_removing_the_last_accepted_text_removes_the_old_overlay(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root, commit = self._layout(base)
            self._build_once(root, base / "work-1", base / "out", commit)
            path = root / "locales/story/story.gtx.jsonl"
            row = json.loads(path.read_text(encoding="utf-8"))
            row["status"] = "pending"
            path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            report, downloads, overlays = self._build_once(root, base / "work-2", base / "out", commit)
            self.assertEqual(overlays, 0)
            self.assertEqual(report["generated_entries"], 1)  # Official catalogue only.
            self.assertFalse([p for p in downloads if p.endswith(".unity3d")])


class GeneratedReleaseContracts(unittest.TestCase):
    def test_streamed_catalogue_rejects_invalid_shape_truncation_and_trailing_bytes(self):
        good = msgpack.packb([{'foo': ['catalog', 'remote.unity3d', 12]}], use_bin_type=True)
        invalid = [msgpack.packb({}), msgpack.packb([{}, {}]), msgpack.packb([{}]),
                   msgpack.packb([{'foo': ['short']}]), good[:-1], good + b'\x00']
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'index.data'
            for payload in invalid:
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with self.assertRaises(ValueError):
                        build.load_official_index(path)

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

    def test_new_asset_version_uses_newest_historical_translation(self):
        # A new official release may reuse the source without adding a row for
        # that exact version. Earlier translations can intentionally differ.
        rows = [
            {"key": "ld_igp_unit_name_239", "source": "同一原文",
             "translation": "较新译文", "asset_version": "1077790"},
            {"key": "ld_igp_unit_name_239", "source": "同一原文",
             "translation": "早期译文", "asset_version": "1077500"},
            {"key": "ld_igp_unit_name_239", "source": "其他原文",
             "translation": "不匹配", "asset_version": "1077800"},
        ]
        for candidates in (rows, list(reversed(rows))):
            chosen = build.select_rows_for_current_sources(
                candidates, {"ld_igp_unit_name_239": "同一原文"}, "1077800"
            )
            self.assertEqual([row["translation"] for row in chosen], ["较新译文"])

    def test_exact_version_wins_regardless_of_candidate_order(self):
        rows = [
            {"key": "k", "source": "原文", "translation": "当前译", "asset_version": "1077800"},
            {"key": "k", "source": "原文", "translation": "旧版译", "asset_version": "1077790"},
        ]
        for candidates in (rows, list(reversed(rows))):
            chosen = build.select_rows_for_current_sources(
                candidates, {"k": "原文"}, "1077800"
            )
            self.assertEqual(chosen[0]["translation"], "当前译")

    def test_selected_historical_version_conflict_is_rejected(self):
        rows = [
            {"key": "k", "source": "原文", "translation": "甲", "asset_version": "1077790"},
            {"key": "k", "source": "原文", "translation": "乙", "asset_version": "1077790"},
            {"key": "k", "source": "原文", "translation": "更旧", "asset_version": "1077500"},
        ]
        with self.assertRaisesRegex(ValueError, "conflicting current-source translation for k"):
            build.select_rows_for_current_sources(rows, {"k": "原文"}, "1077800")

    def test_future_version_is_only_a_fallback(self):
        rows = [
            {"key": "k", "source": "原文", "translation": "未来", "asset_version": "1077900"},
            {"key": "k", "source": "原文", "translation": "旧版", "asset_version": "1077790"},
        ]
        chosen = build.select_rows_for_current_sources(rows, {"k": "原文"}, "1077800")
        self.assertEqual(chosen[0]["translation"], "旧版")
        chosen = build.select_rows_for_current_sources(rows[:1], {"k": "原文"}, "1077800")
        self.assertEqual(chosen[0]["translation"], "未来")

    def test_same_asset_version_conflict_is_rejected(self):
        rows = [
            {"key": "k", "source": "同一原文", "translation": "甲", "asset_version": "1077640"},
            {"key": "k", "source": "同一原文", "translation": "乙", "asset_version": "1077640"},
        ]
        with self.assertRaises(ValueError):
            build.select_rows_for_current_sources(rows, {"k": "同一原文"}, "1077640")

    def test_generated_entries_keep_client_runtime_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            overlay = root / "overlay" / "jp-android"
            overlay.mkdir(parents=True)
            bundle = overlay / "remote-hash.unity3d"
            bundle.write_bytes(b"translated")
            manifest = root / "localization-manifest.json"
            manifest.write_text(json.dumps({"bundles": [{
                "logical": "story.gtx.unity3d",
                "remote": "remote-hash.unity3d",
                "source_bundle_sha256": "a" * 64,
                "output_plain_sha256": "b" * 64,
            }]}), encoding="utf-8")
            entries = build.build_entries(root / "overlay", manifest, "1077640", "9.0.200")
            translated = next(e for e in entries if e["logical_key"] == "story.gtx.unity3d")
            self.assertEqual(translated["runtime_path"],
                             "production/2018/Android/remote-hash.unity3d")
            # The catalogue entry is deliberately not this function's business: it
            # can only be built once every surface's bundles are known, so the
            # caller appends it (see IndexSizeAlignment).
            self.assertEqual(
                [e for e in entries if e["logical_key"] == "__official_asset_index__"], [])


def _catalogue(rows: dict) -> bytes:
    """One official-style msgpack catalogue document."""
    return msgpack.packb([rows], use_bin_type=True)


class IndexSizeAlignment(unittest.TestCase):
    """The published catalogue must describe the bytes the release serves.

    The client requires ``Content-Length == declared_size`` for every object the
    catalogue names, and this release serves rewritten bytes for every bundle it
    covers, so a pristine catalogue makes the client reject its own download.
    These tests pin the replacement: a copy that differs from the official one
    exactly where the release does, and nowhere else.
    """

    def _layout(self, root: Path) -> tuple[Path, list[dict]]:
        """A four-record catalogue, three of its records served by this release.

        ``shared``/``alias`` name the same remote, which is the case a
        name-addressed client cannot tell apart; ``untouched`` is not published.
        """
        index = root / "index.data"
        index.write_bytes(_catalogue({
            "story.gtx.unity3d": ["catalog-a", "remote-a.unity3d", 12],
            "shared.gtx.unity3d": ["catalog-b", "remote-b.unity3d", 99],
            "alias.gtx.unity3d": ["catalog-b", "remote-b.unity3d", 99],
            "untouched.gtx.unity3d": ["catalog-c", "remote-c.unity3d", 7],
        }))
        overlay = root / "overlay"
        (overlay / "jp-android").mkdir(parents=True)
        (overlay / "jp-android" / "remote-a.unity3d").write_bytes(b"translated-a")
        (overlay / "jp-android" / "remote-b.unity3d").write_bytes(b"translated-bb")
        (overlay / "localization-manifest.json").write_text(json.dumps({"bundles": [
            {"logical": "story.gtx.unity3d", "remote": "remote-a.unity3d",
             "source_bundle_sha256": "a" * 64, "output_plain_sha256": "b" * 64},
            {"logical": "shared.gtx.unity3d", "remote": "remote-b.unity3d",
             "source_bundle_sha256": "c" * 64, "output_plain_sha256": "d" * 64},
        ]}), encoding="utf-8")
        entries = build.build_entries(overlay, overlay / "localization-manifest.json",
                                      "1077640", "9.0.200")
        return index, entries

    def test_only_covered_records_move_and_the_pristine_file_survives(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, entries = self._layout(root)
            pristine = index.read_bytes()
            out = root / "catalogue" / "index.data"

            aligned = build.align_index_sizes(index, entries, output_path=out)

            self.assertEqual(index.read_bytes(), pristine, "the pristine index was written to")
            self.assertEqual(build.sha256_file(index),
                             hashlib.sha256(pristine).hexdigest())
            table = msgpack.unpackb(out.read_bytes(), raw=False, strict_map_key=False)[0]
            self.assertEqual(table, {
                # published bytes are 12 long, so this record keeps its size
                "story.gtx.unity3d": ["catalog-a", "remote-a.unity3d", 12],
                # both records naming the served remote move together
                "shared.gtx.unity3d": ["catalog-b", "remote-b.unity3d", 13],
                "alias.gtx.unity3d": ["catalog-b", "remote-b.unity3d", 13],
                "untouched.gtx.unity3d": ["catalog-c", "remote-c.unity3d", 7],
            })
            # Same records in the same order: the client looks names up, and a
            # reordering would only make the two catalogues harder to compare.
            self.assertEqual(list(table), ["story.gtx.unity3d", "shared.gtx.unity3d",
                                           "alias.gtx.unity3d", "untouched.gtx.unity3d"])
            self.assertEqual((aligned.records, aligned.covered_records,
                              aligned.changed_records, aligned.uncovered_records,
                              aligned.unchanged_records), (4, 3, 2, 1, 2))
            self.assertEqual(aligned.path, out)
            self.assertEqual(aligned.sha256, build.sha256_file(out))

    def test_the_manifest_entry_describes_both_files_truthfully(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, entries = self._layout(root)
            out = root / "catalogue" / "index.data"
            aligned = build.align_index_sizes(index, entries, output_path=out)

            entry = build.index_entry(aligned, index_name="index.data",
                                      pristine_sha256=build.sha256_file(index),
                                      asset_version="1077640",
                                      source_client_version="9.0.200")

            self.assertEqual(entry["runtime_path"], "production/2018/Android/index.data")
            self.assertEqual(entry["logical_path"], entry["runtime_path"])
            self.assertEqual(entry["resource_kind"], "other")
            self.assertEqual(entry["reuse_status"], "exact")
            # The published bytes are no longer the official ones, so the entry
            # may not claim the catalogue was reused as it was.
            self.assertEqual(entry["translation_status"], "modified")
            self.assertEqual(entry["source_sha256"], build.sha256_file(index))
            self.assertEqual(entry["translated_sha256"], build.sha256_file(out))
            self.assertEqual(entry["artifact_file"], str(out))
            self.assertNotEqual(entry["source_sha256"], entry["translated_sha256"])

            # The store, which is what the gateway reads, derives the digests from
            # the bytes of artifact_file: the manifest has to agree with the file,
            # or the gateway would index a catalogue nobody serves.
            store = root / "store"
            result = build.GeneratedStore(store).build_release(
                "1077640", [*entries, entry], source_client_version="9.0.200",
                source_commit="a" * 40, translation_commit="b" * 40,
                generated_commit="c" * 40, ci_run_id="12345", entries_base=root)
            self.assertTrue(result.written)
            manifest = json.loads(
                (store / "1077640" / "manifest.json").read_text(encoding="utf-8"))
            stored = next(e for e in manifest["entries"]
                          if e["logical_key"] == "__official_asset_index__")
            self.assertEqual(stored["artifact_sha256"], aligned.sha256)
            self.assertEqual(stored["translated_sha256"], aligned.sha256)
            self.assertEqual(stored["source_sha256"], build.sha256_file(index))
            self.assertEqual((store / stored["object_path"]).read_bytes(), out.read_bytes())
            # The release gate must still accept the release as a whole: a
            # modified catalogue entry is admissible, a mislabelled one is not.
            report = build.GeneratedStore(store).verify_release("1077640")
            self.assertTrue(report.ok, report.failures)
            self.assertEqual(report.failures, [])

    def test_a_remote_name_shared_by_two_records_moves_both(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index = root / "index.data"
            index.write_bytes(_catalogue({
                "first.gtx.unity3d": ["catalog", "shared-remote.unity3d", 2],
                "second.gtx.unity3d": ["catalog", "shared-remote.unity3d", 3],
            }))
            artifact = root / "shared-remote.unity3d"
            artifact.write_bytes(b"published-bytes")
            out = root / "catalogue" / "index.data"

            aligned = build.align_index_sizes(
                index, [{"logical_key": "first.gtx.unity3d", "artifact_file": str(artifact),
                         "runtime_path": "production/2018/Android/shared-remote.unity3d"}],
                output_path=out)

            table = msgpack.unpackb(out.read_bytes(), raw=False, strict_map_key=False)[0]
            self.assertEqual(table["first.gtx.unity3d"][2], 15)
            self.assertEqual(table["second.gtx.unity3d"][2], 15)
            self.assertEqual((aligned.covered_records, aligned.changed_records), (2, 2))

    def test_a_reused_entry_is_sized_from_the_stored_object(self):
        """A reused bundle has no overlay file; its bytes are the store's object."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index = root / "index.data"
            index.write_bytes(_catalogue({
                "story.gtx.unity3d": ["catalog", "remote-a.unity3d", 3],
            }))
            pool = root / "objects" / "sha256"
            pool.mkdir(parents=True)
            digest = hashlib.sha256(b"a-published-bundle").hexdigest()
            (pool / digest).write_bytes(b"a-published-bundle")
            out = root / "catalogue" / "index.data"

            aligned = build.align_index_sizes(
                index, [{"logical_key": "story.gtx.unity3d", "artifact_sha256": digest,
                         "runtime_path": "production/2018/Android/remote-a.unity3d"}],
                output_path=out,
                object_file=lambda wanted: (pool / wanted) if (pool / wanted).is_file() else None)

            table = msgpack.unpackb(out.read_bytes(), raw=False, strict_map_key=False)[0]
            self.assertEqual(table["story.gtx.unity3d"][2], len(b"a-published-bundle"))
            self.assertEqual((aligned.covered_records, aligned.changed_records), (1, 1))

    def test_a_released_bundle_no_record_names_is_refused(self):
        """The catalogue cannot be aligned with a bundle it does not describe."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index = root / "index.data"
            index.write_bytes(_catalogue({
                "story.gtx.unity3d": ["catalog", "remote-a.unity3d", 3],
            }))
            with self.assertRaises(ValueError) as caught:
                build.align_index_sizes(
                    index, [{"logical_key": "ghost.gtx.unity3d",
                             "artifact_sha256": "a" * 64,
                             "runtime_path": "production/2018/Android/ghost-remote.unity3d"}],
                    output_path=root / "catalogue" / "index.data")
            self.assertIn("ghost-remote.unity3d", str(caught.exception))

    def test_an_entry_whose_published_length_is_unknown_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index = root / "index.data"
            index.write_bytes(_catalogue({
                "story.gtx.unity3d": ["catalog", "remote-a.unity3d", 3],
            }))
            with self.assertRaises(ValueError) as caught:
                build.align_index_sizes(
                    index, [{"logical_key": "story.gtx.unity3d",
                             "runtime_path": "production/2018/Android/remote-a.unity3d"}],
                    output_path=root / "catalogue" / "index.data")
            self.assertIn("published length is unknown", str(caught.exception))

    def test_the_aligned_copy_never_replaces_the_file_it_read(self):
        """The pristine download is the release's own reuse key; it must survive."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, entries = self._layout(root)
            pristine = index.read_bytes()
            with self.assertRaises(ValueError) as caught:
                build.align_index_sizes(index, entries, output_path=index)
            self.assertIn("must survive", str(caught.exception))
            self.assertEqual(index.read_bytes(), pristine)

    def test_an_entry_runtime_name_must_be_the_official_index_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, entries = self._layout(root)
            aligned = build.align_index_sizes(
                index, entries, output_path=root / "catalogue" / "renamed.data")
            with self.assertRaises(ValueError) as caught:
                build.index_entry(aligned, index_name="index.data",
                                  pristine_sha256=build.sha256_file(index),
                                  asset_version="1077640", source_client_version="9.0.200")
            self.assertIn("renamed.data", str(caught.exception))


class ImageSurfaceAlignment(unittest.TestCase):
    """The image surface's bundles reach the client through the same one catalogue.

    The image overlay patches bundles the text surface never touches, and both
    surfaces change declared sizes, so a release can only carry one table.  This
    pins that the published table hold both patches -- and that the image bundles
    are covered by the alignment like any other released object, which is what a
    per-surface alignment would have missed.
    """

    INDEX_ROWS = (("story.gtx.unity3d", "remote-hash.unity3d", "a" * 64),
                  ("icon_tex.gtx.unity3d", "image-remote.unity3d", "b" * 64))
    IMAGE_BYTES = b"image-bytes"
    VERSION = "1077640"

    def _repo(self, base: Path) -> Path:
        """A synthetic repository with a reviewed image overlay for one bundle."""
        root = base / "repo"
        _write_synthetic_root(root, images_complete=False)
        overlay = root / "image-overlay" / self.VERSION
        artifacts = overlay / "jp-android"
        artifacts.mkdir(parents=True)
        (artifacts / "image-remote.unity3d").write_bytes(self.IMAGE_BYTES)
        (overlay / "overlay-manifest.json").write_text(json.dumps({"records": [{
            "logical": "icon_tex.gtx.unity3d", "remote": "image-remote.unity3d",
            "original_sha256": "c" * 64,
            "translated_sha256": hashlib.sha256(self.IMAGE_BYTES).hexdigest(),
            "original_bytes": len("icon_tex.gtx.unity3d"),
            "translated_bytes": len(self.IMAGE_BYTES),
        }]}), encoding="utf-8")
        # The catalogue shipped with the overlay: the official sizes plus the
        # image patch, which is the shape merge_image_overlay expects.
        (artifacts / "index.data").write_bytes(_catalogue({
            logical: [content_hash, remote,
                      len(self.IMAGE_BYTES) if remote == "image-remote.unity3d" else len(logical)]
            for logical, remote, content_hash in self.INDEX_ROWS}))
        return root

    def _download(self, url, destination, declared_size, content_key=None, cache_root=None):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix == ".data":
            destination.write_bytes(_catalogue({
                logical: [content_hash, remote, len(logical)]
                for logical, remote, content_hash in self.INDEX_ROWS}))
        else:
            destination.write_bytes(b"official-bytes")
        return "fetched"

    def _overlay(self, snapshot, _archive, _ledger, output, *_args, **_kwargs):
        requested = json.loads(Path(snapshot).read_text(encoding="utf-8"))["objects"]
        Path(output).mkdir(parents=True, exist_ok=True)
        rows = []
        for row in requested:
            target = Path(output) / "jp-android" / row["remote"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"translated:" + row["logical"].encode("utf-8"))
            rows.append({"logical": row["logical"], "remote": row["remote"],
                         "source_bundle_sha256": hashlib.sha256(
                             (Path(_archive) / "jp-android" / row["remote"]).read_bytes()).hexdigest(),
                         "output_plain_sha256": "e" * 64})
        (Path(output) / "localization-manifest.json").write_text(
            json.dumps({"bundles": rows}), encoding="utf-8")

    def test_both_surfaces_sizes_land_in_the_published_catalogue(self):
        from assets_generated_index import GeneratedStore
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = self._repo(base)
            work, output = base / "work", base / "out"
            overlay = root / "image-overlay" / self.VERSION
            argv = [
                "--version-manifest", str(root / "manifests" / "asset-version.json"),
                "--work-root", str(work), "--output-root", str(output),
                "--source-commit", "a" * 40, "--translation-commit", "b" * 40,
                "--generated-commit", "c" * 40, "--ci-run-id", "12345",
                "--image-overlay-manifest", str(overlay / "overlay-manifest.json"),
                "--image-overlay-root", str(overlay / "jp-android"),
                "--image-index", str(overlay / "jp-android" / "index.data"),
            ]
            stdout = io.StringIO()
            with mock.patch.object(build, "ROOT", root), \
                    mock.patch.object(build, "download", side_effect=self._download), \
                    mock.patch.object(build, "run_overlay", side_effect=self._overlay), \
                    mock.patch.object(build, "GeneratedStore",
                                      return_value=GeneratedStore(output)), \
                    mock.patch.object(build, "read_gtx",
                                      return_value=("name", "k^原文", b"cipher")), \
                    mock.patch.object(sys, "argv", ["build_generated_release.py", *argv]), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(build.main(), 0)
            # The image surface reports itself on one line, then the build report.
            lines = stdout.getvalue().splitlines()
            image_report = json.loads(lines[0])
            self.assertEqual(image_report["image_surface"], "merged_into_release")
            self.assertEqual(image_report["image_bundles"], 1)
            report = json.loads("\n".join(lines[1:]))
            self.assertEqual(report["image_surface"], "published_1_bundles")
            self.assertEqual(report["index_alignment"],
                             {"published_index": str(work / "catalogue" / "index.data"),
                              "published_index_sha256":
                                  build.sha256_file(work / "catalogue" / "index.data"),
                              # The image patch was already in the table this build
                              # aligned, so only the text record's size moved here.
                              "aligned_from": str(work / "merged-index.data"),
                              "catalogue_records": 2, "covered_records": 2,
                              "changed_records": 1, "uncovered_records": 0})

            manifest = json.loads(
                (output / self.VERSION / "manifest.json").read_text(encoding="utf-8"))
            entries = {e["logical_key"]: e for e in manifest["entries"]}
            self.assertEqual(len(entries), 3)
            catalog_entry = entries["__official_asset_index__"]
            self.assertEqual(catalog_entry["source_sha256"],
                             build.sha256_file(work / "index.data"))
            catalog = msgpack.unpackb(
                (output / catalog_entry["object_path"]).read_bytes(),
                raw=False, strict_map_key=False)[0]
            for logical, remote in (("story.gtx.unity3d", "remote-hash.unity3d"),
                                    ("icon_tex.gtx.unity3d", "image-remote.unity3d")):
                self.assertEqual(catalog[logical][1], remote)
                self.assertEqual(catalog[logical][2],
                                 (output / entries[logical]["object_path"]).stat().st_size)
            # The text patch is in this table because of the alignment: the merged
            # catalogue the image surface produces carries only its own patch.
            self.assertEqual(catalog["story.gtx.unity3d"][2],
                             len(b"translated:story.gtx.unity3d"))
            self.assertEqual(catalog["icon_tex.gtx.unity3d"][2], len(self.IMAGE_BYTES))


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
