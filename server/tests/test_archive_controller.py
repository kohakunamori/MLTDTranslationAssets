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
                    discovery_retry_delay=0,
                )
            self.assertIs(result, control)
            self.assertIn("TimeoutError", discovery_error)
            reconcile.assert_called_once()

    def test_discovery_retries_a_flaky_endpoint_before_giving_up(self):
        release = {"version": "1077720", "scope": "jp-android"}
        control = {"schema_version": 1, "releases": {}}
        calls = {"n": 0}

        def flaky(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("handshake operation timed out")
            return control, release

        with patch.object(controller, "discover_latest", side_effect=flaky):
            found, discovered = controller.discover_latest_with_retries(
                Path("."), "https://example.invalid/latest", "https://a.invalid/{version}",
                "jp-android", 1, attempts=3, delay=0,
            )
        self.assertEqual(calls["n"], 3)
        self.assertIs(found, control)
        self.assertEqual(discovered["version"], "1077720")

    def test_watch_cycle_retries_discovery_and_reports_only_a_total_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = {"schema_version": 1, "active_version": None, "releases": {}}
            calls = {"n": 0}

            def flaky(*_args, **_kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("handshake operation timed out")
                return control, {"version": "1077720"}

            with (
                patch.object(controller, "discover_latest", side_effect=flaky),
                patch.object(controller, "reconcile_manifest", return_value=control),
            ):
                _result, discovery_error = controller.watch_cycle(
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
                    discovery_attempts=3,
                    discovery_retry_delay=0,
                )
            # A single flaky attempt must not surface as a discovery error.
            self.assertEqual(calls["n"], 2)
            self.assertIsNone(discovery_error)

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
