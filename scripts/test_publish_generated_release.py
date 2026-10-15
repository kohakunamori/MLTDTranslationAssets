"""Real Git races against a local bare remote; no network or production writes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import publish_generated_release as tool


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="generated-publish-test-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.remote = self.directory / "remote.git"
        self.root = self.directory / "build"
        self.other = self.directory / "writer"
        self.git(self.directory, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.directory, "clone", "-c", "core.autocrlf=false", str(self.remote), str(self.root))
        self.configure(self.root)
        self.write(self.root, "manifests/asset-version.json", {"asset_version": "100"})
        self.write(self.root, "manifests/portal-resource-manifest.json", {"release": "old"})
        self.write(self.root, "locales/master/rows.jsonl", '{"zh":"old","status":"pending"}\n')
        self.write(self.root, "scripts/build_generated_release.py", "# writer v1\n")
        self.write(self.root, "generated/100/manifest.json", {
            "asset_version": "100", "build_status": "failed", "translation_commit": "",
        })
        self.write(self.root, "generated/objects/old", b"old")
        self.commit(self.root, "initial inputs")
        self.git(self.root, "push", "origin", "main")
        self.source = self.git(self.root, "rev-parse", "HEAD")
        self.git(self.directory, "clone", "-c", "core.autocrlf=false", str(self.remote), str(self.other))
        self.configure(self.other)

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, encoding="utf-8", check=True)
        return result.stdout.strip()

    def configure(self, root):
        self.git(root, "config", "user.name", "publisher test")
        self.git(root, "config", "user.email", "publisher@example.invalid")
        self.git(root, "config", "core.autocrlf", "false")

    def write(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(json.dumps(value) if isinstance(value, dict) else value,
                            encoding="utf-8", newline="\n")

    def commit(self, root, message):
        self.git(root, "add", "-A")
        self.git(root, "commit", "-m", message)
        return self.git(root, "rev-parse", "HEAD")

    def remote_change(self, changes, message="competing writer"):
        for name, value in changes.items():
            self.write(self.other, name, value)
        commit = self.commit(self.other, message)
        self.git(self.other, "push", "origin", "main")
        return commit

    def outputs(self, root=None, label="candidate"):
        root = root or self.root
        self.write(root, "generated/100/manifest.json", {
            "asset_version": "100", "build_status": "success",
            "translation_commit": self.source, "label": label,
        })
        self.write(root, "generated/objects/new", b"new")
        self.write(root, "manifests/portal-resource-manifest.json", {"release": label})

    def remote_head(self):
        return self.git(self.remote, "rev-parse", "main")

    def remote_json(self, name):
        return json.loads(self.git(self.remote, "show", f"main:{name}"))

    def test_direct_publish_is_a_fast_forward_with_bound_source(self):
        self.outputs()
        result = tool.publish(self.root)
        self.assertTrue(result["pushed"], result)
        self.assertEqual(self.git(self.remote, "rev-parse", "main^"), self.source)
        self.assertEqual(self.remote_json("generated/100/manifest.json")["translation_commit"], self.source)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.source)
        self.assertEqual(self.git(self.root, "diff", "--cached"), "")

    def test_r2_publish_commits_inventory_but_never_binary_payloads(self):
        self.outputs()
        self.write(self.root, 'generated/r2-storage.json', {'objects': {'a' * 64: 3}})
        self.write(self.root, 'generated/build-inputs/text/cache', b'parsed-cache')
        result = tool.publish(self.root)
        self.assertTrue(result['pushed'])
        files = self.git(self.remote, 'ls-tree', '-r', '--name-only', 'main').splitlines()
        self.assertIn('generated/r2-storage.json', files)
        self.assertNotIn('generated/objects/new', files)
        self.assertNotIn('generated/build-inputs/text/cache', files)

    def test_competing_manifest_is_replaced_without_losing_other_outputs(self):
        self.outputs()
        self.outputs(self.other, label="competing")
        self.write(self.other, "generated/objects/other", b"other")
        latest = self.commit(self.other, "first release")
        self.git(self.other, "push", "origin", "main")
        result = tool.publish(self.root, force=True)
        self.assertTrue(result["pushed"], result)
        self.assertEqual(self.git(self.remote, "rev-parse", "main^"), latest)
        self.assertEqual(self.remote_json("generated/100/manifest.json")["label"], "candidate")
        self.assertEqual(self.git(self.remote, "show", "main:generated/objects/other"), "other")

    def test_an_already_current_competing_release_is_reused(self):
        self.outputs()
        self.outputs(self.other, label="winner")
        latest = self.commit(self.other, "current release")
        self.git(self.other, "push", "origin", "main")
        result = tool.publish(self.root)
        self.assertFalse(result["pushed"], result)
        self.assertFalse(result["rebuild"], result)
        self.assertEqual(self.remote_head(), latest)
        self.assertEqual(self.remote_json("generated/100/manifest.json")["label"], "winner")

    def test_local_review_promotions_are_not_skipped_as_already_current(self):
        self.outputs()
        self.write(self.root, "locales/master/rows.jsonl", '{"zh":"old","status":"accepted"}\n')
        self.outputs(self.other, label="unpromoted")
        self.commit(self.other, "unpromoted release")
        self.git(self.other, "push", "origin", "main")
        result = tool.publish(self.root)
        self.assertTrue(result["pushed"], result)
        self.assertIn('"accepted"', self.git(self.remote, "show", "main:locales/master/rows.jsonl"))

    def assert_deferred(self, name, value):
        self.outputs()
        latest = self.remote_change({name: value}, f"update {name}")
        result = tool.publish(self.root, force=True)
        self.assertFalse(result["pushed"], result)
        self.assertTrue(result["rebuild"], result)
        self.assertEqual(self.remote_head(), latest)
        self.assertEqual(self.remote_json("generated/100/manifest.json")["build_status"], "failed")

    def test_new_locale_inputs_defer_instead_of_publishing_stale_outputs(self):
        self.assert_deferred("locales/master/rows.jsonl", '{"zh":"new"}\n')

    def test_new_lyrics_defer_instead_of_publishing_stale_outputs(self):
        self.assert_deferred("lyrics/song.jsonl", '{"zh":"new lyric"}\n')

    def test_a_new_version_defers_instead_of_publishing_stale_outputs(self):
        self.assert_deferred("manifests/asset-version.json", {"asset_version": "101"})

    def test_a_new_writer_defers_instead_of_publishing_stale_outputs(self):
        self.assert_deferred("scripts/build_generated_release.py", "# writer v2\n")

    def test_unrelated_main_changes_and_derived_metadata_do_not_conflict(self):
        self.outputs()
        latest = self.remote_change({"docs/note.md": "keep this\n",
                                     "manifests/portal-resource-manifest.json": {"release": "unbuilt"}})
        result = tool.publish(self.root)
        self.assertTrue(result["pushed"], result)
        self.assertEqual(self.git(self.remote, "rev-parse", "main^"), latest)
        self.assertEqual(self.git(self.remote, "show", "main:docs/note.md"), "keep this")

    def test_removals_are_published_without_deleting_other_writers_files(self):
        self.outputs()
        (self.root / "generated/objects/old").unlink()
        self.remote_change({"generated/objects/retain": b"retain"})
        result = tool.publish(self.root, force=True)
        self.assertTrue(result["pushed"], result)
        paths = self.git(self.remote, "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("generated/objects/old", paths)
        self.assertIn("generated/objects/retain", paths)

    def test_staged_changes_are_refused_and_local_unrelated_edits_are_preserved(self):
        self.outputs()
        self.write(self.root, "docs/unrelated.md", "unrelated\n")
        self.git(self.root, "add", "docs/unrelated.md")
        with self.assertRaisesRegex(RuntimeError, "pre-existing staged"):
            tool.publish(self.root)
        self.assertEqual(self.remote_head(), self.source)
        self.git(self.root, "restore", "--staged", "docs/unrelated.md")
        result = tool.publish(self.root)
        self.assertTrue(result["pushed"], result)
        self.assertNotIn("docs/unrelated.md", self.git(self.remote, "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual((self.root / "docs/unrelated.md").read_text(), "unrelated\n")

    def test_no_changes_do_not_create_a_commit(self):
        result = tool.publish(self.root)
        self.assertFalse(result["pushed"], result)
        self.assertFalse(result["rebuild"], result)
        self.assertEqual(self.remote_head(), self.source)

    def test_no_local_changes_still_queue_new_main_inputs(self):
        latest = self.remote_change({"lyrics/song.jsonl": '{"zh":"new lyric"}\n'})
        result = tool.publish(self.root)
        self.assertTrue(result["rebuild"], result)
        self.assertFalse(result["pushed"], result)
        self.assertEqual(self.remote_head(), latest)

    def race(self, changes):
        real_git = tool.git
        raced = False

        def racing_git(root, *args, **kwargs):
            nonlocal raced
            if args[0] == "push" and not raced:
                raced = True
                self.remote_change(changes, "advanced immediately before push")
            return real_git(root, *args, **kwargs)

        return patch.object(tool, "git", side_effect=racing_git)

    def test_a_real_rejected_push_retries_on_unrelated_changes(self):
        self.outputs()
        with self.race({"docs/race.md": "retain race\n"}):
            result = tool.publish(self.root)
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(self.git(self.remote, "show", "main:docs/race.md"), "retain race")

    def test_a_real_rejected_push_rechecks_inputs_before_retry(self):
        self.outputs()
        with self.race({"locales/master/rows.jsonl": '{"zh":"new in race"}\n'}):
            result = tool.publish(self.root)
        self.assertTrue(result["rebuild"], result)
        self.assertFalse(result["pushed"], result)
        self.assertEqual(self.remote_json("generated/100/manifest.json")["build_status"], "failed")

    def test_exhausted_pushes_fail_without_claiming_publication(self):
        self.outputs()
        with self.race({"docs/race.md": "new\n"}):
            with self.assertRaisesRegex(RuntimeError, "after 1 normal pushes"):
                tool.publish(self.root, attempts=1)
        self.assertEqual(self.remote_json("generated/100/manifest.json")["build_status"], "failed")

    def test_cli_writes_github_outputs_after_a_real_publish(self):
        self.outputs()
        output = self.directory / "github-output"
        code = "import publish_generated_release as p; from pathlib import Path; import sys; p.ROOT=Path(sys.argv.pop()); raise SystemExit(p.main())"
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                env=dict(os.environ, PYTHONPATH=str(Path(tool.__file__).parent),
                                         GITHUB_OUTPUT=str(output)),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pushed=true\n", output.read_text())
        self.assertIn("rebuild=false\n", output.read_text())


class WorkflowTests(unittest.TestCase):
    def test_stale_outputs_dispatch_a_latest_main_build_and_never_deploy(self):
        workflow = yaml.safe_load((tool.ROOT / ".github/workflows/assets-generated.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["build"]["steps"]
        publish = next(s for s in steps if s.get("id") == "publish")
        self.assertIn("--force", publish["run"])
        self.assertNotIn("git rebase", publish["run"])
        rebuild = next(s for s in steps if s.get("name") == "Rebuild main after a release input changed")
        self.assertEqual(rebuild["if"], "steps.publish.outputs.rebuild == 'true'")
        self.assertIn('gh workflow run auto-track-jp-assets.yml --ref main -f stage=release -f force_build="$FORCE_BUILD"', rebuild["run"])
        self.assertIn('-f review_before="$REVIEW_BEFORE" -f review_after="$REVIEW_AFTER"', rebuild["run"])
        promotion = next(s for s in steps if s.get("name") == "Promote reviewed locale changes")
        self.assertIn("inputs.review_before", promotion["env"]["BEFORE_SHA"])
        self.assertIn("inputs.review_after", promotion["env"]["AFTER_SHA"])
        parent = yaml.safe_load((tool.ROOT / '.github/workflows/auto-track-jp-assets.yml').read_text(encoding='utf-8'))
        deploy = parent['jobs']['gateway']
        self.assertIn("needs.release.outputs.pushed == 'true'", deploy["if"])
        self.assertIn("needs.release.result == 'success'", deploy['if'])
        validation = yaml.safe_load((tool.ROOT / ".github/workflows/validate-localization.yml").read_text(encoding="utf-8"))
        self.assertIn("scripts/test_publish_generated_release.py", str(validation["jobs"]["regression"]))


if __name__ == "__main__":
    unittest.main()
