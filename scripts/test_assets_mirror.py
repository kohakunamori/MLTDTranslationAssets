#!/usr/bin/env python3
"""Offline tests for ``scripts/assets_mirror.py``.

No network: every case injects a fake fetch implementation that returns constructed
manifest / checksums / object bytes.  Run directly (``python scripts/test_assets_mirror.py``)
or through pytest (``python -m pytest scripts/test_assets_mirror.py``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.assets_mirror import (  # noqa: E402
    ALLOWED_REUSE_STATUS,
    ALLOWED_TRANSLATION_STATUS,
    MANIFEST_KIND,
    AssetVersionMirror,
    AssetsMirrorError,
    GitHubAssetsSource,
    ManifestValidationError,
    ObjectDigestMismatch,
    ObjectPool,
    RateLimitError,
    SyncError,
    parse_checksums,
    validate_manifest,
    verify_manifest_objects,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
#: The official source snapshot the manifests are built from: an ancestor of
#: both commit A and commit B, so the ancestry gate has something true to say.
SOURCE_COMMIT = "5" * 40
REPO = "kohakunamori/MLTDTranslationAssets"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_entry(logical_path: str, data: bytes, **overrides) -> tuple[dict, bytes]:
    digest = sha256_hex(data)
    entry = {
        "channel": "assets",
        "asset_version": "1077100",
        "client_version": None,
        "logical_path": logical_path,
        "logical_key": logical_path.replace("/", ":"),
        # Flat (owner decision 2026-09-30); the fan-out form is only ever the
        # legacy layout a pre-switch manifest still carries.
        "object_path": f"objects/sha256/{digest}",
        "artifact_sha256": digest,
        "resource_kind": "bundle",
        "size": len(data),
        "reuse_status": "exact",
        "translation_status": "accepted",
    }
    entry.update(overrides)
    return entry, data


def make_manifest(entries: list[dict], *, asset_version: str = "1077100",
                  commit: str = COMMIT_A, **overrides) -> dict:
    manifest = {
        "schema_version": 1,
        "kind": MANIFEST_KIND,
        "asset_version": asset_version,
        "client_version": None,
        "source_client_version": "9.0.200",
        # Three distinct commits, as a real release has: an earlier source
        # snapshot, a translation commit, and the commit the generator inputs
        # were taken from -- all of them behind the commit it is read at.
        "source_commit": SOURCE_COMMIT,
        "translation_commit": COMMIT_B if commit != COMMIT_B else COMMIT_A,
        "generated_commit": commit,
        # Present and null: a producer with no run context records no run identity,
        # but the field is never simply absent (that is what a provenance field is for).
        "ci_run_id": None,
        "build_status": "success",
        "entries": entries,
    }
    manifest.update(overrides)
    return manifest


def checksums_text_for(manifest: dict) -> str:
    return "".join(
        f"{entry['artifact_sha256']}  {entry['object_path']}\n" for entry in manifest["entries"]
    )


class FakeSource:
    """Injectable stand-in for :class:`GitHubAssetsSource`.

    ``head`` is the branch HEAD; ``manifests`` is keyed by ``(asset_version, commit)`` so a
    mismatch between the pinned commit and the manifest is expressible.  Every object fetch is
    counted so idempotency can be asserted by download count rather than by inspection.

    ``compare`` answers the provenance gate the way the compare endpoint does, keyed by the
    candidate commit (``base...head`` in the URL is ``candidate...snapshot``).  ``_get`` is the
    transport the mirror's default compare fetch rides on, so an offline test can drive the
    real ``sync`` path -- including its refusals -- without a socket.
    """

    def __init__(self, *, head: str = COMMIT_A, repo: str = REPO, branch: str = "main") -> None:
        self.repo = repo
        self.branch = branch
        self.head = head
        self.raw_base = "https://raw.githubusercontent.com"
        self.manifests: dict[tuple[str, str], dict] = {}
        self.checksums: dict[tuple[str, str], str] = {}
        self.objects: dict[tuple[str, str], bytes] = {}
        self.object_requests: list[tuple[str, str]] = []
        self.manifest_requests: list[tuple[str, str]] = []
        self.head_requests = 0
        self.rate_limit_on_objects = False
        #: ``{candidate_commit: compare status}``; an absent entry is "diverged".
        self.compare: dict[str, str] = {}
        self.compare_requests: list[str] = []
        self.compare_status_override: int | None = None

    def _get(self, url: str):  # noqa: ANN202 - mirrors GitHubAssetsSource._get
        from scripts.assets_mirror import FetchResponse

        self.compare_requests.append(url)
        if self.compare_status_override is not None:
            status = self.compare_status_override
            return FetchResponse(status=status, body=b'{"message":"refused"}',
                                 headers={})
        candidate = url.rsplit("/", 1)[-1].split("...", 1)[0]
        status = self.compare.get(candidate, "diverged")
        total = {"identical": 0, "ahead": 3, "behind": 0, "diverged": 5}[status]
        return FetchResponse(status=200,
                             body=json.dumps({"status": status,
                                              "total_commits": total}).encode(),
                             headers={})

    def set_related(self, *commits: str) -> None:
        """Declare `commits` to be ancestors of (or identical to) the snapshot."""
        for commit in commits:
            self.compare[commit] = "ahead"

    # -- test helpers -----------------------------------------------------------------

    def publish(self, manifest: dict, objects: dict[str, bytes], *, commit: str | None = None) -> None:
        commit = commit or manifest["generated_commit"]
        version = manifest["asset_version"]
        self.manifests[(version, commit)] = manifest
        self.checksums[(version, commit)] = checksums_text_for(manifest)
        for digest, data in objects.items():
            self.objects[(digest, commit)] = data

    # -- GitHubAssetsSource surface ---------------------------------------------------

    def head_commit(self) -> str:
        self.head_requests += 1
        return self.head

    def generated_versions(self, commit: str) -> list[str]:
        versions = {version for version, snapshot in self.manifests if snapshot == commit}
        return sorted(versions, key=lambda value: (int(value), value), reverse=True)

    def fetch_manifest(self, asset_version: str, commit: str) -> dict:
        self.manifest_requests.append((asset_version, commit))
        key = (asset_version, commit)
        if key not in self.manifests:
            raise AssetsMirrorError(f"no manifest for {asset_version} at {commit}")
        return json.loads(json.dumps(self.manifests[key]))

    def fetch_checksums(self, asset_version: str, commit: str) -> str:
        key = (asset_version, commit)
        if key not in self.checksums:
            raise AssetsMirrorError(f"no checksums for {asset_version} at {commit}")
        return self.checksums[key]

    def fetch_object(self, digest: str, commit: str) -> bytes:
        self.object_requests.append((digest, commit))
        if self.rate_limit_on_objects:
            raise RateLimitError(f"rate limited fetching object {digest}", status=429, retry_after="60")
        key = (digest, commit)
        if key not in self.objects:
            raise AssetsMirrorError(f"no object {digest} at {commit}")
        data = self.objects[key]
        # Mirrors the real source: bytes are verified against the requested digest.
        actual = sha256_hex(data)
        if actual != digest:
            raise ObjectDigestMismatch(digest, actual, origin=f"fake://{digest}")
        return data


class MirrorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="assets-mirror-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pool = ObjectPool(self.root)
        self.source = FakeSource()
        self.mirror = AssetVersionMirror(self.source, self.pool, self.root)

    def publish_related(self, manifest: dict, objects: dict[str, bytes],
                        *, commit: str | None = None) -> None:
        """Publish a manifest whose input commits are in the snapshot's history.

        The provenance gate is part of the real sync path, so a fixture that
        expects a successful mirror has to satisfy it the same way a real
        repository would: by the commits actually descending from the snapshot.
        """
        self.source.publish(manifest, objects, commit=commit)
        resolved = commit or manifest.get("generated_commit") or COMMIT_A
        for field in ("source_commit", "translation_commit", "generated_commit"):
            candidate = manifest.get(field)
            if isinstance(candidate, str):
                if candidate == resolved:
                    self.source.compare.setdefault(candidate, "identical")
                else:
                    self.source.compare.setdefault(candidate, "ahead")


# ---------------------------------------------------------------------------------------
# 1. mapping by original logical path
# ---------------------------------------------------------------------------------------


class TestResolveByLogicalPath(MirrorTestCase):
    def test_original_path_maps_to_content_addressed_object(self) -> None:
        entry_a, data_a = make_entry("event/001/title.unity3d", b"bundle-A")
        entry_b, data_b = make_entry("master/BGM_001.unity3d", b"bundle-B")
        manifest = make_manifest([entry_a, entry_b])
        self.publish_related(manifest, {entry_a["artifact_sha256"]: data_a,
                                       entry_b["artifact_sha256"]: data_b})

        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)

        resolved = self.mirror.resolve("1077100", "event/001/title.unity3d")
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.object_path, entry_a["object_path"])
        self.assertEqual(resolved.artifact_sha256, entry_a["artifact_sha256"])
        self.assertEqual(resolved.pool_path, str(self.pool.path_for(entry_a["artifact_sha256"])))
        self.assertTrue(Path(resolved.pool_path).is_file())
        self.assertEqual(Path(resolved.pool_path).read_bytes(), data_a)
        self.assertEqual(resolved.size, len(data_a))

        second = self.mirror.resolve("1077100", "master/BGM_001.unity3d")
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.artifact_sha256, entry_b["artifact_sha256"])

        # The server never has to know about content addressing: the request stays a plain path.
        self.assertIsNone(self.mirror.resolve("1077100", "event/001/unknown.unity3d"))

    def test_unpublished_version_resolves_to_none(self) -> None:
        self.assertIsNone(self.mirror.resolve("9999999", "event/001/title.unity3d"))


# ---------------------------------------------------------------------------------------
# 2. the archive 'current' pointer is never consulted
# ---------------------------------------------------------------------------------------


class TestCurrentPointerIsNeverUsed(MirrorTestCase):
    def test_current_symlink_does_not_influence_sync_or_resolve(self) -> None:
        entry_new, data_new = make_entry("event/900/new.unity3d", b"new-version-payload")
        manifest_new = make_manifest([entry_new], asset_version="1077100")
        self.publish_related(manifest_new, {entry_new["artifact_sha256"]: data_new})

        # A foreign 'current' pointer exists and points at a completely different version.
        decoy_dir = self.root / "published" / "1077999"
        decoy_dir.mkdir(parents=True)
        (decoy_dir / "state.json").write_text(json.dumps({"sync_status": "success"}), encoding="utf-8")
        (self.root / "current").symlink_to(Path("published") / "1077999", target_is_directory=True)

        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)

        resolved = self.mirror.resolve("1077100", "event/900/new.unity3d")
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.artifact_sha256, entry_new["artifact_sha256"])
        # Nothing was resolved through the decoy, and 'current' still points where it did.
        self.assertEqual(os.readlink(self.root / "current").replace("\\", "/"), "published/1077999")
        self.assertIsNone(self.mirror.resolve("1077100", "event/900/absent.unity3d"))

        versions = {v["asset_version"] for v in self.mirror.list_versions()}
        self.assertEqual(versions, {"1077100", "1077999"})

    def test_source_is_never_asked_for_a_current_ref(self) -> None:
        entry, data = make_entry("a/b.unity3d", b"payload")
        manifest = make_manifest([entry])
        self.publish_related(manifest, {entry["artifact_sha256"]: data})
        seen_refs: list[str] = []
        original = self.source.fetch_manifest

        def spy(asset_version: str, commit: str) -> dict:
            seen_refs.append(commit)
            return original(asset_version, commit)

        self.source.fetch_manifest = spy  # type: ignore[assignment]
        self.mirror.sync("1077100", dry_run=True)
        self.assertEqual(seen_refs, [COMMIT_A])
        self.assertNotIn("current", seen_refs)


# ---------------------------------------------------------------------------------------
# 3. multiple versions coexist
# ---------------------------------------------------------------------------------------


class TestMultipleVersionsCoexist(MirrorTestCase):
    def test_two_versions_mirror_resolve_independently(self) -> None:
        entry_old, data_old = make_entry("event/001/title.unity3d", b"old-title")
        old_commit = COMMIT_A
        manifest_old = make_manifest([entry_old], asset_version="1077100", commit=old_commit)
        self.publish_related(manifest_old, {entry_old["artifact_sha256"]: data_old})

        entry_new, data_new = make_entry("event/001/title.unity3d", b"new-title")
        new_commit = COMMIT_B
        manifest_new = make_manifest([entry_new], asset_version="1077500", commit=new_commit)
        self.publish_related(manifest_new, {entry_new["artifact_sha256"]: data_new})

        self.mirror.sync("1077100", commit=old_commit, dry_run=False)
        self.mirror.sync("1077500", commit=new_commit, dry_run=False)

        versions = {v["asset_version"]: v for v in self.mirror.list_versions()}
        self.assertEqual(set(versions), {"1077100", "1077500"})
        for version in versions.values():
            self.assertEqual(version["sync_status"], "success")
            self.assertEqual(version["entry_count"], 1)

        resolved_old = self.mirror.resolve("1077100", "event/001/title.unity3d")
        resolved_new = self.mirror.resolve("1077500", "event/001/title.unity3d")
        assert resolved_old is not None and resolved_new is not None
        self.assertNotEqual(resolved_old.artifact_sha256, resolved_new.artifact_sha256)
        self.assertEqual(Path(resolved_old.pool_path).read_bytes(), data_old)
        self.assertEqual(Path(resolved_new.pool_path).read_bytes(), data_new)
        # Two distinct bodies, two distinct pool objects; no overwrite happened.
        self.assertTrue(self.pool.has(entry_old["artifact_sha256"]))
        self.assertTrue(self.pool.has(entry_new["artifact_sha256"]))


class TestRefreshNeverPublishesOrDeletes(MirrorTestCase):
    """B2：刷新只做镜像，别的什么都不做。

    镜像同时服务多个版本；一次定时刷新不得移动本地默认版本（``current.json``），
    也不得删除任何已发布版本或 CAS 对象。激活与清理是彼此独立的显式运维动作，
    分别由下面的测试覆盖。
    """

    def _publish_version(self, version: str, commit: str, payload: bytes) -> tuple[dict, bytes]:
        entry, data = make_entry("event/001/title.unity3d", payload)
        manifest = make_manifest([entry], asset_version=version, commit=commit)
        self.publish_related(manifest, {entry["artifact_sha256"]: data}, commit=commit)
        return manifest, data

    def test_sync_latest_mirrors_newest_and_keeps_every_version(self) -> None:
        old, old_data = self._publish_version("1077100", COMMIT_A, b"old")
        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        new, new_data = self._publish_version("1077500", COMMIT_A, b"new")

        report = self.mirror.sync_latest(dry_run=False)

        self.assertEqual(report["selected_as_latest"], "1077500")
        self.assertEqual(report["sync_status"], "success")
        # The version mirrored by this call is published...
        resolved_new = self.mirror.resolve("1077500", "event/001/title.unity3d")
        self.assertIsNotNone(resolved_new)
        assert resolved_new is not None
        self.assertEqual(Path(resolved_new.pool_path).read_bytes(), new_data)
        # ...and the older version, its metadata and its object all survive.
        self.assertTrue(self.mirror.version_dir("1077100").exists())
        self.assertTrue(self.pool.has(old["entries"][0]["artifact_sha256"]))
        resolved_old = self.mirror.resolve("1077100", "event/001/title.unity3d")
        self.assertIsNotNone(resolved_old)
        assert resolved_old is not None
        self.assertEqual(Path(resolved_old.pool_path).read_bytes(), old_data)

    def test_no_refresh_touches_current_json(self) -> None:
        """current.json is byte-identical across an explicit sync and a latest sync."""
        self._publish_version("1077100", COMMIT_A, b"old")
        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.mirror.activate_current("1077100")
        before = self.mirror.current_path.read_bytes()

        self._publish_version("1077500", COMMIT_A, b"new")
        self.mirror.sync("1077500", commit=COMMIT_A, dry_run=False)
        self.mirror.sync_latest(dry_run=False)

        self.assertEqual(self.mirror.current_path.read_bytes(), before)
        self.assertEqual(self.mirror.current_version(), "1077100")

    def test_refresh_writes_are_confined_to_the_named_version(self) -> None:
        self._publish_version("1077100", COMMIT_A, b"old")
        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}

        self._publish_version("1077500", COMMIT_A, b"new")
        self.mirror.sync("1077500", commit=COMMIT_A, dry_run=False)

        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        for path, data in before.items():
            self.assertEqual(after.get(path), data, f"a sync must not rewrite {path}")
        self.assertNotIn("current.json", after)
        self.assertNotIn("retained.json", after)

    def test_sync_latest_dry_run_writes_nothing(self) -> None:
        self._publish_version("1077100", COMMIT_A, b"old")
        report = self.mirror.sync_latest(dry_run=True)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["sync_status"], "planned")
        self.assertEqual(report["selected_as_latest"], "1077100")
        self.assertEqual(sorted(os.listdir(self.root)), [])


class TestSyncLatestCompatibilitySurface(MirrorTestCase):
    """兼容入口收到保留请求必须响亮失败，而不是悄悄丢弃。"""

    def test_keep_versions_arguments_are_removed_not_ignored(self) -> None:
        with self.assertRaises(TypeError):
            self.mirror.sync_latest(keep_versions=["1077100"])  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            self.mirror.sync_latest(persist_keep=True)  # type: ignore[call-arg]

    def test_cli_sync_latest_rejects_keep_version_flag(self) -> None:
        from scripts import assets_mirror as module
        with self.assertRaises(SystemExit) as ctx:
            module.main(["--root", str(self.root), "sync-latest", "--keep-version", "1077100"])
        self.assertEqual(ctx.exception.code, 2)


class TestExplicitPruneIsDryRunByDefault(MirrorTestCase):
    def _publish_version(self, version: str, commit: str, payload: bytes) -> tuple[dict, bytes]:
        entry, data = make_entry("event/001/title.unity3d", payload)
        manifest = make_manifest([entry], asset_version=version, commit=commit)
        self.publish_related(manifest, {entry["artifact_sha256"]: data}, commit=commit)
        return manifest, data

    def test_prune_without_apply_removes_nothing(self) -> None:
        old, _ = self._publish_version("1077100", COMMIT_A, b"old")
        self._publish_version("1077500", COMMIT_A, b"new")
        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.mirror.sync("1077500", commit=COMMIT_A, dry_run=False)
        before = self.snapshot()

        report = self.mirror.prune()

        self.assertEqual(report["mode"], "dry-run")
        # The plan is allowed to name candidates; the tree must be untouched.
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.mirror.version_dir("1077100").exists())
        self.assertTrue(self.mirror.version_dir("1077500").exists())
        self.assertTrue(self.pool.has(old["entries"][0]["artifact_sha256"]))

    def test_prune_apply_keeps_current_and_retained_versions(self) -> None:
        old, _ = self._publish_version("1077100", COMMIT_A, b"old")
        self._publish_version("1077500", COMMIT_A, b"new")
        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.mirror.sync("1077500", commit=COMMIT_A, dry_run=False)
        self.mirror.activate_current("1077500")
        self.mirror.set_retained_versions(["1077100"])

        report = self.mirror.prune(dry_run=False)

        self.assertEqual(report["mode"], "apply")
        self.assertEqual(report["kept_versions"], ["1077100", "1077500"])
        self.assertEqual(report["removed_versions"], [])
        self.assertTrue(self.mirror.version_dir("1077100").exists())
        self.assertTrue(self.mirror.version_dir("1077500").exists())

        # Dropping the pin is what makes an explicit apply eligible to remove it,
        # and then the orphaned CAS object goes with it.
        self.mirror.set_retained_versions([])
        report = self.mirror.prune(dry_run=False)
        self.assertEqual(report["removed_versions"], ["1077100"])
        self.assertFalse(self.mirror.version_dir("1077100").exists())
        self.assertTrue(self.mirror.version_dir("1077500").exists())
        self.assertFalse(self.pool.has(old["entries"][0]["artifact_sha256"]))

    def snapshot(self) -> dict:
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}


class TestInterruptedSyncLeavesNoPublication(MirrorTestCase):
    """下载中途失败不得表现为“已发布”，且重跑要能收敛。"""

    def _two_entry_manifest(self) -> tuple[dict, dict, dict]:
        entry_a, data_a = make_entry("event/001/a.unity3d", b"payload-a")
        entry_b, data_b = make_entry("event/001/b.unity3d", b"payload-b")
        manifest = make_manifest([entry_a, entry_b])
        self.publish_related(manifest, {entry_a["artifact_sha256"]: data_a,
                                       entry_b["artifact_sha256"]: data_b})
        return entry_a, entry_b, manifest

    def test_interrupted_download_never_publishes(self) -> None:
        entry_a, entry_b, manifest = self._two_entry_manifest()
        original = self.source.fetch_object
        calls = {"count": 0}

        def flaky(digest: str, commit: str) -> bytes:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated connection reset mid-download")
            return original(digest, commit)

        self.source.fetch_object = flaky  # type: ignore[assignment]
        with self.assertRaises(OSError):
            self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)

        # Not published: no manifest, no state, nothing resolves.
        self.assertFalse(self.mirror.manifest_path("1077100").exists())
        self.assertFalse(self.mirror.state_path("1077100").exists())
        self.assertIsNone(self.mirror.resolve("1077100", "event/001/a.unity3d"))
        # The object already admitted is content-addressed and deliberately kept.
        self.assertTrue(self.pool.has(entry_a["artifact_sha256"]))
        self.assertFalse(self.pool.has(entry_b["artifact_sha256"]))

        # A later sync converges idempotently and downloads only what is missing.
        self.source.fetch_object = original  # type: ignore[assignment]
        report = self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(report["sync_status"], "success")
        self.assertEqual(report["downloaded"], 1)
        self.assertIsNotNone(self.mirror.resolve("1077100", "event/001/a.unity3d"))

    def test_rate_limit_mid_sync_never_publishes(self) -> None:
        self._two_entry_manifest()
        self.source.rate_limit_on_objects = True
        with self.assertRaises(RateLimitError):
            self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertFalse(self.mirror.manifest_path("1077100").exists())
        self.assertFalse(self.mirror.state_path("1077100").exists())
        self.assertIsNone(self.mirror.resolve("1077100", "event/001/a.unity3d"))


class TestWatchCli(MirrorTestCase):
    """无人值守刷新只接收显式版本，且除同步外什么都不做。"""

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        import contextlib
        import io
        from scripts import assets_mirror as module
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_watch_without_a_version_is_refused(self) -> None:
        code, _, err = self._run_cli(["--root", str(self.root), "watch", "--once"])
        self.assertEqual(code, 1)
        self.assertIn("watch requires at least one explicit --asset-version", err)
        self.assertEqual(sorted(os.listdir(self.root)), [])

    def test_watch_accepts_comma_separated_versions(self) -> None:
        from unittest import mock
        from scripts import assets_mirror as module

        for version, payload in (("1077100", b"one"), ("1077500", b"two")):
            entry, data = make_entry("event/001/title.unity3d", payload)
            manifest = make_manifest([entry], asset_version=version, commit=COMMIT_A)
            self.publish_related(manifest, {entry["artifact_sha256"]: data})

        with mock.patch.object(module, "GitHubAssetsSource", lambda **kwargs: self.source):
            code, _, err = self._run_cli(
                ["--root", str(self.root), "watch", "--asset-version", "1077100,1077500", "--once"])

        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / "published" / "1077100" / "manifest.json").is_file())
        self.assertTrue((self.root / "published" / "1077500" / "manifest.json").is_file())
        self.assertFalse((self.root / "current.json").exists())

    def test_watch_refuses_a_non_numeric_version(self) -> None:
        code, _, err = self._run_cli(
            ["--root", str(self.root), "watch", "--asset-version", "current", "--once"])
        self.assertEqual(code, 1)
        self.assertIn("digits only", err)
        self.assertEqual(sorted(os.listdir(self.root)), [])

    def test_watch_once_syncs_only_the_named_version(self) -> None:
        from unittest import mock
        from scripts import assets_mirror as module

        entry, data = make_entry("event/001/title.unity3d", b"watched")
        manifest = make_manifest([entry], asset_version="1077100", commit=COMMIT_A)
        self.publish_related(manifest, {entry["artifact_sha256"]: data})
        # A newer release exists upstream; a watch of 1077100 must not mirror it.
        newer, newer_data = make_entry("event/001/title.unity3d", b"newer")
        newer_manifest = make_manifest([newer], asset_version="1077500", commit=COMMIT_A)
        self.publish_related(newer_manifest, {newer["artifact_sha256"]: newer_data})

        with mock.patch.object(module, "GitHubAssetsSource", lambda **kwargs: self.source):
            code, out, err = self._run_cli(
                ["--root", str(self.root), "watch", "--asset-version", "1077100", "--once"])

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["asset_version"], "1077100")
        self.assertEqual(payload["sync_status"], "success")
        self.assertTrue((self.root / "published" / "1077100" / "manifest.json").is_file())
        self.assertFalse((self.root / "published" / "1077500").exists())
        self.assertFalse((self.root / "current.json").exists())
        self.assertFalse((self.root / "retained.json").exists())

    def test_watch_once_fails_when_the_named_version_cannot_sync(self) -> None:
        from unittest import mock
        from scripts import assets_mirror as module
        # No manifest published for 1077100: the tick must report failure, not success.
        with mock.patch.object(module, "GitHubAssetsSource", lambda **kwargs: self.source):
            code, _, err = self._run_cli(
                ["--root", str(self.root), "watch", "--asset-version", "1077100", "--once"])
        self.assertEqual(code, 1)
        self.assertIn("watch refresh failed for asset_version=1077100", err)


class TestActivateCli(MirrorTestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        import contextlib
        import io
        from scripts import assets_mirror as module
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _publish_and_sync(self, version: str, payload: bytes) -> None:
        entry, data = make_entry("event/001/title.unity3d", payload)
        manifest = make_manifest([entry], asset_version=version, commit=COMMIT_A)
        self.publish_related(manifest, {entry["artifact_sha256"]: data}, commit=COMMIT_A)
        self.mirror.sync(version, commit=COMMIT_A, dry_run=False)

    def test_activate_is_a_separate_explicit_action(self) -> None:
        self._publish_and_sync("1077100", b"old")
        self.assertFalse(self.mirror.current_path.exists())

        code, out, err = self._run_cli(
            ["--root", str(self.root), "activate", "--asset-version", "1077100"])

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["asset_version"], "1077100")
        self.assertTrue(self.mirror.current_path.is_file())
        self.assertEqual(self.mirror.current_version(), "1077100")

    def test_activate_of_an_unpublished_version_is_refused(self) -> None:
        self._publish_and_sync("1077100", b"old")
        self._run_cli(["--root", str(self.root), "activate", "--asset-version", "1077100"])
        before = self.mirror.current_path.read_bytes()

        code, _, err = self._run_cli(
            ["--root", str(self.root), "activate", "--asset-version", "1079999"])

        self.assertEqual(code, 1)
        self.assertIn("cannot activate unpublished", err)
        self.assertEqual(self.mirror.current_path.read_bytes(), before)

    def test_failed_version_cannot_be_activated(self) -> None:
        self._publish_and_sync("1077100", b"old")
        state_path = self.mirror.state_path("1077100")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["sync_status"] = "failed"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(SyncError):
            self.mirror.activate_current("1077100")


# ---------------------------------------------------------------------------------------
# 4. checksum failure is fail-closed
# ---------------------------------------------------------------------------------------


class TestChecksumFailureFailsClosed(MirrorTestCase):
    def test_tampered_object_bytes_never_reach_the_pool(self) -> None:
        entry, data = make_entry("event/002/title.unity3d", b"correct-bytes")
        manifest = make_manifest([entry])
        self.publish_related(manifest, {entry["artifact_sha256"]: data})
        # The source now serves different bytes for the same digest (corruption / tampering).
        self.source.objects[(entry["artifact_sha256"], COMMIT_A)] = b"tampered-bytes"

        with self.assertRaises(ObjectDigestMismatch):
            self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)

        self.assertFalse(self.pool.has(entry["artifact_sha256"]))
        self.assertFalse((self.root / "published" / "1077100" / "manifest.json").exists())
        self.assertEqual(list(self.pool.iter_digests()), [])

    def test_pool_bytes_that_do_not_match_the_manifest_are_rejected(self) -> None:
        """Defence in depth: a pre-seeded pool holding the wrong bytes for a digest."""
        entry, data = make_entry("event/003/title.unity3d", b"expected")
        digest = entry["artifact_sha256"]
        manifest = make_manifest([entry])
        self.publish_related(manifest, {digest: data})

        # Seed the pool path directly with bytes that do not hash to the digest.
        target = self.pool.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"corrupted-on-disk")

        result = verify_manifest_objects(manifest, self.pool, checksums_text=checksums_text_for(manifest))
        self.assertFalse(result.ok)
        self.assertEqual([m["expected"] for m in result.mismatched], [digest])

        with self.assertRaises(SyncError):
            self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)

        state = json.loads((self.root / "published" / "1077100" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["sync_status"], "failed")
        self.assertFalse((self.root / "published" / "1077100" / "manifest.json").exists())

    def test_checksums_file_disagreeing_with_manifest_is_rejected(self) -> None:
        entry, data = make_entry("event/004/title.unity3d", b"payload")
        manifest = make_manifest([entry])
        self.publish_related(manifest, {entry["artifact_sha256"]: data})
        other = sha256_hex(b"other")
        self.source.checksums[("1077100", COMMIT_A)] = f"{other}  {entry['object_path']}\n"

        result = verify_manifest_objects(manifest, self.pool, checksums_text=self.source.checksums[("1077100", COMMIT_A)])
        # Objects are absent, but the checksum disagreement must already fail the verification.
        self.assertFalse(result.ok)
        self.assertEqual(result.checksum_mismatches[0]["checksums_txt"], other)

    def test_missing_checksums_file_fails_verification(self) -> None:
        entry, data = make_entry("event/005/title.unity3d", b"payload")
        manifest = make_manifest([entry])
        self.publish_related(manifest, {entry["artifact_sha256"]: data})
        result = verify_manifest_objects(manifest, self.pool, checksums_text="")
        self.assertFalse(result.ok)
        self.assertTrue(any("checksums.txt" in problem for problem in result.problems))


# ---------------------------------------------------------------------------------------
# 5-9. manifest validation (fail-closed, nothing written)
# ---------------------------------------------------------------------------------------


class TestManifestValidationRejections(MirrorTestCase):
    def _assert_rejected_and_unwritten(self, manifest: dict, objects: dict[str, bytes],
                                       expected_substring: str, **sync_kwargs) -> None:
        self.source.publish(manifest, objects, commit=sync_kwargs.get("commit") or None)
        with self.assertRaises(ManifestValidationError) as ctx:
            self.mirror.sync(manifest["asset_version"], dry_run=False, **sync_kwargs)
        self.assertTrue(
            any(expected_substring in problem for problem in ctx.exception.problems),
            f"expected a problem containing {expected_substring!r}, got {ctx.exception.problems}",
        )
        # Fail-closed: not one byte of the pool or the published tree was created.
        self.assertEqual(list(self.pool.iter_digests()), [])
        self.assertFalse((self.root / "published").exists())

    def test_build_status_not_success_is_rejected(self) -> None:
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        manifest = make_manifest([entry], build_status="failure")
        self._assert_rejected_and_unwritten(
            manifest, {entry["artifact_sha256"]: data}, "build_status must be 'success'",
            commit=COMMIT_A,
        )

    def test_combined_version_is_rejected(self) -> None:
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        combined = "9.0.200+1077100"
        manifest = make_manifest([entry], asset_version=combined)
        self.source.publish(manifest, {entry["artifact_sha256"]: data}, commit=COMMIT_A)
        # Rejected at the boundary: a combined string is not an asset_version at all, so the
        # sync refuses before it even reads the manifest.
        with self.assertRaises(AssetsMirrorError) as ctx:
            self.mirror.sync(combined, commit=COMMIT_A, dry_run=False)
        self.assertIn("digits only", str(ctx.exception))
        self.assertEqual(list(self.pool.iter_digests()), [])
        self.assertEqual(self.source.manifest_requests, [])
        self.assertFalse((self.root / "published").exists())

        # The same string inside the manifest is reported as a validation problem.
        combined_problems = validate_manifest(
            make_manifest([entry], asset_version=combined),
            asset_version="1077100", expected_head_commit=COMMIT_A,
        )
        self.assertTrue(any("combined" in problem for problem in combined_problems), combined_problems)

        # The combined form is also rejected on the source_client_version axis.
        problems = validate_manifest(
            make_manifest([entry], source_client_version="client-9.0.200-assets-1077100"),
            asset_version="1077100", expected_head_commit=COMMIT_A,
        )
        self.assertTrue(any("-assets-" in problem for problem in problems), problems)

    def test_non_null_client_version_is_rejected(self) -> None:
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        manifest = make_manifest([entry], client_version="9.0.200")
        self._assert_rejected_and_unwritten(
            manifest, {entry["artifact_sha256"]: data}, "client_version must be null",
            commit=COMMIT_A,
        )

    def test_the_provenance_gate_runs_on_the_real_sync_path(self) -> None:
        """A manifest whose commits do not descend from the snapshot is refused.

        The gate is wired into ``sync`` -- not merely offered as a CLI the
        operator might remember to run -- and it runs before the first object
        request, so a refused release costs no downloads and leaves no bytes.
        An unanswerable comparison (404 from the compare endpoint) is refused
        the same way: an unverified ancestry is not a passing ancestry.
        """
        parts = {"event/001/title.unity3d": b"payload-one",
                 "event/001/body.unity3d": b"payload-two"}
        entries, objects = [], {}
        for logical, data in parts.items():
            entry, blob = make_entry(logical, data)
            entries.append(entry)
            objects[entry["artifact_sha256"]] = blob
        manifest = make_manifest(entries)

        cases = {"ahead": ("ahead", True), "identical": ("identical", True),
                 "behind": ("behind", False), "diverged": ("diverged", False)}
        for label, (status, accepted) in cases.items():
            with self.subTest(compare=label):
                self.setUp()
                source = FakeSource(head=COMMIT_A)
                source.publish(manifest, objects, commit=COMMIT_A)
                for field in ("source_commit", "translation_commit", "generated_commit"):
                    source.compare[manifest[field]] = status
                mirror = AssetVersionMirror(source, self.pool, self.root)

                if accepted:
                    report = mirror.sync("1077100", commit=COMMIT_A, dry_run=True)
                    self.assertEqual(report["sync_status"], "planned")
                    # source and translation are asked about; generated_commit
                    # equals the snapshot, and an object contains itself, so it
                    # costs no request.
                    self.assertEqual(len(source.compare_requests), 2)
                    self.assertTrue(all(COMMIT_A in url for url in source.compare_requests))
                    self.assertNotIn(manifest["generated_commit"] + "...",
                                     " ".join(source.compare_requests))
                else:
                    with self.assertRaises(ManifestValidationError) as ctx:
                        mirror.sync("1077100", commit=COMMIT_A, dry_run=True)
                    self.assertTrue(any("provenance" in problem
                                        for problem in ctx.exception.problems),
                                    ctx.exception.problems)
                self.assertEqual(source.object_requests, [],
                                 "the gate must run before any object is requested")
                self.assertEqual(list(self.pool.iter_digests()), [])
                self.assertFalse((self.root / "published").exists())

        # The compare endpoint itself failing closed: HTTP 404 is not a pass.
        self.setUp()
        source = FakeSource(head=COMMIT_A)
        source.publish(manifest, objects, commit=COMMIT_A)
        source.compare_status_override = 404
        mirror = AssetVersionMirror(source, self.pool, self.root)
        with self.assertRaises(ManifestValidationError) as ctx:
            mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertTrue(any("HTTP 404" in problem for problem in ctx.exception.problems),
                        ctx.exception.problems)
        self.assertEqual(source.object_requests, [])
        self.assertEqual(list(self.pool.iter_digests()), [])
        self.assertFalse((self.root / "published").exists())

    def test_the_report_separates_the_manifest_source_commit_from_the_resolved_snapshot(self) -> None:
        """``source_commit`` is the release's input; ``snapshot_commit`` is what was read."""
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        manifest = make_manifest([entry])          # generated_commit == COMMIT_A
        self.publish_related(manifest, {entry["artifact_sha256"]: data})
        report = self.mirror.sync("1077100", commit=COMMIT_A, dry_run=True)

        self.assertEqual(report["source_commit"], SOURCE_COMMIT,
                         "the field names the commit the release was built from")
        self.assertEqual(report["snapshot_commit"], COMMIT_A,
                         "the field names the commit this mirror actually resolved")
        self.assertNotEqual(report["source_commit"], report["snapshot_commit"])

        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        state = json.loads(self.mirror.state_path("1077100").read_text(encoding="utf-8"))
        self.assertEqual(state["source_commit"], SOURCE_COMMIT)
        self.assertEqual(state["snapshot_commit"], COMMIT_A)

    def test_a_manifest_cannot_dictate_its_own_snapshot(self) -> None:
        """A ``snapshot_commit`` in the manifest never becomes the compared base."""
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        manifest = make_manifest([entry], snapshot_commit=COMMIT_B)
        self.source.publish(manifest, {entry["artifact_sha256"]: data}, commit=COMMIT_A)
        # The manifest claims it was read at B, but the mirror resolved A and the
        # gate compares against A: the statement does not steer the check.
        self.source.compare[SOURCE_COMMIT] = "ahead"
        self.source.compare[COMMIT_B] = "ahead"
        self.source.compare[COMMIT_A] = "identical"
        with self.assertRaises(ManifestValidationError) as ctx:
            self.mirror.sync("1077100", commit=COMMIT_A, dry_run=True)
        self.assertTrue(any("snapshot_commit" in problem for problem in ctx.exception.problems),
                        ctx.exception.problems)

    def test_suggested_and_blocked_entries_are_rejected_with_reason(self) -> None:
        good, good_data = make_entry("event/001/ok.unity3d", b"ok")
        suggested, suggested_data = make_entry("event/001/suggested.unity3d", b"suggested",
                                               reuse_status="suggested")
        blocked, blocked_data = make_entry("event/001/blocked.unity3d", b"blocked",
                                           reuse_status="blocked")
        unaccepted, unaccepted_data = make_entry("event/001/pending.unity3d", b"pending",
                                                 translation_status="pending")

        for bad_entry, bad_data, needle in (
            (suggested, suggested_data, "reuse_status='suggested'"),
            (blocked, blocked_data, "reuse_status='blocked'"),
            (unaccepted, unaccepted_data, "translation_status='pending'"),
        ):
            with self.subTest(entry=bad_entry["logical_path"]):
                self.setUp()  # fresh temp root per case
                manifest = make_manifest([good, bad_entry])
                self._assert_rejected_and_unwritten(
                    manifest,
                    {good["artifact_sha256"]: good_data, bad_entry["artifact_sha256"]: bad_data},
                    needle, commit=COMMIT_A,
                )

        # The whitelists the implementation enforces are the spec whitelists.
        self.assertEqual(ALLOWED_REUSE_STATUS, frozenset({"exact", "verified-compatible"}))
        self.assertEqual(ALLOWED_TRANSLATION_STATUS, frozenset({"accepted", "modified", "reused"}))

    def test_whitelisted_status_combinations_are_accepted(self) -> None:
        entries: list[dict] = []
        objects: dict[str, bytes] = {}
        for index, (reuse, translation) in enumerate(
            (("exact", "accepted"), ("exact", "modified"), ("verified-compatible", "reused"))
        ):
            entry, data = make_entry(f"event/00{index}/ok.unity3d", f"payload-{index}".encode(),
                                     reuse_status=reuse, translation_status=translation)
            entries.append(entry)
            objects[entry["artifact_sha256"]] = data
        manifest = make_manifest(entries)
        self.publish_related(manifest, objects)
        self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(sorted(self.pool.iter_digests()), sorted(objects))

    def test_path_traversal_is_rejected(self) -> None:
        entry, data = make_entry("../../etc/passwd", b"payload")
        manifest = make_manifest([entry])
        self._assert_rejected_and_unwritten(
            manifest, {entry["artifact_sha256"]: data}, "path traversal", commit=COMMIT_A,
        )

        absolute_problems = validate_manifest(
            make_manifest([make_entry("/etc/passwd", b"x")[0]]),
            asset_version="1077100", expected_head_commit=COMMIT_A,
        )
        self.assertTrue(any("must be relative" in p for p in absolute_problems), absolute_problems)

        embedded_problems = validate_manifest(
            make_manifest([make_entry("event/../../secret.unity3d", b"x")[0]]),
            asset_version="1077100", expected_head_commit=COMMIT_A,
        )
        self.assertTrue(any("path traversal" in p for p in embedded_problems), embedded_problems)

    def test_object_path_must_be_canonical_for_its_digest(self) -> None:
        entry, data = make_entry("event/001/title.unity3d", b"payload",
                                 object_path="objects/sha256/zz/deadbeef")
        problems = validate_manifest(make_manifest([entry]), asset_version="1077100",
                                     expected_head_commit=COMMIT_A)
        self.assertTrue(any("canonical content-addressed path" in p for p in problems), problems)

    def test_a_legacy_object_path_is_accepted_and_served_from_the_shard(self) -> None:
        """An old release must stay mirrorable, shard path and all."""
        entry, data = make_entry("event/001/title.unity3d", b"legacy payload")
        digest = entry["artifact_sha256"]
        entry["object_path"] = f"objects/sha256/{digest[:2]}/{digest}"
        manifest = make_manifest([entry])
        self.assertEqual(validate_manifest(manifest, asset_version="1077100",
                                           expected_head_commit=COMMIT_A), [])

        # The upstream repo serves it at the shard URL; the mirror must find and
        # verify it, and keep the bytes.  The mirror's own pool is flat (a new
        # mirror writes the flat layout), but the object it stores is the one the
        # manifest names -- the digest is the identity, the path is layout.
        self.publish_related(manifest, {digest: data})
        report = self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(report["sync_status"], "success")
        self.assertEqual(self.pool.existing_path(digest), self.pool.path_for(digest))
        resolved = self.mirror.resolve("1077100", "event/001/title.unity3d")
        assert resolved is not None
        self.assertEqual(resolved.artifact_sha256, digest)
        self.assertEqual(Path(resolved.pool_path).read_bytes(), data)
        self.assertTrue(self.mirror.verify("1077100")["ok"])

    def test_a_checksums_row_for_another_path_does_not_vouch_for_the_object(self) -> None:
        """Exact path per entry: 'the digest appears somewhere' is not a checksum.

        Relaxing the comparison to the digest alone would let this pass -- the
        manifest promises the flat path, the checksums file blesses a shard, and
        a consumer that resolves one against the other finds no file.
        """
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        digest = entry["artifact_sha256"]
        manifest = make_manifest([entry])

        # Same digest, but listed at the retired path the manifest does not use.
        self.pool.put_bytes(digest, data)  # so the object itself is not the problem
        checksums = f"{digest}  objects/sha256/{digest[:2]}/{digest}\n"
        result = verify_manifest_objects(manifest, self.pool, checksums_text=checksums)
        self.assertFalse(result.ok)
        self.assertEqual(result.checksum_mismatches[0]["checksums_txt"], None)

        # The reverse: a manifest that names the shard and a checksums file that
        # blesses the flat path.
        legacy_entry = dict(entry, object_path=f"objects/sha256/{digest[:2]}/{digest}")
        legacy_checksums = f"{digest}  objects/sha256/{digest}\n"
        result2 = verify_manifest_objects(make_manifest([legacy_entry]), self.pool,
                                          checksums_text=legacy_checksums)
        self.assertFalse(result2.ok)
        self.assertEqual(result2.checksum_mismatches[0]["object_path"],
                         f"objects/sha256/{digest[:2]}/{digest}")

    def test_a_snapshot_ahead_of_the_generated_commit_is_not_an_error(self) -> None:
        """The rule that made every pipeline manifest unmirrorable, withdrawn.

        The generator commits the release and later commits land on top, so the
        manifest's generated_commit is legitimately behind the branch head it is
        read at.  What matters is that the manifest was read at the commit it
        says it was read at -- which a legacy manifest cannot say, and a new one
        says through `snapshot_commit`.
        """
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        manifest = make_manifest([entry], commit=COMMIT_B)
        self.source.publish(manifest, {entry["artifact_sha256"]: data}, commit=COMMIT_B)
        problems = validate_manifest(manifest, asset_version="1077100",
                                     expected_head_commit=COMMIT_A)
        self.assertEqual(problems, [], problems)

    def test_snapshot_commit_must_agree_with_the_commit_being_read(self) -> None:
        """A new manifest that declares its read commit is held to it."""
        entry, data = make_entry("event/001/title.unity3d", b"payload")
        manifest = make_manifest([entry], commit=COMMIT_B, snapshot_commit=COMMIT_A)
        self.source.publish(manifest, {entry["artifact_sha256"]: data}, commit=COMMIT_B)
        problems = validate_manifest(manifest, asset_version="1077100",
                                     expected_head_commit=COMMIT_A)
        self.assertEqual(problems, [], problems)

        problems = validate_manifest(manifest, asset_version="1077100",
                                     expected_head_commit=COMMIT_B)
        self.assertTrue(any("disagrees with the pinned commit" in p for p in problems), problems)

        malformed = make_manifest([entry], commit=COMMIT_B, snapshot_commit="not-a-sha")
        self.source.publish(malformed, {entry["artifact_sha256"]: data}, commit=COMMIT_B)
        problems = validate_manifest(malformed, asset_version="1077100",
                                     expected_head_commit=None)
        self.assertTrue(any("snapshot_commit must be a 40-hex sha" in p for p in problems),
                        problems)

    def test_kind_and_empty_entries_are_rejected(self) -> None:
        problems = validate_manifest({"kind": "something-else"}, asset_version="1077100",
                                     expected_head_commit=None)
        self.assertTrue(any("kind must be" in p for p in problems), problems)
        empty = validate_manifest(make_manifest([]), asset_version="1077100", expected_head_commit=COMMIT_A)
        self.assertTrue(any("must not be empty" in p for p in empty), empty)


# ---------------------------------------------------------------------------------------
# 10. idempotency
# ---------------------------------------------------------------------------------------


class TestIdempotentSync(MirrorTestCase):
    def test_second_sync_downloads_nothing(self) -> None:
        entry_a, data_a = make_entry("event/001/a.unity3d", b"payload-a")
        entry_b, data_b = make_entry("event/001/b.unity3d", b"payload-b")
        manifest = make_manifest([entry_a, entry_b])
        self.publish_related(manifest, {entry_a["artifact_sha256"]: data_a,
                                       entry_b["artifact_sha256"]: data_b})

        first = self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(first["downloaded"], 2)
        self.assertEqual(first["skipped_existing"], 0)
        self.assertEqual(len(self.source.object_requests), 2)

        second = self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(second["downloaded"], 0)
        self.assertEqual(second["skipped_existing"], 2)
        self.assertEqual(len(self.source.object_requests), 2, "objects must not be re-downloaded")

        third = self.mirror.sync("1077100", dry_run=True)
        self.assertEqual(third["to_download"], [])
        self.assertEqual(len(self.source.object_requests), 2)
        self.assertEqual(third["skipped_existing"], 2)

    def test_put_bytes_is_idempotent_and_verifies_first(self) -> None:
        digest = sha256_hex(b"payload")
        self.assertTrue(self.pool.put_bytes(digest, b"payload"))
        self.assertFalse(self.pool.put_bytes(digest, b"payload"))
        with self.assertRaises(ObjectDigestMismatch):
            self.pool.put_bytes(digest, b"different bytes")


# ---------------------------------------------------------------------------------------
# 11. dry-run writes nothing
# ---------------------------------------------------------------------------------------


class TestDryRun(MirrorTestCase):
    def test_dry_run_creates_no_files_and_downloads_nothing(self) -> None:
        entry_a, data_a = make_entry("event/001/a.unity3d", b"payload-a")
        entry_b, data_b = make_entry("event/001/b.unity3d", b"payload-b")
        manifest = make_manifest([entry_a, entry_b])
        self.publish_related(manifest, {entry_a["artifact_sha256"]: data_a,
                                       entry_b["artifact_sha256"]: data_b})

        report = self.mirror.sync("1077100", dry_run=True)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["sync_status"], "planned")
        self.assertEqual(report["entry_count"], 2)
        self.assertEqual(len(report["to_download"]), 2)
        self.assertEqual(self.source.object_requests, [])
        self.assertEqual(sorted(os.listdir(self.root)), [])

    def test_dry_run_reports_partial_availability(self) -> None:
        entry_a, data_a = make_entry("event/001/a.unity3d", b"payload-a")
        entry_b, data_b = make_entry("event/001/b.unity3d", b"payload-b")
        manifest = make_manifest([entry_a, entry_b])
        self.publish_related(manifest, {entry_a["artifact_sha256"]: data_a,
                                       entry_b["artifact_sha256"]: data_b})
        self.pool.put_bytes(entry_a["artifact_sha256"], data_a)

        report = self.mirror.sync("1077100", dry_run=True)
        self.assertEqual(report["skipped_existing"], 1)
        self.assertEqual([item["artifact_sha256"] for item in report["to_download"]],
                         [entry_b["artifact_sha256"]])
        self.assertEqual(self.source.object_requests, [])


# ---------------------------------------------------------------------------------------
# 12. object pool deduplication
# ---------------------------------------------------------------------------------------


class TestPoolDeduplication(MirrorTestCase):
    def test_two_logical_paths_sharing_one_digest_store_one_object(self) -> None:
        shared = b"identical-payload"
        entry_a, _ = make_entry("event/001/title.unity3d", shared)
        entry_b, _ = make_entry("event/001/title_copy.unity3d", shared)
        self.assertNotEqual(entry_a["logical_path"], entry_b["logical_path"])
        self.assertEqual(entry_a["artifact_sha256"], entry_b["artifact_sha256"])
        manifest = make_manifest([entry_a, entry_b])
        self.publish_related(manifest, {entry_a["artifact_sha256"]: shared})

        report = self.mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(report["entry_count"], 2)
        self.assertEqual(report["object_count"], 1)
        self.assertEqual(report["downloaded"], 1)
        self.assertEqual(len(self.source.object_requests), 1)

        digests = list(self.pool.iter_digests())
        self.assertEqual(digests, [entry_a["artifact_sha256"]])
        object_files = [path for path in self.pool.objects_dir.rglob("*") if path.is_file()]
        self.assertEqual(len(object_files), 1)

        for logical_path in (entry_a["logical_path"], entry_b["logical_path"]):
            resolved = self.mirror.resolve("1077100", logical_path)
            assert resolved is not None
            self.assertEqual(resolved.pool_path, str(object_files[0]))


# ---------------------------------------------------------------------------------------
# source-level checks
# ---------------------------------------------------------------------------------------


class TestGitHubSource(unittest.TestCase):
    def _source(self, responses: dict[str, object]) -> tuple[GitHubAssetsSource, list[str]]:
        seen: list[str] = []

        def fetch(url: str, **kwargs):  # noqa: ANN001, ANN003
            from scripts.assets_mirror import FetchResponse

            seen.append(url)
            for fragment, payload in responses.items():
                if fragment in url:
                    if isinstance(payload, Exception):
                        raise payload
                    status, body, headers = payload  # type: ignore[misc]
                    return FetchResponse(status=status, body=body, headers=headers)
            return FetchResponse(status=404, body=b'{"message":"Not Found"}')

        return GitHubAssetsSource(repo=REPO, branch="main", fetchImpl=fetch), seen

    def test_head_commit_parses_and_validates(self) -> None:
        source, seen = self._source({
            "/git/ref/heads/main": (200, json.dumps({"object": {"sha": COMMIT_A, "type": "commit"}}).encode(), {}),
        })
        self.assertEqual(source.head_commit(), COMMIT_A)
        self.assertIn("/repos/kohakunamori/MLTDTranslationAssets/git/ref/heads/main", seen[0])

        bad, _ = self._source({"/git/ref/heads/main": (200, json.dumps({"object": {"sha": "zzz"}}).encode(), {})})
        with self.assertRaises(AssetsMirrorError):
            bad.head_commit()

    def test_generated_versions_reads_one_commit_tree_and_sorts_descending(self) -> None:
        source, seen = self._source({
            f"/git/trees/{COMMIT_A}?recursive=1": (
                200,
                json.dumps({
                    "truncated": False,
                    "tree": [
                        {"path": "generated/1077100/manifest.json", "type": "blob"},
                        {"path": "generated/1077600/manifest.json", "type": "blob"},
                        {"path": "generated/1077600/checksums.txt", "type": "blob"},
                        {"path": "generated/not-a-version/manifest.json", "type": "blob"},
                    ],
                }).encode(),
                {},
            ),
        })
        self.assertEqual(source.generated_versions(COMMIT_A), ["1077600", "1077100"])
        self.assertIn(f"/git/trees/{COMMIT_A}?recursive=1", seen[0])

    def test_manifest_is_base64_decoded_from_the_contents_api(self) -> None:
        manifest = make_manifest([make_entry("a/b.unity3d", b"x")[0]])
        body = json.dumps({
            "encoding": "base64",
            "content": base64.b64encode(json.dumps(manifest).encode()).decode(),
        }).encode()
        source, seen = self._source({"manifest.json": (200, body, {})})
        fetched = source.fetch_manifest("1077100", COMMIT_A)
        self.assertEqual(fetched["asset_version"], "1077100")
        self.assertIn(f"/contents/generated/1077100/manifest.json?ref={COMMIT_A}", seen[0])

    def test_large_contents_payload_uses_commit_pinned_raw_download(self) -> None:
        manifest = make_manifest([make_entry("a/b.unity3d", b"x")[0]])
        raw_url = f"https://raw.githubusercontent.com/{REPO}/{COMMIT_A}/generated/1077100/manifest.json"
        api_body = json.dumps({
            "encoding": "none",
            "content": "",
            "download_url": raw_url,
        }).encode()
        raw_body = json.dumps(manifest).encode()
        source, seen = self._source({
            "/contents/generated/1077100/manifest.json": (200, api_body, {}),
            raw_url: (200, raw_body, {}),
        })
        fetched = source.fetch_manifest("1077100", COMMIT_A)
        self.assertEqual(fetched["asset_version"], "1077100")
        self.assertEqual(seen[1], raw_url)

    def test_object_fetch_verifies_the_digest_and_rejects_mismatch(self) -> None:
        good = b"object-bytes"
        digest = sha256_hex(good)
        source, seen = self._source({"/generated/objects/sha256/": (200, good, {})})
        self.assertEqual(source.fetch_object(digest, COMMIT_A), good)
        self.assertIn(f"/{COMMIT_A}/generated/objects/sha256/{digest[:2]}/{digest}", seen[0])
        self.assertTrue(seen[0].startswith("https://raw.githubusercontent.com/"))

        wrong = sha256_hex(b"something-else")
        with self.assertRaises(ObjectDigestMismatch):
            source.fetch_object(wrong, COMMIT_A)

    def test_object_fetch_falls_back_to_the_legacy_flat_url_on_404(self) -> None:
        """A release mirrored while the repo used the old layout stays fetchable.

        The sharded URL is tried first; only a 404 -- "not at that path" -- falls
        back.  Any other failure (rate limiting, a 500) is reported as itself.
        """
        good = b"legacy-object-bytes"
        digest = sha256_hex(good)
        shard = f"/generated/objects/sha256/{digest}"
        source, seen = self._source({shard: (200, good, {})})
        self.assertEqual(source.fetch_object(digest, COMMIT_A), good)
        self.assertEqual(len(seen), 2, "the sharded URL is tried first, then the legacy flat URL")
        self.assertTrue(seen[0].endswith(f"/generated/objects/sha256/{digest[:2]}/{digest}"), seen[0])
        self.assertTrue(seen[1].endswith(shard), seen[1])

        # A 403 is a refusal, not a missing path: no fallback, and no silent retry.
        source, seen = self._source({
            "/generated/objects/sha256/": (403, b'{"message":"rate limited"}', {}),
        })
        with self.assertRaises(AssetsMirrorError):
            source.fetch_object(digest, COMMIT_A)
        self.assertEqual(len(seen), 1)

    def test_rate_limit_exposes_retry_hints_without_silent_retry(self) -> None:
        source, seen = self._source({
            "git/ref/heads/main": (429, b'{"message":"rate limited"}',
                                   {"Retry-After": "60", "X-RateLimit-Reset": "1790000000",
                                    "X-RateLimit-Remaining": "0"}),
        })
        with self.assertRaises(RateLimitError) as ctx:
            source.head_commit()
        self.assertEqual(ctx.exception.retry_after, "60")
        self.assertEqual(ctx.exception.reset_at, "1790000000")
        self.assertEqual(len(seen), 1, "a rate-limited request must not be retried silently")

    def test_asset_version_and_commit_are_validated_before_any_request(self) -> None:
        source, seen = self._source({})
        for bad_version in ("9.0.200+1077100", "current", "1077100+", ""):
            with self.assertRaises(AssetsMirrorError):
                source.fetch_manifest(bad_version, COMMIT_A)
        with self.assertRaises(AssetsMirrorError):
            source.fetch_manifest("1077100", "main")
        self.assertEqual(seen, [])


class TestPoolLayoutAndParsing(unittest.TestCase):
    def test_pool_path_layout(self) -> None:
        """Flat is canonical; the retired fan-out path is still resolvable."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = ObjectPool(tmp)
            digest = sha256_hex(b"x")
            self.assertEqual(Path(pool.path_for(digest)),
                             Path(tmp) / "objects" / "sha256" / digest)
            self.assertEqual(Path(pool.legacy_path_for(digest)),
                             Path(tmp) / "objects" / "sha256" / digest[:2] / digest)
            self.assertIsNone(pool.existing_path(digest))
            self.assertFalse(pool.has(digest))

            legacy = pool.legacy_path_for(digest)
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"x")
            self.assertEqual(pool.existing_path(digest), legacy)
            self.assertTrue(pool.has(digest))
            self.assertEqual(list(pool.iter_digests()), [digest])

            flat = pool.path_for(digest)
            flat.write_bytes(b"x")
            self.assertEqual(pool.existing_path(digest), flat,
                             "the flat path wins when both hold the bytes")
            self.assertEqual(list(pool.iter_digests()), [digest],
                             "one digest at two paths is one object, not two")

    def test_checksums_parsing_handles_binary_marker_and_noise(self) -> None:
        digest = sha256_hex(b"x")
        text = f"# comment\n\ngarbage line\n{digest} *objects/sha256/aa/{digest}\n"
        self.assertEqual(parse_checksums(text), {f"objects/sha256/aa/{digest}": digest})


class TestProducerManifestIsSyncable(MirrorTestCase):
    """The mirror must accept the manifest the *producer* actually writes.

    This is the one seam no other test crossed: every other case builds its
    manifest from ``make_manifest()``, i.e. from this module's own idea of the
    contract.  A one-word drift between the two sides (the ``kind`` literal)
    therefore stayed green here while a real ``sync`` refused the producer's
    own output on rule 1 of the validation table.  This test drives
    ``scripts/assets_generated_index.GeneratedStore`` and feeds its emitted
    manifest straight into ``sync``/``resolve``.
    """

    def test_kind_matches_the_producer_constant(self) -> None:
        from scripts import assets_generated_index as producer
        self.assertEqual(MANIFEST_KIND, producer.MANIFEST_KIND)

    def test_generated_store_output_syncs_and_resolves(self) -> None:
        from scripts import assets_generated_index as producer

        payload = b"unityfs-bytes-cross-check" * 64
        digest = sha256_hex(payload)
        logical = "event/001/title.unity3d"
        stage = self.root / "stage"
        stage.mkdir()
        blob = stage / "bundle.unity3d"
        blob.write_bytes(payload)

        store = producer.GeneratedStore(self.root / "generated")
        stored = store.put_object(blob)
        result = store.build_release(
            "1077100",
            [{
                "channel": "assets",
                "asset_version": "1077100",
                "client_version": None,
                "source_client_version": "9.0.200",
                "source_commit": SOURCE_COMMIT,
                "translation_commit": COMMIT_B,
                "generated_commit": COMMIT_A,
                "logical_key": logical,
                "logical_path": logical,
                "resource_kind": "bundle",
                "source_sha256": sha256_hex(b"ja source"),
                "translated_sha256": digest,
                "object_path": stored.rel_path,
                "artifact_sha256": digest,
                "reuse_status": "exact",
                "translation_status": "modified",
            }],
            source_client_version="9.0.200",
            source_commit=SOURCE_COMMIT,
            translation_commit=COMMIT_B,
            generated_commit=COMMIT_A,
            build_status="success",
            entries_base=stage,
        )
        self.assertTrue(result.written)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

        # No translation of the manifest in between: exactly the bytes the
        # producer wrote go into the source the mirror reads -- including the
        # provenance gate, which the commits have to satisfy for real.
        source = FakeSource(head=COMMIT_A)
        source.publish(manifest, {digest: payload})
        source.compare[SOURCE_COMMIT] = "ahead"
        source.compare[COMMIT_B] = "ahead"
        source.compare[COMMIT_A] = "identical"
        mirror = AssetVersionMirror(source, self.pool, self.root / "mirror")

        report = mirror.sync("1077100", dry_run=False)
        self.assertEqual(report["sync_status"], "success")
        resolved = mirror.resolve("1077100", logical)
        self.assertIsNotNone(resolved, "the producer's own manifest must resolve")
        self.assertEqual(resolved.artifact_sha256, digest)
        self.assertEqual(Path(resolved.pool_path).read_bytes(), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
