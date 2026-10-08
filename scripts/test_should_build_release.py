"""Offline tests for the generated-release reuse gate.

Nothing here touches the network: every case builds a throwaway git repository
and asserts the decision the build workflow would act on.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import should_build_release as tool


class ReleaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "tests")
        for path, body in (
            ("manifests/asset-version.json",
             json.dumps({"client_version": "9.0.0", "asset_version": 100}) + "\n"),
            ("locales/master/rows.jsonl", '{"bundle":"a.gtx","item_key":"k","zh":"你好"}\n'),
            ("pipelines/text/writer.py", "WRITER = 1\n"),
            ("schema/rows.schema.json", "{}\n"),
            ("images/.keep", "\n"),
            ("lyrics/.keep", "\n"),
            ("scripts/build_generated_release.py", "# builder\n"),
            ("scripts/assets_generated_index.py", "# store\n"),
            ("scripts/promote_merged_locales.py", "# promote\n"),
            ("scripts/build_portal_resource_manifest.py", "# portal\n"),
        ):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "inputs")
        self.built_from = self._git("rev-parse", "HEAD").stdout.strip()
        self._write_release(self.built_from)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "release")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=True)

    def _write_release(self, built_from: str, *, build_status: str = "success",
                       asset_version: int = 100) -> None:
        target = self.root / "generated" / "100" / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "asset_version": asset_version, "build_status": build_status,
            "translation_commit": built_from,
        }) + "\n", encoding="utf-8")

    def decide(self, **kwargs) -> dict:
        with patch.object(tool, "ROOT", self.root):
            return tool.decide(**kwargs)

    def test_an_up_to_date_release_is_skipped(self):
        decision = self.decide()
        self.assertFalse(decision["build"], decision)
        self.assertEqual(decision["differences"], [])

    def test_a_changed_release_input_rebuilds(self):
        (self.root / "locales/master/rows.jsonl").write_text(
            '{"bundle":"a.gtx","item_key":"k","zh":"再会"}\n', encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "translation")
        decision = self.decide()
        self.assertTrue(decision["build"])
        self.assertIn("locales", decision["differences"])
        self.assertIn("locales", decision["reason"])

    def test_an_unrelated_script_change_is_reused(self):
        # The whole point of the gate: CI-only edits must not cost a rebuild.
        (self.root / "scripts/test_something.py").write_text("# tests\n", encoding="utf-8")
        (self.root / "docs/note.md").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "docs/note.md").write_text("prose\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "ci only")
        self.assertFalse(self.decide()["build"])

    def test_a_dirty_working_tree_rebuilds(self):
        # promote_merged_locales.py rewrites locales before the gate runs.
        (self.root / "locales/master/rows.jsonl").write_text(
            '{"bundle":"a.gtx","item_key":"k","zh":"审校"}\n', encoding="utf-8")
        decision = self.decide()
        self.assertTrue(decision["build"])
        self.assertIn("locales/master/rows.jsonl", decision["dirty_paths"])

    def test_a_build_relevant_script_change_rebuilds(self):
        (self.root / "scripts/build_generated_release.py").write_text("# builder v2\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "builder")
        decision = self.decide()
        self.assertTrue(decision["build"])
        self.assertIn("scripts/build_generated_release.py", decision["differences"])

    def test_missing_release_rebuilds(self):
        (self.root / "generated/100/manifest.json").unlink()
        self.assertTrue(self.decide()["build"])

    def test_a_failed_release_rebuilds(self):
        self._write_release(self.built_from, build_status="failed")
        decision = self.decide()
        self.assertTrue(decision["build"])
        self.assertIn("build_status", decision["reason"])

    def test_a_version_mismatch_rebuilds(self):
        self._write_release(self.built_from, asset_version=99)
        self.assertTrue(self.decide()["build"])

    def test_an_unknown_source_commit_rebuilds(self):
        self._write_release("0" * 40)
        decision = self.decide()
        self.assertTrue(decision["build"])
        self.assertIn("not available", decision["reason"])

    def test_a_path_missing_in_the_built_from_commit_rebuilds(self):
        # Newly added release input: nothing to reuse, even though HEAD matches HEAD.
        (self.root / "lyrics/new.song.json").write_text("{}\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "new lyric input")
        decision = self.decide()
        self.assertTrue(decision["build"])
        self.assertIn("lyrics", decision["differences"])

    def test_the_builds_own_portal_manifest_does_not_force_a_rebuild(self):
        # portal-resource-manifest.json records generated_at and is written by
        # the build itself.  Counting it as an input made every release look
        # stale one build later, so the gate could never skip (found by running
        # the gate against the real repository, not in a unit test).
        portal = self.root / "manifests/portal-resource-manifest.json"
        portal.write_text(json.dumps({"generated_at": "2026-10-08T13:12:29Z"}) + "\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "build portal manifest")
        (portal).write_text(json.dumps({"generated_at": "2026-10-08T15:51:19Z"}) + "\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "build portal manifest again")
        decision = self.decide()
        self.assertFalse(decision["build"], decision)
        self.assertEqual(decision["differences"], [])

    def test_a_dirty_derived_output_does_not_force_a_rebuild(self):
        (self.root / "manifests/portal-resource-manifest.json").write_text("{}\n", encoding="utf-8")
        decision = self.decide()
        self.assertFalse(decision["build"], decision)
        self.assertEqual(decision["dirty_paths"], [])

    def test_a_path_absent_from_the_built_from_commit_rebuilds(self):
        # An input tree that did not exist when the release was built: nothing
        # to compare, so the gate must build rather than guess.
        (self.root / "schema/extra").mkdir(parents=True, exist_ok=True)
        (self.root / "schema/extra/rules.json").write_text("{}\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "schema addition")
        with patch.object(tool, "ROOT", self.root), patch.object(
            tool, "RELEASE_INPUT_TREES", tool.RELEASE_INPUT_TREES + ("schema/extra",)
        ):
            self.assertNotIsInstance(tool.digest(self.built_from, "schema/extra"), str)
            decision = tool.decide()
        self.assertTrue(decision["build"])
        self.assertIn("schema/extra", decision["differences"])

    def test_force_overrides_a_clean_fingerprint(self):
        decision = self.decide(force=True)
        self.assertTrue(decision["build"])
        self.assertIn("forced", decision["reason"])

    def test_an_unreadable_version_manifest_rebuilds(self):
        (self.root / "manifests/asset-version.json").write_text("{ not json", encoding="utf-8")
        self.assertTrue(self.decide()["build"])


class WorkflowGateTests(unittest.TestCase):
    """The quota-saving gates must survive future workflow edits."""

    ROOT = Path(tool.__file__).resolve().parents[1]

    def _workflow(self, name: str) -> dict:
        import yaml
        return yaml.safe_load((self.ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))

    def _steps(self, name: str, job: str = "build") -> list[dict]:
        return self._workflow(name)["jobs"][job]["steps"]

    def test_the_build_workflow_asks_the_gate_first(self):
        steps = self._steps("assets-generated.yml")
        names = [s.get("name") for s in steps]
        gate = next(s for s in steps if s.get("id") == "gate")
        self.assertIn("scripts/should_build_release.py", gate["run"])
        self.assertLess(names.index("Decide whether the release is already current"),
                        names.index("Install asset pipeline dependencies"))

    def test_every_expensive_build_step_is_gated(self):
        steps = self._steps("assets-generated.yml")
        for name in (
            "Install asset pipeline dependencies",
            "Validate translation source",
            "Build generated Unity3D release",
            "Verify generated release",
            "Build portal resource manifest",
            "Commit reviewed translations and generated release",
        ):
            step = next(s for s in steps if s.get("name") == name)
            with self.subTest(name):
                self.assertEqual(step.get("if"), "steps.gate.outputs.build == 'true'")

    def test_the_build_workflow_offers_an_explicit_force(self):
        dispatch = self._workflow("assets-generated.yml")["on"]["workflow_dispatch"]
        self.assertIn("force", dispatch.get("inputs", {}))

    def test_the_translation_workflow_skips_an_empty_queue(self):
        steps = self._steps("llm-translate-assets.yml", job="draft")
        collect = next(s for s in steps if s.get("id") == "collect")
        self.assertIn("queue_size", collect["run"])
        translate = next(s for s in steps if s.get("id") == "translate")
        self.assertEqual(translate.get("if"), "steps.collect.outputs.queue_size != '0'")
        apply_step = next(s for s in steps if s.get("id") == "apply")
        self.assertIn("steps.collect.outputs.queue_size != '0'", apply_step.get("if", ""))
        # Promotion and the commit are deliberately NOT gated on the queue: they
        # also carry a version-manifest bump and drafts left from earlier runs.
        self.assertNotIn("queue_size", str(next(s for s in steps if s.get("id") == "publish").get("if", "")))

    def test_the_hourly_image_worker_does_not_install_pillow_when_idle(self):
        steps = self._steps("backfill-image.yml", job="backfill")
        names = [s.get("name") for s in steps]
        install = next(s for s in steps if s.get("name") == "Install imaging dependency")
        self.assertEqual(install.get("if"), "steps.queue.outputs.queued != '0'")
        self.assertLess(names.index("List queued backfills"), names.index("Install imaging dependency"))


class ReleaseGateCliTests(unittest.TestCase):
    def test_cli_prints_a_decision_object(self):
        result = subprocess.run(
            [__import__("sys").executable, str(Path(tool.__file__)), "--force", "true"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["build"])
        self.assertTrue(payload["forced"])


if __name__ == "__main__":
    unittest.main()
