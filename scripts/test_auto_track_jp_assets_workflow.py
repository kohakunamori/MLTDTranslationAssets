"""Immutable outgoing-version freeze and fail-closed tracking, without network."""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/auto-track-jp-assets.yml"
GIT = shutil.which("git")
BASH = shutil.which("bash") or shutil.which("sh")
if os.name == 'nt' and GIT:
    git_bash = Path(GIT).resolve().parents[1] / 'bin/bash.exe'
    if git_bash.is_file():
        BASH = str(git_bash)

def _step_run(job, fragment):
    return next(step["run"] for step in job["steps"] if fragment.lower() in step.get("name", "").lower())

def run_git(cwd, *args):
    return subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)

class TrackingContractTest(unittest.TestCase):
    def test_tracking_stays_read_only_and_freeze_is_a_separate_writer(self):
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        jobs = doc["jobs"]
        self.assertEqual(jobs["resolve-target"]["permissions"], {"contents": "read"})
        self.assertEqual(jobs["freeze"]["permissions"], {"contents": "write"})
        self.assertIn("resolve-target", jobs["freeze"]["needs"])
        check = _step_run(jobs["resolve-target"], "Check Japanese asset version")
        self.assertIn("treating as failure", check)
        self.assertGreaterEqual(check.count("exit 1"), 2)
        freeze = _step_run(jobs["freeze"], "Freeze the outgoing")
        self.assertIn("refs/tags/${TAG}^{}", freeze)
        self.assertIn("already freezes asset version", freeze)
        commands = [line.strip() for line in freeze.splitlines() if line.strip().startswith("git push")]
        self.assertTrue(commands)
        for line in commands:
            self.assertNotIn("--force", line)
            self.assertNotIn(" -f ", line)
            self.assertNotIn("refs/heads", line)
        self.assertNotIn("track_jp_assets.py --update", check)
        self.assertFalse(any("gh workflow run" in step.get("run", "")
                             for key in ("resolve-target", "freeze") for step in jobs[key]["steps"]))

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
        cls.script_template = script.replace("${{ needs.plan.outputs.source_commit }}", "${GITHUB_SHA}")
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



if __name__ == "__main__":
    unittest.main()
