#!/usr/bin/env python3
"""``auto-track-jp-assets.yml`` 的离线回归：冻结标签不可变、无 bot 自动合并。

B6 改动前的行为（问题）：

* 冻结步骤用 ``git tag -f`` + ``git push -f`` 强推 ``assets-<version>``，会静默移动
  一个已被 ``generated/<asset_version>/manifest.json`` 记录为 ``source_commit`` 的标签；
* 版本检查把任何非 10 的退出码（网络/API 故障）当作“无更新”静默成功；
* 更新分支用 ``git push -u origin --force``；
* 走完 PR 后立即 ``gh pr merge --squash``，绕过分支保护/审核。

本套测试固定修复后的边界：

1. 结构断言（YAML）：三个 job 的分工与权限、checkout 显式绑定 ``github.sha``、
   不存在 ``gh pr merge``/``--squash``/``git tag -f``/任何带 force 的 push 命令行。
2. 行为断言（真实 bash 沙箱）：把冻结步骤的脚本抽出来，在临时目录里对着**本地**
   bare 远端执行——标签不存在则创建、已存在且同 SHA 明确 no-op、不同 SHA 直接失败
   且不改动远端、复合版本号拒绝、checkout 与 github.sha 不一致时失败。

沙箱只用本地临时 git 仓库与本地 bare 远端：不访问网络、不接触真实 origin、
不推送/删除/移动任何真实分支或标签。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "auto-track-jp-assets.yml"

GIT = shutil.which("git")
BASH = shutil.which("bash") or shutil.which("sh")


def _triggers(workflow: dict) -> dict:
    """PyYAML 把裸 ``'on':`` 解析为字符串/布尔键，两种都取。"""
    return workflow.get("on", workflow.get(True))


def _step_run(job: dict, name_fragment: str) -> str:
    for step in job.get("steps", []):
        if name_fragment.lower() in str(step.get("name", "")).lower():
            run = step.get("run")
            if isinstance(run, str):
                return run
    raise AssertionError(f"no step matching {name_fragment!r}")


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


class WorkflowShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow not present: {WORKFLOW}")
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)
        cls.jobs = cls.workflow["jobs"]

    # -- 1. job 分工与权限 -------------------------------------------------- #
    def test_three_jobs_split_read_check_from_writes(self) -> None:
        self.assertEqual(
            set(self.jobs), {"resolve-target", "freeze", "update-pr"}
        )
        self.assertEqual(self.jobs["resolve-target"]["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["freeze"]["permissions"], {"contents": "write"})
        self.assertEqual(
            self.jobs["update-pr"]["permissions"],
            {"contents": "write", "pull-requests": "write"},
        )
        for name in ("freeze", "update-pr"):
            with self.subTest(job=name):
                self.assertEqual(self.jobs[name]["needs"], "resolve-target")
                self.assertIn(
                    "needs.resolve-target.outputs.has_update == 'true'",
                    str(self.jobs[name]["if"]),
                )

    def test_every_checkout_is_pinned_to_github_sha(self) -> None:
        checkouts = [
            step for job in self.jobs.values()
            for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/checkout")
        ]
        self.assertEqual(len(checkouts), 3)
        for step in checkouts:
            with self.subTest(step=step.get("name")):
                self.assertEqual(step.get("with", {}).get("ref"), "${{ github.sha }}")

    # -- 2. 冻结标签不可变 --------------------------------------------------- #
    @staticmethod
    def _command_lines(text: str) -> list[str]:
        """run 块里的命令行；跳过 # 注释与普通 YAML 键行。"""
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("- "):
                continue
            if stripped.startswith(("git push", "git tag")):
                lines.append(stripped)
        return lines

    def test_no_force_push_or_tag_move_commands(self) -> None:
        commands = self._command_lines(self.text)
        self.assertTrue(commands, "expected at least one git push/git tag command")
        for line in commands:
            with self.subTest(line=line):
                self.assertNotRegex(line, r"(?:^|\s)(?:-f|--force)(?:\s|$)")
                self.assertNotIn("--force-with-lease", line)
        # 说明性注释可以引用被修掉的旧命令，但命令本身不能出现。
        self.assertTrue(all("git tag -f" not in line for line in commands))
        self.assertTrue(all("git push -f" not in line for line in commands))

    def test_freeze_remains_a_scheduled_writer(self) -> None:
        triggers = _triggers(self.workflow)
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], False)

    # -- 3. 不再有 bot 自动合并 ---------------------------------------------- #
    def test_no_automatic_merge_remains(self) -> None:
        merged = [line for line in self._command_lines(self.text) if "gh pr merge" in line]
        self.assertEqual(merged, [], "the tracker must not merge pull requests")
        commands = "\n".join(self._command_lines(self.text))
        self.assertNotIn("--squash", commands)
        self.assertIn("gh pr create", self.text)
        self.assertIn("gh pr list", self.text)

    # -- 4. 错误退出不再被吞 ------------------------------------------------- #
    def test_version_check_fails_loudly_on_non_0_10(self) -> None:
        run = _step_run(self.jobs["resolve-target"], "Check Japanese asset version")
        self.assertIn("EXIT_CODE", run)
        # 只有 0（无更新）与 10（有更新）被接受；其余显式 exit 1。
        self.assertGreaterEqual(run.count("exit 1"), 2)
        self.assertIn("treating as failure", run)

    def test_source_commit_is_the_checkout_not_a_moving_branch(self) -> None:
        for job_name in ("resolve-target", "freeze", "update-pr"):
            run = "\n".join(
                step.get("run", "") for step in self.jobs[job_name].get("steps", [])
            )
            with self.subTest(job=job_name):
                self.assertIn("git rev-parse HEAD", run)
                self.assertIn("${{ github.sha }}", run)


class FreezeSandboxTest(unittest.TestCase):
    """在本地 bare 远端上执行冻结步骤脚本；网络与真实仓库都不参与。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow not present: {WORKFLOW}")
        if GIT is None or BASH is None:
            raise unittest.SkipTest("git or bash is unavailable; cannot run the sandbox")
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        script = _step_run(workflow["jobs"]["freeze"], "Freeze the outgoing")
        # 把步骤里的 github.sha 表达式换成环境变量，便于测试伪造“checkout 与事件 SHA 不一致”。
        cls.script = script.replace("${{ github.sha }}", "${GITHUB_SHA}")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="freeze-sandbox-")
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        run_git(self.root, "init", "-q", "--bare", str(self.origin))
        run_git(self.root, "init", "-q", "-b", "main", str(self.work))
        for key, value in (("user.email", "t@example.invalid"), ("user.name", "tracker test"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            run_git(self.work, "config", key, value)
        manifest = self.work / "manifests" / "asset-version.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}\n", encoding="utf-8", newline="\n")
        self.first = self.commit("first")
        # 第二个提交必须真的改动内容，否则 git commit 无事可做、两个 SHA 相同。
        manifest.write_text('{"asset_version": 1}\n', encoding="utf-8", newline="\n")
        self.second = self.commit("second")
        if self.first == self.second:
            raise AssertionError("sandbox fixture failed to create two distinct commits")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def commit(self, message: str) -> str:
        run_git(self.work, "add", "-A")
        run_git(self.work, "commit", "-q", "--no-verify", "-m", message)
        return run_git(self.work, "rev-parse", "HEAD").stdout.strip()

    def remote_tag_sha(self, tag: str) -> str:
        proc = run_git(self.work, "ls-remote", "origin", f"refs/tags/{tag}")
        return proc.stdout.split()[0] if proc.stdout.strip() else ""

    def checkout(self, sha: str) -> None:
        """把工作树 HEAD（detached）切到指定提交，模拟 checkout ref: github.sha。"""
        result = run_git(self.work, "checkout", "-q", "--detach", sha)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_freeze(self, old_ver: str, github_sha: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, OLD_VER=old_ver, GITHUB_SHA=github_sha)
        return subprocess.run(
            [BASH, "-c", self.script], cwd=str(self.work), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def test_creates_the_tag_when_absent(self) -> None:
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        self.checkout(self.second)
        result = self.run_freeze("1077500", self.second)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_tag_sha("assets-1077500"), self.second)

    def test_same_sha_is_an_explicit_noop(self) -> None:
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        run_git(self.work, "tag", "assets-1077500", self.first)
        run_git(self.work, "push", "-q", "origin", "refs/tags/assets-1077500")
        self.checkout(self.first)
        result = self.run_freeze("1077500", self.first)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no-op", result.stdout)
        self.assertEqual(self.remote_tag_sha("assets-1077500"), self.first)

    def test_different_sha_fails_without_moving_the_tag(self) -> None:
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        run_git(self.work, "tag", "assets-1077500", self.first)
        run_git(self.work, "push", "-q", "origin", "refs/tags/assets-1077500")
        self.checkout(self.second)
        result = self.run_freeze("1077500", self.second)
        self.assertEqual(result.returncode, 1)
        self.assertIn("immutable", result.stderr)
        self.assertEqual(self.remote_tag_sha("assets-1077500"), self.first)

    def test_composite_version_is_refused_before_any_push(self) -> None:
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        self.checkout(self.second)
        result = self.run_freeze("9.0.200+1077500", self.second)
        self.assertEqual(result.returncode, 1)
        self.assertIn("composite", result.stderr)
        self.assertEqual(self.remote_tag_sha("assets-9.0.200+1077500"), "")

    def test_checkout_must_match_the_event_sha(self) -> None:
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        self.checkout(self.second)
        result = self.run_freeze("1077500", "f" * 40)  # 事件 SHA 与 checkout HEAD 不一致
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL", result.stderr)
        self.assertEqual(self.remote_tag_sha("assets-1077500"), "")


if __name__ == "__main__":
    unittest.main()
