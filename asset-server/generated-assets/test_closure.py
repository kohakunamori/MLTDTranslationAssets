#!/usr/bin/env python3
"""Offline contract for the historical `mltd-generated-assets` source closure.

`deployed.json` records repository-relative sources and service contracts;
machine-specific deployment paths are intentionally kept out of this repository.

No network, no SSH, no docker: every assertion is a local source and behavior contract.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLOSURE = ROOT / "asset-server" / "generated-assets"


class SourceRecordTests(unittest.TestCase):
    """The source record describes files and behavior without host details."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.record = json.loads((CLOSURE / "deployed.json").read_text(encoding="utf-8"))

    def test_record_identity(self):
        self.assertEqual(self.record["kind"], "mltd-generated-assets-source-record")
        self.assertEqual(self.record["schema_version"], 2)
        self.assertTrue(self.record["observed_at"])
        self.assertEqual(self.record["status"], "retired")
        self.assertNotIn("host_alias", self.record)
        self.assertNotIn("project_dir", self.record)
        self.assertNotIn("mirror_root", self.record)

    def test_every_recorded_source_exists_in_the_repository(self):
        for entry in self.record["files"]:
            with self.subTest(entry["repo_source"]):
                self.assertTrue((ROOT / entry["repo_source"]).is_file(),
                                f"missing repo source {entry['repo_source']}")
                self.assertTrue(entry.get("role"))

    def test_service_contracts_are_recorded(self):
        services = self.record["services"]
        self.assertEqual(services["generated-assets"]["bind"], "127.0.0.1")
        self.assertEqual(services["generated-assets"]["port"], 18765)
        self.assertEqual(services["generated-assets-sync"]["interval_seconds"], 21600)
        self.assertEqual(services["generated-assets-sync"]["repository"], "kohakunamori/MLTDTranslationAssets")


class SyncLoopContractTests(unittest.TestCase):
    """The loop keeps one release in step, and never loses the one it has."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (CLOSURE.parent / "sync_release.py").read_text(encoding="utf-8")

    def test_the_newest_release_is_discovered_rather_than_listed(self):
        self.assertIn("def newest_version(", self.text)
        # A version list is how the old deployment ended up holding eight of them.
        self.assertNotIn("ASSETS_MIRROR_VERSIONS", self.text)

    def test_a_failed_tick_leaves_the_previous_release_serving(self):
        self.assertIn("kept the previous release", self.text)
        self.assertIn("A failed tick is not a reason to stop", self.text)
        self.assertIn("time.sleep(args.interval)", self.text)

    def test_only_one_writer_may_hold_the_mirror(self):
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", self.text)
        self.assertIn("another generated-assets sync is already running", self.text)
        # Without the lock a slower tick can swap an older release back over a newer one.
        self.assertIn("single_writer_lock", self.text)


class ImageAndComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (CLOSURE / "Dockerfile").read_text(encoding="utf-8")
        cls.compose = (CLOSURE / "docker-compose.yml").read_text(encoding="utf-8")

    def test_image_is_built_from_its_own_uploaded_context(self):
        for copied in ("serve_release.py", "sync_release.py"):
            self.assertIn(f"COPY {copied}", self.dockerfile)
        # The programs that carried a version list are gone; an image that still
        # copies them is an image that can still be started against eight versions.
        for gone in ("assets_route.py", "sync_loop.py", "scripts"):
            self.assertNotIn(f"COPY {gone}", self.dockerfile)

    def test_the_deployment_runs_the_single_release_programs(self):
        # The reader holds one release; the loop keeps that one release in step.
        self.assertIn('"python", "/app/serve_release.py", "serve"', self.compose)
        self.assertIn('"python", "/app/sync_release.py", "--root", "/data"', self.compose)

    def test_route_service_binds_loopback_only(self):
        self.assertIn('"--bind", "127.0.0.1", "--port", "18765"', self.compose)
        self.assertNotIn("0.0.0.0", self.compose)

    def test_route_service_cannot_write_the_mirror(self):
        route_block = self.compose.split("generated-assets-sync:")[0]
        self.assertIn("${MLTD_GENERATED_ROOT:?Set MLTD_GENERATED_ROOT in .env}:/data:ro", route_block)
        self.assertIn("read_only: true", route_block)
        self.assertIn("no-new-privileges:true", route_block)

    def test_official_fallback_is_explicit_and_outside_the_cas(self):
        self.assertIn("--official-base-url", self.compose)
        self.assertIn("--official-cache-root", self.compose)
        self.assertIn("${MLTD_OFFICIAL_CACHE_ROOT:?Set MLTD_OFFICIAL_CACHE_ROOT in .env}:/official-cache", self.compose)

    def test_sync_service_is_the_only_writer_and_is_pinned_to_one_repository(self):
        sync_block = self.compose.split("generated-assets-sync:")[1]
        self.assertIn("${MLTD_GENERATED_ROOT:?Set MLTD_GENERATED_ROOT in .env}:/data", sync_block)
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
