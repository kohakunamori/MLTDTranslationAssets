#!/usr/bin/env python3
"""Local fixtures + real loopback HTTP only; never production Unity/HTTP evidence."""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

import msgpack

# 本仓根目录插入 sys.path：从产品仓自身导入 scripts/*（仓库无 __init__.py，
# 走 Python 3 命名空间包）。不依赖任何兄弟仓库或 PYTHONPATH 回退。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.assets_mirror import AssetVersionMirror, ObjectPool
from scripts.test_assets_mirror import FakeSource, COMMIT_A, make_entry, make_manifest
spec = importlib.util.spec_from_file_location("assets_route", Path(__file__).with_name("assets_route.py"))
route = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = route
spec.loader.exec_module(route)

class ReadOnlyRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mltd-route-unit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mirror"
        self.official_root = Path(self.tmp.name) / "official"
        self.legacy_root = Path(self.tmp.name) / "legacy-cn"
        self.source = FakeSource()
        self.mirror = AssetVersionMirror(self.source, ObjectPool(self.root), self.root)
        self.payload = b"unit fixture; not Unity3D production bytes"
        for version in ("1077100", "1077500"):
            entry, payload = make_entry("event/001/title.unity3d", self.payload + version.encode())
            manifest = make_manifest([entry], asset_version=version, commit=COMMIT_A)
            self.source.publish(manifest, {entry["artifact_sha256"]: payload}, commit=COMMIT_A)
            for field in ("source_commit", "translation_commit", "generated_commit"):
                commit = manifest.get(field)
                if isinstance(commit, str):
                    self.source.compare[commit] = "identical" if commit == COMMIT_A else "ahead"
            self.mirror.sync(version, commit=COMMIT_A, dry_run=False)
        fallback = self.official_root / "views" / "1077500" / "jp-android" / "event/001/untranslated.unity3d"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(b"official-japanese-fallback")
        official_manifest = self.official_root / "manifest.json"
        official_manifest.write_text(json.dumps({"releases": {
            "1077500": {"index_name": "fixture.data"}
        }}), encoding="utf-8")
        official_index = self.official_root / "views" / "1077500" / "jp-android" / "fixture.data"
        official_index.parent.mkdir(parents=True, exist_ok=True)
        official_index.write_bytes(msgpack.packb([{
            "event/001/title.unity3d": ["catalog", "0123456789abcdef0123456789abcdef01234567.unity3d", 1]
        }], use_bin_type=True))
        legacy = self.legacy_root / "1077200" / "jp-android" / "event/001/title.unity3d"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"legacy-cn-overlay")
        self.server = route.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            route.handler_for(
                self.mirror,
                official_root=self.official_root,
                legacy_overlay_root=self.legacy_root,
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()
    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
    def test_original_path_and_multiple_versions_are_exact(self):
        for version in ("1077100", "1077500"):
            status, headers, body = self.request(f"/assets/{version}/event/001/title.unity3d")
            self.assertEqual(status, 200)
            self.assertEqual(body, self.payload + version.encode())
            self.assertEqual(headers["ETag"], '"' + hashlib.sha256(body).hexdigest() + '"')
            self.assertIn("must-revalidate", headers["Cache-Control"])
            status, _, body = self.request(
                f"/assets/{version}/production/2018/Android/event/001/title.unity3d"
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, self.payload + version.encode())
    def test_client_runtime_path_alias_resolves_same_translated_object(self):
        manifest_path = self.mirror.manifest_path("1077500")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["entries"][0]
        entry["runtime_path"] = (
            "production/2018/Android/"
            "0123456789abcdef0123456789abcdef01234567.unity3d"
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        status, headers, body = self.request(
            "/assets/1077500/production/2018/Android/"
            "0123456789abcdef0123456789abcdef01234567.unity3d"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload + b"1077500")
        self.assertEqual(headers["X-Asset-Source"], "translated")
    def test_read_does_not_generate_views_manifests_alias_maps_or_pointers(self):
        before = self.snapshot()
        self.assertEqual(self.request("/assets/1077100/event/001/title.unity3d")[0], 200)
        self.assertEqual(self.snapshot(), before)
    def test_current_uses_latest_successful_version_and_unknown_paths_are_404(self):
        decoy = self.root / "current" / "event/001/title.unity3d"
        decoy.parent.mkdir(parents=True)
        decoy.write_bytes(b"not authorized")
        status, _, body = self.request("/assets/current/event/001/title.unity3d")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload + b"1077500")
        status, _, body = self.request("/assets/event/001/title.unity3d")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload + b"1077500")
        for path in ("/assets/1077600/event/001/title.unity3d", "/assets/1077100/other.unity3d", "/objects/sha256/test"):
            self.assertEqual(self.request(path)[0], 404, path)

    def test_missing_translation_falls_back_to_official_japanese_file(self):
        status, headers, body = self.request("/assets/current/event/001/untranslated.unity3d")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"official-japanese-fallback")
        self.assertEqual(headers["X-Asset-Source"], "official-jp")

    def test_cn_namespace_bridges_legacy_overlay_for_unpublished_version(self):
        status, headers, body = self.request(
            "/assets/1077200/production/2018/Android/event/001/title.unity3d",
            headers={"X-MLTD-Asset-Namespace": "cn"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"legacy-cn-overlay")
        self.assertEqual(headers["X-Asset-Source"], "legacy-overlay")
        self.assertEqual(self.request(
            "/assets/1077200/production/2018/Android/event/001/title.unity3d"
        )[0], 404)

    def test_cn_refuses_implicit_version_and_partial_publication(self):
        headers = {"X-MLTD-Asset-Namespace": "cn"}
        for path in ("/assets/current/production/2018/Android/a.unity3d",
                     "/assets/production/2018/Android/a.unity3d"):
            self.assertEqual(self.request(path, headers=headers)[0], 404)
        state = self.mirror.state_path("1077200")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"sync_status":"failed"}', encoding="utf-8")
        self.assertEqual(self.request(
            "/assets/1077200/production/2018/Android/event/001/title.unity3d",
            headers=headers,
        )[0], 404)

    def test_cn_namespace_uses_generated_runtime_mapping_first(self):
        manifest_path = self.mirror.manifest_path("1077500")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["entries"][0]
        entry["runtime_path"] = (
            "production/2018/Android/"
            "fedcba9876543210fedcba9876543210fedcba98.unity3d"
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        status, headers, body = self.request(
            "/assets/1077500/production/2018/Android/"
            "fedcba9876543210fedcba9876543210fedcba98.unity3d",
            headers={"X-MLTD-Asset-Namespace": "cn"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload + b"1077500")
        self.assertEqual(headers["X-Asset-Source"], "translated")

    def test_old_generated_manifest_uses_official_catalog_runtime_mapping(self):
        manifest_path = self.mirror.manifest_path("1077500")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entries"][0].pop("runtime_path", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        status, headers, body = self.request(
            "/assets/1077500/production/2018/Android/"
            "0123456789abcdef0123456789abcdef01234567.unity3d",
            headers={"X-MLTD-Asset-Namespace": "cn"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload + b"1077500")
        self.assertEqual(headers["X-Asset-Source"], "translated")
    def test_legacy_sharded_manifest_resolves_flat_local_pool(self):
        manifest_path = self.mirror.manifest_path("1077500")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["entries"][0]
        digest = entry["artifact_sha256"]
        entry["object_path"] = f"objects/sha256/{digest[:2]}/{digest}"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.mirror.checksums_path("1077500").write_text(
            f"{digest}  {entry['object_path']}\n", encoding="utf-8"
        )
        status, _, body = self.request("/assets/1077500/event/001/title.unity3d")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload + b"1077500")
    def test_traversal_encoding_and_directory_listing_are_refused(self):
        for path in ("/assets/1077100/../secret", "/assets/1077100/%2e%2e/secret", "/assets/1077100/event%2f001/title.unity3d", "/assets/1077100/", "/assets/", "/assets/1077100/%00", "/assets/1077100/%zz", "/assets/1077100/event\\x"):
            self.assertEqual(self.request(path)[0], 404, path)
    def test_corrupt_object_is_not_served_and_other_version_survives(self):
        item = self.mirror.resolve("1077100", "event/001/title.unity3d")
        Path(item.pool_path).write_bytes(b"corrupt")
        self.assertEqual(self.request("/assets/1077100/event/001/title.unity3d")[0], 404)
        self.assertEqual(self.request("/assets/1077500/event/001/title.unity3d")[0], 200)
    def test_bad_checksums_or_failed_publication_are_not_served(self):
        self.mirror.checksums_path("1077100").write_text("invalid checksum\n", encoding="utf-8")
        self.assertEqual(self.request("/assets/1077100/event/001/title.unity3d")[0], 404)
        state = json.loads(self.mirror.state_path("1077500").read_text(encoding="utf-8"))
        state["sync_status"] = "failed"
        self.mirror.state_path("1077500").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.request("/assets/1077500/event/001/title.unity3d")[0], 404)
    def test_head_conditional_get_and_write_refusal(self):
        path = "/assets/1077100/event/001/title.unity3d"
        status, headers, body = self.request(path, "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(self.request(path, headers={"If-None-Match": headers["ETag"]})[0], 304)
        self.assertEqual(self.request(path, "POST")[0], 405)

if __name__ == "__main__":
    unittest.main()
