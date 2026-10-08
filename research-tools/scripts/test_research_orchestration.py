#!/usr/bin/env python3
"""Bounded migration checks: synthetic metadata/refusals, no producer or store writes."""
from __future__ import annotations

import importlib.abc
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class NoLegacyWriter(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"assets_generated_index", "scripts.assets_generated_index"}:
            raise AssertionError("research must not import an unpinned legacy writer")
        return None


sys.meta_path.insert(0, NoLegacyWriter())
RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
from scripts import materialize_generated_release as materializer
from scripts.localization_version_identity import version_identity


class ResearchClosure(unittest.TestCase):
    def args(self, root: Path, *, preflight: bool = False, pair=()):
        argv = ["--asset-version", "1077100", "--source-client-version", "9.0.200",
                "--source-commit", "1" * 40, "--input-root", str(root / "absent-input"),
                "--output-root", str(root / "absent-output"), *pair]
        if preflight:
            argv.append("--preflight-only")
        return materializer.build_parser().parse_args(argv)

    def test_all_modes_require_explicit_writer_pair_before_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for preflight in (False, True):
                for pair in ((), ("--assets-writer-root", str(root / "absent-writer")),
                             ("--assets-writer-pin", "0" * 64)):
                    with self.subTest(preflight=preflight, pair_length=len(pair)):
                        with self.assertRaises(materializer.RefusedInput):
                            materializer.run(self.args(root, preflight=preflight, pair=pair))
                        self.assertEqual(list(root.iterdir()), [])

    def test_untrusted_wrong_pin_is_rejected_without_executing_fixture(self):
        # Deliberately WRONG literal pin. Never derive a trusted pin from a checkout.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer_root = root / "untrusted-fixture"
            scripts = writer_root / "scripts"
            scripts.mkdir(parents=True)
            sentinel = root / "executed"
            (scripts / "assets_generated_index.py").write_text(
                "from pathlib import Path\nPath(" + repr(str(sentinel)) +
                ").write_text('unexpected execution')\n", encoding="utf-8")
            for preflight in (False, True):
                with self.subTest(preflight=preflight):
                    with self.assertRaisesRegex(materializer.RefusedInput, "does not match"):
                        materializer.run(self.args(root, preflight=preflight, pair=(
                            "--assets-writer-root", str(writer_root),
                            "--assets-writer-pin", "0" * 64)))
            self.assertFalse(sentinel.exists())
            self.assertFalse((root / "absent-input").exists())
            self.assertFalse((root / "absent-output").exists())

    def test_empty_input_has_no_implicit_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(materializer.RefusedInput, "no accepted translation ledger"):
                materializer.resolve_ledgers(Path(tmp), [])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_image_adapter_refuses_implicit_private_inputs(self):
        for name, argv in (("inject_reviewed_textures.py", ["--preflight-context"]),
                           ("verify_bundle_repack.py", ["--report", "never-opened.json"])):
            with self.subTest(entry=name):
                script = RESEARCH / "tools/mltd_image_localization" / name
                result = subprocess.run([sys.executable, "-B", str(script), *argv],
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--install-manifest must be an explicit", result.stderr)

    def test_image_context_rejects_synthetic_unapproved_metadata_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "synthetic-unapproved.jsonl"
            manifest.write_text(json.dumps({"source_id": "synthetic-only",
                                            "review_status": "pending"}) + "\n", encoding="utf-8")
            output = root / "not-created"
            report = root / "not-written.json"
            probe = materializer._image_input_context(
                RESEARCH / "tools/mltd_image_localization/inject_reviewed_textures.py",
                RESEARCH, output, manifest, root, report, True)
            self.assertFalse(probe.answered)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_version_identity_uses_only_explicit_synthetic_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "synthetic.index"
            index.write_bytes(b"synthetic index; not an official asset")
            snapshot = root / "synthetic.snapshot.json"
            snapshot.write_text(json.dumps({
                "complete": True, "scope": "jp-android",
                "upstream_root": "https://example.invalid/1077100/production/",
                "asset_index": str(index), "objects": [{"synthetic": True}],
            }), encoding="utf-8")
            identity = version_identity(snapshot, client_version="9.0.200",
                                        asset_version="1077100", asset_index=index)
            self.assertEqual(identity["assets_version"], "1077100")
            self.assertEqual(identity["client_version"], "9.0.200")
            with self.assertRaisesRegex(ValueError, "assets version mismatch"):
                version_identity(snapshot, client_version="9.0.200", asset_version="1077101")


if __name__ == "__main__":
    unittest.main()
