from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace
from unittest import mock

import requests
from pathlib import Path
from urllib.request import Request, urlopen

from server.versioned_asset_store import (
    LEGACY_ENTRY_INDEX,
    UNUSED_ENTRY_INDEXES,
    VersionConflict,
    VersionedAssetStore,
)
from tools.versioned_assets import Client, VersionedAssetHTTPServer, maintenance, verify


class VersionedAssetStoreTests(unittest.TestCase):
    def make_store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = VersionedAssetStore(temp.name)
        # The store pools its connections now, so it has to release them before
        # the temporary directory can be removed (Windows keeps files locked).
        self.addCleanup(store.close)
        return store

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
        self.addCleanup(readonly.close)
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


class StoreConnectionPoolingTests(unittest.TestCase):
    """A connection per store call cost ~1.9 MB of page reads on the NAS index.

    Measured 2026-10-08 against the live 1.9 GB index + 124 MB WAL: 500 per-asset
    lookups (the old find_md5_reuse path) read 965 MB, while one batch read of the
    same table costs 47.6 MB.  These tests pin the pooling that removes it.
    """

    def make_root(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def make_store(self) -> VersionedAssetStore:
        store = VersionedAssetStore(self.make_root())
        self.addCleanup(store.close)
        store.ensure_version(
            "1077100",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        return store

    @staticmethod
    def count_connects():
        """Patch sqlite3.connect and return the list of connections made."""
        connects = []
        real_connect = sqlite3.connect

        def counting(*args, **kwargs):
            connects.append(args)
            return real_connect(*args, **kwargs)

        patcher = mock.patch.object(sqlite3, "connect", counting)
        patcher.start()
        return connects, patcher

    def test_readers_and_writer_are_each_created_once(self):
        store = self.make_store()  # construction already pooled the writer
        connects, patcher = self.count_connects()
        try:
            for _ in range(25):
                store.version("1077100", "jp-android")
            readers = len(connects)
            for _ in range(25):
                store.mark_complete("1077100", "jp-android", True)
        finally:
            patcher.stop()
        # One lazily created reader for this thread, and no further connections:
        # writes reuse the writer pooled at construction.
        self.assertEqual(readers, 1)
        self.assertEqual(len(connects), 1)

    def test_read_only_store_reuses_one_reader_per_thread(self):
        root = self.make_root()
        writer = VersionedAssetStore(root)
        self.addCleanup(writer.close)
        writer.ensure_version(
            "1077100",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store = VersionedAssetStore(root, read_only=True)
        self.addCleanup(store.close)
        connects, patcher = self.count_connects()
        try:
            for _ in range(20):
                store.version("1077100", "jp-android")
            self.assertEqual(len(connects), 1)
            worker = threading.Thread(
                target=lambda: [store.version("1077100", "jp-android") for _ in range(5)]
            )
            worker.start()
            worker.join()
        finally:
            patcher.stop()
        self.assertEqual(len(connects), 2)

    def test_worker_threads_share_the_writer_connection(self):
        store = self.make_store()
        names = [f"asset_{index}.bundle" for index in range(8)]
        store.register_names("1077100", "jp-android", names)
        contents = {name: f"payload-{name}".encode() for name in names}

        def bind(name):
            content = contents[name]
            store.bind_object(
                "1077100",
                "jp-android",
                name,
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
                status=200,
                headers={"ETag": '"upstream"'},
                content_md5=hashlib.md5(content).hexdigest(),
            )

        threads = [threading.Thread(target=bind, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for name in names:
            self.assertEqual(
                store.lookup("1077100", "jp-android", name)["sha256"],
                hashlib.sha256(contents[name]).hexdigest(),
            )

    def test_register_names_inserts_in_key_order(self):
        """Unordered 168k-row registrations scattered one random page per row."""
        store = self.make_store()
        names = ["z.bundle", "a.bundle", "m/nested.bundle", "b.bundle"]
        store.register_names("1077100", "jp-android", names)
        with store.db() as conn:
            inserted = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM entries WHERE version=? AND scope=? ORDER BY rowid",
                    ("1077100", "jp-android"),
                )
            ]
        self.assertEqual(inserted, sorted(names))

    def test_pooled_connections_carry_the_tuning_pragmas(self):
        store = self.make_store()
        with store.write_db() as conn:
            self.assertEqual(
                conn.execute("PRAGMA cache_size").fetchone()[0],
                -VersionedAssetStore.WRITER_CACHE_KIB,
            )
            self.assertEqual(conn.execute("PRAGMA temp_store").fetchone()[0], 2)  # MEMORY
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 120000)
            self.assertIn(
                conn.execute("PRAGMA mmap_size").fetchone()[0],
                (0, VersionedAssetStore.MMAP_BYTES),
            )
        with store.db() as reader:
            self.assertEqual(
                reader.execute("PRAGMA cache_size").fetchone()[0],
                -VersionedAssetStore.READER_CACHE_KIB,
            )
        reader_store = VersionedAssetStore(store.root, read_only=True)
        self.addCleanup(reader_store.close)
        with reader_store.db() as reader:
            self.assertEqual(
                reader.execute("PRAGMA cache_size").fetchone()[0],
                -VersionedAssetStore.READER_CACHE_KIB,
            )

    def test_reads_use_their_own_connection_while_a_write_is_open(self):
        """Readers must not queue behind the writer: 256 workers depend on it."""
        store = self.make_store()
        with store.write_db() as writer:
            writer.execute(
                "INSERT INTO versions(version,scope,asset_root,manifest_name,created_at,updated_at)"
                " VALUES('9999999','jp-android','https://example.invalid/a','m.data',0,0)"
            )
            with store.db() as reader:
                self.assertIsNot(reader, writer)
                # The uncommitted row is invisible, and the read did not block.
                self.assertEqual(
                    reader.execute(
                        "SELECT COUNT(*) FROM versions WHERE version='9999999'"
                    ).fetchone()[0],
                    0,
                )
        with store.db() as reader:
            self.assertEqual(
                reader.execute(
                    "SELECT COUNT(*) FROM versions WHERE version='9999999'"
                ).fetchone()[0],
                1,
            )

    def test_bulk_writes_raise_the_checkpoint_threshold_and_restore_it(self):
        store = self.make_store()
        with store.write_db() as conn:
            default = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        self.assertGreater(default, 0)
        self.assertLess(default, VersionedAssetStore.BULK_AUTOCHECKPOINT_PAGES)
        with store.bulk_writes():
            with store.write_db() as conn:
                self.assertEqual(
                    conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0],
                    VersionedAssetStore.BULK_AUTOCHECKPOINT_PAGES,
                )
        with store.write_db() as conn:
            self.assertEqual(
                conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0], default
            )

    def test_bulk_writes_are_rejected_on_a_read_only_store(self):
        store = self.make_store()
        reader_store = VersionedAssetStore(store.root, read_only=True)
        self.addCleanup(reader_store.close)
        with self.assertRaises(PermissionError):
            with reader_store.bulk_writes():
                pass

    def test_writes_are_rejected_on_a_read_only_store(self):
        store = self.make_store()
        reader_store = VersionedAssetStore(store.root, read_only=True)
        self.addCleanup(reader_store.close)
        with self.assertRaises(PermissionError):
            with reader_store.write_db():
                pass


class ChecksumIndexReuseTests(unittest.TestCase):
    """Reuse must be a dict hit, not one indexed query per asset."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = VersionedAssetStore(temp.name)
        self.addCleanup(self.store.close)
        self.store.ensure_version(
            "1077100",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        self.store.register_names("1077100", "jp-android", ["manifest.data"])
        self.content = b"reusable asset bytes"
        self.md5 = hashlib.md5(self.content).hexdigest()
        digest = hashlib.sha256(self.content).hexdigest()
        path = self.store.object_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content)
        self.store.record_object_checksums([(digest, len(self.content), self.md5)])
        self.digest = digest
        self.client = Client(
            self.store,
            version="1077100",
            scope="jp-android",
            asset_root="https://example.invalid/assets",
        )

    def test_lookup_reads_the_checksum_table_once(self):
        reads = []
        real_index = self.store.object_checksum_index
        self.store.object_checksum_index = lambda: (reads.append(1), real_index())[1]
        self.store.object_by_md5 = lambda *args, **kwargs: self.fail(
            "per-asset checksum query is back"
        )
        for _ in range(20):
            candidate = self.client.find_md5_reuse(self.md5, len(self.content))
            self.assertEqual(candidate["sha256"], self.digest)
            self.assertEqual(candidate["source_version"], "checksum-cache")
        self.assertEqual(len(reads), 1)

    def test_missing_object_and_size_mismatch_do_not_reuse(self):
        self.assertIsNone(self.client.find_md5_reuse(self.md5, len(self.content) + 1))
        self.store.object_path(self.digest).unlink()
        self.assertIsNone(self.client.find_md5_reuse(self.md5, len(self.content)))

    def test_cache_availability_uses_the_memory_index(self):
        self.store.has_object_checksums = lambda: self.fail("per-call cache probe is back")
        self.assertTrue(self.client.checksum_cache_available())
        self.assertTrue(self.client.checksum_cache_available())

    def test_freshly_learned_checksum_is_usable_without_a_reread(self):
        reads = []
        real_index = self.store.object_checksum_index
        self.store.object_checksum_index = lambda: (reads.append(1), real_index())[1]
        self.client.find_md5_reuse(self.md5, len(self.content))

        other = b"a second asset learned from the network"
        other_md5 = hashlib.md5(other).hexdigest()
        other_sha = hashlib.sha256(other).hexdigest()
        self.store.object_path(other_sha).write_bytes(other)
        self.client.remember_checksum(other_md5, len(other), other_sha)

        candidate = self.client.find_md5_reuse(other_md5, len(other))
        self.assertEqual(candidate["sha256"], other_sha)
        self.assertEqual(len(reads), 1)

    def test_a_fresh_client_sees_objects_bound_by_the_store(self):
        """bind_object is what persists the md5, so a new client must reuse it."""
        new = b"third asset bound without the client"
        new_md5 = hashlib.md5(new).hexdigest()
        new_sha = hashlib.sha256(new).hexdigest()
        self.store.register_names("1077100", "jp-android", ["bound.bundle"])
        self.store.object_path(new_sha).write_bytes(new)
        self.store.bind_object(
            "1077100",
            "jp-android",
            "bound.bundle",
            sha256=new_sha,
            size=len(new),
            status=200,
            headers={"ETag": '"upstream"'},
            content_md5=new_md5,
        )
        fresh = Client(
            self.store,
            version="1077100",
            scope="jp-android",
            asset_root="https://example.invalid/assets",
        )
        candidate = fresh.find_md5_reuse(new_md5, len(new))
        self.assertEqual(candidate["sha256"], new_sha)
        self.assertEqual(candidate["source_version"], "checksum-cache")


    def test_a_populated_checksum_index_skips_the_etag_hint_scan(self):
        """The hint scan is a full pass over `entries`; a miss must not pay for it."""
        self.store.reuse_etag_hints = lambda *args, **kwargs: self.fail(
            "legacy ETag hint scan is back on the hot path"
        )
        self.assertEqual(
            self.client.find_md5_reuse(hashlib.md5(b"genuinely new").hexdigest(), 4),
            None,
        )

    def test_legacy_store_without_checksums_still_uses_etag_hints(self):
        """A store built before object_checksums must keep working."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = VersionedAssetStore(temp.name)
        self.addCleanup(store.close)
        content = b"legacy object without a checksum row"
        md5 = hashlib.md5(content).hexdigest()
        sha256 = hashlib.sha256(content).hexdigest()
        store.ensure_version(
            "1077100",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names("1077100", "jp-android", ["a.bundle"])
        store.object_path(sha256).write_bytes(content)
        store.bind_object(
            "1077100",
            "jp-android",
            "a.bundle",
            sha256=sha256,
            size=len(content),
            status=200,
            headers={"ETag": '"' + md5 + '"'},
        )
        with store.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM object_checksums").fetchone()[0], 0
            )
        client = Client(
            store,
            version="1077200",
            scope="jp-android",
            asset_root="https://example.invalid/assets",
        )
        candidate = client.find_md5_reuse(md5, len(content))
        self.assertEqual(candidate["sha256"], sha256)


class MaintenanceCommandTests(unittest.TestCase):
    """Unused entry indexes are removed deliberately, never at store startup."""

    def make_store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = VersionedAssetStore(temp.name)
        self.addCleanup(store.close)
        return store

    def make_store_with_unused_indexes(self):
        store = self.make_store()
        with store.write_db() as conn:
            for name in UNUSED_ENTRY_INDEXES:
                conn.execute(f"CREATE INDEX {name} ON entries(scope,name,version)")
        return store

    def index_names(self, store) -> set[str]:
        with store.db() as conn:
            return {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }

    def test_a_fresh_store_creates_no_unused_entry_indexes(self):
        store = self.make_store()
        names = self.index_names(store)
        for unused in UNUSED_ENTRY_INDEXES:
            self.assertNotIn(unused, names)
        # the checksum lookup index is still needed
        self.assertIn("idx_object_checksums_md5_size", names)

    def test_a_store_without_the_indexes_reports_nothing_to_do(self):
        store = self.make_store()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = maintenance(SimpleNamespace(root=str(store.root), dry_run=False))
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(buffer.getvalue().strip()),
            {"unused_entry_indexes": "absent", "changed": False},
        )

    def test_dry_run_measures_without_dropping(self):
        store = self.make_store_with_unused_indexes()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = maintenance(SimpleNamespace(root=str(store.root), dry_run=True))
        payload = json.loads(buffer.getvalue().strip())
        self.assertEqual(code, 0)
        self.assertEqual(sorted(payload["unused_entry_indexes"]), sorted(UNUSED_ENTRY_INDEXES))
        self.assertFalse(payload["changed"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(
            sorted(payload["index_bytes"]), sorted(UNUSED_ENTRY_INDEXES)
        )
        for unused in UNUSED_ENTRY_INDEXES:
            self.assertIn(unused, self.index_names(store))

    def test_dropping_is_idempotent(self):
        store = self.make_store_with_unused_indexes()
        first = io.StringIO()
        with redirect_stdout(first):
            maintenance(SimpleNamespace(root=str(store.root), dry_run=False))
        payload = json.loads(first.getvalue().strip())
        self.assertEqual(sorted(payload["unused_entry_indexes"]), sorted(UNUSED_ENTRY_INDEXES))
        self.assertTrue(payload["changed"])
        for unused in UNUSED_ENTRY_INDEXES:
            self.assertNotIn(unused, self.index_names(store))

        second = io.StringIO()
        with redirect_stdout(second):
            maintenance(SimpleNamespace(root=str(store.root), dry_run=False))
        self.assertEqual(
            json.loads(second.getvalue().strip()),
            {"unused_entry_indexes": "absent", "changed": False},
        )


class BindBatchTests(unittest.TestCase):
    """Binds are applied as one transaction sorted by primary key."""

    def make_store(self, version: str = "1077100", names=("a.bundle",)):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = VersionedAssetStore(temp.name)
        self.addCleanup(store.close)
        store.ensure_version(
            version,
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names(version, "jp-android", ["manifest.data", *names])
        return store

    @staticmethod
    def row(name: str, **overrides) -> dict:
        content = b"payload:" + name.encode()
        row = {
            "name": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "status": 200,
            "content_type": "application/octet-stream",
            "etag": '"' + hashlib.md5(content).hexdigest() + '"',
            "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            "cache_control": "public, max-age=60",
            "content_md5": hashlib.md5(content).hexdigest(),
        }
        row.update(overrides)
        return row

    def test_a_batch_stores_the_same_fields_as_one_object_binds(self):
        names = [f"asset_{index:02d}.bundle" for index in range(6)]
        store = self.make_store(names=names)
        rows = [self.row(name) for name in names]
        for row in rows[:3]:
            store.bind_object(
                "1077100",
                "jp-android",
                row["name"],
                sha256=row["sha256"],
                size=row["size"],
                status=row["status"],
                headers={
                    "Content-Type": row["content_type"],
                    "ETag": row["etag"],
                    "Last-Modified": row["last_modified"],
                    "Cache-Control": row["cache_control"],
                },
                content_md5=row["content_md5"],
            )
        self.assertEqual(store.bind_objects("1077100", "jp-android", rows[3:]), 3)

        for row in rows:
            stored = store.lookup("1077100", "jp-android", row["name"])
            for field in ("sha256", "size", "status", "content_type", "etag",
                          "last_modified", "cache_control"):
                self.assertEqual(stored[field], row[field], field)
            self.assertIsNotNone(stored["fetched_at"])
        with store.db() as conn:
            cached = {
                row[0]: (row[1], row[2])
                for row in conn.execute("SELECT sha256,size,md5 FROM object_checksums")
            }
        for row in rows:
            self.assertEqual(cached[row["sha256"]], (row["size"], row["content_md5"]))

    def test_a_batch_is_one_transaction_sorted_by_name(self):
        store = self.make_store(names=["a.bundle", "b.bundle", "c.bundle"])
        recorded: list[tuple[str, list[str]]] = []
        real_write_db = store.write_db

        class Recorder:
            def __init__(self, conn):
                self._conn = conn

            def executemany(self, sql, params):
                recorded.append((sql.split()[0].upper(), [param[-1] for param in params]))
                return self._conn.executemany(sql, params)

            def __getattr__(self, item):
                return getattr(self._conn, item)

        @contextmanager
        def recording_write_db():
            with real_write_db() as conn:
                yield Recorder(conn)

        with mock.patch.object(store, "write_db", recording_write_db):
            store.bind_objects(
                "1077100",
                "jp-android",
                [self.row(name) for name in ("c.bundle", "a.bundle", "b.bundle")],
            )

        # One UPDATE and one checksum upsert, i.e. a single transaction, and the
        # updates arrive in primary-key order.
        self.assertEqual([statement for statement, _ in recorded], ["UPDATE", "INSERT"])
        self.assertEqual(recorded[0][1], ["a.bundle", "b.bundle", "c.bundle"])

    def test_an_empty_batch_writes_nothing(self):
        store = self.make_store()
        self.assertEqual(store.bind_objects("1077100", "jp-android", []), 0)

    def test_an_unregistered_name_rolls_the_whole_batch_back(self):
        store = self.make_store(names=["a.bundle", "b.bundle"])
        with self.assertRaises(KeyError) as raised:
            store.bind_objects(
                "1077100",
                "jp-android",
                [self.row("a.bundle"), self.row("missing.bundle")],
            )
        self.assertIn("missing.bundle", str(raised.exception))
        self.assertIsNone(store.lookup("1077100", "jp-android", "a.bundle")["sha256"])
        with store.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM object_checksums").fetchone()[0], 0
            )

    def test_resolve_defers_the_bind_that_fetch_applies(self):
        store = self.make_store(names=["old.bundle"])
        content = b"unchanged"
        digest = hashlib.sha256(content).hexdigest()
        part = store.part_path("1077100", "jp-android", "old.bundle")
        part.write_bytes(content)
        store.commit_part(part, digest)
        store.bind_object(
            "1077100",
            "jp-android",
            "old.bundle",
            sha256=digest,
            size=len(content),
            status=200,
            headers={"ETag": '"' + hashlib.md5(content).hexdigest() + '"'},
            content_md5=hashlib.md5(content).hexdigest(),
        )
        store.ensure_version(
            "1077340",
            "jp-android",
            asset_root="https://example.invalid/assets",
            manifest_name="manifest.data",
        )
        store.register_names("1077340", "jp-android", ["manifest.data", "new.bundle"])

        class FakeResponse:
            status_code = 206
            headers = {
                "Content-Range": "bytes 0-0/%d" % len(content),
                "X-Goog-Hash": "md5=" + base64.b64encode(hashlib.md5(content).digest()).decode(),
                "ETag": '"' + hashlib.md5(content).hexdigest() + '"',
                "Cache-Control": "public",
            }

            def close(self):
                pass

        class FakeSession:
            def get(self, url, *, headers, stream, timeout):
                return FakeResponse()

        client = Client(
            store,
            version="1077340",
            scope="jp-android",
            asset_root="https://example.invalid/assets",
            timeout=1,
        )
        client.session = lambda: FakeSession()

        resolved = client.resolve("new.bundle")
        self.assertEqual(resolved["status"], "reused")
        self.assertEqual(resolved["bind"]["sha256"], digest)
        self.assertIsNone(store.lookup("1077340", "jp-android", "new.bundle")["sha256"])

        fetched = client.fetch("new.bundle")
        self.assertNotIn("bind", fetched)
        self.assertEqual(
            store.lookup("1077340", "jp-android", "new.bundle")["sha256"], digest
        )


if __name__ == "__main__":
    unittest.main()
