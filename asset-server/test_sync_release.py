#!/usr/bin/env python3
"""A single-release sync has one job: never leave a mixture on disk.

Every test here is about that.  A download that fails, a manifest that does not
describe its own objects, a checksums file that disagrees, a digest that does not
match its name -- in all of those the release already on disk has to keep serving,
because the alternative is a distributor that answers with half of two releases.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("sync_release", Path(__file__).with_name("sync_release.py"))
sync_release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync_release
spec.loader.exec_module(sync_release)

COMMIT = "a" * 40
REPOSITORY = "owner/repo"


def object_bytes(seed: str) -> bytes:
    return f"fixture object {seed}".encode() * 3


def build_release(version: str, seeds: list[str], *, generated_commit: str = COMMIT,
                  manifest_override=None, drop_from_checksums: str | None = None) -> tuple[dict, dict]:
    """A release description plus the objects it names."""
    entries, objects = [], {}
    for seed in seeds:
        body = object_bytes(seed)
        digest = hashlib.sha256(body).hexdigest()
        objects[digest] = body
        entries.append({
            "logical_path": f"production/2018/Android/{seed}.unity3d",
            "runtime_path": f"production/2018/Android/{seed}.unity3d",
            "artifact_sha256": digest,
            "object_path": f"objects/sha256/{digest}",
        })
    manifest = {"schema_version": 1, "asset_version": version, "generated_commit": generated_commit,
                "entries": entries}
    if manifest_override:
        manifest.update(manifest_override)
    lines = [f"{digest}  objects/sha256/{digest}" for digest in objects
             if digest != drop_from_checksums]
    return manifest, {"objects": objects, "checksums": "\n".join(lines) + "\n", "manifest": manifest}


class FakeGitHub:
    """An in-memory repository: a commit, a generated tree, and raw objects."""

    def __init__(self, releases: dict[str, dict], commit: str = COMMIT, extra_dirs=(),
                 api_status: int = 200):
        self.releases = releases
        self.commit = commit
        self.extra_dirs = list(extra_dirs)
        self.api_status = api_status
        self.raw_calls: list[str] = []
        self.tracked_version = max(releases, key=lambda value: (int(value), value)) if releases else ""
        self.broken_objects: set[str] = set()

    def _api(self, payload: object):
        if self.api_status != 200:
            raise sync_release.HttpError(f"HTTP {self.api_status} for the API", status=self.api_status)
        return sync_release.FetchResponse(200, json.dumps(payload).encode(), {})

    def fetch(self, url: str, **kwargs):
        if "/commits/" in url:
            return self._api({"sha": self.commit})
        if "/contents/generated?" in url:
            listing = [{"name": name, "type": "dir"} for name in
                       [*self.releases, *self.extra_dirs, "objects"]]
            return self._api(listing)
        if url.startswith(sync_release.RAW_BASE):
            path = url.split(f"{sync_release.RAW_BASE}/{REPOSITORY}/", 1)[1].split("?", 1)[0]
            _commit, *rest = path.split("/")
            body_path = "/".join(rest)
            if body_path == "manifests/asset-version.json":
                return sync_release.FetchResponse(
                    200, json.dumps({"asset_version": self.tracked_version}).encode(), {})
            if body_path.startswith("generated/") and body_path.split("/")[2] in ("manifest.json",
                                                                                 "checksums.txt"):
                _, version, name = body_path.split("/", 2)
                release = self.releases[version]
                body = json.dumps(release["manifest"]) if name == "manifest.json" else release["checksums"]
                return sync_release.FetchResponse(200, body.encode(), {})
            if body_path.startswith(f"generated/{sync_release.OBJECT_DIRNAME}/"):
                digest = body_path.rsplit("/", 1)[-1]
                self.raw_calls.append(digest)
                for release in self.releases.values():
                    if digest in release["objects"]:
                        body = release["objects"][digest]
                        if digest in self.broken_objects:
                            body = b"tampered"
                        return sync_release.FetchResponse(200, body, {})
                return sync_release.FetchResponse(404, b"", {})
        raise AssertionError(f"unexpected URL {url}")


class NewestVersionTests(unittest.TestCase):
    def test_the_newest_release_is_chosen_by_number_not_by_name(self):
        source = sync_release.GitHubReleaseSource(repository=REPOSITORY,
                                                 fetch=FakeGitHub({"99999": {}, "1077741": {}}).fetch)
        self.assertEqual(source.newest_version(COMMIT), "1077741")

    def test_non_release_directories_are_ignored(self):
        fake = FakeGitHub({"1077741": {}}, extra_dirs=("objects", "notes", "1077741-beta"))
        source = sync_release.GitHubReleaseSource(repository=REPOSITORY, fetch=fake.fetch)
        self.assertEqual(source.newest_version(COMMIT), "1077741")

    def test_a_repository_without_releases_is_an_error(self):
        fake = FakeGitHub({}, extra_dirs=("objects",))
        source = sync_release.GitHubReleaseSource(repository=REPOSITORY, fetch=fake.fetch)
        with self.assertRaises(sync_release.SyncError):
            source.newest_version(COMMIT)

    def test_a_rate_limited_api_falls_back_to_the_branch_and_the_tracked_version(self):
        # GitHub allows 60 unauthenticated API calls an hour for the whole machine.
        # A sync that stops when that runs out stops mirroring, so the release is
        # readable without any API call at all.
        fake = FakeGitHub({"1077741": {}}, api_status=403)
        source = sync_release.GitHubReleaseSource(repository=REPOSITORY, fetch=fake.fetch)
        self.assertEqual(source.head_commit(), source.branch)
        self.assertEqual(source.newest_version(source.branch), "1077741")

    def test_reading_a_branch_ref_asks_for_a_fresh_copy(self):
        _, bundle = build_release("1077741", ["alpha"])
        fake = FakeGitHub({"1077741": bundle})
        source = sync_release.GitHubReleaseSource(repository=REPOSITORY, fetch=fake.fetch)
        seen: list[str] = []
        source.fetch = lambda url, **kwargs: (seen.append(url), fake.fetch(url))[1]
        source.fetch_text("generated/1077741/manifest.json", "main")
        self.assertIn("?sync=", seen[0])
        seen.clear()
        source.fetch_text("generated/1077741/manifest.json", COMMIT)
        self.assertNotIn("?sync=", seen[0])


class SyncOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mltd-sync-release-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"

    def sync(self, fake, **kwargs):
        return sync_release.sync_once(
            self.root, source=sync_release.GitHubReleaseSource(repository=REPOSITORY, fetch=fake.fetch),
            workers=2, **kwargs)

    def test_a_fresh_root_gets_the_whole_release(self):
        manifest, bundle = build_release("1077741", ["alpha", "beta"])
        fake = FakeGitHub({"1077741": bundle})
        report = self.sync(fake)

        self.assertEqual(report["sync_status"], "success")
        self.assertEqual(report["asset_version"], "1077741")
        self.assertEqual(report["downloaded"], 2)
        self.assertTrue(report["verification"]["ok"])
        self.assertEqual(json.loads((self.root / "manifest.json").read_text(encoding="utf-8")), manifest)
        for digest, body in bundle["objects"].items():
            self.assertEqual((self.root / sync_release.OBJECT_DIRNAME / digest).read_bytes(), body)
        version = json.loads((self.root / "version.json").read_text(encoding="utf-8"))
        self.assertEqual((version["asset_version"], version["entries"]), ("1077741", 2))

    def test_a_second_run_downloads_nothing_new(self):
        _, bundle = build_release("1077741", ["alpha"])
        fake = FakeGitHub({"1077741": bundle})
        self.sync(fake)
        report = self.sync(fake)
        self.assertEqual(report["downloaded"], 0)
        self.assertTrue(report["unchanged"])

    def test_objects_the_new_release_does_not_use_are_removed(self):
        _, old = build_release("1077720", ["alpha", "retired"])
        fake = FakeGitHub({"1077720": old})
        self.sync(fake)
        retired = next(iter(old["objects"]))
        # Promote a release that no longer carries 'retired'.
        _, current = build_release("1077741", ["alpha"])
        fake.releases = {"1077741": current}
        report = self.sync(fake)
        self.assertEqual(report["asset_version"], "1077741")
        self.assertEqual(report["removed"], 1)
        leftover = sorted(p.name for p in (self.root / sync_release.OBJECT_DIRNAME).iterdir())
        self.assertEqual(leftover, sorted(current["objects"]))

    def test_a_tampered_object_leaves_the_previous_release_serving(self):
        _, old = build_release("1077720", ["alpha"])
        fake = FakeGitHub({"1077720": old})
        self.sync(fake)
        before = (self.root / "manifest.json").read_bytes()

        _, current = build_release("1077741", ["alpha", "gamma"])
        fake.releases = {"1077741": current}
        fake.broken_objects = set(current["objects"])
        with self.assertRaises(sync_release.SyncError):
            self.sync(fake)

        self.assertEqual((self.root / "manifest.json").read_bytes(), before)
        self.assertEqual(json.loads((self.root / "version.json").read_text(encoding="utf-8"))["asset_version"],
                         "1077720")
        # The object that failed verification is not left behind to be served later.
        leftover = {p.name for p in (self.root / sync_release.OBJECT_DIRNAME).iterdir()}
        self.assertTrue(leftover.issubset(set(old["objects"]) | set(current["objects"])))
        self.assertTrue(all(p.read_bytes() in old["objects"].values()
                            or hashlib.sha256(p.read_bytes()).hexdigest() == p.name for p in
                            (self.root / sync_release.OBJECT_DIRNAME).iterdir()))

    def test_a_manifest_that_does_not_describe_its_own_object_is_refused_before_downloading(self):
        _, bundle = build_release("1077741", ["alpha"])
        broken = dict(bundle)
        broken["manifest"] = json.loads(json.dumps(bundle["manifest"]))
        broken["manifest"]["entries"][0]["object_path"] = "objects/sha256/" + "0" * 64
        fake = FakeGitHub({"1077741": broken})
        with self.assertRaises(sync_release.SyncError):
            self.sync(fake)
        self.assertEqual(fake.raw_calls, [])
        self.assertFalse((self.root / "manifest.json").exists())

    def test_a_checksums_file_that_omits_an_object_is_refused(self):
        manifest, bundle = build_release("1077741", ["alpha", "beta"])
        digest = hashlib.sha256(object_bytes("beta")).hexdigest()
        fake = FakeGitHub({"1077741": {**bundle, "manifest": manifest, "checksums":
                                       bundle["checksums"].replace(
                                           f"{digest}  objects/sha256/{digest}\n", "")}})
        with self.assertRaises(sync_release.SyncError):
            self.sync(fake)
        self.assertFalse((self.root / "manifest.json").exists())

    def test_a_manifest_for_another_version_is_refused(self):
        manifest, bundle = build_release("1077741", ["alpha"])
        manifest["asset_version"] = "1077720"
        fake = FakeGitHub({"1077741": {**bundle, "manifest": manifest}})
        with self.assertRaises(sync_release.SyncError):
            self.sync(fake)

    def test_the_whole_sync_survives_an_api_that_refuses_every_call(self):
        _, bundle = build_release("1077741", ["alpha", "beta"])
        fake = FakeGitHub({"1077741": bundle}, api_status=403)
        report = self.sync(fake)
        self.assertEqual(report["sync_status"], "success")
        self.assertEqual(report["downloaded"], 2)
        self.assertEqual(report["source_commit"], sync_release.DEFAULT_BRANCH)
        self.assertTrue(report["verification"]["ok"])

    def test_the_root_holds_exactly_the_release_and_nothing_else(self):
        # The swap writes through temporary files beside their targets; a leftover
        # one would be an unpublished file sitting in the served directory.
        _, bundle = build_release("1077741", ["alpha"])
        self.sync(FakeGitHub({"1077741": bundle}))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         [sync_release.CHECKSUMS_NAME, sync_release.MANIFEST_NAME,
                          sync_release.OBJECT_DIRNAME.split("/")[0], sync_release.VERSION_NAME])
        pool = self.root / sync_release.OBJECT_DIRNAME
        self.assertEqual(sorted(p.name for p in pool.iterdir()), sorted(bundle["objects"]))


class ChecksumsTests(unittest.TestCase):
    def test_a_checksums_line_without_a_digest_is_refused(self):
        with self.assertRaises(sync_release.SyncError):
            sync_release.parse_checksums("objects/sha256/abc  objects/sha256/abc\n")

    def test_comments_and_blank_lines_are_ignored(self):
        digest = "c" * 64
        parsed = sync_release.parse_checksums(f"\n# generated\n{digest}  objects/sha256/{digest}\n")
        self.assertEqual(parsed, {f"objects/sha256/{digest}": digest})


if __name__ == "__main__":
    unittest.main()
