from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import archive_controller as controller


class ArchiveControllerTests(unittest.TestCase):
    def test_manifest_reconcile_processes_all_retained_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = {
                "schema_version": 1,
                "game": "mltd",
                "active_version": None,
                "releases": {
                    "100": {
                        "version": "100",
                        "retained": True,
                        "complete": True,
                        "materialized": True,
                    },
                    "200": {
                        "version": "200",
                        "retained": False,
                        "complete": False,
                        "materialized": False,
                    },
                    "300": {
                        "version": "300",
                        "complete": True,
                        "materialized": True,
                    },
                },
            }
            seen: list[str] = []

            def fake_reconcile(_root, _control, release, **_kwargs):
                seen.append(str(release["version"]))
                return release

            with (
                patch.object(controller, "import_store"),
                patch.object(controller, "refresh_status"),
                patch.object(controller, "reconcile_release", side_effect=fake_reconcile),
                patch.object(controller, "verify_release") as verify,
                patch.object(controller, "activate_release") as activate,
            ):
                result = controller.reconcile_manifest(
                    root,
                    control,
                    workers=4,
                    timeout=1,
                    proxy=None,
                    durable=False,
                    minimum_free_bytes=0,
                    activate_latest=True,
                )

            self.assertEqual(seen, ["300", "100"])
            verify.assert_called_once()
            self.assertEqual(str(verify.call_args.args[1]["version"]), "300")
            activate.assert_called_once()
            self.assertEqual(
                str(activate.call_args.args[1]["version"]),
                "300",
            )
            self.assertIs(result, control)


    def test_reconcile_complete_unmaterialized_verifies_before_materialize(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = {
                "schema_version": 1,
                "game": "mltd",
                "active_version": None,
                "releases": {
                    "300": {
                        "version": "300",
                        "scope": "jp-android",
                        "retained": True,
                        "complete": True,
                        "materialized": False,
                    }
                },
            }
            order: list[str] = []

            with (
                patch.object(controller, "refresh_status"),
                patch.object(controller, "verify_release", side_effect=lambda *_: order.append("verify")),
                patch.object(controller, "materialize_release", side_effect=lambda *_: order.append("materialize")),
            ):
                controller.reconcile_release(
                    root,
                    control,
                    control["releases"]["300"],
                    workers=4,
                    timeout=1,
                    proxy=None,
                    durable=False,
                    minimum_free_bytes=0,
                )

            self.assertEqual(order, ["verify", "materialize"])

    def test_verify_release_uses_full_sha256_hash_gate(self):
        with patch.object(controller, "run_checked") as run:
            controller.verify_release(
                Path("/archive"),
                {"version": "300", "scope": "jp-android"},
            )
        argv = run.call_args.args[0]
        self.assertIn("verify", argv)
        self.assertIn("--hash", argv)
        self.assertIn("300", argv)

    def test_watch_cycle_reconciles_manifest_when_discovery_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = {
                "schema_version": 1,
                "game": "mltd",
                "active_version": None,
                "releases": {},
            }
            with (
                patch.object(controller, "discover_latest", side_effect=TimeoutError("offline")),
                patch.object(controller, "load_control", return_value=control),
                patch.object(controller, "reconcile_manifest", return_value=control) as reconcile,
            ):
                result, discovery_error = controller.watch_cycle(
                    root,
                    version_api="https://example.invalid/latest",
                    asset_root_template="https://assets.invalid/{version}",
                    scope="jp-android",
                    discovery_timeout=1,
                    workers=4,
                    archive_timeout=2,
                    proxy=None,
                    durable=False,
                    minimum_free_bytes=0,
                    auto_activate=False,
                )
            self.assertIs(result, control)
            self.assertIn("TimeoutError", discovery_error)
            reconcile.assert_called_once()

    def test_merge_release_defaults_to_retained(self):
        control = controller.empty_control("https://example.invalid")
        release = controller.merge_release(
            control,
            {
                "version": "123",
                "scope": "jp-android",
                "asset_root": "https://assets.invalid/123",
                "index_name": "index.data",
            },
        )
        self.assertTrue(release["retained"])


if __name__ == "__main__":
    unittest.main()
