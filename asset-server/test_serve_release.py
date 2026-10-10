#!/usr/bin/env python3
"""One release, served honestly.

The reader has no version to select, so the tests are about the promises that
remain: the bytes hash to the digest the release recorded, an untranslated bundle
falls back to the official original *for that client's version*, and nothing is ever
answered from outside the configured roots.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("serve_release", Path(__file__).with_name("serve_release.py"))
serve_release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = serve_release
spec.loader.exec_module(serve_release)

VERSION = "1077741"
RUNTIME = "310c6889a525095e3cd3455aee18305d3594abec.unity3d"
LOGICAL = "scrobj_ittana.unity3d"
TRANSLATED = "japanese line\nchinese line".encode()
OFFICIAL = b"official japanese original"
OFFICIAL_ONLY = "cd_jp.gtx.unity3d"


def build_root(root: Path, *, checksums_agree: bool = True) -> str:
    """A release that translated exactly one bundle, and nothing else."""
    digest = hashlib.sha256(TRANSLATED).hexdigest()
    entries = [{"logical_path": f"production/2018/Android/{LOGICAL}",
                "runtime_path": f"production/2018/Android/{RUNTIME}",
                "artifact_sha256": digest, "object_path": f"objects/sha256/{digest}"}]
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "asset_version": VERSION, "entries": entries}), encoding="utf-8")
    line = f"{digest}  objects/sha256/{digest}"
    if not checksums_agree:
        line = f"{'0' * 64}  objects/sha256/{digest}"
    (root / "checksums.txt").write_text(line + "\n", encoding="utf-8")
    pool = root / "objects" / "sha256"
    pool.mkdir(parents=True, exist_ok=True)
    (pool / digest).write_bytes(TRANSLATED)
    return digest


class ServeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mltd-serve-release-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "data"
        self.official_root = self.base / "official"
        self.legacy_root = self.base / "cn-version"
        self.cdn_root = self.base / "cdn"
        self.digest = build_root(self.root)

        # The official archive holds originals for the bundles this release does not
        # translate, including the one whose runtime name the release *does* know.
        fallback = self.official_root / "views" / VERSION / "jp-android"
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / OFFICIAL_ONLY).write_bytes(b"official from the archive")
        (fallback / RUNTIME).write_bytes(b"official from the archive")
        overlay = self.legacy_root / VERSION / "jp-android" / OFFICIAL_ONLY
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_bytes(b"legacy cn overlay")
        cdn = self.cdn_root / VERSION / "production" / "2018" / "Android" / "only-on-cdn.unity3d"
        cdn.parent.mkdir(parents=True, exist_ok=True)
        cdn.write_bytes(b"official from the cdn")

        cdn_hits: list[str] = []
        self.cdn_hits = cdn_hits
        cdn_root = self.cdn_root

        class CdnHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                cdn_hits.append(self.path)
                candidate = cdn_root / self.path.lstrip("/")
                if candidate.is_file():
                    body = candidate.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

        self.cdn = ThreadingHTTPServer(("127.0.0.1", 0), CdnHandler)
        threading.Thread(target=self.cdn.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_cdn)
        self.start_server()

    def stop_cdn(self):
        self.cdn.shutdown()
        self.cdn.server_close()

    def start_server(self, *, official_root=True, legacy_root=True, base_url=True):
        distributor = serve_release.Distributor(
            serve_release.ReleaseStore(self.root),
            official_root=self.official_root if official_root else None,
            official_base_url=(f"http://127.0.0.1:{self.cdn.server_port}" if base_url else None),
            official_cache_root=self.base / "cache",
            legacy_overlay_root=self.legacy_root if legacy_root else None,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve_release.handler_for(distributor))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_the_translated_object_is_served_under_its_runtime_name(self):
        status, headers, body = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}")
        self.assertEqual((status, body), (200, TRANSLATED))
        self.assertEqual(headers["ETag"], f'"{self.digest}"')
        self.assertEqual(headers["X-Asset-Source"], "translated")
        self.assertEqual(headers["Content-Length"], str(len(TRANSLATED)))

    def test_the_logical_name_reaches_the_same_object(self):
        status, _, body = self.request(f"/assets/{VERSION}/production/2018/Android/{LOGICAL}")
        self.assertEqual((status, body), (200, TRANSLATED))

    def test_the_version_in_the_path_does_not_select_a_release(self):
        # Clients follow the official version, so a request that names another one
        # still has to find the release this distributor holds.
        status, _, body = self.request(f"/assets/1077100/production/2018/Android/{RUNTIME}")
        self.assertEqual((status, body), (200, TRANSLATED))

    def test_an_untranslated_bundle_falls_back_to_the_official_archive(self):
        status, headers, body = self.request(f"/assets/{VERSION}/production/2018/Android/{OFFICIAL_ONLY}")
        self.assertEqual((status, body), (200, b"official from the archive"))
        self.assertEqual(headers["X-Asset-Source"], "official-jp")

    def test_the_legacy_overlay_is_only_used_for_the_cn_namespace(self):
        status, headers, body = self.request(f"/assets/{VERSION}/production/2018/Android/{OFFICIAL_ONLY}",
                                             headers={"X-MLTD-Asset-Namespace": "cn"})
        self.assertEqual((status, body), (200, b"legacy cn overlay"))
        self.assertEqual(headers["X-Asset-Source"], "legacy-cn-overlay")

    def test_a_bundle_the_release_never_carried_comes_from_the_cdn(self):
        status, headers, body = self.request(f"/assets/{VERSION}/production/2018/Android/only-on-cdn.unity3d")
        self.assertEqual((status, body), (200, b"official from the cdn"))
        self.assertEqual(headers["X-Asset-Source"], "official-cdn")
        before = len(self.cdn_hits)
        status, _, body = self.request(f"/assets/{VERSION}/production/2018/Android/only-on-cdn.unity3d")
        self.assertEqual((status, body), (200, b"official from the cdn"))
        self.assertEqual(len(self.cdn_hits), before, "the second request must come from the cache")

    def test_an_object_missing_from_the_pool_is_not_invented(self):
        (self.root / serve_release.OBJECT_DIRNAME / self.digest).unlink()
        status, _, _ = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}")
        self.assertEqual(status, 404)

    def test_an_object_whose_bytes_changed_is_not_served(self):
        (self.root / serve_release.OBJECT_DIRNAME / self.digest).write_bytes(b"tampered")
        status, _, _ = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}")
        self.assertEqual(status, 404)

    def test_a_checksums_file_that_disagrees_disables_that_entry(self):
        build_root(self.root, checksums_agree=False)
        status, headers, body = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}")
        # The entry is not trustworthy, so the client gets the official original
        # rather than bytes nothing vouches for.
        self.assertEqual((status, body), (200, b"official from the archive"))
        self.assertEqual(headers["X-Asset-Source"], "official-jp")

    def test_head_returns_the_headers_without_a_body(self):
        status, headers, body = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}",
                                             method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["Content-Length"], str(len(TRANSLATED)))

    def test_a_matching_etag_gets_a_304(self):
        status, headers, _ = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}",
                                          headers={"If-None-Match": f'"{self.digest}"'})
        self.assertEqual(status, 304)

    def test_writing_is_not_allowed(self):
        for method in ("POST", "PUT", "DELETE"):
            status, _, _ = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}", method=method)
            self.assertEqual(status, 405)

    def test_a_path_that_escapes_the_roots_is_not_served(self):
        for path in (f"/assets/{VERSION}/../../etc/passwd",
                     "/assets/1077741/production/2018/Android/../../../etc/passwd",
                     "/assets/1077741/"):
            status, _, _ = self.request(path)
            self.assertEqual(status, 404, path)

    def test_without_a_release_every_request_falls_through_to_the_official_original(self):
        (self.root / "manifest.json").unlink()
        self.start_server()
        status, headers, body = self.request(f"/assets/{VERSION}/production/2018/Android/{RUNTIME}")
        self.assertEqual(headers.get("X-Asset-Source"), "official-jp")
        self.assertEqual(status, 200)


class RequestTargetTests(unittest.TestCase):
    def test_the_version_segment_is_optional(self):
        self.assertEqual(serve_release.request_target("/assets/production/2018/Android/x.unity3d"),
                         ("", "production/2018/Android/x.unity3d"))
        self.assertEqual(serve_release.request_target("/assets/current/production/2018/Android/x.unity3d"),
                         ("current", "production/2018/Android/x.unity3d"))

    def test_a_query_string_is_not_part_of_the_path(self):
        self.assertEqual(serve_release.request_target("/assets/1077741/a/b.unity3d?v=2"),
                         ("1077741", "a/b.unity3d"))

    def test_an_empty_or_traversing_path_is_refused(self):
        for path in ("", "/", "/assets/", "/assets/1077741/", "/assets/1077741/../x"):
            self.assertIsNone(serve_release.request_target(path), path)


if __name__ == "__main__":
    unittest.main()
