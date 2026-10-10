#!/usr/bin/env python3
"""Offline contract for the deployed NAS `mltd-generated-assets` closure.

The NAS writes generated releases into
`/vol2/1000/imas-asset-archive/mltd/generated` and serves them read-only.  Until
2026-10-08 the only copy of `sync_loop.py`, the image recipe and the compose file
lived on the NAS itself, and the deployed nginx vhost had no repository copy at
all; `deployed.json` and this suite make the deployed shape reviewable, and make
silent drift fail.

No network, no SSH, no docker: every assertion is a file-content contract plus the
recorded hashes.

Repository hashes are taken from the committed blob (`git cat-file blob HEAD:<path>`),
not from the working tree, so the suite means the same thing on a CRLF Windows checkout
and on a Linux CI checkout.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLOSURE = ROOT / "asset-server" / "generated-assets"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blob_sha256(relative: str) -> str:
    """Hash the committed blob, so EOL normalisation cannot change the answer."""
    data = subprocess.run(
        ["git", "cat-file", "blob", f"HEAD:{relative}"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout
    return hashlib.sha256(data).hexdigest()


class DeploymentRecordTests(unittest.TestCase):
    """`deployed.json` must describe the deployed bytes, not a wish."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.record = json.loads((CLOSURE / "deployed.json").read_text(encoding="utf-8"))

    def test_record_identity(self):
        self.assertEqual(self.record["kind"], "mltd-generated-assets-deployment-record")
        self.assertEqual(self.record["schema_version"], 1)
        self.assertTrue(self.record["observed_at"])
        self.assertEqual(self.record["host_alias"], "nas")

    def test_every_recorded_file_matches_its_repository_copy(self):
        for entry in self.record["files"]:
            with self.subTest(entry["nas_path"]):
                self.assertTrue((ROOT / entry["repo_source"]).is_file(),
                                f"missing repo source {entry['repo_source']}")
                self.assertEqual(blob_sha256(entry["repo_source"]), entry["repo_sha256"])

    def test_identical_files_really_are_identical(self):
        for entry in self.record["files"]:
            if not entry["repo_matches_deployed"]:
                continue
            with self.subTest(entry["nas_path"]):
                self.assertEqual(entry["repo_sha256"], entry["deployed_sha256"])

    def test_recorded_drift_is_real_drift(self):
        """A `false` entry must carry both hashes and an explanation.

        This used to also demand that at least one such entry existed, because the
        NAS ran an older revision of the mirror module and a record claiming full
        convergence would have been a lie.  The 2026-10-10 deployment converged
        every managed file, so demanding a permanent divergence would now ask the
        record to lie in the other direction.  What must still hold is that every
        entry claiming a difference really differs, and says why.
        """
        for entry in self.record["files"]:
            if entry["repo_matches_deployed"]:
                continue
            with self.subTest(entry["nas_path"]):
                self.assertNotEqual(entry["repo_sha256"], entry["deployed_sha256"])
                self.assertTrue(entry.get("note"), "drift needs an explanation")

    def test_the_record_is_internally_consistent(self):
        """A `true` entry must name one hash, not two that disagree."""
        for entry in self.record["files"]:
            with self.subTest(entry["nas_path"]):
                if entry["repo_matches_deployed"]:
                    self.assertEqual(entry["repo_sha256"], entry["deployed_sha256"])
                    self.assertNotIn("note", entry)
                self.assertRegex(entry["deployed_sha256"], r"^[0-9a-f]{64}$")

    def test_services_and_route_are_recorded(self):
        services = self.record["services"]
        self.assertIn("--bind 127.0.0.1 --port 18765", services["generated-assets"]["command"])
        self.assertEqual(services["generated-assets-sync"]["interval_seconds"], 21600)
        self.assertEqual(services["generated-assets-sync"]["repository"], "kohakunamori/MLTDTranslationAssets")


class SyncLoopContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (CLOSURE / "sync_loop.py").read_text(encoding="utf-8")

    def test_no_current_pointer_is_ever_used(self):
        self.assertNotIn("current.json", self.text)
        self.assertIn('"current_pointer_used": False', self.text)
        self.assertIn('"current_pointer_written": False', self.text)

    def test_versions_are_discovered_from_the_generated_directory(self):
        self.assertIn("/contents/generated?ref=", self.text)
        self.assertIn("isdigit()", self.text)
        self.assertIn('item.get("type") == "dir"', self.text)

    def test_only_successful_builds_are_mirrored(self):
        guard = self.text.index('manifest.get("build_status") != "success"')
        sync = self.text.index("mirror.sync(asset_version, commit=head, dry_run=False)")
        self.assertLess(guard, sync, "a refused build_status must be checked before the sync")

    def test_single_writer_lock_is_non_blocking(self):
        self.assertIn("fcntl.flock", self.text)
        self.assertIn("LOCK_EX | fcntl.LOCK_NB", self.text)
        self.assertIn("another generated-assets sync is already running", self.text)

    def test_the_loop_keeps_running_after_a_failed_tick(self):
        self.assertIn("keep the distributor alive; next poll retries", self.text)
        self.assertIn("time.sleep(args.interval)", self.text)

    def test_it_consumes_the_repository_mirror_module(self):
        self.assertIn("from assets_mirror import", self.text)
        for name in ("AssetVersionMirror", "GitHubAssetsSource", "ObjectPool",
                     "AssetsMirrorError", "ManifestValidationError"):
            self.assertIn(name, self.text)
        # Two implementations would be a divergence risk.
        self.assertNotIn("def sync(", self.text)


class ImageAndComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (CLOSURE / "Dockerfile").read_text(encoding="utf-8")
        cls.compose = (CLOSURE / "docker-compose.yml").read_text(encoding="utf-8")

    def test_image_is_built_from_its_own_uploaded_context(self):
        for copied in ("serve_release.py", "sync_release.py", "assets_route.py", "scripts", "sync_loop.py"):
            self.assertIn(f"COPY {copied}", self.dockerfile)

    def test_the_deployment_runs_the_single_release_programs(self):
        # The reader holds one release; the loop keeps that one release in step.
        self.assertIn('"python", "/app/serve_release.py", "serve"', self.compose)
        self.assertIn('"python", "/app/sync_release.py", "--root", "/data"', self.compose)

    def test_route_service_binds_loopback_only(self):
        self.assertIn('"--bind", "127.0.0.1", "--port", "18765"', self.compose)
        self.assertNotIn("0.0.0.0", self.compose)

    def test_route_service_cannot_write_the_mirror(self):
        route_block = self.compose.split("generated-assets-sync:")[0]
        self.assertIn("/generated:/data:ro", route_block)
        self.assertIn("read_only: true", route_block)
        self.assertIn("no-new-privileges:true", route_block)

    def test_official_fallback_is_explicit_and_outside_the_cas(self):
        self.assertIn("--official-base-url", self.compose)
        self.assertIn("--official-cache-root", self.compose)
        self.assertIn("/generated-official-cache:/official-cache", self.compose)

    def test_sync_service_is_the_only_writer_and_is_pinned_to_one_repository(self):
        sync_block = self.compose.split("generated-assets-sync:")[1]
        self.assertIn("/generated:/data", sync_block)
        self.assertNotIn("/data:ro", sync_block)
        self.assertIn("MLTD_ASSETS_REPOSITORY: kohakunamori/MLTDTranslationAssets", sync_block)
        self.assertIn("MLTD_ASSETS_BRANCH: main", sync_block)
        self.assertIn("MLTD_SYNC_INTERVAL", sync_block)
        self.assertIn("restart: unless-stopped", sync_block)


class VhostContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vhost = (ROOT / "asset-server" / "nginx-vhost.conf").read_text(encoding="utf-8")

    def _location(self, prefix: str) -> str:
        start = self.vhost.index(f"location {prefix}")
        rest = self.vhost[start + 1:]
        end = rest.find("location ")
        return self.vhost[start:] if end == -1 else self.vhost[start:start + 1 + end]

    def test_generated_route_proxies_the_loopback_resolver(self):
        block = self._location("^~ /generated-assets/")
        self.assertIn("proxy_pass http://127.0.0.1:18765/assets/;", block)

    def test_generated_route_is_read_only(self):
        block = self._location("^~ /generated-assets/")
        self.assertIn("limit_except GET HEAD", block)

    def test_generated_route_carries_no_current_pointer(self):
        block = self._location("^~ /generated-assets/")
        self.assertNotIn("current", block)
        self.assertNotIn("alias", block)

    def test_official_namespaces_are_untouched(self):
        self.assertIn("alias /srv/imas/mltd/current/;", self.vhost)
        self.assertIn("alias /srv/imas/mltd/views/;", self.vhost)
        native = re.search(r"location ~ \^/\(\[0-9\]\+\)/production/2018/Android/\(\.\+\)\$", self.vhost)
        self.assertIsNotNone(native, "the native version route must stay")


if __name__ == "__main__":
    unittest.main()
