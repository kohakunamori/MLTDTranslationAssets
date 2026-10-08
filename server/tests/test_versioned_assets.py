from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

import requests
from pathlib import Path
from urllib.request import Request, urlopen

from server.versioned_asset_store import VersionConflict, VersionedAssetStore
from tools.versioned_assets import Client, VersionedAssetHTTPServer, verify


class VersionedAssetStoreTests(unittest.TestCase):
    def make_store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return VersionedAssetStore(temp.name)

    @staticmethod
    def headers():
        return {
            "Content-Type": "application/octet-stream",
            "ETag": '"upstream"',
            "Cache-Control": "public",
        }

    def seed(self, store, version, scope, name, content):
        store.ensure_version(
            version,
            scope,
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names(version, scope, ["manifest.data", name])
        digest = hashlib.sha256(content).hexdigest()
        part = store.part_path(version, scope, name)
        part.write_bytes(content)
        path = store.commit_part(part, digest)
        headers = self.headers()
        headers["ETag"] = '"' + hashlib.md5(content).hexdigest() + '"'
        store.bind_object(
            version,
            scope,
            name,
            sha256=digest,
            size=len(content),
            status=200,
            headers=headers,
        )
        return digest, path

    @staticmethod
    def goog_md5(content: bytes) -> str:
        return base64.b64encode(hashlib.md5(content).digest()).decode()

    def test_same_object_is_shared_across_versions(self):
        store = self.make_store()
        content = b"same immutable asset bytes"
        digest1, path1 = self.seed(store, "1077100", "jp-android", "a.bundle", content)
        digest2, path2 = self.seed(store, "1077200", "jp-android", "a.bundle", content)

        self.assertEqual(digest1, digest2)
        self.assertEqual(path1, path2)
        objects = [p for p in store.objects_root.rglob("*") if p.is_file()]
        self.assertEqual(objects, [path1])
        self.assertEqual(store.stats("1077100", "jp-android")["unique_objects"], 1)
        self.assertEqual(store.stats("1077200", "jp-android")["unique_objects"], 1)

    def test_verify_groups_duplicate_cas_references_and_counts_unmapped(self):
        store = self.make_store()
        version = "1077340"
        scope = "jp-android"
        names = ["a.bundle", "b.bundle", "missing.bundle"]
        store.ensure_version(
            version,
            scope,
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names(version, scope, names)

        payload = b"shared verification payload"
        digest = hashlib.sha256(payload).hexdigest()
        part = store.part_path(version, scope, "a.bundle")
        part.write_bytes(payload)
        store.commit_part(part, digest)
        for name in names[:2]:
            store.bind_object(
                version,
                scope,
                name,
                sha256=digest,
                size=len(payload),
                status=200,
                headers=self.headers(),
            )

        output = io.StringIO()
        args = SimpleNamespace(
            root=store.root,
            version=version,
            scope=scope,
            hash=True,
            hash_workers=2,
        )
        with redirect_stdout(output):
            rc = verify(args)
        report = json.loads(output.getvalue())

        self.assertEqual(rc, 2)
        self.assertEqual(report["registered"], 3)
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["mismatched"], 0)
        self.assertEqual(report["hash_workers"], 2)
        self.assertEqual(report["checksum_cache_objects"], 1)
        self.assertEqual(report["checksum_cache_added"], 1)
        self.assertFalse(report["complete"])
        self.assertEqual(
            store.object_by_md5(hashlib.md5(payload).hexdigest(), len(payload))["sha256"],
            digest,
        )

    def test_fetch_reuses_cross_version_object_by_current_url_md5(self):
        class FakeResponse:
            status_code = 206
            headers = {
                "Content-Range": "bytes 0-0/9",
                "X-Goog-Hash": "md5=" + VersionedAssetStoreTests.goog_md5(b"unchanged"),
                "ETag": '"' + hashlib.md5(b"unchanged").hexdigest() + '"',
                "Cache-Control": "public",
            }

            def close(self):
                pass

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, *, headers, stream, timeout):
                self.calls.append(dict(headers))
                return FakeResponse()

        store = self.make_store()
        old_digest, old_path = self.seed(
            store, "1077100", "jp-android", "old-name.bundle", b"unchanged"
        )
        store.ensure_version(
            "1077340",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names("1077340", "jp-android", ["new-name.bundle"])
        client = Client(
            store,
            version="1077340",
            scope="jp-android",
            asset_root="https://example.invalid/assets",
            timeout=1,
        )
        fake_session = FakeSession()
        client.session = lambda: fake_session

        result = client.fetch("new-name.bundle")

        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["source_version"], "1077100")
        self.assertEqual(result["sha256"], old_digest)
        self.assertEqual(len(fake_session.calls), 1)
        self.assertEqual(fake_session.calls[0]["Range"], "bytes=0-0")
        self.assertNotIn("If-None-Match", fake_session.calls[0])
        row = store.lookup("1077340", "jp-android", "new-name.bundle")
        self.assertEqual(row["sha256"], old_digest)
        self.assertEqual(
            store.object_by_md5(hashlib.md5(b"unchanged").hexdigest(), 9)["sha256"],
            old_digest,
        )
        self.assertTrue(old_path.is_file())

    def test_fetch_changed_cross_version_candidate_falls_back_to_full_download(self):
        class FakeResponse:
            def __init__(self, status_code, headers, chunks=()):
                self.status_code = status_code
                self.headers = headers
                self._chunks = list(chunks)

            def iter_content(self, chunk_size):
                yield from self._chunks

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {self.status_code}")

            def close(self):
                pass

        class FakeSession:
            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeResponse(
                        206,
                        {
                            "Content-Range": "bytes 0-0/3",
                            "Content-Length": "1",
                            "X-Goog-Hash": "md5=" + VersionedAssetStoreTests.goog_md5(b"new"),
                        },
                    ),
                    FakeResponse(
                        200,
                        {
                            "ETag": '"' + hashlib.md5(b"new").hexdigest() + '"',
                            "Content-Length": "3",
                            "X-Goog-Hash": "md5=" + VersionedAssetStoreTests.goog_md5(b"new"),
                        },
                        [b"new"],
                    ),
                ]

            def get(self, url, *, headers, stream, timeout):
                self.calls.append(dict(headers))
                return self.responses.pop(0)

        store = self.make_store()
        old_digest, _old_path = self.seed(
            store, "1077100", "jp-android", "old-name.bundle", b"old"
        )
        store.ensure_version(
            "1077340",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names("1077340", "jp-android", ["new-name.bundle"])
        client = Client(
            store,
            version="1077340",
            scope="jp-android",
            asset_root="https://example.invalid/assets",
            timeout=1,
        )
        fake_session = FakeSession()
        client.session = lambda: fake_session

        result = client.fetch("new-name.bundle")

        self.assertEqual(result["status"], "downloaded")
        self.assertNotEqual(result["sha256"], old_digest)
        self.assertEqual(len(fake_session.calls), 2)
        self.assertEqual(fake_session.calls[0]["Range"], "bytes=0-0")
        self.assertNotIn("If-None-Match", fake_session.calls[0])
        self.assertNotIn("If-None-Match", fake_session.calls[1])
        self.assertNotIn("Range", fake_session.calls[1])
        row = store.lookup("1077340", "jp-android", "new-name.bundle")
        self.assertEqual(store.object_path(row["sha256"]).read_bytes(), b"new")
        self.assertEqual(
            store.object_by_md5(hashlib.md5(b"new").hexdigest(), 3)["sha256"],
            row["sha256"],
        )

    def test_read_only_store_serves_lookup_but_rejects_mutation(self):
        store = self.make_store()
        content = b"immutable"
        self.seed(store, "1077100", "jp-android", "a.bundle", content)

        readonly = VersionedAssetStore(store.root, read_only=True)
        row = readonly.lookup("1077100", "jp-android", "a.bundle")
        self.assertEqual(row["size"], len(content))
        with self.assertRaises(PermissionError):
            readonly.register_names("1077100", "jp-android", ["other.bundle"])

    def test_parallel_metadata_binding_serializes_sqlite_writes(self):
        store = self.make_store()
        version = "1077340"
        scope = "jp-android"
        names = [f"{i:04d}.bundle" for i in range(128)]
        store.ensure_version(
            version,
            scope,
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names(version, scope, names)

        errors = []
        def bind(i, name):
            try:
                payload = f"payload-{i}".encode()
                digest = hashlib.sha256(payload).hexdigest()
                part = store.part_path(version, scope, name)
                part.write_bytes(payload)
                store.commit_part(part, digest)
                store.bind_object(
                    version,
                    scope,
                    name,
                    sha256=digest,
                    size=len(payload),
                    status=200,
                    headers=self.headers(),
                )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=bind, args=(i, name))
            for i, name in enumerate(names)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(store.stats(version, scope)["mapped"], len(names))

    def test_same_version_rejects_manifest_hash_change(self):
        store = self.make_store()
        store.ensure_version(
            "1077100",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
            manifest_sha256="1" * 64,
        )
        with self.assertRaises(VersionConflict):
            store.ensure_version(
                "1077100",
                "jp-android",
                asset_root="https://example.invalid/assets",
                manifest_name="manifest.data",
                manifest_sha256="2" * 64,
            )

    def test_fetch_resumes_after_stream_incomplete_read(self):
        class FakeResponse:
            def __init__(self, status_code, headers, chunks, error=None):
                self.status_code = status_code
                self.headers = headers
                self._chunks = list(chunks)
                self._error = error

            def iter_content(self, chunk_size):
                for chunk in self._chunks:
                    yield chunk
                if self._error is not None:
                    raise self._error

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {self.status_code}")

            def close(self):
                pass

        class FakeSession:
            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeResponse(
                        200,
                        {"Content-Length": "10"},
                        [b"abcd"],
                        requests.exceptions.ChunkedEncodingError("truncated"),
                    ),
                    FakeResponse(
                        206,
                        {"Content-Range": "bytes 4-9/10"},
                        [b"efghij"],
                    ),
                ]

            def get(self, url, *, headers, stream, timeout):
                self.calls.append(dict(headers))
                return self.responses.pop(0)

        store = self.make_store()
        version = "1077340"
        scope = "jp-android"
        name = "resume.bundle"
        store.ensure_version(
            version,
            scope,
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names(version, scope, [name])

        client = Client(
            store,
            version=version,
            scope=scope,
            asset_root="https://example.invalid/assets",
            timeout=1,
        )
        fake_session = FakeSession()
        client.session = lambda: fake_session

        result = client.fetch(name)

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["size"], 10)
        self.assertEqual(fake_session.calls[0].get("Range"), None)
        self.assertEqual(fake_session.calls[1].get("Range"), "bytes=4-")
        row = store.lookup(version, scope, name)
        path = store.object_path(row["sha256"])
        self.assertEqual(path.read_bytes(), b"abcdefghij")
        self.assertFalse(store.part_path(version, scope, name).exists())

    def test_fetch_commits_complete_partial_after_416(self):
        class FakeResponse:
            status_code = 416
            headers = {"Content-Range": "bytes */10"}

            def raise_for_status(self):
                raise requests.HTTPError("HTTP 416")

            def close(self):
                pass

        class FakeSession:
            def __init__(self):
                self.headers = None

            def get(self, url, *, headers, stream, timeout):
                self.headers = dict(headers)
                return FakeResponse()

        store = self.make_store()
        version = "1077340"
        scope = "jp-android"
        name = "complete-part.bundle"
        store.ensure_version(
            version,
            scope,
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names(version, scope, [name])
        part = store.part_path(version, scope, name)
        part.write_bytes(b"abcdefghij")

        client = Client(
            store,
            version=version,
            scope=scope,
            asset_root="https://example.invalid/assets",
            timeout=1,
        )
        fake_session = FakeSession()
        client.session = lambda: fake_session

        result = client.fetch(name)

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["size"], 10)
        self.assertEqual(fake_session.headers.get("Range"), "bytes=10-")
        row = store.lookup(version, scope, name)
        self.assertIsNotNone(row["sha256"])
        self.assertEqual(store.object_path(row["sha256"]).read_bytes(), b"abcdefghij")
        self.assertFalse(part.exists())

    def test_http_server_current_alias_uses_switched_version(self):
        store = self.make_store()
        first = b"first"
        second = b"second"
        self.seed(store, "1077100", "jp-android", "a.bundle", first)
        self.seed(store, "1077200", "jp-android", "a.bundle", second)
        store.mark_complete("1077100", "jp-android", True)
        store.mark_complete("1077200", "jp-android", True)
        views = store.root / "views"
        (views / "1077100").mkdir(parents=True)
        (views / "1077200").mkdir(parents=True)
        try:
            (store.root / "current").symlink_to(
                Path("views") / "1077200", target_is_directory=True
            )
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        server = VersionedAssetHTTPServer(
            ("127.0.0.1", 0),
            store=store,
            prefix="assets",
            fetch_missing=False,
            proxy=None,
            timeout=5.0,
            require_complete=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        with urlopen(
            f"http://{host}:{port}/assets/current/jp-android/a.bundle",
            timeout=5,
        ) as response:
            self.assertEqual(response.read(), second)

    def test_http_server_resolves_version_and_range(self):
        store = self.make_store()
        content = b"0123456789abcdef"
        digest, _path = self.seed(
            store, "1077100", "jp-android", "folder/test.bundle", content
        )
        server = VersionedAssetHTTPServer(
            ("127.0.0.1", 0),
            store=store,
            prefix="assets",
            fetch_missing=False,
            proxy=None,
            timeout=5.0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        host, port = server.server_address
        url = f"http://{host}:{port}/assets/1077100/jp-android/folder/test.bundle"
        with urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), content)
            self.assertEqual(response.headers["ETag"], f'"sha256:{digest}"')
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")

        request = Request(url, headers={"Range": "bytes=3-7"})
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), content[3:8])
            self.assertEqual(response.headers["Content-Range"], "bytes 3-7/16")


if __name__ == "__main__":
    unittest.main()
