#!/usr/bin/env python3
"""``workflow-patch.yml`` 的离线回归：手动一键要么只按入口，要么逐段等结果。

背景：2026-10-12 起仓库里不再有任何 GitHub 定时，整条链只有一个入口（跟踪流程），
平时由 vps_racknerd 派发。这个流程是给人用的同一个按钮，外加一个排障用的 ``full``
模式——逐段派发并等结果。

本套测试固定两件事：

1. 结构（YAML）：触发器只有 ``workflow_dispatch``、有 ``actions: write``（否则派发不了）、
   ``chain`` 那一步只派发跟踪流程、``full`` 那一步认识四个阶段。
2. 行为（真实 bash + 假的 gh）：``full`` 必须按 跟踪 → 翻译 → 出包 → 网关 的顺序派发；
   已经在跑的那一段要"加入"而不是重复派发；某一段失败不拦着后面几段照跑（修网关比
   "翻译失败就什么都不做"更有用），但整轮要报失败。

沙箱只用一个假的 ``gh``：不访问网络、不碰任何真实仓库或真实运行。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "workflow-patch.yml"

BASH = shutil.which("bash") or shutil.which("sh")

STUB_GH = '''#!/usr/bin/env python3
"""假的 gh：把调用记下来，并模拟"派发 -> 排队 -> 跑完"的运行记录。"""
import json
import os
import pathlib
import sys

state = pathlib.Path(os.environ["GH_STATE"])
state.mkdir(parents=True, exist_ok=True)
calls = pathlib.Path(os.environ["GH_CALLS"])
with calls.open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
fail_workflow = os.environ.get("GH_FAIL_WORKFLOW", "")
never_appear = os.environ.get("GH_NEVER_APPEAR", "")


def latest_path(workflow):
    return state / ("latest." + workflow)


def record_path(rid):
    return state / (rid + ".json")


if args[:2] == ["workflow", "run"]:
    workflow = args[2]
    if workflow == never_appear:
        sys.exit(0)
    counter = state / "counter"
    rid = str(int(counter.read_text().strip() or "0") + 1) if counter.exists() else "1"
    counter.write_text(rid)
    record_path(rid).write_text(json.dumps({"workflow": workflow, "polls": 0}))
    latest_path(workflow).write_text(rid)
    sys.exit(0)

if args[:2] == ["run", "list"]:
    workflow = ""
    for arg in args:
        if arg.startswith("--workflow="):
            workflow = arg.split("=", 1)[1]
    path = latest_path(workflow)
    if not path.exists():
        print("[]")
        sys.exit(0)
    rid = path.read_text().strip()
    print(json.dumps([{"databaseId": int(rid), "status": "in_progress", "conclusion": ""}]))
    sys.exit(0)

if args[:2] == ["run", "view"]:
    rid = args[2]
    record = json.loads(record_path(rid).read_text())
    record["polls"] += 1
    record_path(rid).write_text(json.dumps(record))
    if record["polls"] < 2:
        print(json.dumps({"status": "in_progress", "conclusion": ""}))
    else:
        conclusion = "failure" if record["workflow"] == fail_workflow else "success"
        print(json.dumps({"status": "completed", "conclusion": conclusion}))
    sys.exit(0)

sys.exit(0)
'''


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))


def _step_run(job: dict, name_prefix: str) -> str:
    for step in job.get("steps", []):
        if str(step.get("name", "")).startswith(name_prefix):
            return step.get("run", "")
    raise AssertionError(f"no step named like {name_prefix!r}")


class WorkflowPatchShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.job = cls.workflow["jobs"]["patch"]

    def test_it_is_a_manual_button_only(self) -> None:
        triggers = _triggers(self.workflow)
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        inputs = triggers["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"mode", "daily_housekeeping", "force_build"})
        self.assertEqual(inputs["mode"]["default"], "chain")

    def test_it_may_dispatch_runs(self) -> None:
        self.assertEqual(self.workflow["permissions"]["actions"], "write")
        self.assertEqual(self.workflow["permissions"]["contents"], "read")

    def test_chain_mode_only_presses_the_entry_point(self) -> None:
        run = _step_run(self.job, "Press the one button")
        self.assertIn("gh workflow run auto-track-jp-assets.yml --ref main", run)
        self.assertIn("daily_housekeeping", run)
        # 入口之后全靠连锁：这里不能再自己派发别的阶段。
        for other in ("llm-translate-assets.yml", "assets-generated.yml", "cloudflare-gateway.yml"):
            self.assertNotIn(other, run)

    def test_full_mode_knows_every_stage(self) -> None:
        # 阶段名通过环境变量传进脚本（这样测试能替换），所以这里查 env 而不是脚本正文。
        step = next(s for s in self.job["steps"]
                    if str(s.get("name", "")).startswith("Run every stage"))
        env = step.get("env", {})
        self.assertEqual(env.get("TRACK_WORKFLOW"), "auto-track-jp-assets.yml")
        self.assertEqual(env.get("TRANSLATE_WORKFLOW"), "llm-translate-assets.yml")
        self.assertEqual(env.get("BUILD_WORKFLOW"), "assets-generated.yml")
        self.assertEqual(env.get("GATEWAY_WORKFLOW"), "cloudflare-gateway.yml")
        run = step.get("run", "")
        self.assertIn("gh workflow run", run)
        self.assertIn("-f force=true", run)


class WorkflowPatchSandboxTest(unittest.TestCase):
    """在假的 gh 上跑 ``full`` 那一段：顺序、加入、失败不拦后面。"""

    @classmethod
    def setUpClass(cls) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("needs a POSIX bash; run it on Linux (or under WSL python)")
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow not present: {WORKFLOW}")
        if BASH is None:
            raise unittest.SkipTest("bash is unavailable; cannot run the sandbox")
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.script = _step_run(workflow["jobs"]["patch"], "Run every stage and wait")
        if "gh workflow run" not in cls.script:
            raise AssertionError("the full-mode step no longer dispatches anything")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="patch-sandbox-")
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        self.calls = self.root / "gh-calls.log"
        self.summary = self.root / "summary.md"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "gh"
        stub.write_text(STUB_GH, encoding="utf-8", newline="\n")
        stub.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_full(self, *, housekeeping: str = "true", force_build: str = "true",
                 **env_overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            GH_TOKEN="sandbox-token",
            GH_STATE=str(self.state),
            GH_CALLS=str(self.calls),
            GITHUB_STEP_SUMMARY=str(self.summary),
            HOUSEKEEPING=housekeeping,
            FORCE_BUILD=force_build,
            TRACK_WORKFLOW="auto-track-jp-assets.yml",
            TRANSLATE_WORKFLOW="llm-translate-assets.yml",
            BUILD_WORKFLOW="assets-generated.yml",
            GATEWAY_WORKFLOW="cloudflare-gateway.yml",
            POLL_SECONDS="0",
        )
        env.update(env_overrides)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [BASH, "-c", self.script], cwd=str(self.root), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def dispatched_workflows(self) -> list[str]:
        if not self.calls.is_file():
            return []
        found = []
        for line in self.calls.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts[:2] == ["workflow", "run"]:
                found.append(parts[2])
        return found

    def test_every_stage_runs_in_order(self) -> None:
        result = self.run_full()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            self.dispatched_workflows(),
            [
                "auto-track-jp-assets.yml",
                "llm-translate-assets.yml",
                "assets-generated.yml",
                "cloudflare-gateway.yml",
            ],
        )
        summary = self.summary.read_text(encoding="utf-8")
        for label in ("1/4 track and freeze", "2/4 translate", "3/4 build the release (forced)",
                      "4/4 deploy the CDN gateway"):
            self.assertIn(label, summary)

    def test_a_stage_already_running_is_joined_not_stacked(self) -> None:
        """翻译任务已经在跑：那一段只等它，不再派发第二个。"""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "counter").write_text("1")
        (self.state / "1.json").write_text('{"workflow": "llm-translate-assets.yml", "polls": 1}')
        (self.state / "latest.llm-translate-assets.yml").write_text("1")
        result = self.run_full()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("joining it instead of stacking another", result.stdout)
        self.assertNotIn("llm-translate-assets.yml", self.dispatched_workflows())

    def test_a_failed_stage_does_not_stop_the_tail(self) -> None:
        """出包失败也要继续修网关——最后整轮报失败。"""
        result = self.run_full(GH_FAIL_WORKFLOW="assets-generated.yml")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("cloudflare-gateway.yml", self.dispatched_workflows())
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn("| 3/4 build the release (forced) | failure |", summary)
        self.assertIn("| 4/4 deploy the CDN gateway | success |", summary)

    def test_force_build_can_be_turned_off(self) -> None:
        result = self.run_full(force_build="false")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        call_lines = self.calls.read_text(encoding="utf-8")
        self.assertIn("workflow run assets-generated.yml", call_lines)
        self.assertNotIn("force=true", call_lines)


if __name__ == "__main__":
    unittest.main()
