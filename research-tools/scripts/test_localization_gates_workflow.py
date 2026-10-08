#!/usr/bin/env python3
"""``Localization and release gates`` 工作流契约（B6 组件分流后）。

``.github/workflows/localization-gates.yml`` 是本仓库唯一真正在 GitHub 托管 runner
上运行的工作流；B6 把它从单个 ``candidate-gates`` job 拆成 ``select`` 组件选择 +
``nav``/``portal``/``apk``/``assets``/``runtime`` 五个门禁 job（runtime 为 server
consumer seam 的小型合成门）。本套测试固定这次拆分的行为边界：

* 触发面：push/pull_request 的 ``paths-ignore`` **只**排除物理产物目录
  （build/work/reverse/analysis）。docs/、README、asset-server/ 等都必须触达
  workflow——文档变化由 select job 收敛为 nav-only（不得在入口层跳过，否则导航门
  永远收不到文档提交）。
* 输出流（曾真实踩过的缺陷）：select 步骤的 ``run`` 必须把选择器 stdout ``tee`` 进
  ``$GITHUB_OUTPUT``；这里不只做字符串断言，而是把整个 ``run`` 块在临时 git 仓库里
  真执行（dispatch 与 base/head 两个分支），验证四个键确实落进 GH 环境文件、
  docs-only 时 nav 真而重组件假、python 不可用时步骤失败且文件里不出现任何
  ``=true`` 伪值。
* 依赖安装仍在用到的 job 里：apk 与 assets 都安装并缓存
  ``client/requirements-apk-ci.txt``；assets 另有 asset-server 路由/镜像上下文两个
  套件（需要 msgpack 固定版本，asset-server/requirements.txt 的闭包）。
* 测试覆盖不缩水：原 APK 步的每个测试文件、Assets 两步的全部 pytest 套件、B2 新增的
  ``client/tests/test_apk_candidate_workflow.py``、asset-server 的两个套件仍被某个
  ``run`` 点名；portal 收敛为 ``npm test`` 单一入口，且 package.json 的 test 脚本
  必须点名全部 8 个 harness。
* 门禁仍是只读：顶层与所有 job 保持 ``contents: read``，任何 ``run`` 里不得出现
  ``git push`` / ``gh release`` / ``git commit`` / ``wrangler deploy``。
* 每个被点名的测试路径必须真实存在于磁盘，避免断言落空。

这些断言是静态 YAML 解析 + 本地文件检查 + 本地临时 git 沙箱：不访问网络、不调用
GitHub API、不执行 Actions。可直接运行（``python scripts/test_localization_gates_workflow.py``，
nav job 即以此方式执行）或经 pytest 运行。
"""
from __future__ import annotations

import ast
import json
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "localization-gates.yml"
SELECTOR = REPO / "scripts" / "maintenance" / "select-component-gates.py"
PORTAL = REPO / "web" / "translation-portal"

_spec = importlib.util.spec_from_file_location("select_component_gates", SELECTOR)
selector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selector)

GIT = shutil.which("git")
BASH = shutil.which("bash")

# 原 candidate-gates 步里 APK 相关的测试文件，一个都不能少。
APK_TEST_FILES = (
    "client/tests/test_hotplay_source.py",
    "client/tests/test_apk_builtin_localization.py",
    "client/tests/test_bottom_bar_install.py",
    "client/tests/test_bottom_bar_footer.py",
    "client/tests/test_localized_apk_ci.py",
    "client/tests/test_apk_candidate_pipeline.py",
    "scripts/test_prepare_appguard_free_apktool.py",
    "scripts/test_rewrite_arm64_split_appguard_free.py",
    "scripts/test_align_gtx_translations.py",
    "client/tests/test_release_channel.py",
    "client/tests/test_release_bundle.py",
    "scripts/test_run_dual_track_pipeline.py",
    "client/tests/test_local_track_pin.py",
)
# B2 退役 self-hosted 候选后新增的 APK 工作流边界测试（终态 6 项断言）。
APK_NEW_TESTS = ("client/tests/test_apk_candidate_workflow.py",)
# assets job 的三步：candidate 快照/发布束校验 + asset-server 闭包 + release 模型/生成物/镜像。
ASSETS_STEP_TESTS = (
    "scripts/test_consume_translation_portal_snapshot.py",
    "scripts/test_validate_localization_release_bundle.py",
)
ASSETS_SERVER_TESTS = (
    "asset-server/test_assets_route.py",
    "asset-server/test_docker_context.py",
)
ASSETS_SUITE_TESTS = (
    "scripts/test_release_decoupling.py",
    "scripts/test_private_build_poller.py",
    "scripts/test_materialize_generated_release.py",
    "scripts/test_assets_generated_index.py",
    "scripts/test_assets_mirror.py",
    "scripts/test_assets_generated_workflow.py",
)
# nav job 的两个本地契约测试（本文件 + 组件选择器回归）。
NAV_TESTS = (
    "scripts/test_localization_gates_workflow.py",
    "scripts/maintenance/test_select_component_gates.py",
)
# portal 单入口必须覆盖的 8 套 harness。
PORTAL_HARNESSES = (
    "test_worker.mjs",
    "test_sync.mjs",
    "test_config.mjs",
    "test_image_ratio.mjs",
    "test_github_collab.mjs",
    "test_github_session.mjs",
    "test_frontend_github.mjs",
    "test_resource_manifest.mjs",
)
# 入口层允许忽略的目录：只放物理产物，绝不含 docs/ 或任何源码/文档入口。
TRIGGER_IGNORES = ("build/**", "work/**", "reverse/**", "analysis/**")
# 这些路径必须能触发 workflow（由 select job 决定跑哪些门）。
TRIGGER_MUST_NOT_IGNORE = ("docs/**", "docs/streams/client/**", "*.md",
                           "asset-server/**", "web/translation-portal/**", "client/**")
COMPONENT_JOBS = ("nav", "portal", "apk", "assets", "runtime")
# runtime 门的精确六套回归：固定最小依赖，不安装整套 APK 依赖。
RUNTIME_TESTS = (
    "server/tests/test_localserver_consumer.py",
    "server/tests/test_runtime_manifest.py",
    "server/tests/test_research_consumer_source_seam.py",
    "server/tests/test_research_consumer_source_seam_loader_wiring.py",
    "server/tests/test_offline_shape_official_counts_seam.py",
    "server/tests/test_observed_rpc_surface_seam_inputs.py",
)
# 契约独立列举输入闭包，避免从 selector 自身读取期望值而放过漏项。
RUNTIME_SOURCE_FILES = (
    "scripts/run-local-arm64-responder.py",
    "scripts/start-local-arm64-stack.ps1",
    "server/runtime_manifest.py",
    "scripts/research_consumer_source_seam.py",
    "scripts/audit-current-runtime-oracle-responder-diff.py",
    "scripts/audit-current-runtime-oracle-makecache-batch-replay.py",
    "scripts/audit-current-runtime-oracle-full-replay.py",
    "scripts/build-current-runtime-oracle-observed-rpc-surface.py",
    "scripts/audit-current-runtime-oracle-all-read-shapes.py",
    "scripts/audit-current-runtime-oracle-existing-state-chains.py",
    "scripts/audit-oneplus8t-archived-session.py",
    "scripts/audit-oneplus8t-event-ranking-provenance.py",
    "scripts/audit-oneplus8t-live-resource-conditions.py",
    "scripts/audit-oneplus8t-new-routes-local-shapes.py",
    "scripts/probe-current-runtime-oracle-offline-shape.py",
    *RUNTIME_TESTS,
    "server/prototype_responder.py",
    "server/mltd_wire.py",
    "server/idol_detail_projection.py",
    "server/theater_projection.py",
    "server/live_settlement.py",
    "server/birthday_projection.py",
    "server/collection_projection.py",
    "server/glasses_detail_projection.py",
    "server/status_content_projection.py",
    "server/local_state.py",
    "server/wire_shape_coverage.py",
)
FORBIDDEN_RUN_TOKENS = ("git push", "gh release", "git commit", "wrangler deploy")

TEST_PATH_RE = re.compile(r"[\w./-]*test_[\w.-]*\.(?:py|mjs)")


def _triggers(workflow: dict) -> dict:
    """PyYAML 把裸 ``on`` 解析为布尔 True（YAML 1.1）。"""
    return workflow.get("on", workflow.get(True))


def _run_texts(workflow: dict) -> list[str]:
    texts: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str):
                texts.append(run)
    return texts


class LocalizationGatesWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow not present: {WORKFLOW}")
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)
        cls.jobs = cls.workflow["jobs"]
        cls.runs = _run_texts(cls.workflow)
        cls.joined_runs = "\n".join(cls.runs)
        cls.named = {match for run in cls.runs for match in TEST_PATH_RE.findall(run)}

    def _job_text(self, name: str) -> str:
        return "\n".join(
            step.get("run", "") for step in self.jobs[name].get("steps", [])
        )

    # -- 1. job 结构与 select 输出接线 ------------------------------------- #
    def test_five_jobs_with_component_gates_on_selector_outputs(self) -> None:
        self.assertEqual(set(self.jobs), {"select", *COMPONENT_JOBS})
        self.assertEqual(
            set(self.jobs["select"]["outputs"]), set(COMPONENT_JOBS)
        )
        for name in COMPONENT_JOBS:
            with self.subTest(job=name):
                job = self.jobs[name]
                self.assertEqual(job["runs-on"], "ubuntu-latest")
                self.assertEqual(job["needs"], "select")
                self.assertIn(
                    f"needs.select.outputs.{name} == 'true'", str(job["if"])
                )

    def test_select_job_runs_the_component_selector(self) -> None:
        select = self._job_text("select")
        self.assertIn("scripts/maintenance/select-component-gates.py", select)
        self.assertIn("--format github", select)
        self.assertIn("--all", select)  # workflow_dispatch 分支
        # push 用 github.event.before；PR 用 pull_request.base.sha（merge-base 起点）。
        self.assertIn("github.event.before", select)
        self.assertIn("github.event.pull_request.base.sha", select)
        # merge-base 与 blob 级真实变化判断需要 base 提交在本地存在。
        checkout = self.jobs["select"]["steps"][0]
        self.assertEqual(checkout.get("with", {}).get("fetch-depth"), 0)

    def test_select_step_writes_the_selector_stdout_into_github_output(self) -> None:
        """stdout 必须被转发进 $GITHUB_OUTPUT；只写命令行不算接线。"""
        select_step = next(
            step for step in self.jobs["select"]["steps"]
            if step.get("id") == "components"
        )
        run = select_step["run"]
        self.assertIn('"$GITHUB_OUTPUT"', run)
        self.assertIn("tee -a", run)
        # 每个调用分支都必须带转发（dispatch / base-head 两处）。先把 shell 续行
        # 拼成逻辑单行，再检查“调用 + 同一逻辑行的 tee”。
        joined = run.replace("\\\n", " ")
        invocations = re.findall(r"select-component-gates\.py[^\n]*", joined)
        self.assertEqual(len(invocations), 2)
        for invocation in invocations:
            with self.subTest(invocation=invocation.strip()):
                self.assertIn("tee -a", invocation)
                self.assertIn('"$GITHUB_OUTPUT"', invocation)
        self.assertIn("set -euo pipefail", run)

    def test_each_component_job_keeps_its_own_toolchain(self) -> None:
        apk_uses = [s.get("uses", "") for s in self.jobs["apk"]["steps"]]
        assets_uses = [s.get("uses", "") for s in self.jobs["assets"]["steps"]]
        portal_uses = [s.get("uses", "") for s in self.jobs["portal"]["steps"]]
        self.assertIn("actions/setup-python@v5", apk_uses)
        self.assertIn("actions/setup-python@v5", assets_uses)
        self.assertIn("actions/setup-node@v4", portal_uses)
        self.assertNotIn("actions/setup-python@v5", portal_uses)

    # -- 2. 触发面 ---------------------------------------------------------- #
    def test_paths_ignore_covers_only_physical_artifact_dirs(self) -> None:
        triggers = _triggers(self.workflow)
        for event in ("push", "pull_request"):
            with self.subTest(event=event):
                block = triggers[event]
                self.assertNotIn("paths", block)  # 正列表会漏掉未来新组件目录
                ignores = block["paths-ignore"]
                self.assertEqual(sorted(ignores), sorted(TRIGGER_IGNORES))
                for pattern in TRIGGER_MUST_NOT_IGNORE:
                    self.assertNotIn(pattern, ignores)

    def test_workflow_dispatch_takes_no_inputs(self) -> None:
        triggers = _triggers(self.workflow)
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn(triggers["workflow_dispatch"], (None, {}))

    # -- 3. 测试覆盖不缩水 --------------------------------------------------- #
    def test_apk_job_names_every_original_apk_test(self) -> None:
        apk = self._job_text("apk")
        for path in (*APK_TEST_FILES, *APK_NEW_TESTS):
            with self.subTest(path=path):
                self.assertIn(path, apk)
                self.assertTrue((REPO / path).is_file(), f"missing test file: {path}")

    def test_assets_job_runs_the_asset_server_closure_tests(self) -> None:
        assets = self._job_text("assets")
        for path in ASSETS_SERVER_TESTS:
            with self.subTest(path=path):
                self.assertIn(path, assets)
                self.assertTrue((REPO / path).is_file(), f"missing test file: {path}")
        self.assertIn("msgpack", assets)

    def test_assets_job_pins_schema_dependency_and_asserts_it_imports(self) -> None:
        # schema 一致性用例在缺 jsonschema 时会静默 skip；assets 门必须固定安装
        # 版本并在运行测试前显式 import/断言，让“漏装”成为硬失败而不是隐形跳过。
        assets_job = self.jobs["assets"]
        runs = [step.get("run", "") for step in assets_job["steps"]]
        joined = "\n".join(runs)
        self.assertIn("jsonschema==4.26.0", joined)
        import_steps = [run for run in runs if "import" in run and "jsonschema" in run]
        self.assertTrue(import_steps, "assets job must assert jsonschema imports")
        self.assertRegex(import_steps[0], r"4\.26\.0")

    def test_assets_job_names_every_pytest_suite(self) -> None:
        assets = self._job_text("assets")
        for path in (*ASSETS_STEP_TESTS, *ASSETS_SUITE_TESTS):
            with self.subTest(path=path):
                self.assertIn(path, assets)
                self.assertTrue((REPO / path).is_file(), f"missing test file: {path}")

    def test_nav_job_runs_governance_audit_and_contract_tests(self) -> None:
        nav = self._job_text("nav")
        self.assertIn("validate-agent-workstreams.py --ci", nav)
        for path in NAV_TESTS:
            with self.subTest(path=path):
                self.assertIn(path, nav)
                self.assertTrue((REPO / path).is_file(), f"missing test file: {path}")

    def test_portal_job_uses_the_single_npm_test_entrypoint(self) -> None:
        portal_job = self.jobs["portal"]
        npm_steps = [
            step for step in portal_job["steps"]
            if "npm test" in step.get("run", "")
        ]
        self.assertEqual(len(npm_steps), 1)
        self.assertEqual(
            npm_steps[0].get("working-directory"), "web/translation-portal"
        )
        # 不再逐个内联 node harness；覆盖范围由 package.json 的 test 脚本保证。
        self.assertNotIn("node test_", self._job_text("portal"))
        manifest = json.loads((PORTAL / "package.json").read_text(encoding="utf-8"))
        test_script = manifest["scripts"]["test"]
        for harness in PORTAL_HARNESSES:
            with self.subTest(harness=harness):
                self.assertIn(harness, test_script)
                self.assertTrue((PORTAL / harness).is_file(), f"missing: {harness}")

    def test_runtime_job_runs_only_the_consumer_seam_tests(self) -> None:
        # 精确固定六套及整个命令，禁止新增过滤参数或绕过 process-tree 安全用例。
        runtime = self._job_text("runtime")
        pytest_steps = [step["run"] for step in self.jobs["runtime"]["steps"]
                        if "python -m pytest" in step.get("run", "")]
        self.assertEqual(len(pytest_steps), 1)
        self.assertEqual(shlex.split(pytest_steps[0]),
                         ["python", "-m", "pytest", *RUNTIME_TESTS, "-q"])
        self.assertEqual(set(TEST_PATH_RE.findall(runtime)), set(RUNTIME_TESTS))
        for path in RUNTIME_TESTS:
            with self.subTest(path=path):
                self.assertIn(path, runtime)
                self.assertTrue((REPO / path).is_file(), f"missing test file: {path}")

    def test_runtime_job_pins_minimal_dependencies_and_asserts_imports(self) -> None:
        steps = self.jobs["runtime"]["steps"]
        installs = [step["run"] for step in steps
                    if "pip install" in step.get("run", "")]
        self.assertEqual(len(installs), 1)
        self.assertEqual(shlex.split(installs[0]), [
            "python", "-m", "pip", "install", "pytest==8.4.2", "pycryptodome==3.23.0",
        ])
        pins = (REPO / "client/requirements-apk-ci.txt").read_text(encoding="utf-8").splitlines()
        for pin in ("pytest==8.4.2", "pycryptodome==3.23.0"):
            self.assertIn(pin, pins)
        setup = next(step for step in steps if step.get("uses") == "actions/setup-python@v5")
        self.assertEqual(setup["with"]["cache-dependency-path"], "client/requirements-apk-ci.txt")
        imports = [step["run"] for step in steps
                   if shlex.split(step.get("run", ""))[:2] == ["python", "-c"]]
        self.assertEqual(len(imports), 1, "依赖必须在 pytest 之前硬断言导入")
        self.assertLess(next(i for i, s in enumerate(steps) if s.get("run") == installs[0]),
                        next(i for i, s in enumerate(steps) if s.get("run") == imports[0]))
        self.assertLess(next(i for i, s in enumerate(steps) if s.get("run") == imports[0]),
                        next(i for i, s in enumerate(steps) if "python -m pytest" in s.get("run", "")))
        command = shlex.split(imports[0])
        self.assertEqual(len(command), 3)
        # 解析真实 Python 语句，注释中的 import/assert 不能冒充依赖门。
        body = ast.parse(command[2]).body
        imported = {alias.name for node in body if isinstance(node, ast.Import)
                    for alias in node.names}
        self.assertIn("pytest", imported)
        self.assertTrue(any(isinstance(node, ast.Import) and any(
            alias.name == "importlib.metadata" and alias.asname == "m" for alias in node.names
        ) for node in body))
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == "Crypto.Cipher"
                            and any(alias.name == "AES" for alias in node.names) for node in body))
        assertions = {ast.unparse(node.test) for node in body if isinstance(node, ast.Assert)}
        self.assertTrue({"m.version('pytest') == '8.4.2'",
                         "m.version('pycryptodome') == '3.23.0'",
                         "AES.block_size == 16"}.issubset(assertions))
        self.assertFalse(any(isinstance(node, (ast.Try, ast.If)) for node in body),
                         "导入或版本失败不能被捕获或条件跳过")

    def test_runtime_job_source_closure_is_explicit_and_synthetic(self) -> None:
        # 共享 helper、直接 consumer、lazy loader、测试、默认导入及研究 helper 闭包。
        files = selector.COMPONENT_FILES["runtime"]
        self.assertEqual(set(files), set(RUNTIME_SOURCE_FILES))
        self.assertEqual(len(files), len(RUNTIME_SOURCE_FILES), "精确清单不能含重复项")
        self.assertEqual(selector.COMPONENT_PREFIXES.get("runtime", ()), ())
        # 兄弟产品树 D:/Project/MLTDLocalServer 是 CI 之外的输入：runtime job 不得引用
        # 它；进程安全用例仍完整接入，执行验收须等 main 的源码 freeze。
        self.assertNotIn("MLTDLocalServer", self._job_text("runtime"))

    def test_portal_job_keeps_the_worker_syntax_boundary(self) -> None:
        # worker 语法检查随 portal 组件走（worker.js 属于 web/translation-portal/）；
        # 行为覆盖由 npm test 里的 test_worker.mjs 提供。
        portal = self._job_text("portal")
        self.assertIn("node --check web/translation-portal/src/worker.js", portal)

    def test_every_named_test_file_exists_on_disk(self) -> None:
        self.assertTrue(self.named, "no test file is named by any step")
        for path in sorted(self.named):
            with self.subTest(path=path):
                self.assertTrue((REPO / path).is_file(),
                                f"workflow names a missing test file: {path}")

    # -- 4. 依赖安装与缓存仍在使用的 job 内 --------------------------------- #
    def test_requirements_install_and_cache_survive_in_apk_and_assets(self) -> None:
        for name in ("apk", "assets"):
            with self.subTest(job=name):
                job = self.jobs[name]
                runs = "\n".join(step.get("run", "") for step in job["steps"])
                self.assertIn("requirements-apk-ci", runs)
                setup = [
                    step for step in job["steps"]
                    if step.get("uses", "").startswith("actions/setup-python")
                ]
                self.assertTrue(setup)
                self.assertEqual(
                    setup[0].get("with", {}).get("cache-dependency-path"),
                    "client/requirements-apk-ci.txt",
                )

    # -- 5. 门禁保持只读 ----------------------------------------------------- #
    def test_permissions_stay_read_only(self) -> None:
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                if "permissions" in job:
                    self.assertEqual(job["permissions"], {"contents": "read"})

    def test_no_step_pushes_or_publishes(self) -> None:
        for forbidden in FORBIDDEN_RUN_TOKENS:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.joined_runs)


class SelectStepSandboxTest(unittest.TestCase):
    """把 select 步骤的 run 块放进临时 git 仓库真执行（本地沙箱，无网络）。

    覆盖两个事件分支与失败路径：dispatch 全真、docs-only 只开 nav、真实改动
    只开对应组件、python 不可用时非零退出且 GH 输出文件里没有伪值。
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow not present: {WORKFLOW}")
        if GIT is None or BASH is None:
            raise unittest.SkipTest("git or bash is unavailable; cannot run the sandbox")
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        step = next(
            step for step in workflow["jobs"]["select"]["steps"]
            if step.get("id") == "components"
        )
        cls.script = step["run"]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="select-step-")
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._init_repo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [GIT, *args], cwd=str(self.repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )

    def _init_repo(self) -> None:
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "select sandbox")
        self._git("config", "commit.gpgsign", "false")
        self._git("config", "core.autocrlf", "false")
        # 选择器按工作流里的相对路径调用，因此放进同构路径。
        target = self.repo / "scripts" / "maintenance" / "select-component-gates.py"
        target.parent.mkdir(parents=True)
        shutil.copyfile(SELECTOR, target)
        (self.repo / "README.md").write_text("# base\n", encoding="utf-8", newline="\n")
        self._commit("base")
        self.base = self._git("rev-parse", "HEAD").stdout.strip()

    def _commit(self, message: str) -> None:
        self._git("add", "-A")
        self._git("commit", "-q", "--no-verify", "-m", message)

    def _touch(self, relative: str, text: str = "x\n") -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _run_step(self, *, event: str, base: str | None, head: str,
                  forced_all: str, path_override: str | None = None) -> tuple[
                      subprocess.CompletedProcess[str], dict[str, str]]:
        script = (self.script
                  .replace("${{ github.event_name }}", event)
                  .replace("${{ github.event.pull_request.base.sha }}", base or "")
                  .replace("${{ github.event.before }}", base or "")
                  .replace("${{ github.sha }}", head))
        output_file = self.root / f"gh-output-{event}-{forced_all}.txt"
        output_file.write_text("", encoding="utf-8")
        env = dict(os.environ,
                   FORCED_ALL=forced_all,
                   GITHUB_OUTPUT=output_file.as_posix())
        if path_override is not None:
            env["PATH"] = path_override
        proc = subprocess.run(
            [BASH, "-c", script], cwd=str(self.repo), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        keys = {}
        for line in output_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                keys[key.strip()] = value.strip()
        return proc, keys

    # -- dispatch：全开 ------------------------------------------------------ #
    def test_dispatch_branch_writes_all_true(self) -> None:
        proc, keys = self._run_step(
            event="workflow_dispatch", base=None, head=self.base, forced_all="true"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(keys, {"nav": "true", "portal": "true", "apk": "true",
                                "assets": "true", "runtime": "true"})

    # -- docs-only：nav 真、重组件假（真实走一遍 git 变化判断） ------------- #
    def test_docs_only_branch_enables_nav_and_skips_heavy_gates(self) -> None:
        self._touch("docs/note.md", "# doc\n")
        self._touch("docs/streams/client/STATE.md", "# state\n")
        self._commit("docs only")
        proc, keys = self._run_step(
            event="push", base=self.base, head=self._head(), forced_all="false"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(keys, {"nav": "true", "portal": "false", "apk": "false",
                                "assets": "false", "runtime": "false"})

    # -- 组件变化：asset-server 闭包 -> assets 真 --------------------------- #
    def test_asset_server_change_enables_assets_only(self) -> None:
        self._touch("asset-server/Dockerfile", "FROM python:3.13-slim\n")
        self._touch("asset-server/test_assets_route.py", "x\n")
        self._touch("server/asset_archive.py", "x\n")
        self._commit("asset-server closure change")
        proc, keys = self._run_step(
            event="push", base=self.base, head=self._head(), forced_all="false"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(keys, {"nav": "true", "portal": "false", "apk": "false",
                                "assets": "true", "runtime": "false"})

    # -- runtime consumer seam 变化 -> 只开 runtime ------------------------- #
    def test_runtime_consumer_change_enables_runtime_only(self) -> None:
        self._touch("scripts/run-local-arm64-responder.py", "x = 1\n")
        self._touch("server/runtime_manifest.py", "x\n")
        self._touch("server/tests/test_localserver_consumer.py", "x\n")
        self._commit("runtime consumer seam change")
        proc, keys = self._run_step(
            event="push", base=self.base, head=self._head(), forced_all="false"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(keys, {"nav": "true", "portal": "false", "apk": "false",
                                "assets": "false", "runtime": "true"})

    # -- 失败路径：python 不可用 => 非零退出且无伪值 ------------------------ #
    def test_missing_python_fails_without_emitting_fake_keys(self) -> None:
        proc, keys = self._run_step(
            event="push", base=self.base, head=self._head(), forced_all="false",
            path_override="/usr/bin",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(keys, {}, "a failed selector must not emit fake outputs")


if __name__ == "__main__":
    unittest.main()
