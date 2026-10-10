#!/usr/bin/env python3
"""Offline contract tests for the incremental reuse decision.

The decision is the only thing standing between "CI rebuilds one changed bundle"
and "CI publishes something a full build would not have produced", so every test
here is about a condition that must *refuse* reuse as much as about the one that
allows it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_reuse as reuse


def index_row(remote: str, size: int = 10) -> dict:
    return {"catalog_hash": "c" * 40, "remote": remote, "declared_size": size}


def previous_entry(logical: str, remote: str, digest: str = "a" * 64) -> dict:
    return {
        "logical_key": logical,
        "logical_path": f"production/2018/Android/{logical}",
        "runtime_path": f"production/2018/Android/{remote}",
        "resource_kind": "bundle",
        "reuse_status": "exact",
        "translation_status": "modified",
        "source_sha256": "b" * 64,
        "translated_sha256": "c" * 64,
        "artifact_sha256": digest,
        "object_path": f"objects/sha256/{digest}",
    }


def reuse_record(*, exact: int = 10, memory: int = 0, stale_exact: int = 0,
                 unresolved: int = 0, index_sha256: str = "d" * 64) -> dict:
    """The inner ``reuse`` block a previous release would carry."""
    return reuse.reuse_block(
        index_sha256=index_sha256,
        resolution=reuse.Resolution(exact=exact, memory=memory,
                                    stale_exact=stale_exact, unresolved=unresolved),
        scope="release")[reuse.REUSE_KEY]


class ReuseFixture(unittest.TestCase):
    """A git checkout whose release inputs can be moved one at a time."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._init_repo()
        for path, body in (
            ("manifests/asset-version.json", '{"asset_version": 1077640}\n'),
            ("locales/master/a.gtx.jsonl",
             '{"bundle":"a.gtx","item_key":"k","zh":"A"}\n'),
            ("locales/master/b.gtx.jsonl",
             '{"bundle":"b.gtx","item_key":"k","zh":"B"}\n'),
            ("scripts/build_generated_release.py", "# builder\n"),
            ("pipelines/text/writer.py", "WRITER = 1\n"),
        ):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "inputs")
        self.built_from = self._git("rev-parse", "HEAD").stdout.strip()
        self.index = {"a.gtx.unity3d": index_row("aaa.unity3d"),
                      "b.gtx.unity3d": index_row("bbb.unity3d")}
        self.sources = {"a.gtx.unity3d": {"locales/master/a.gtx.jsonl"},
                        "b.gtx.unity3d": {"locales/master/b.gtx.jsonl"}}
        self.objects = {"a" * 64: self.root / "objects" / ("a" * 64),
                        "e" * 64: self.root / "objects" / ("e" * 64)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=True)

    def _init_repo(self) -> None:
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "reuse tests")
        # A developer's global signing configuration must not decide whether
        # these tests can commit.
        self._git("config", "commit.gpgsign", "false")

    def _object_file(self, digest: str) -> Path | None:
        return self.objects.get(digest)

    def _manifest(self, *, entries=None, block=None, commit: str | None = None) -> dict:
        return {
            "kind": "mltd-assets-generated-manifest",
            "asset_version": "1077640",
            "build_status": "success",
            "translation_commit": commit or self.built_from,
            reuse.REUSE_KEY: reuse_record() if block is None else block,
            "entries": entries if entries is not None else [
                previous_entry("a.gtx.unity3d", "aaa.unity3d"),
                previous_entry("b.gtx.unity3d", "bbb.unity3d"),
            ],
        }

    _DEFAULT = object()

    def _plan(self, manifest=_DEFAULT, *, index=None, sources=None, index_sha256="d" * 64):
        return reuse.plan_reuse(
            root=self.root,
            manifest=self._manifest() if manifest is self._DEFAULT else manifest,
            asset_version="1077640",
            index=self.index if index is None else index,
            bundle_sources=self.sources if sources is None else sources,
            index_sha256=index_sha256,
            object_file=self._object_file)

    # -- the happy path ----------------------------------------------------- #


class ReusePlanTests(ReuseFixture):
    def test_unchanged_inputs_reuse_every_bundle(self):
        plan = self._plan()
        self.assertEqual(sorted(plan.reusable), ["a.gtx.unity3d", "b.gtx.unity3d"])
        self.assertEqual(plan.rebuild, {})
        self.assertTrue(plan.incremental)
        self.assertEqual(plan.reason, "incremental")

    def test_a_changed_locale_file_rebuilds_only_its_bundle(self):
        (self.root / "locales/master/a.gtx.jsonl").write_text(
            '{"bundle":"a.gtx","item_key":"k","zh":"A2"}\n', encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "translate a")
        plan = self._plan()
        self.assertEqual(sorted(plan.rebuild), ["a.gtx.unity3d"])
        self.assertEqual(sorted(plan.reusable), ["b.gtx.unity3d"])
        self.assertEqual(plan.changed_locale_files, ("locales/master/a.gtx.jsonl",))

    def test_a_dirty_locale_file_is_a_change_too(self):
        # promote_merged_locales.py rewrites locales in the working tree only.
        (self.root / "locales/master/b.gtx.jsonl").write_text(
            '{"bundle":"b.gtx","item_key":"k","zh":"B2"}\n', encoding="utf-8")
        plan = self._plan()
        self.assertEqual(sorted(plan.rebuild), ["b.gtx.unity3d"])
        self.assertEqual(sorted(plan.reusable), ["a.gtx.unity3d"])

    def test_a_reused_entry_carries_the_published_bytes_and_no_artifact_file(self):
        entry = self._plan().reusable["a.gtx.unity3d"]
        self.assertEqual(entry["artifact_sha256"], "a" * 64)
        self.assertEqual(entry["object_path"], f"objects/sha256/{'a' * 64}")
        self.assertEqual(entry["runtime_path"], "production/2018/Android/aaa.unity3d")
        self.assertEqual(entry["reuse_status"], "exact")
        self.assertEqual(entry["translation_status"], "modified")
        # The store resolves the digest in its own pool and re-hashes it; an
        # artifact_file here would bypass exactly that check.
        self.assertNotIn("artifact_file", entry)

    # -- every reason to refuse --------------------------------------------- #

    def test_no_previous_release_is_a_full_rebuild(self):
        plan = self._plan(manifest=None)
        self.assertEqual(sorted(plan.rebuild), ["a.gtx.unity3d", "b.gtx.unity3d"])
        self.assertEqual(plan.reusable, {})

    def test_a_release_without_a_reuse_record_is_a_full_rebuild(self):
        manifest = self._manifest(block=None)
        manifest.pop(reuse.REUSE_KEY)
        plan = self._plan(manifest)
        self.assertEqual(len(plan.rebuild), 2)

    def test_a_release_that_answered_from_memory_is_a_full_rebuild(self):
        block = reuse_record(exact=9, memory=1)
        plan = self._plan(self._manifest(block=block))
        self.assertEqual(plan.reusable, {})
        self.assertIn("own row", plan.reason)

    def test_a_release_with_unresolved_keys_is_a_full_rebuild(self):
        block = reuse_record(exact=9, unresolved=1)
        self.assertEqual(self._plan(self._manifest(block=block)).reusable, {})

    def test_a_release_with_stale_rows_is_a_full_rebuild(self):
        block = reuse_record(exact=9, stale_exact=1)
        self.assertEqual(self._plan(self._manifest(block=block)).reusable, {})

    def test_a_changed_writer_rebuilds_everything(self):
        (self.root / "pipelines/text/writer.py").write_text("WRITER = 2\n",
                                                            encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "writer")
        plan = self._plan()
        self.assertEqual(plan.reusable, {})
        self.assertIn("outside locales/", plan.reason)

    def test_a_changed_builder_script_rebuilds_everything(self):
        (self.root / "scripts/build_generated_release.py").write_text(
            "# builder v2\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "builder")
        self.assertEqual(self._plan().reusable, {})

    def test_a_changed_catalogue_rebuilds_everything(self):
        plan = self._plan(index_sha256="f" * 64)
        self.assertEqual(plan.reusable, {})
        self.assertIn("catalogue", plan.reason)

    def test_a_renamed_official_object_rebuilds_that_bundle(self):
        index = {"a.gtx.unity3d": index_row("renamed.unity3d"),
                 "b.gtx.unity3d": index_row("bbb.unity3d")}
        plan = self._plan(index=index)
        self.assertEqual(sorted(plan.rebuild), ["a.gtx.unity3d"])
        self.assertEqual(sorted(plan.reusable), ["b.gtx.unity3d"])

    def test_a_missing_object_rebuilds_that_bundle(self):
        entries = [previous_entry("a.gtx.unity3d", "aaa.unity3d", digest="9" * 64),
                   previous_entry("b.gtx.unity3d", "bbb.unity3d")]
        plan = self._plan(self._manifest(entries=entries))
        self.assertEqual(sorted(plan.rebuild), ["a.gtx.unity3d"])
        self.assertEqual(sorted(plan.reusable), ["b.gtx.unity3d"])

    def test_a_bundle_the_release_never_published_is_rebuilt(self):
        entries = [previous_entry("a.gtx.unity3d", "aaa.unity3d")]
        plan = self._plan(self._manifest(entries=entries))
        self.assertEqual(sorted(plan.rebuild), ["b.gtx.unity3d"])

    def test_a_bundle_with_no_known_locale_source_is_rebuilt(self):
        plan = self._plan(sources={"a.gtx.unity3d": {"locales/master/a.gtx.jsonl"}})
        self.assertEqual(sorted(plan.rebuild), ["b.gtx.unity3d"])
        self.assertIn("no locale file feeds this bundle", plan.declined)

    def test_a_deleted_locale_file_cannot_be_proven_harmless(self):
        (self.root / "locales/master/b.gtx.jsonl").unlink()
        plan = self._plan()
        self.assertEqual(plan.reusable, {})
        self.assertIn("cannot tell", plan.reason)

    def test_an_unreadable_previous_commit_is_a_full_rebuild(self):
        manifest = self._manifest(commit="0" * 40)
        plan = self._plan(manifest)
        self.assertEqual(plan.reusable, {})
        self.assertIn("cannot tell", plan.reason)

    def test_a_failed_previous_build_is_a_full_rebuild(self):
        manifest = self._manifest()
        manifest["build_status"] = "failure"
        self.assertEqual(self._plan(manifest).reusable, {})

    def test_a_previous_release_of_another_version_is_a_full_rebuild(self):
        manifest = self._manifest()
        manifest["asset_version"] = "1077600"
        self.assertEqual(self._plan(manifest).reusable, {})


class LyricReusePlanTests(ReuseFixture):
    """The same decision for the second surface: songs, not text bundles.

    A song's only input of its own is ``lyrics/songs/<bundle>.jsonl``, so a build
    that changed one song must repackage that song and no other -- and a build that
    changed none must repackage nothing, which is the four minutes every release
    used to spend patching 491 unchanged bundles.
    """

    def setUp(self) -> None:
        super().setUp()
        for name, body in (("scrobj_alpha.unity3d", '{"slot":"1"}\n'),
                           ("scrobj_beta.unity3d", '{"slot":"2"}\n')):
            target = self.root / f"lyrics/songs/{name}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "lyric library")
        self.built_from = self._git("rev-parse", "HEAD").stdout.strip()
        self.bundles = {"scrobj_alpha.unity3d": index_row("aaa.unity3d"),
                        "scrobj_beta.unity3d": index_row("bbb.unity3d")}

    def _lyric_manifest(self, entries=None, *, block=None, commit: str | None = None) -> dict:
        document = self._manifest(entries=[
            previous_entry("scrobj_alpha.unity3d", "aaa.unity3d", digest="a" * 64),
            previous_entry("scrobj_beta.unity3d", "bbb.unity3d", digest="e" * 64),
        ] if entries is None else entries, block=block, commit=commit)
        return document

    def _lyric_plan(self, manifest=ReusePlanTests._DEFAULT, *, bundles=None,
                    index_sha256="d" * 64):
        return reuse.plan_lyric_reuse(
            root=self.root,
            manifest=self._lyric_manifest() if manifest is self._DEFAULT else manifest,
            asset_version="1077640",
            bundles=self.bundles if bundles is None else bundles,
            index_sha256=index_sha256,
            object_file=self._object_file,
        )

    def test_an_unchanged_library_reuses_every_song(self) -> None:
        plan = self._lyric_plan()
        self.assertEqual(sorted(plan.reusable), ["scrobj_alpha.unity3d", "scrobj_beta.unity3d"])
        self.assertEqual(plan.rebuild, {})
        self.assertEqual(plan.reason, "incremental")

    def test_only_the_song_whose_own_lyrics_moved_is_rebuilt(self) -> None:
        (self.root / "lyrics/songs/scrobj_beta.unity3d.jsonl").write_text(
            '{"slot":"2b"}\n', encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "one song")
        plan = self._lyric_plan()
        self.assertEqual(sorted(plan.reusable), ["scrobj_alpha.unity3d"])
        self.assertEqual(sorted(plan.rebuild), ["scrobj_beta.unity3d"])
        self.assertEqual(plan.declined, {"lyric source changed": 1})

    def test_a_new_song_is_patched_rather_than_carried_over(self) -> None:
        plan = self._lyric_plan(bundles={**self.bundles, "scrobj_gamma.unity3d": index_row("ccc.unity3d")})
        self.assertIn("scrobj_gamma.unity3d", plan.rebuild)
        self.assertEqual(plan.declined.get("not in the published release"), 1)

    def test_a_changed_official_catalogue_refuses_the_whole_surface(self) -> None:
        plan = self._lyric_plan(index_sha256="f" * 64)
        self.assertEqual(plan.reusable, {})
        self.assertEqual(len(plan.rebuild), 2)
        self.assertIn("official catalogue changed", plan.reason)

    def test_a_release_without_the_record_refuses_the_whole_surface(self) -> None:
        plan = self._lyric_plan(self._lyric_manifest(block={}))
        self.assertEqual(plan.reusable, {})
        self.assertEqual(len(plan.rebuild), 2)

    def test_the_object_leaving_the_store_refuses_that_song(self) -> None:
        self.objects.pop("a" * 64)
        plan = self._lyric_plan()
        self.assertEqual(sorted(plan.reusable), ["scrobj_beta.unity3d"])
        self.assertEqual(plan.declined, {"the published object is no longer in the store": 1})

    def test_a_changed_writer_refuses_the_whole_surface(self) -> None:
        (self.root / "scripts/build_lyric_overlay.py").write_text("# changed\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "writer")
        plan = self._lyric_plan()
        self.assertEqual(plan.reusable, {})
        self.assertIn("release inputs outside locales/ changed", plan.reason)

    def test_sources_are_the_songs_own_file(self) -> None:
        sources = reuse.lyric_sources(self.root / "lyrics")
        self.assertEqual(sources["scrobj_alpha.unity3d"],
                         {"lyrics/songs/scrobj_alpha.unity3d.jsonl"})
        self.assertEqual(len(sources), 2)

    def test_a_missing_library_is_not_an_error(self) -> None:
        self.assertEqual(reuse.lyric_sources(self.root / "nowhere"), {})


class ResolutionTests(unittest.TestCase):
    def test_overlay_counters_split_the_routes(self):
        resolution = reuse.Resolution.from_overlay({
            "source_candidates": 10, "resolved_exact": 7, "resolved_memory": 1,
            "stale_exact": 1})
        self.assertEqual(resolution, reuse.Resolution(exact=7, memory=1,
                                                      stale_exact=1, unresolved=1))
        self.assertFalse(resolution.exclusive)

    def test_an_all_exact_overlay_is_exclusive(self):
        resolution = reuse.Resolution.from_overlay({
            "source_candidates": 10, "resolved_exact": 10, "resolved_memory": 0,
            "stale_exact": 0})
        self.assertTrue(resolution.exclusive)

    def test_an_overlay_without_route_counters_proves_nothing(self):
        self.assertIsNone(reuse.Resolution.from_overlay({
            "source_candidates": 10, "resolved": 10, "stale_exact": 0}))

    def test_counters_that_cannot_add_up_prove_nothing(self):
        self.assertIsNone(reuse.Resolution.from_overlay({
            "source_candidates": 1, "resolved_exact": 5, "resolved_memory": 0,
            "stale_exact": 0}))

    def test_the_recorded_block_round_trips(self):
        block = reuse.reuse_block(index_sha256="d" * 64,
                                  resolution=reuse.Resolution(exact=3), scope="regenerated")
        self.assertTrue(block[reuse.REUSE_KEY]["every_key_resolved_exactly"])
        self.assertEqual(reuse.Resolution.from_reuse_block(block[reuse.REUSE_KEY]),
                         reuse.Resolution(exact=3))

    def test_a_block_whose_claim_is_false_is_not_trusted(self):
        # The counters parse, but the block itself says it is not exclusive.
        block = {"schema": reuse.REUSE_SCHEMA,
                 "every_key_resolved_exactly": False,
                 "text_resolution": {"exact": 3, "memory": 0,
                                     "stale_exact": 0, "unresolved": 0}}
        self.assertEqual(reuse.Resolution.from_reuse_block(block),
                         reuse.Resolution(exact=3))
        self.assertFalse(block["every_key_resolved_exactly"])

    def test_an_unknown_block_schema_is_not_read(self):
        self.assertIsNone(reuse.Resolution.from_reuse_block(
            {"schema": 99, "every_key_resolved_exactly": True,
             "text_resolution": {"exact": 3, "memory": 0, "stale_exact": 0,
                                 "unresolved": 0}}))


class GitGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "reuse tests")
        self._git("config", "commit.gpgsign", "false")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=True)

    def test_a_derived_output_the_build_rewrites_is_never_an_input_change(self):
        # The workflow commits portal-resource-manifest.json on every build, so
        # counting it would disable reuse forever.
        target = self.root / "manifests/portal-resource-manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "inputs")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        target.write_text('{"generated_at": "later"}\n', encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "release")
        self.assertEqual(reuse.changed_paths(self.root, head, ("manifests",)), set())

    def test_an_unknown_commit_proves_nothing(self):
        self.assertIsNone(reuse.changed_paths(self.root, "", ("manifests",)))
        self.assertIsNone(reuse.changed_paths(self.root, "0" * 40, ("manifests",)))

    def test_a_tree_absent_from_the_checkout_is_not_a_change(self):
        target = self.root / "locales/keep.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "inputs")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(reuse.changed_paths(self.root, head, ("images", "schema")), set())


if __name__ == "__main__":
    unittest.main()
