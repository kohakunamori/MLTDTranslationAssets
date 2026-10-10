"""The step that makes a night's translations survive a later failure.

Run 38058410546 translated 2,937 rows, validated them, and then lost every one
when a later step failed: `apply` wrote them into the runner's checkout, but the
commit only happened at the end of the job.  The drafts were recovered from the
diagnostics artifact by hand.  These tests pin the behaviour that avoids it:
the applied rows are committed and pushed as soon as they validate.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import commit_llm_drafts as committer  # noqa: E402


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)


def _repository(tmp: str) -> Path:
    """A repository with one commit and an `origin` that accepts a push."""
    root = Path(tmp) / "work"
    root.mkdir(parents=True)
    remote = Path(tmp) / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "remote", "add", "origin", str(remote))
    (root / "manifests").mkdir()
    (root / "manifests" / "asset-version.json").write_text(
        json.dumps({"asset_version": "1077741"}), encoding="utf-8")
    (root / "locales" / "story").mkdir(parents=True)
    (root / "locales" / "story" / "x.jsonl").write_text('{"ja": "あ"}\n', encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "validate_repo.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    _git(root, "push", "-u", "origin", "main")
    return root


class TranslationCommitTests(unittest.TestCase):
    def test_nothing_staged_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repository(tmp)
            self.assertEqual(committer.publish(root), 0)
            self.assertEqual(_git(root, "log", "--oneline").stdout.count("\n"), 1)

    def test_applied_rows_are_committed_and_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repository(tmp)
            (root / "locales" / "story" / "x.jsonl").write_text('{"ja": "あ", "zh": "啊"}\n',
                                                                encoding="utf-8")
            os.environ.pop("GITHUB_OUTPUT", None)
            self.assertEqual(committer.publish(root), 0)
            self.assertIn("apply LLM translations for asset 1077741",
                          _git(root, "log", "-1", "--format=%s").stdout)
            pushed = _git(root, "log", "-1", "--format=%s", "origin/main").stdout
            self.assertIn("apply LLM translations for asset 1077741", pushed)

    def test_the_message_records_a_promotion_only_when_one_happened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repository(tmp)
            subject, body = committer.commit_messages(root)
            self.assertIn("apply LLM translations", subject)
            self.assertIsNone(body, "the early commit must not claim a promotion")
            (root / ".llm-publish.json").write_text(json.dumps({"promoted": 1532}), encoding="utf-8")
            (root / ".llm-publish-lyrics.json").write_text(json.dumps({"promoted": 1392}),
                                                           encoding="utf-8")
            subject, body = committer.commit_messages(root)
            self.assertIn("apply and publish LLM drafts", subject)
            self.assertIn("1532 text row(s)", body)
            self.assertIn("1392 row(s)", body)

    def test_a_push_reports_that_main_moved(self):
        """The build dispatch keys off this output; a GITHUB_TOKEN push is silent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _repository(tmp)
            (root / "locales" / "story" / "x.jsonl").write_text('{"ja": "あ", "zh": "啊"}\n',
                                                                encoding="utf-8")
            output = Path(tmp) / "output.txt"
            output.write_text("", encoding="utf-8")
            os.environ["GITHUB_OUTPUT"] = str(output)
            try:
                self.assertEqual(committer.publish(root), 0)
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
            self.assertEqual(output.read_text(encoding="utf-8"), "published=true\n")

    def test_staged_paths_skip_manifests_that_do_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repository(tmp)
            paths = committer.stage_paths(root)
            self.assertIn("locales", paths)
            self.assertIn("manifests/asset-version.json", paths)
            self.assertNotIn("manifests/official-bundle-index.json", paths)
            self.assertNotIn("lyrics", paths, "a missing tree must not be staged")

    def test_the_workflow_commits_inside_the_apply_step(self):
        workflow = (Path(__file__).resolve().parents[1]
                    / ".github" / "workflows" / "llm-translate-assets.yml").read_text(encoding="utf-8")
        apply_run = workflow.split("name: Apply LLM translations", 1)[1].split("name: Publish", 1)[0]
        self.assertIn("python scripts/commit_llm_drafts.py", apply_run)
        self.assertLess(apply_run.index("python scripts/validate_repo.py"),
                        apply_run.index("python scripts/commit_llm_drafts.py"),
                        "the rows must validate before they are committed")


if __name__ == "__main__":
    unittest.main()
