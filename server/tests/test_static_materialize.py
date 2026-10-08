from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.versioned_asset_store import VersionedAssetStore
from tools.materialize_versioned_assets import materialize


class StaticMaterializeTests(unittest.TestCase):
    def test_materialize_uses_hardlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            store = VersionedAssetStore(temp)
            version = "123"
            scope = "jp-android"
            store.ensure_version(
                version,
                scope,
                asset_root="https://example.invalid/123",
                manifest_name="manifest.data",
            )
            store.register_names(version, scope, ["manifest.data", "dir/a.unity3d"])
            for name, payload in (
                ("manifest.data", b"manifest"),
                ("dir/a.unity3d", b"asset-bytes"),
            ):
                digest = store.sha256_file(self._write_part(store, version, scope, name, payload))
                part = store.part_path(version, scope, name)
                # _write_part created this exact part path.
                source = store.commit_part(part, digest)
                store.bind_object(
                    version,
                    scope,
                    name,
                    sha256=digest,
                    size=len(payload),
                    status=200,
                    headers={},
                )
                self.assertTrue(source.is_file())

            rc = materialize(SimpleNamespace(
                root=Path(temp), version=version, scope=scope, replace=False
            ))
            self.assertEqual(rc, 0)
            row = store.lookup(version, scope, "dir/a.unity3d")
            source = store.object_path(row["sha256"])
            view = Path(temp) / "views" / version / scope / "dir" / "a.unity3d"
            self.assertTrue(view.is_file())
            self.assertTrue(os.path.samefile(source, view))


    def test_materialize_rejects_same_size_corrupt_cas_object(self):
        with tempfile.TemporaryDirectory() as temp:
            store = VersionedAssetStore(temp)
            version = "123"
            scope = "jp-android"
            name = "manifest.data"
            payload = b"manifest"
            store.ensure_version(
                version,
                scope,
                asset_root="https://example.invalid/123",
                manifest_name=name,
            )
            store.register_names(version, scope, [name])
            digest = store.sha256_file(
                self._write_part(store, version, scope, name, payload)
            )
            part = store.part_path(version, scope, name)
            source = store.commit_part(part, digest)
            store.bind_object(
                version,
                scope,
                name,
                sha256=digest,
                size=len(payload),
                status=200,
                headers={},
            )
            source.write_bytes(b"corrupt!")
            self.assertEqual(source.stat().st_size, len(payload))

            with self.assertRaisesRegex(RuntimeError, "SHA-256 verification failure"):
                materialize(SimpleNamespace(
                    root=Path(temp), version=version, scope=scope, replace=False
                ))

    @staticmethod
    def _write_part(store, version, scope, name, payload):
        part = store.part_path(version, scope, name)
        part.write_bytes(payload)
        return part


if __name__ == "__main__":
    unittest.main()
