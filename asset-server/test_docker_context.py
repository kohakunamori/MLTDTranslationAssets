#!/usr/bin/env python3
"""Focused closure regression for the asset-server image context (本仓自足版).

这个测试验证 ``asset-server/Dockerfile`` 逐文件 COPY 出的**最小上下文**真的自洽：
每个 COPY 源都存在且是文件、不整树 COPY 目录；复制出的源码不引用主仓
（mltd-current）名字；每个 COPY 进镜像的模块所 import 的本仓模块也在 COPY 清单里；
把这份上下文复制到临时目录后（不挂任何兄弟仓库，剔除 PYTHONPATH），闭包模块可导入、
五个镜像 CLI 入口从复制后的 app 执行 ``--help`` 并退出 0，且 mirror 的顶层名 / 包名两条兼容导入分支都解析到同一份
producer 字节。未入镜像的 assets_route.py 另作宿主专用 CLI 检查。

它**不**运行 docker（不 pull、不 build、不起服务、不联网），也**不**验证生产部署；
镜像闭包是候选状态，构建与上线另行授权。
"""
from __future__ import annotations

import hashlib
import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "asset-server" / "Dockerfile"
COMPOSE = REPO / "asset-server" / "docker-compose.yml"

# ``COPY <src> <dst>``（当前 Dockerfile 无 --from、无多阶段）。
COPY_RE = re.compile(r"^COPY\s+(\S+)\s+(\S+)\s*$", re.M)

# 闭包模块：三个 compose 服务入口 + 它们 import 的共享模块。
CLOSURE_MODULES = (
    "server.asset_archive",
    "server.versioned_asset_store",
    "tools.asset_version",
    "tools.versioned_assets",
    "tools.materialize_versioned_assets",
    "tools.archive_controller",
    "scripts.assets_generated_index",
    "scripts.assets_mirror",
)

# 镜像 CLI 入口：只从物化 app 跑 ``--help``（无写盘/无网络）。
CLI_HELP_ENTRIES = (
    "tools/asset_version.py",
    "tools/versioned_assets.py",
    "tools/materialize_versioned_assets.py",
    "tools/archive_controller.py",
    "scripts/assets_mirror.py",
)

# 未列入 Dockerfile COPY 或 Compose 的入口，仅验证宿主 --help。
HOST_ONLY_CLI_HELP_ENTRIES = ("asset-server/assets_route.py",)

#: 本仓自足：闭包源码中不得出现主仓检出目录名（跨仓路径/回退的痕迹）。
FOREIGN_REPO_MARKERS = ("mltd-current",)

#: compose 三服务与 archive_controller 子进程链在镜像内需要的全部源文件
#: （逐文件 COPY 的「闭包」清单基线；新增运行时依赖必须同时更新这里与 Dockerfile）。
REQUIRED_COPIES = (
    "asset-server/requirements.txt",
    "server/asset_archive.py",
    "server/versioned_asset_store.py",
    "tools/archive_controller.py",
    "tools/versioned_assets.py",
    "tools/materialize_versioned_assets.py",
    "tools/asset_version.py",
    "scripts/assets_generated_index.py",
    "scripts/assets_mirror.py",
)

LOCAL_IMPORT_RE = re.compile(r"^\s*from\s+(scripts|server|tools)\.([A-Za-z_]\w*)", re.M)


def copy_pairs(text: str) -> list[tuple[str, str]]:
    """Dockerfile 里的 ``(src, dst)`` 对，按出现顺序。"""
    return [(m.group(1), m.group(2)) for m in COPY_RE.finditer(text)]


def missing_copy_sources(text: str, root: Path) -> list[str]:
    """上下文根 ``root`` 下不存在（或不是文件）的 COPY 源。

    目录算缺失，这样 ``COPY server /app/server`` 这类整树复制会被测试拒绝：
    镜像闭包必须逐文件声明，回归才能钉住它。
    """
    missing: list[str] = []
    for src, _dst in copy_pairs(text):
        path = root / src
        if not path.is_file():
            missing.append(src)
    return missing


def referenced_local_files(text: str) -> list[str]:
    """源码里以 ``scripts./server./tools.`` 前缀引用的本仓模块文件（POSIX 相对路径）。

    只认显式前缀导入——这正是本仓自足性要求的形式：跨仓引用要么是本仓文件，
    要么是第三方依赖；其余都应被本函数报出并在测试里对照 COPY 清单。
    """
    return sorted({f"{pkg}/{name}.py" for pkg, name in LOCAL_IMPORT_RE.findall(text)})


class DockerContextClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dockerfile_text = DOCKERFILE.read_text(encoding="utf-8")
        assert self.dockerfile_text, "Dockerfile must not be empty"
        self.copies = copy_pairs(self.dockerfile_text)

    def test_every_copy_source_exists_as_a_file(self):
        """逐文件 COPY：源必须存在且是文件（目录=整树复制，回归拒绝）。"""
        missing = missing_copy_sources(self.dockerfile_text, REPO)
        self.assertEqual(
            missing, [],
            f"COPY 源缺失或为目录（禁止整树 COPY）: {missing}")

    def test_missing_sources_are_detected(self):
        """负例自检：缺失/目录源必须被同一函数报出，避免测试自证循环。"""
        self.assertEqual(
            missing_copy_sources("COPY server /app/server\n", REPO), ["server"])
        self.assertEqual(
            missing_copy_sources("COPY no/such_file.py /app/x.py\n", REPO),
            ["no/such_file.py"])

    def test_copied_sources_never_name_a_foreign_repo(self):
        """闭包源码必须本仓自足：不得出现主仓/兄弟仓标识。"""
        for src, _dst in self.copies:
            if not src.endswith(".py"):
                continue
            text = (REPO / src).read_text(encoding="utf-8")
            for marker in FOREIGN_REPO_MARKERS:
                self.assertNotIn(
                    marker, text,
                    f"{src} 引用了本仓之外的项目标识 {marker!r}；闭包必须可独立构建")

    def test_local_imports_of_copied_modules_are_covered_by_copy_list(self):
        """COPY 进镜像的模块所 import 的本仓模块，必须也在 COPY 清单里。

        这是「闭包」的定义本身：镜像里任何一条 ``from server.x import y``
        都要求 ``server/x.py`` 被显式复制，而不是碰巧存在于构建机工作树上。
        """
        copied = {src for src, _dst in self.copies}
        for src, _dst in self.copies:
            if not src.endswith(".py"):
                continue
            for dependency in referenced_local_files((REPO / src).read_text(encoding="utf-8")):
                self.assertIn(
                    dependency, copied,
                    f"{src} import {dependency}，但它不在 Dockerfile COPY 清单里")

    def test_required_runtime_entries_are_all_copied(self):
        """compose 三服务 + archive_controller 子进程链的入口必须在 COPY 清单里。

        逐文件闭包容易在「精简」时删掉仍被调用的入口（例如 asset_version 经
        ``REPO/tools/*.py`` 子进程调用后的三个同级入口）；这条测试把最小集合钉死。
        """
        copied = {src for src, _dst in self.copies}
        missing = [src for src in REQUIRED_COPIES if src not in copied]
        self.assertEqual(missing, [], f"镜像闭包缺少运行时入口: {missing}")

    def test_compose_build_context_is_this_repo_root(self):
        """compose 的构建上下文必须是本仓根。

        compose 文件位于 ``asset-server/``，因此 ``context`` 相对它解析：
        ``..`` = 本仓根（闭包源码的唯一来源）；``.`` 会错误地把 asset-server/
        自身当上下文（COPY 源全部缺失）。
        """
        text = COMPOSE.read_text(encoding="utf-8")
        contexts = re.findall(r"^\s*context:\s*(\S+)\s*$", text, re.M)
        self.assertTrue(contexts, "compose 必须显式声明 build.context")
        self.assertEqual(set(contexts), {".."},
                         f"构建上下文必须是本仓根（..，相对 asset-server/ 解析），实际 {contexts}")
        self.assertIn("dockerfile: asset-server/Dockerfile", text)
        # 正例自检：以本仓根为上下文时全部 COPY 源存在。
        self.assertEqual(missing_copy_sources(self.dockerfile_text, REPO), [])

    def test_requirements_pin_the_closure_third_party_deps(self):
        """闭包用到的第三方依赖必须在 requirements.txt 里被固定。"""
        requirements = (REPO / "asset-server" / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("msgpack", requirements)
        self.assertIn("requests", requirements)

    def materialize_context(self) -> Path:
        """按同一份 Dockerfile COPY 清单物化临时 app。"""
        tmp = Path(tempfile.mkdtemp(prefix="asset-server-context-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        app = tmp / "app"

        install_steps: list[tuple[str, str]] = []
        for src, dst in self.copies:
            # dst 形如 /app/<sub>/<name>；Windows 上转成相对 app 根的路径。
            parts = Path(dst.lstrip("/")).parts
            self.assertEqual(parts[0], "app", f"unexpected WORKDIR copy target: {dst}")
            target = app.joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / src, target)
            install_steps.append((src, dst))
        self.assertTrue(install_steps, "Dockerfile 必须至少复制服务入口")
        return app

    def cli_environment(self) -> dict[str, str]:
        """清除宿主 Python 路径注入，并禁止生成字节码。"""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONSTARTUP")
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def run_cli_help(self, root: Path, entry: str) -> subprocess.CompletedProcess:
        """只执行指定根下的入口，不回落产品仓库。"""
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(root / entry), "--help"],
            cwd=root, env=self.cli_environment(), capture_output=True,
            text=True, encoding="utf-8", timeout=120)

    def assert_cli_help(self, root: Path, entry: str) -> None:
        completed = self.run_cli_help(root, entry)
        self.assertEqual(
            completed.returncode, 0,
            f"{root / entry} --help 退出码非 0: {completed.stderr.strip()}")
        self.assertIn("usage", (completed.stdout + completed.stderr).lower(),
                      f"{root / entry} --help 未输出 usage")

    def test_host_only_route_cli_help(self):
        """route 仅作宿主 CLI 安全检查，不计入镜像闭包。"""
        for entry in HOST_ONLY_CLI_HELP_ENTRIES:
            self.assert_cli_help(REPO, entry)

    def test_materialized_cli_missing_entry_does_not_fall_back_to_repo(self):
        """删除镜像入口后，同一执行检查必须非零且拒绝宿主回落。"""
        app = self.materialize_context()
        entry = CLI_HELP_ENTRIES[0]
        self.assertTrue((REPO / entry).is_file(), "宿主同名入口须存在")
        self.assertTrue((app / entry).is_file(), "删除前镜像入口须存在")
        (app / entry).unlink()
        completed = self.run_cli_help(app, entry)
        self.assertNotEqual(completed.returncode, 0, "镜像缺入口却回落宿主并成功")
        self.assertNotIn("usage", (completed.stdout + completed.stderr).lower())

    def test_materialized_context_imports_and_cli_help(self):
        """把精确 COPY 出的上下文复制到临时目录（不挂本仓/兄弟仓）后：
        每个闭包模块可导入，五个镜像 CLI 从 app 执行 ``--help`` 退出码 0。"""
        app = self.materialize_context()
        env = self.cli_environment()

        for module in CLOSURE_MODULES:
            completed = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                cwd=app, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(
                completed.returncode, 0,
                f"镜像上下文导入失败 {module}: {completed.stderr.strip()}")

        for entry in CLI_HELP_ENTRIES:
            self.assert_cli_help(app, entry)

        # 两条兼容导入分支（顶层名 / 包名）都必须解析到**同一份 producer 字节**：
        # 顶层名分支是镜像内 `python scripts/assets_mirror.py` 的真实形态；
        # 包名分支是 asset-server/assets_route.py 等以包方式引用的形态。
        probe = (
            "import hashlib, importlib, inspect, sys\n"
            "m = importlib.import_module('assets_mirror')\n"
            "p = importlib.import_module('assets_generated_index')\n"
            "assert m.check_release_commits.__module__ == 'assets_generated_index'\n"
            "print(hashlib.sha256(inspect.getsource(p.check_release_commits).encode()).hexdigest())\n"
        )
        top_level = subprocess.run(
            [sys.executable, "-c", probe], cwd=app / "scripts", env=env,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(top_level.returncode, 0, top_level.stderr.strip())

        probe_pkg = (
            "import hashlib, importlib, inspect, sys\n"
            "m = importlib.import_module('scripts.assets_mirror')\n"
            "p = importlib.import_module('scripts.assets_generated_index')\n"
            "assert m.check_release_commits.__module__ == 'scripts.assets_generated_index'\n"
            "print(hashlib.sha256(inspect.getsource(p.check_release_commits).encode()).hexdigest())\n"
        )
        package = subprocess.run(
            [sys.executable, "-c", probe_pkg], cwd=app, env=env,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(package.returncode, 0, package.stderr.strip())
        self.assertEqual(top_level.stdout.strip(), package.stdout.strip(),
                         "顶层名与包名两条分支的 producer 实现字节不一致")
        # 上下文里的 producer 必须逐字节等于本仓脚本（复制而非改写）。
        self.assertEqual(
            hashlib.sha256((app / "scripts" / "assets_generated_index.py").read_bytes()).hexdigest(),
            hashlib.sha256((REPO / "scripts" / "assets_generated_index.py").read_bytes()).hexdigest(),
            "上下文中的 producer 与本仓脚本字节不一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
