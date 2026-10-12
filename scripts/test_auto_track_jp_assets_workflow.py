#!/usr/bin/env python3
"""``auto-track-jp-assets.yml`` 的离线回归：冻结标签不可变、只派发不开申请单。

B6 改动前的行为（问题）：

* 冻结步骤用 ``git tag -f`` + ``git push -f`` 强推 ``assets-<version>``，会静默移动
  一个已被 ``generated/<asset_version>/manifest.json`` 记录为 ``source_commit`` 的标签；
* 版本检查把任何非 10 的退出码（网络/API 故障）当作“无更新”静默成功；
* 更新分支用 ``git push -u origin --force``；
* 走完 PR 后立即 ``gh pr merge --squash``，绕过分支保护/审核。

本套测试固定修复后的边界：

1. 结构断言（YAML）：三个 job 的分工与权限、checkout 显式绑定 ``github.sha``、
   不存在 ``gh pr merge``/``gh pr create``/``gh pr list``/``--squash``/``git tag -f``/
   任何带 force 的 push 命令行，也不存在任何写 ``manifests/asset-version.json``
   或推分支的命令——版本号只有一个写者，就是翻译工作流自己。
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
from datetime import datetime, timezone
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
            set(self.jobs), {"resolve-target", "freeze", "wake-translation"}
        )
        self.assertEqual(self.jobs["resolve-target"]["permissions"], {"contents": "read"})
        # 冻结标签仍然要写权限；唤醒 job 只读仓库，只多一个 actions: write（派发）。
        self.assertEqual(self.jobs["freeze"]["permissions"], {"contents": "write"})
        self.assertEqual(
            self.jobs["wake-translation"]["permissions"],
            {"contents": "read", "actions": "write"},
        )
        self.assertEqual(self.jobs["freeze"]["needs"], "resolve-target")
        self.assertIn(
            "needs.resolve-target.outputs.has_update == 'true'",
            str(self.jobs["freeze"]["if"]),
        )
        # 唤醒 job **故意不加** has_update 门槛：兜底叫醒（没有新版本）时它也要跑，
        # 否则“重试失败过的行、推进待审草稿”这件事就没人做了。
        self.assertEqual(self.jobs["wake-translation"]["needs"], "resolve-target")
        self.assertNotIn("has_update", str(self.jobs["wake-translation"].get("if", "")))

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

    def test_freeze_remains_a_dispatchable_writer(self) -> None:
        """冻结标签的能力保留，但触发器只剩 workflow_dispatch。

        2026-10-12 起仓库里不再有任何 schedule：唯一的时钟是 vps_racknerd 上的
        mltd-asset-watch.timer（每 5 分钟查一次版本号，发现新版立刻派发）。GitHub
        自己的定时实测被推迟 4~7 小时，同一天还出现过一次推送完全不产生事件，
        所以不再把“立刻”托付给它。
        """
        triggers = _triggers(self.workflow)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("schedule", triggers)
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], False)

    def test_no_workflow_in_this_repository_carries_a_schedule(self) -> None:
        """整个仓库只允许一个时钟：vps_racknerd 上的 mltd-asset-watch.timer。

        定时任务曾经被推迟 4~7 小时（16:00 UTC 的 cron 19:48~21:03 才启动），
        02:17 UTC 的翻译 cron 08:36~09:13 才启动。owner 2026-10-12 决定不再用
        GitHub 的定时，全部改由 VPS 叫醒——这条断言防止有人以后再悄悄加回来。
        """
        workflows = sorted((REPO / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows, "expected workflow files to exist")
        for path in workflows:
            with self.subTest(workflow=path.name):
                triggers = _triggers(yaml.safe_load(path.read_text(encoding="utf-8")))
                self.assertNotIn("schedule", triggers or {})

    def test_every_workflow_the_vps_wakes_stays_dispatchable(self) -> None:
        """唤醒器只能叫醒声明了 workflow_dispatch 的流程（否则 GitHub 回 422）。"""
        for name in (
            "auto-track-jp-assets.yml",
            "llm-translate-assets.yml",
            "image-localize-assets.yml",
            "cloudflare-gateway.yml",
            "assets-generated.yml",
        ):
            with self.subTest(workflow=name):
                path = REPO / ".github" / "workflows" / name
                triggers = _triggers(yaml.safe_load(path.read_text(encoding="utf-8")))
                self.assertIn("workflow_dispatch", triggers or {})

    # -- 3. bot 不再合并、也不再开申请单 -------------------------------------- #
    def test_no_pull_request_machinery_remains(self) -> None:
        """跟踪流程不再建分支、不再开 PR，也不再自己写版本号。

        版本号只有一个写者（翻译工作流自己，经 ``scripts/commit_llm_drafts.py``
        连清单一起推 main）。旧流程既开“等 owner 合并”的申请单、又让翻译工作流
        直接推 main，于是每个版本都留下一张追不上的申请单（#40 就是一张）。
        """
        self.assertNotIn("gh pr merge", self.text)
        self.assertNotIn("gh pr create", self.text)
        self.assertNotIn("gh pr list", self.text)
        self.assertNotIn("--squash", self.text)
        # 跟踪流程不得自己改清单，也不得推分支。
        self.assertNotIn("track_jp_assets.py --update", self.text)
        self.assertNotIn("git add", self.text)
        self.assertNotIn("git commit", self.text)
        self.assertNotIn("refs/heads/", self.text)

    # -- 4. 错误退出不再被吞 ------------------------------------------------- #
    def test_version_check_fails_loudly_on_non_0_10(self) -> None:
        run = _step_run(self.jobs["resolve-target"], "Check Japanese asset version")
        self.assertIn("EXIT_CODE", run)
        # 只有 0（无更新）与 10（有更新）被接受；其余显式 exit 1。
        self.assertGreaterEqual(run.count("exit 1"), 2)
        self.assertIn("treating as failure", run)

    def test_freeze_reads_the_tagged_commit_before_reusing_a_tag(self) -> None:
        """复用已在别处的冻结标签之前，必须先证明它记录的正是正在离开的版本。

        附注标签在 ``git ls-remote`` 里给出的是标签对象；直接与 HEAD 比较会把
        “标签已经正确”误判成冲突，所以 ``^{}`` 那一行必须优先。
        """
        run = _step_run(self.jobs["freeze"], "Freeze the outgoing")
        self.assertIn("refs/tags/${TAG}^{}", run)
        self.assertIn("manifests/asset-version.json", run)
        self.assertIn("already freezes asset version", run)
        self.assertIn("2>/dev/null || true", run)
        self.assertIn("exit 1", run)

    def test_wake_dispatches_and_is_guarded_against_stacking(self) -> None:
        """唤醒 job 是整条链唯一的入口：叫翻译任务，兜底那轮再叫一次图片汉化。

        跟踪流程会被 vps_racknerd 每 5 分钟叫一次（发现新版本）和每 24 小时叫一次
        （兜底），所以它必须自己回答几个问题：这一轮是“有新版本”还是“只收尾”、
        版本是不是已经被翻译工作流写过了（幂等）、是不是已经有任务在跑（不叠班）、
        上一次是不是刚起过头就失败（冷却）。
        """
        run = _step_run(self.jobs["wake-translation"], "Wake the translation workflow")
        self.assertIn("gh workflow run", run)
        self.assertIn("llm-translate-assets.yml", run)
        self.assertIn("--ref main", run)
        # 两条入口：有新版本，或兜底叫醒；都不是就什么都不做。
        self.assertIn('"${HAS_UPDATE}" = "true"', run)
        self.assertIn('"${DAILY_HOUSEKEEPING}" = "true"', run)
        self.assertIn("nothing to wake", run)
        # 幂等：清单已是目标版本就不再派发。
        self.assertIn('"$HEAD_VER" = "$LATEST_VER"', run)
        # 不叠班：排队中/运行中的任务都算。
        self.assertIn("queued", run)
        self.assertIn("in_progress", run)
        # 冷却：上一次刚起头的运行不立刻重试。
        self.assertIn("COOLDOWN_SECONDS", run)
        self.assertIn("createdAt", run)
        # 派发前必须证明 checkout 的版本号就是版本检查报告的当前版本。
        self.assertIn('"$HEAD_VER" != "$CURRENT_VER"', run)

        image = _step_run(self.jobs["wake-translation"], "Wake the image localization run")
        self.assertIn("image-localize-assets.yml", image)
        self.assertIn("gh workflow run", image)
        # 只在兜底那一轮按一次，并且不叠班。
        self.assertIn(
            "github.event.inputs.daily_housekeeping == 'true'",
            str(next(s for s in self.jobs["wake-translation"]["steps"]
                     if s.get("name") == "Wake the image localization run")["if"]),
        )
        self.assertIn("queued", image)
        self.assertIn("in_progress", image)

    def test_source_commit_is_the_checkout_not_a_moving_branch(self) -> None:
        for job_name in ("resolve-target", "freeze", "wake-translation"):
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
        cls.script_template = script.replace("${{ github.sha }}", "${GITHUB_SHA}")
        if "curl -fsSL" not in cls.script_template:
            raise AssertionError("the freeze step no longer reads the tagged manifest over HTTP")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="freeze-sandbox-")
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        # 冻结步骤用一个 HTTP 请求读“标签所在提交记录的版本号”。沙箱里换成桩，
        # 这样这一步依然完全离线：桩打印 MLTD_STUB_ASSET_VERSION，未设置就失败
        # （等价于“读不到”，必须 fail closed）。
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "curl"
        stub.write_text(
            "#!/bin/sh\n"
            '[ -n "${MLTD_STUB_ASSET_VERSION:-}" ] || exit 22\n'
            'printf \'{"asset_version": %s}\\n\' "$MLTD_STUB_ASSET_VERSION"\n',
            encoding="utf-8", newline="\n",
        )
        stub.chmod(0o755)
        # Git bash prepends its own /mingw64/bin to PATH, so a PATH stub for curl
        # would never win; the harness therefore calls the stub by absolute path
        # in the extracted script, the same way it substitutes the event SHA.
        self.script = self.script_template.replace("curl -fsSL", f"{self.bin.as_posix()}/curl -fsSL")
        # 步骤里用 python3 解析 API 返回的 JSON；沙箱提供同名入口指向当前解释器，
        # 这样被验证的仍是真正的解析代码，而不是一个更宽松的替身。
        python3 = self.bin / "python3"
        python3.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8", newline="\n",
        )
        python3.chmod(0o755)
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

    def run_freeze(self, old_ver: str, github_sha: str,
                   frozen_version: str | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, OLD_VER=old_ver, GITHUB_SHA=github_sha,
                   GH_TOKEN="sandbox-token", GITHUB_REPOSITORY="owner/repo")
        # 桩 curl 通过 PATH 生效；MSYS/Git bash 会把 Windows 风格的分隔符转换好。
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        if frozen_version is None:
            env.pop("MLTD_STUB_ASSET_VERSION", None)
        else:
            env["MLTD_STUB_ASSET_VERSION"] = frozen_version
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
        # 标签在别处，而且它记录的版本不是正在离开的这一版：必须失败，且不动远端。
        result = self.run_freeze("1077500", self.second, frozen_version="1077400")
        self.assertEqual(result.returncode, 1)
        self.assertIn("immutable", result.stderr)
        self.assertEqual(self.remote_tag_sha("assets-1077500"), self.first)

    def test_unreadable_tagged_manifest_fails_closed(self) -> None:
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        run_git(self.work, "tag", "assets-1077500", self.first)
        run_git(self.work, "push", "-q", "origin", "refs/tags/assets-1077500")
        self.checkout(self.second)
        result = self.run_freeze("1077500", self.second)  # 桩不可用 → 读不到版本
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot be reused", result.stderr)
        self.assertEqual(self.remote_tag_sha("assets-1077500"), self.first)

    def test_an_existing_freeze_of_the_outgoing_version_is_reused(self) -> None:
        """标签已经在别处，但它记录的正是正在离开的这一版：沿用，不移动。

        这正是 2026-10-11 那次失败的情形：pr #40 还开着、assets-1077741 已经在
        eae9a5ed 上冻结，而运行想把标签放到更新的 main 上。
        """
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        run_git(self.work, "tag", "assets-1077500", self.first)
        run_git(self.work, "push", "-q", "origin", "refs/tags/assets-1077500")
        self.checkout(self.second)
        result = self.run_freeze("1077500", self.second, frozen_version="1077500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already freezes asset version 1077500", result.stdout)
        self.assertEqual(self.remote_tag_sha("assets-1077500"), self.first)

    def test_annotated_tag_is_compared_by_its_commit(self) -> None:
        """附注标签：清单里的标签对象不是提交，比较必须用 ^{} 那一行。"""
        run_git(self.work, "remote", "add", "origin", str(self.origin))
        run_git(self.work, "tag", "-a", "assets-1077500", "-m", "freeze", self.second)
        run_git(self.work, "push", "-q", "origin", "refs/tags/assets-1077500")
        self.checkout(self.second)
        result = self.run_freeze("1077500", self.second, frozen_version="1077500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no-op", result.stdout)
        tag_object = self.remote_tag_sha("assets-1077500")
        self.assertNotEqual(tag_object, self.second, "the fixture must produce an annotated tag")

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


class WakeSandboxTest(unittest.TestCase):
    """在沙箱里执行唤醒步骤：三个门槛与派发本身。

    这一步现在承担“立刻”的接力（vps_racknerd 每 5 分钟派发本流程，本流程派发翻译
    工作流），所以它不能只有形状断言。沙箱提供 ``gh`` 与 ``date``，清单用真实文件，
    不访问网络、不接触真实仓库、不需要 git。

    需要 POSIX 的 bash：Windows 解释器给出的临时目录是 ``C:\\...``，PATH 里的替身
    在 WSL 的 bash 里找不到。CI 与手动核对都在 Linux 上跑这一套。
    """

    @classmethod
    def setUpClass(cls) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("needs a POSIX bash; run it on Linux (or under WSL python)")
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow not present: {WORKFLOW}")
        if BASH is None:
            raise unittest.SkipTest("bash is unavailable; cannot run the sandbox")
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.script = _step_run(workflow["jobs"]["wake-translation"], "Wake the translation workflow")
        if "gh workflow run" not in cls.script:
            raise AssertionError("the wake step no longer dispatches anything")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wake-sandbox-")
        self.root = Path(self._tmp.name)
        self.calls = self.root / "gh-calls.log"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "gh"
        stub.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$GH_CALLS"\n'
            'if [ "${GH_STUB_FAIL:-0}" = "1" ]; then exit 1; fi\n'
            'case "$*" in\n'
            '  *--status=queued*) printf \'%s\\n\' "${GH_STUB_QUEUED:-0}" ;;\n'
            '  *--status=in_progress*) printf \'%s\\n\' "${GH_STUB_IN_PROGRESS:-0}" ;;\n'
            '  *--json\\ createdAt*) printf \'%s\\n\' "${GH_STUB_LAST_CREATED:-}" ;;\n'
            '  *) exit 0 ;;\n'
            "esac\n",
            encoding="utf-8", newline="\n",
        )
        stub.chmod(0o755)
        manifest = self.root / "manifests" / "asset-version.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"asset_version": 1077500}\n', encoding="utf-8", newline="\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_wake(self, *, current: str = "1077500", latest: str = "1077600",
                 manifest: str = "1077500", has_update: str = "true",
                 daily: str = "false", **env_overrides: str
                 ) -> subprocess.CompletedProcess[str]:
        (self.root / "manifests" / "asset-version.json").write_text(
            f'{{"asset_version": {manifest}}}\n', encoding="utf-8", newline="\n"
        )
        env = dict(os.environ, GH_TOKEN="sandbox-token", CURRENT_VER=current,
                   LATEST_VER=latest, HAS_UPDATE=has_update,
                   DAILY_HOUSEKEEPING=daily, GH_CALLS=str(self.calls))
        env.update(env_overrides)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [BASH, "-c", self.script], cwd=str(self.root), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def dispatched(self) -> bool:
        if not self.calls.is_file():
            return False
        return "workflow run" in self.calls.read_text(encoding="utf-8")

    def test_dispatches_when_behind_and_idle(self) -> None:
        result = self.run_wake(GH_STUB_LAST_CREATED="2000-01-01T00:00:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dispatched(), self.calls.read_text(encoding="utf-8"))
        self.assertIn("Dispatched llm-translate-assets.yml", result.stdout)

    def test_already_tracked_is_a_noop(self) -> None:
        """清单已经是目标版本：翻译工作流写过了，不再派发。"""
        result = self.run_wake(current="1077500", latest="1077500", manifest="1077500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dispatched())
        self.assertIn("Already tracking", result.stdout)

    def test_a_queued_or_running_run_blocks_the_dispatch(self) -> None:
        for name, value in (("GH_STUB_QUEUED", "1"), ("GH_STUB_IN_PROGRESS", "1")):
            with self.subTest(status=name):
                self.calls.unlink(missing_ok=True)
                result = self.run_wake(GH_STUB_LAST_CREATED="2000-01-01T00:00:00Z", **{name: value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.dispatched(), f"{name} must block the dispatch")
                self.assertIn("not dispatching", result.stdout)

    def test_a_run_that_just_started_gets_room_before_a_retry(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = self.run_wake(GH_STUB_LAST_CREATED=now)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dispatched())
        self.assertIn("leaving it room", result.stdout)

    def test_an_unreadable_run_state_fails_closed(self) -> None:
        result = self.run_wake(GH_STUB_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dispatched())
        self.assertIn("cannot read the queued run state", result.stdout)

    def test_a_checkout_that_disagrees_with_the_check_fails(self) -> None:
        result = self.run_wake(current="1077400", latest="1077600", manifest="1077500")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.dispatched())
        self.assertIn("FAIL", result.stderr)

    def test_the_daily_tick_wakes_the_translation_without_a_new_version(self) -> None:
        """兜底叫醒：版本没变也要叫一次翻译任务（重试失败过的行、推进待审草稿）。"""
        result = self.run_wake(has_update="false", daily="true",
                              current="1077500", latest="1077500", manifest="1077500",
                              GH_STUB_LAST_CREATED="2000-01-01T00:00:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dispatched(), self.calls.read_text(encoding="utf-8"))
        self.assertIn("daily housekeeping", result.stdout)

    def test_a_quiet_tick_wakes_nothing(self) -> None:
        """既没有新版本、也不是兜底那一轮：什么都不做（就是每 5 分钟的正常一轮）。"""
        result = self.run_wake(has_update="false", daily="false",
                              current="1077500", latest="1077500", manifest="1077500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dispatched())
        self.assertIn("nothing to wake", result.stdout)


class ImageWakeSandboxTest(unittest.TestCase):
    """兜底那一轮里的“按一次图片汉化”：闲置才按，已在跑就不叠班。"""

    @classmethod
    def setUpClass(cls) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("needs a POSIX bash; run it on Linux (or under WSL python)")
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow not present: {WORKFLOW}")
        if BASH is None:
            raise unittest.SkipTest("bash is unavailable; cannot run the sandbox")
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.script = _step_run(
            workflow["jobs"]["wake-translation"], "Wake the image localization run"
        )
        if "gh workflow run" not in cls.script:
            raise AssertionError("the image wake step no longer dispatches anything")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="image-wake-sandbox-")
        self.root = Path(self._tmp.name)
        self.calls = self.root / "gh-calls.log"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "gh"
        stub.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$GH_CALLS"\n'
            'if [ "${GH_STUB_FAIL:-0}" = "1" ]; then exit 1; fi\n'
            'case "$*" in\n'
            '  *--status=queued*) printf \'%s\\n\' "${GH_STUB_QUEUED:-0}" ;;\n'
            '  *--status=in_progress*) printf \'%s\\n\' "${GH_STUB_IN_PROGRESS:-0}" ;;\n'
            '  *) exit 0 ;;\n'
            "esac\n",
            encoding="utf-8", newline="\n",
        )
        stub.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_image_wake(self, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, GH_TOKEN="sandbox-token", GH_CALLS=str(self.calls))
        env.update(env_overrides)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [BASH, "-c", self.script], cwd=str(self.root), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def dispatched(self) -> bool:
        if not self.calls.is_file():
            return False
        return "workflow run" in self.calls.read_text(encoding="utf-8")

    def test_dispatches_the_image_cohort_when_idle(self) -> None:
        result = self.run_image_wake()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dispatched(), self.calls.read_text(encoding="utf-8"))
        self.assertIn("image-localize-assets.yml", self.calls.read_text(encoding="utf-8"))

    def test_a_running_image_cohort_is_not_stacked(self) -> None:
        for name, value in (("GH_STUB_QUEUED", "1"), ("GH_STUB_IN_PROGRESS", "1")):
            with self.subTest(status=name):
                self.calls.unlink(missing_ok=True)
                result = self.run_image_wake(**{name: value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.dispatched(), f"{name} must block the dispatch")

    def test_an_unreadable_image_run_state_fails_closed(self) -> None:
        result = self.run_image_wake(GH_STUB_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dispatched())
        self.assertIn("cannot read the queued run state", result.stdout)


if __name__ == "__main__":
    unittest.main()
