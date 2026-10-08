from __future__ import annotations

import argparse
import json
import hashlib
import tempfile
import unittest
import msgpack
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from scripts.run_latest_localization_ci import (LatestLocalizationError, load_release,
    nas_latest_release, runner_command, safe_remote_object, build_candidate_plan,
    write_candidate_plan, build_prior_backed_source_map, verify_nas_source_fingerprint)
from scripts.run_latest_localization_ci import prepare_nas_delta


class LatestLocalizationCITests(unittest.TestCase):
    def test_prior_source_map_queries_only_rows_without_verified_local_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); index = root / "index.data"
            old_remote = "a" * 40 + ".unity3d"; new_remote = "b" * 40 + ".unity3d"
            index.write_bytes(msgpack.packb([{"old_jp.gtx.unity3d": ["x", old_remote, 4],
                                              "new_jp.gtx.unity3d": ["y", new_remote, 5]}], use_bin_type=True))
            src = root / "old.unity3d"; src.write_bytes(b"old!")
            manifest = root / "localization-manifest.json"
            old_sha = hashlib.sha256(src.read_bytes()).hexdigest()
            manifest.write_text(json.dumps({"bundles":[{"logical":"old_jp.gtx.unity3d",
                "source_path":"old.unity3d","source_bundle_sha256":old_sha}]}), encoding="utf-8")
            release = {"version":"1077500","app_version":"9.0.200","complete":True,
                       "materialized":True,"scope":"jp-android","index_sha256":hashlib.sha256(index.read_bytes()).hexdigest()}
            response = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([
                {"remote":new_remote,"source_sha256":"c"*64,"declared_size":5}]))
            with patch("scripts.run_latest_localization_ci.subprocess.run", return_value=response) as run:
                result = build_prior_backed_source_map(index, release, manifest, root, "nas", "/archive")
            self.assertEqual(result["source_identity_mode"], "prior_logical_sha_plus_new_nas_sql")
            self.assertEqual(len(result["objects"]), 2)
            self.assertEqual(run.call_count, 1)
    def test_candidate_plan_reuses_exact_source_and_preserves_fallback_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "source.data"
            remote = "a" * 40 + ".unity3d"
            original = [{"logical_jp.gtx.unity3d": ["catalog", remote, 3], "other.bin": ["x", "b", 4]}]
            source.write_bytes(msgpack.packb(original, use_bin_type=True))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            bundle = root / "out.unity3d"; bundle.write_bytes(b"cn!")
            manifest = root / "localization-manifest.json"
            manifest.write_text(json.dumps({"version_identity":{"client_version":"9.0.200","assets_version":"1077100"},
                "scope":"jp-android","bundles":[{"logical":"logical_jp.gtx.unity3d",
                "source_bundle_sha256":"f"*64,"output_path":"out.unity3d",
                "output_bundle_sha256":hashlib.sha256(b"cn!").hexdigest(),"output_bytes":3}]}),encoding="utf-8")
            sm = {"release":{"version":"1077500","app_version":"9.0.200","complete":True,"materialized":True,"scope":"jp-android","index_sha256":digest},
                  "index_sha256":digest,"objects":[{"remote":remote,"source_sha256":"f"*64,"declared_size":3}]}
            plan, packed = build_candidate_plan(sm, source, [(root, manifest)], "9.0.200")
            self.assertEqual(plan["counts"]["reusable_overlay_objects"], 1)
            self.assertEqual(plan["fallback"]["index_rows_unchanged"], 1)
            self.assertEqual(msgpack.unpackb(packed, raw=False)[0]["other.bin"], ["x", "b", 4])
            result = write_candidate_plan(plan, packed, root / "candidate")
            self.assertEqual(result["status"], "candidate_written")
            self.assertEqual(write_candidate_plan(plan, packed, root / "candidate")["status"], "skipped_verified")
    def test_delta_uses_verified_cache_and_excludes_reused_nas_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            run=Path(td); source=run/'source/jp-android'; source.mkdir(parents=True)
            index=source/'index.data'; index.write_bytes(b'index')
            remote='a'*40+'.unity3d'; (source/remote).write_bytes(b'new')
            plan={'release':{'version':'1077500','index_name':'index.data','asset_root':'https://example/1077500/production'},
                  'index_sha256':hashlib.sha256(b'index').hexdigest(), 'objects':[
                      {'remote':remote,'source_sha256':hashlib.sha256(b'new').hexdigest(),'declared_size':3,'reuse_overlay':None},
                      {'remote':'b'*40+'.unity3d','reuse_overlay':{'version':'1077100'}}]}
            with patch('scripts.run_latest_localization_ci.run_command') as transfer:
                result=prepare_nas_delta(plan,run,'nas','/archive')
            transfer.assert_not_called()
            snapshot=json.loads(result.read_text())
            self.assertEqual(len(snapshot['objects']),1)
            self.assertEqual(snapshot['reused_on_nas'],1)
            self.assertTrue(snapshot['partial_universe'])
            index.write_bytes(b'wrong')
            with self.assertRaisesRegex(LatestLocalizationError,'index SHA-256'):
                prepare_nas_delta(plan,run,'nas','/archive')

    def test_compact_source_fingerprint_accepts_matching_remote_digest(self):
        rows = [("a.unity3d", "a" * 64, 3), ("b.unity3d", "b" * 64, 4)]
        payload = "".join(f"{n}\t{h}\t{s}\n" for n, h, s in rows).encode("utf-8")
        remote = {"object_count": 2, "fingerprint_sha256": hashlib.sha256(payload).hexdigest()}
        response = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(remote))
        release = {"version": "1077500", "index_name": "i.data", "index_sha256": "c" * 64}
        source_map = {"objects": [{"remote": n, "source_sha256": h, "declared_size": size} for n, h, size in rows]}
        with patch("scripts.run_latest_localization_ci.subprocess.run", return_value=response):
            result = verify_nas_source_fingerprint("nas", "/archive", release, source_map)
        self.assertTrue(result["verified"])
        self.assertEqual(result["object_count"], 2)

    def test_nas_latest_release_selects_newest_complete_materialized(self):
        payload = json.dumps({
            "active_version": "1077460",
            "releases": {
                "1077460": {"version": "1077460", "complete": True, "materialized": True},
                "1077500": {"version": "1077500", "complete": True, "materialized": True, "index_name": "latest.data"},
                "1077600": {"version": "1077600", "complete": False, "materialized": False},
            },
        })
        with patch("scripts.run_latest_localization_ci.run_command", return_value=payload):
            result = nas_latest_release("nas", "/archive/manifest.json")
        self.assertEqual(result["version"], "1077500")
        self.assertEqual(result["source"], "nas-archive-manifest")

    def test_nas_remote_object_rejects_traversal(self):
        with self.assertRaises(LatestLocalizationError):
            safe_remote_object("../outside.bundle")

    def test_load_release_requires_complete_materialized_version(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "manifest.json").write_text(json.dumps({
                "releases": {"1077500": {"version": "1077500", "complete": False, "materialized": False}}
            }), encoding="utf-8")
            with self.assertRaises(LatestLocalizationError):
                load_release(root, "1077500")

    def test_runner_command_wires_snapshot_auto_prepare_bypass_and_publish(self):
        args = argparse.Namespace(
            client_version="9.0.200",
            config="config.json",
            batch_mode="dynamic",
            artifact_store_root=Path("artifact-store"),
            nas_overlay_root="/vol2/1000/imas-asset-archive/mltd/cn-version",
            publish_transport="ssh",
            publish_ssh_target="nas",
            publish_apply=True,
            model_id=["gpt-6-sol"],
            prepare_translation=["seed.jsonl"],
            companion=[["--companion-fontrender", "font.jsonl"]],
        )
        command = runner_command(
            args,
            {"version": "1077500"},
            Path("snapshot.json"),
            Path("views/1077500"),
            Path("run"),
        )
        self.assertIn("--auto-prepare", command)
        self.assertIn("--owner-bypass-quality", command)
        self.assertIn("--publish-assets", command)
        self.assertIn("--publish-apply", command)
        self.assertIn("--prepare-translation", command)
        self.assertIn("--companion-fontrender", command)


if __name__ == "__main__":
    unittest.main()
