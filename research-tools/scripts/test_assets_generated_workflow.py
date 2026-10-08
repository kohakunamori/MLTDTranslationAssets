#!/usr/bin/env python3
"""Offline regression suite for the Assets generated-release workflow template.

The workflow ``.github/workflows/assets-generated.yml.example`` is the Assets CI
contract (``docs/ASSETS_GENERATED_CI.md``).  Nothing in this repository has ever
run on a GitHub runner -- the template says so in its own header -- so these
tests are **offline static + local-execution** checks, never a CI run:

* step 10 drives the ONE unified entry point
  (``scripts/materialize_generated_release.py``) and does **not** assemble the
  entry list itself (that inline ``assets_generated_index.py build`` path was
  the "no unified entry point" gap);
* the identity it passes comes from the HEAD read in step 4, and the two
  version axes stay independent (composite identities are refused by the entry
  point itself);
* step 10 re-reads the run's machine-readable report and requires
  ``build_status == "success"`` **and** ``generated_written == true``; the
  report-gate JavaScript embedded in the step is also *executed* here (via
  ``node``) against synthetic reports, because "the YAML mentions the key" is
  weaker evidence than "the block that would run refuses a non-publication";
* step 11 经与 step 10 相同的已核 pin 写者复验发布，不清理 store，且两个方向都是
  *实际执行*（对合成 store）而非模式匹配（该行由本任务改写）；
* step 7's aspect-ratio tolerance is the same number as the portal's
  ``DEFAULT_RATIO_TOLERANCE`` (``web/translation-portal/src/image_ratio.js``)
  and both sides refuse a downscaled upload; the portal side of that claim is
  executed through the real ES module with ``node``;
* step 9 keeps its fail-closed presence check for the Unity3D injector, and
  step 12 stays the only ``git add``/commit, on ``generated`` only, with
  ``[skip ci]`` in the message;
* steps 10 and 11 declare the writer pair as an explicit input and reject a
  missing/half pair before anything is written; the pin is a raw-byte SHA-256,
  never derived by hashing a checkout at run time.
  （本任务新增：两 step 都要求报告的 ``writer_source`` 为 pinned/verified 且 sha 等
  于 pin、报告的 ``asset_version`` 等于本 run 从 HEAD 读出的 AV，并要求候选事务
  未清理孤儿——本 workflow 不做任何自动 GC。）

External-blocked (stated honestly): no runner, the official baseline egress is
unverified, the image-side injector is not in the commit contract, and no step
fetches the accepted release ledger the entry point requires.  A green run of
this file proves the *wiring*, not a publishable release.

Run: python scripts/test_assets_generated_workflow.py
Also collected by: python -m pytest scripts/test_assets_generated_workflow.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import materialize_generated_release as entry_mod  # noqa: E402

WORKFLOW = REPO / ".github" / "workflows" / "assets-generated.yml.example"
PORTAL_RATIO_JS = REPO / "web" / "translation-portal" / "src" / "image_ratio.js"

ENTRY_POINT = "scripts/materialize_generated_release.py"
REPORT_PATH = "work/generated-build-report.json"
# The injector's HOME path in this repository.  The Assets repository ships the
# same two files under `pipelines/image/`, which is why the entry point takes
# `--image-entry`: that path is an explicit override there, never the default
# here (this repository has no `pipelines/` directory at all).
INJECTOR = "tools/mltd_image_localization/inject_reviewed_textures.py"
AUDIT = "tools/mltd_image_localization/verify_bundle_repack.py"
# The Assets-repository layout, which this repository must never claim to have.
ASSETS_LAYOUT_INJECTOR = "pipelines/image/inject_reviewed_textures.py"
REPO_LOCAL_INJECTOR = REPO / INJECTOR

# The one heredoc marker the template uses; the tests execute the bodies.
HEREDOC_RE = re.compile(r"^python (?P<args>[^\n<]*)<<'(?P<marker>[A-Z]+)'\n(?P<body>.*?)\n(?P=marker)$",
                        re.S | re.M)


def _png_header(width: int, height: int) -> bytes:
    """A 24-byte PNG whose IHDR declares width x height (no pixel data).

    ``parsePngSize`` reads exactly signature(8) + length(4) + "IHDR"(4) +
    width(4) + height(4), so this is a real header and a zero-cost fixture.
    """
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height))


def _module_url(path: Path) -> str:
    """A file:// URL Node can import (Windows drive letters included)."""
    return urllib.parse.urljoin("file:", path.as_uri())


def report_fixture(**overrides) -> dict:
    """合成一份入口报告，除非覆盖否则为成功（不做 GC）的那一种。

    测试用 workflow 自己的 gate/guard 块跑这份报告，因此断言针对的是 workflow
    的逻辑，而不是正则匹配。成功报告的 ``writer_source`` 是一个
    ``kind=pinned`` / ``verified=true`` 的字典，``transaction.pruned_orphans``
    为 ``False``——本 workflow 不做任何自动 GC。
    """
    report = {
        "kind": entry_mod.REPORT_KIND,
        "schema_version": entry_mod.REPORT_SCHEMA_VERSION,
        "mode": "materialize",
        "build_status": "success",
        "generated_written": True,
        "asset_version": "1077100",
        "surfaces": [{"surface": "text-event-unit", "build_status": "success",
                      "reason": None}],
        "store": {"accepted": 853, "verify_ok": True, "verify_failures": [],
                  "transaction": {"staged": True, "pruned_orphans": False, "promoted": True}},
        "writer_source": {"kind": "pinned", "verified": True, "sha256": None,
                          "pin": None, "module": None, "path": None, "unknown": False},
        "failure_reason": None,
    }
    report.update(overrides)
    return report


def _writer_module_source() -> str:
    """A minimal store module that satisfies the entry point's writer API.

    It carries the SAME module-level names and ``GeneratedStore`` methods the
    real writer exports (``WRITER_MODULE_CALLABLES`` / ``WRITER_STORE_METHODS``),
    so the real central ``resolve_writer_source`` accepts it, and its
    ``GeneratedStore.verify_release`` answers a deterministic ``ok``.  The
    tests use it to exercise the pinned-writer resolution behaviour without
    running the product writer or a real build.
    """
    return (
        "import hashlib\n"
        "class GeneratedStoreError(RuntimeError):\n"
        "    pass\n"
        "class _VerifyReport:\n"
        "    def __init__(self, ok, failures, checked_objects):\n"
        "        self.ok = ok\n"
        "        self.failures = failures\n"
        "        self.checked_objects = checked_objects\n"
        "class ReuseLedger:\n"
        "    pass\n"
        "def sha256_file(path):\n"
        "    h = hashlib.sha256()\n"
        "    with open(path, 'rb') as handle:\n"
        "        for block in iter(lambda: handle.read(1 << 20), b''):\n"
        "            h.update(block)\n"
        "    return h.hexdigest()\n"
        "def validate_asset_version(value):\n"
        "    return str(value)\n"
        "def validate_ci_run_id(value):\n"
        "    return value\n"
        "def validate_commit(value, field_name):\n"
        "    return str(value)\n"
        "def validate_source_client_version(value):\n"
        "    return str(value)\n"
        "ADMISSIBLE_REUSE_STATUSES = ('exact', 'verified-compatible')\n"
        "ADMISSIBLE_TRANSLATION_STATUSES = ('accepted', 'modified', 'reused')\n"
        "RESOURCE_KIND_BUNDLE = 'bundle'\n"
        "RESOURCE_KIND_OTHER = 'other'\n"
        "RESOURCE_KIND_TEXTURE = 'texture'\n"
        "class GeneratedStore:\n"
        "    def __init__(self, root, *args, **kwargs):\n"
        "        self.root = root\n"
        "    def manifest_path(self, asset_version):\n"
        "        return self.root / str(asset_version) / 'manifest.json'\n"
        "    def checksums_path(self, asset_version):\n"
        "        return self.root / str(asset_version) / 'checksums.txt'\n"
        "    def load_manifest(self, asset_version):\n"
        "        import json\n"
        "        return json.loads(self.manifest_path(asset_version).read_text())\n"
        "    def build_release(self, *args, **kwargs):\n"
        "        return None\n"
        "    def put_object(self, *args, **kwargs):\n"
        "        return None\n"
        "    def verify_release(self, asset_version):\n"
        "        import json\n"
        "        path = self.manifest_path(asset_version)\n"
        "        if not path.is_file():\n"
        "            return _VerifyReport(False, ['missing manifest: ' + str(path)], 0)\n"
        "        data = json.loads(path.read_text())\n"
        "        if data.get('ok') is True:\n"
        "            return _VerifyReport(True, [], 1)\n"
        "        return _VerifyReport(False, ['synthetic store refused'], 0)\n"
        "    def transaction(self, *args, prune=False, **kwargs):\n"
        "        raise NotImplementedError\n"
    )


def good_report(real_sha: str, **overrides) -> dict:
    """合成一份通过 gate 的报告：写者来源为已核 pin 的产品副本。

    ``real_sha`` 是磁盘上写者文件的真实字节摘要，因此 ``writer_source`` 是一个
    ``kind=pinned`` / ``verified=true`` / ``sha256==pin`` 的字典——正是 step 10/11
    要求的完整形状。调用方若自己传入 ``writer_source`` 覆盖，则以覆盖为准（用于
    构造缺字段 / 非 pinned / false / 错 sha 的负控）。
    """
    report = report_fixture(**overrides)
    if "writer_source" not in overrides:
        report["writer_source"] = {"kind": "pinned", "verified": True, "sha256": real_sha,
                                   "pin": real_sha, "module": None, "path": None,
                                   "unknown": False}
    return report


def _seed_store(root: Path, asset_version: str, *, ok: bool = True) -> None:
    """A synthetic ``generated/<version>/manifest.json`` the fake writer verifies."""
    release = root / asset_version
    release.mkdir(parents=True, exist_ok=True)
    (release / "manifest.json").write_text(
        json.dumps({"asset_version": asset_version, "ok": ok}), encoding="utf-8")
    (release / "checksums.txt").write_text("synthetic\n", encoding="utf-8")


class WorkflowTemplateCase(unittest.TestCase):
    """Parse the template once and expose steps by their leading number."""

    @classmethod
    def setUpClass(cls):
        if not WORKFLOW.is_file():
            raise unittest.SkipTest(f"workflow template not present: {WORKFLOW}")
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.doc = yaml.safe_load(cls.text)
        # PyYAML resolves the bare key `on:` as the YAML 1.1 boolean True.
        if True in cls.doc and "on" not in cls.doc:
            cls.doc["on"] = cls.doc.pop(True)
        jobs = cls.doc["jobs"]
        cls.job = jobs["generate"]
        cls.steps = cls.job["steps"]

    def step(self, number: int):
        """The single step whose name starts with ``"<number>."``."""
        prefix = f"{number}."
        matches = [step for step in self.steps
                   if str(step.get("name", "")).startswith(prefix)]
        self.assertEqual(len(matches), 1,
                         f"expected exactly one step named {prefix!r}, got {len(matches)}")
        return matches[0]

    def run_text(self, number: int) -> str:
        return str(self.step(number).get("run", ""))

    @staticmethod
    def executable_lines(run_text: str) -> str:
        """The run block without its comment lines.

        Comments may *name* a forbidden construct to explain why it is gone;
        only lines the shell would execute are evidence.
        """
        return "\n".join(row for row in run_text.splitlines()
                         if row.strip() and not row.lstrip().startswith("#"))

    @staticmethod
    def heredocs(run_text: str) -> list[tuple[str, str]]:
        """``[(command_line, body)]`` for every ``python ... <<'MARKER'`` block."""
        return [(match.group("args").strip(), match.group("body"))
                for match in HEREDOC_RE.finditer(run_text)]

    def run_script(self, body: str, argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
        """Execute an embedded ``python - ...`` body exactly as written.

        ``PYTHONPATH`` points at the repository so the block's own
        ``from scripts...`` imports resolve the same way they would on a runner
        where the checkout is the working directory (the scratch cwd is not the
        repo, so the import has to be told where the package is).
        """
        script = cwd / "embedded.py"
        script.write_text(body + "\n", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run([sys.executable, str(script), *argv], cwd=str(cwd),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, env=env)

    @staticmethod
    def writer_root_with_pin(tmp: Path) -> tuple[Path, str, str]:
        """A fake product writer checkout plus its raw-byte SHA-256 pin.

        ``(root, pin, real_sha)``: ``pin`` is what a caller would pass to the
        workflow; ``real_sha`` is the bytes actually on disk.  They differ only
        when a test wants a mismatching pin.
        """
        root = tmp / "writer"
        (root / "scripts").mkdir(parents=True)
        target = root / "scripts" / "assets_generated_index.py"
        payload = _writer_module_source()
        target.write_text(payload, encoding="utf-8")
        real_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        return root, real_sha, real_sha


class TestStepTenUsesTheSingleEntryPoint(WorkflowTemplateCase):
    """The unified entry point is the only thing that assembles the release."""

    def test_exactly_one_step_invokes_the_entry_point(self):
        matches = [step for step in self.steps if ENTRY_POINT in str(step.get("run", ""))]
        self.assertEqual(len(matches), 1,
                         f"exactly one step may invoke {ENTRY_POINT}")
        self.assertTrue(str(matches[0]["name"]).startswith("10."),
                        "the entry point belongs to step 10")

    def test_step_ten_never_builds_the_entry_list_itself(self):
        """The inline inventory->entries->`assets_generated_index.py build` path is gone.

        That path was the second code path the unified entry point replaced; if
        it comes back, the "one entry point" claim is false again.
        """
        executable = self.executable_lines(self.run_text(10))
        self.assertNotIn("generated-entries.json", executable)
        self.assertNotIn("entries.append", executable)
        self.assertNotIn("inventory.json", executable)
        self.assertNotIn("generated-bundles", executable)
        # The store CLI is never invoked: the old inline
        # `assets_generated_index.py build` path was the second code path the
        # unified entry point replaced.  The name may still appear as the VALUE
        # of --assets-writer-pin (it names the pinned file), but no subcommand
        # of the store CLI may run here.
        self.assertNotIn("assets_generated_index.py build", executable)
        self.assertNotIn("assets_generated_index.py verify", executable)
        self.assertNotIn("assets_generated_index.py prune", executable)
        # The only executable block *after* the entry point is the report gate
        # (asserted in TestStepTenReportGate); a heredoc before the invocation
        # would be a place to stage entries again.
        run = self.run_text(10)
        self.assertLess(run.index(ENTRY_POINT), run.index("<<'PY'"),
                        "the entry-point invocation must precede every heredoc")

    def test_step_ten_passes_every_identity_and_path_flag(self):
        run = self.run_text(10)
        for flag in ("--asset-version", "--source-client-version", "--source-commit",
                     "--translation-commit", "--generated-commit", "--ci-run-id",
                     "--input-root", "--output-root", "--repository-root", "--report"):
            self.assertIn(flag, run, f"step 10 must pass {flag}")
        for value in ('"${AV}"', '"${CLIENT}"', '"${COMMIT}"'):
            self.assertIn(value, run)
        # 写者来源是两个显式 flag（逐字传入），且候选事务的自动 GC 已显式关闭。
        self.assertIn('--assets-writer-root "${ASSETS_WRITER_ROOT}"', run)
        self.assertIn('--assets-writer-pin "${ASSETS_WRITER_PIN}"', run)
        self.assertIn("--no-prune-orphans", self.executable_lines(run))

    def test_step_ten_declares_the_writer_pair_from_the_caller(self):
        """The two writer inputs are repository variables, never derived.

        ``ASSETS_WRITER_ROOT`` is a checkout the caller supplies and
        ``ASSETS_WRITER_PIN`` is that writer's raw-byte SHA-256.  Neither may be
        produced by hashing something at run time, defaulted to a sibling path,
        or read back from the branch.
        """
        env = self.step(10).get("env") or {}
        self.assertEqual(env.get("ASSETS_WRITER_ROOT"), "${{ vars.ASSETS_WRITER_ROOT }}")
        self.assertEqual(env.get("ASSETS_WRITER_PIN"), "${{ vars.ASSETS_WRITER_PIN }}")
        # The pair is re-read in step 11 from the same source (same bytes).
        env11 = self.step(11).get("env") or {}
        self.assertEqual(env11.get("ASSETS_WRITER_ROOT"), "${{ vars.ASSETS_WRITER_ROOT }}")
        self.assertEqual(env11.get("ASSETS_WRITER_PIN"), "${{ vars.ASSETS_WRITER_PIN }}")
        executable = self.executable_lines(self.run_text(10))
        # No run-time hashing of the writer, no automatic discovery, no fetch.
        self.assertNotIn("sha256sum", executable)
        self.assertNotIn("shasum", executable)
        self.assertNotIn("git clone", executable)
        self.assertNotIn("git checkout", executable)
        self.assertNotIn("--assets-writer-pin $(python", executable)

    def test_step_ten_rejects_a_missing_or_half_writer_pair(self):
        """Both writer inputs are required, and a half pair is refused.

        The guard is the aborting ``: "${VAR:?}"`` form (not a bare expansion),
        exactly like the run-identity guard -- so a run with only one of the two
        set stops before the entry point is invoked.
        """
        executable = self.executable_lines(self.run_text(10))
        for name in ("ASSETS_WRITER_ROOT", "ASSETS_WRITER_PIN"):
            self.assertIn(f': "${{{name}:?', executable,
                          f"step 10 must abort when {name} is unset")
        # The flags are only reached after the pair is proven.
        run = self.run_text(10)
        self.assertLess(run.index(': "${ASSETS_WRITER_ROOT:?'),
                        run.index("--assets-writer-root"))

    def test_the_writer_pair_guard_actually_aborts(self):
        """实跑两行 guard：缺失、半对、完整对。

        裸的 ``$ASSETS_WRITER_PIN`` 在 ``set +e`` 下会展开为空串并以半对到达入口；
        ``${VAR:?}`` 会中止。只有真正执行这两行才能区分。本用例是本任务新增的
        真实负控，因此 bash 缺失时明确 fail，而不是 skip——否则环境一坏，这条
        最重要的写入前拒绝就静默消失了。
        """
        bash = shutil.which("bash")
        self.assertIsNotNone(
            bash, "本用例是本任务新增的真实负控，需要 bash；缺失必须 fail 而不是 skip")
        executable = self.executable_lines(self.run_text(10))
        guards = "\n".join(row for row in executable.splitlines()
                           if ': "${ASSETS_WRITER_' in row)
        self.assertEqual(len(guards.splitlines()), 2, guards)
        base = {key: value for key, value in os.environ.items()
                if key not in ("ASSETS_WRITER_ROOT", "ASSETS_WRITER_PIN")}
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "writer-guard.sh"
            path.write_text("set -e\nset +e\n" + guards + "\necho AFTER-GUARD\n",
                            encoding="utf-8")

            def run_with(extra):
                return subprocess.run([bash, str(path)], capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", check=False,
                                      env=dict(base, **extra))
            missing = run_with({})
            half_root = run_with({"ASSETS_WRITER_ROOT": "/somewhere"})
            half_pin = run_with({"ASSETS_WRITER_PIN": "a" * 64})
            full = run_with({"ASSETS_WRITER_ROOT": "/somewhere", "ASSETS_WRITER_PIN": "a" * 64})
        for label, result in (("missing", missing), ("half-root", half_root),
                              ("half-pin", half_pin)):
            self.assertNotEqual(result.returncode, 0, label)
            self.assertNotIn("AFTER-GUARD", result.stdout, label)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertIn("AFTER-GUARD", full.stdout)

    def test_step_ten_passes_the_writer_root_verbatim(self):
        """实跑 step 10 的前半段 shell，截取入口收到的 argv。

        用一个假的 ``python`` 记录它收到的参数，验证带空格与 ``café`` 的写者根、
        固定 pin、以及 ``--no-prune-orphans`` 都被逐字、作为独立 token 传入——
        引号没丢、没有二次分词。
        """
        bash = shutil.which("bash")
        self.assertIsNotNone(bash, "本用例需要 bash 才能实跑 step 10 的 shell 前半段")
        executable = self.executable_lines(self.run_text(10)).splitlines()
        start = next(i for i, row in enumerate(executable)
                     if row.strip().startswith("python scripts/materialize_generated_release.py"))
        end = next(i for i, row in enumerate(executable)
                   if "--report work/generated-build-report.json" in row)
        self.assertLess(start, end)
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            bindir = cwd / "bin"
            bindir.mkdir()
            argv_out = cwd / "argv.txt"
            fake = bindir / "python"
            fake.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$ARGV_OUT\"\nexit 0\n",
                            encoding="utf-8")
            fake.chmod(0o755)
            sh = cwd / "step10.sh"
            sh.write_text("set -e\n" + "\n".join(executable[:end + 1]) + "\n", encoding="utf-8")
            writer_root = "/tmp/wr iter/café"
            env = dict(os.environ)
            env.update({
                "PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
                "ARGV_OUT": str(argv_out),
                "GITHUB_RUN_ID": "1234567890",
                "GITHUB_RUN_ATTEMPT": "2",
                "MLTD_IMAGE_INSTALL_MANIFEST": "/some/manifest.jsonl",
                "MLTD_IMAGE_ORIGINAL_ROOT": "",
                "ASSETS_WRITER_ROOT": writer_root,
                "ASSETS_WRITER_PIN": "a" * 64,
                "AV": "1077100", "CLIENT": "9.0.200", "COMMIT": "c" * 40,
            })
            result = subprocess.run([bash, str(sh)], cwd=str(cwd), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    check=False, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            argv = argv_out.read_text(encoding="utf-8").splitlines()

        def after(flag: str) -> str:
            self.assertIn(flag, argv, f"{flag} was not passed to the entry point")
            return argv[argv.index(flag) + 1]

        self.assertEqual(after("--assets-writer-root"), writer_root)
        self.assertEqual(after("--assets-writer-pin"), "a" * 64)
        self.assertEqual(after("--asset-version"), "1077100")
        self.assertEqual(after("--report"), "work/generated-build-report.json")
        self.assertIn("--no-prune-orphans", argv)
        # 带空格的写者根仍是**一个** token（引号在 shell 中保住了）。
        self.assertIn(writer_root, argv)

    def test_the_run_identity_comes_from_the_runner_not_a_input(self):
        """``ci_run_id`` is the runner's own value, and it is passed explicitly.

        The entry point can read ``$GITHUB_RUN_ID`` itself, but a build list that
        does not name the run it belongs to cannot be audited against the run
        that produced it -- so step 10 passes it, from the runner's variables,
        never from a dispatch input a caller could type.
        """
        run = self.run_text(10)
        passed = self.step(10).get("env") or {}
        self.assertNotIn("CI_RUN_ID", passed,
                         "the run id must be interpolated by the runner, not read back "
                         "into a job-level env var a later step could mask")
        self.assertIn('--ci-run-id "${GITHUB_RUN_ID}.${GITHUB_RUN_ATTEMPT}"', run)
        self.assertNotIn("inputs.", run, "a run identity is never a dispatch input")

    def guard_lines(self) -> list[str]:
        """The step-10 lines that refuse an unattributed run, as written.

        Read from the *executable* lines: the failure mode below is that the
        guard is written but the parameter expansion in it is not the aborting
        one, and a commented-out example would hide that.
        """
        lines = [row for row in self.executable_lines(self.run_text(10)).splitlines()
                 if "set +e" not in row and ': "${GITHUB_RUN' in row]
        self.assertEqual(len(lines), 2,
                         "step 10 must refuse a run without GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT")
        return lines

    def test_the_run_identity_guard_actually_aborts(self):
        """The guard is executed, because its value depends on the expansion used.

        Under ``set +e`` a bare ``$GITHUB_RUN_ID`` expands to the empty string
        and the step continues to an unattributed (but successful-looking)
        release; ``${GITHUB_RUN_ID:?word}`` aborts the shell and the step fails.
        Only running the line distinguishes the two, so both environments are
        exercised here.
        """
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not installed; the guard cannot be executed offline")
        guard = "\n".join(self.guard_lines())
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "guard.sh"
            # `set +e` is the state the guard runs in: two lines earlier the
            # template turns error-exit off to capture the entry point's code,
            # but `: "${VAR:?}"` still aborts.
            path.write_text("set -e\nset +e\n" + guard + "\necho AFTER-GUARD\n",
                            encoding="utf-8")
            unattributed = {key: value for key, value in os.environ.items()
                            if key not in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")}
            missing = subprocess.run([bash, str(path)], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", check=False,
                                     env=unattributed)
            self.assertNotEqual(missing.returncode, 0,
                                "an unattributed run must not reach the entry point")
            self.assertNotIn("AFTER-GUARD", missing.stdout,
                             "the guard must stop the step, not print past it")
            attributed = dict(unattributed,
                              GITHUB_RUN_ID="1234567890", GITHUB_RUN_ATTEMPT="2")
            ok = subprocess.run([bash, str(path)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False,
                                env=attributed)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("AFTER-GUARD", ok.stdout)

    def test_the_inline_guards_execute_in_both_directions(self):
        """Step 5's baseline guard and step 10's identity guard, actually run.

        The guard script sits in a build/ run directory (evidence, not a commit
        contract), so it is a sibling of the code that runs it: when that script
        is absent the case skips and says so, and when the template's copied
        guards drift the script's own copy check fails (run-guards prints FAIL).
        """
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not installed; the guards cannot be executed offline")
        script = (REPO / "build" / "runs" / "text-localization" / "9.0.200"
                  / "manifest-field-unification-20260929" / "runner-wiring-copycheck"
                  / "guard-check.sh")
        if not script.is_file():
            self.skipTest(f"the guard-check script is not present: {script}")
        for mode in ("identity", "baseline"):
            result = subprocess.run([bash, str(script), str(WORKFLOW), mode],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", check=False)
            self.assertEqual(result.returncode, 0,
                             f"{mode}:\n{result.stdout}\n{result.stderr}")
            self.assertIn("ok    the copied guards still match the template", result.stdout)
            self.assertIn("exit=1", result.stdout, f"{mode} must show a refusal")
            self.assertIn("exit=0", result.stdout, f"{mode} must show the passing case")

    def test_the_job_runs_where_the_guard_can_read_the_runner_variables(self):
        """`${GITHUB_RUN_ID:?}` must abort an unattributed run.

        The guard is only honest because GitHub *does* provide these variables:
        the identity has one source (the runner), and "it was empty" can only
        mean "this is not a GitHub runner", which is exactly what the guard
        says. A self-hosted runner that declares neither an Actions runner nor
        a host environment would still get them from the runner process -- but a
        job moved off `ubuntu-latest` (say, to a runner supplying identities
        some other way) would meet the guard before the entry point and must
        then be reconsidered here, deliberately, not silently.
        """
        self.assertEqual(self.job.get("runs-on"), "ubuntu-latest")
        for line in self.guard_lines():
            self.assertIn("must run on a GitHub runner", line,
                          "the guard's failure message must say where the value comes from")

    def test_the_identity_values_come_from_the_head_read_in_step_four(self):
        """No dispatch input may exist: a typed version can drift from the branch.

        The three env vars step 10 forwards are step 4's outputs, and step 4
        derives them from ``manifests/asset-version.json`` at HEAD.
        """
        env = self.step(10).get("env") or {}
        self.assertEqual(env.get("AV"), "${{ steps.identity.outputs.asset_version }}")
        self.assertEqual(env.get("CLIENT"), "${{ steps.identity.outputs.client_version }}")
        self.assertEqual(env.get("COMMIT"), "${{ steps.identity.outputs.commit }}")
        step4 = self.run_text(4)
        self.assertIn("manifests/asset-version.json", step4)
        self.assertIn("git", step4)
        self.assertNotIn("inputs.", self.executable_lines(step4))
        # The template must stay dispatchable with no version input to type.
        self.assertNotIn("workflow_dispatch:\n    inputs:", self.text)

    def test_step_ten_writes_the_store_where_step_twelve_commits(self):
        run = self.run_text(10)
        self.assertIn("--output-root .", run)
        self.assertIn("--repository-root .", run)
        self.assertIn("--input-root .", run)
        self.assertIn(f"--report {REPORT_PATH}", run)

    def test_the_embedded_blocks_do_not_interpolate_untrusted_values(self):
        """``${{ }}`` inside a heredoc would let branch content reach the shell."""
        for number in (10, 11):
            run = self.run_text(number)
            for _args, body in self.heredocs(run):
                self.assertNotIn("${{", body,
                                 f"step {number}: a heredoc body must not interpolate GitHub "
                                 "context values")

    def test_the_entry_point_defaults_to_the_path_that_actually_exists(self):
        """A default that resolves to nothing can only ever report not_implemented.

        The injector and its re-audit are committed under
        ``tools/mltd_image_localization/``; ``pipelines/`` does not exist in this
        repository at all, so it can only ever be an explicit ``--image-entry``
        override (it is the Assets repository's layout).
        """
        self.assertFalse((REPO / "pipelines").exists())
        self.assertTrue(REPO_LOCAL_INJECTOR.is_file(),
                        "the injector is expected at tools/mltd_image_localization/ in this repo")
        self.assertTrue((REPO / AUDIT).is_file(), "the independent re-audit is committed too")
        self.assertEqual(entry_mod.IMAGE_SCRIPT_DEFAULT, INJECTOR)
        self.assertEqual(entry_mod.IMAGE_AUDIT_SCRIPT_DEFAULT, AUDIT)
        self.assertEqual(REPO / entry_mod.IMAGE_SCRIPT_DEFAULT, REPO_LOCAL_INJECTOR)
        # The Assets-repository path is documented as an override, not a default.
        self.assertNotIn(ASSETS_LAYOUT_INJECTOR, entry_mod.IMAGE_SCRIPT_DEFAULT)

    def test_step_nine_reads_the_assets_layout_where_this_template_runs(self):
        """This template lives in the Assets repo; the toolchain lives under pipelines/ there."""
        self.assertIn(ASSETS_LAYOUT_INJECTOR, self.run_text(9))
        self.assertIn("tools/mltd_image_localization", self.run_text(9),
                      "the step must say where the SAME programs live in this repository, so a "
                      "reader of either checkout can find them")

    def test_step_nine_proves_the_inputs_and_does_not_produce_bundles(self):
        """Step 9 must not be a second producer; step 10 owns production."""
        run = self.run_text(9)
        self.assertIn("--preflight-context", run)
        self.assertIn("mkdir -p work/image-preflight", run)
        executable = self.executable_lines(run)
        self.assertNotIn("--all", executable,
                         "step 9 must not run the injector's production mode")
        self.assertNotIn(f'python "${{AUDIT}}"', executable,
                         "the re-audit happens inside step 10, on step 10's report")
        self.assertNotIn("--report work/generated-bundles", executable)
        self.assertIn("--report", executable)
        # The missing-input path is a hard failure that names the remedy.
        self.assertIn("exit 1", run)
        self.assertIn("Supply it through the repository variables step 9 reads",
                      " ".join(run.split()))
        self.assertIn("MLTD_IMAGE_INSTALL_MANIFEST", run)
        self.assertIn("MLTD_IMAGE_ORIGINAL_ROOT", run)
        self.assertIn("Nothing was written to generated/", run)
        # The step reads the manifest from the repository variable, exactly like
        # step 10 does: one source for the cohort, not two.
        expected = "${{ vars.MLTD_IMAGE_INSTALL_MANIFEST }}"
        for number in (9, 10):
            env = self.step(number).get("env") or {}
            self.assertEqual(env.get("MLTD_IMAGE_INSTALL_MANIFEST"), expected,
                             f"step {number} must read the reviewed cohort from the same "
                             "repository variable step 9 checks")
        self.assertEqual((self.step(10).get("env") or {}).get("MLTD_IMAGE_ORIGINAL_ROOT"),
                         "${{ vars.MLTD_IMAGE_ORIGINAL_ROOT }}")

    def test_the_context_block_reads_the_step_nine_document_not_a_guess(self):
        """The block that decides "inputs are present" runs on the real document."""
        run = self.run_text(9)
        blocks = [block for block in self.heredocs(run) if "sys.argv[1]" in block[1]]
        self.assertEqual(len(blocks), 1, "step 9 must read its context document back")
        args, body = blocks[0]
        self.assertIn('"${CONTEXT}"', args)
        self.assertIn("manifest_sha256", body)
        self.assertNotIn("${{", body)


class TestStepTenReportGate(WorkflowTemplateCase):
    """`generated_written` is proven from the report, and this gate really runs."""

    def report_gate(self) -> tuple[str, str]:
        blocks = [block for block in self.heredocs(self.run_text(10))
                  if '"$code"' in block[0]]
        self.assertEqual(len(blocks), 1,
                         "step 10 must have exactly one report-reading block")
        return blocks[0]

    def test_the_gate_reads_the_report_the_entry_point_wrote(self):
        _args, body = self.report_gate()
        self.assertIn(REPORT_PATH, self.run_text(10))
        self.assertIn(f'Path("{REPORT_PATH}")', body)
        self.assertIn('report.get("build_status") != "success"', body)
        self.assertIn('report.get("generated_written") is not True', body)
        self.assertIn('store.get("verify_ok") is not True', body)
        self.assertIn(entry_mod.REPORT_KIND, body)

    def test_the_gate_re_resolves_the_writer_through_the_central_resolver(self):
        """The report gate proves the pinned writer, not just the report keys.

        It must use the real central ``resolve_writer_source`` (the same one the
        entry point uses) and compare the resolved source's SHA-256 to the pin,
        so a report produced through a different writer cannot pass.
        """
        _args, body = self.report_gate()
        self.assertIn("resolve_writer_source", body)
        self.assertIn("from scripts.materialize_generated_release import", body)
        self.assertIn('source.kind != "pinned"', body)
        self.assertIn("source.module_sha256 != writer_pin", body)
        self.assertIn('declared.get("kind") != "pinned"', body)
        self.assertIn('declared.get("verified") is not True', body)
        self.assertIn('declared.get("sha256") != writer_pin', body)
        # 调用方的 AV 要与报告里的 asset_version 对比。
        self.assertIn('str(report.get("asset_version")) != str(caller_av)', body)
        # 必须证明候选事务未做 GC。
        self.assertIn('transaction.get("pruned_orphans") is not False', body)
        # No second loader or hash implementation is written into the step.
        self.assertNotIn("exec(", body)
        self.assertNotIn("hashlib", body)

    def _gate_exit(self, *, build_report=None, write: bool = True, code: int = 0,
                   pin_override: str | None = None, root_override: str | None = None,
                   caller_av: str = "1077100") -> subprocess.CompletedProcess:
        """在临时 cwd 里跑 step 10 的 report gate。

        写者对是真实（合成）的 checkout 加其 pin，因此块内的中央解析会真正执行；
        ``build_report(real_sha)`` 让用例基于磁盘上写者的真实摘要构造报告（这样
        writer_source 的 sha 与 pin 一致，负控只针对被考察的那个字段）；
        ``pin_override`` 让用例传入与磁盘字节不符的 pin；``caller_av`` 模拟 step 4
        从 HEAD 读出的 AV。默认报告是 ``good_report(real_sha)``（完整写者形状、不做
        GC）。
        """
        _args, body = self.report_gate()
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            target = cwd / REPORT_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            root, real_sha, _ = self.writer_root_with_pin(cwd)
            if write:
                report = (build_report or good_report)(real_sha)
                if report is not None:
                    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
            pin = pin_override if pin_override is not None else real_sha
            root_arg = root_override if root_override is not None else str(root)
            return self.run_script(body, [str(code), root_arg, pin, caller_av], cwd)

    def test_a_written_verified_release_passes_the_gate(self):
        result = self._gate_exit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("generated_written=True", result.stdout)
        self.assertIn("release materialised", result.stdout)
        self.assertIn("writer source: pinned", result.stdout)

    def test_the_gate_refuses_a_wrong_caller_version(self):
        """报告里的 asset_version 必须与 step 4 的 AV 严格一致。

        负控：报告的 AV=1077101 冒充本 run 的 1077100，即使其它字段全对也必须
        在 store/verify 之前拒绝。
        """
        result = self._gate_exit(build_report=lambda sha: good_report(sha, asset_version="1077101"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("but this run is", result.stdout + result.stderr)

    def test_the_gate_refuses_a_missing_caller_version(self):
        result = self._gate_exit(caller_av="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no caller asset_version", result.stdout + result.stderr)

    def test_the_gate_refuses_a_wrong_writer_pin(self):
        """A report alone is not enough: the writer must match the caller's pin."""
        result = self._gate_exit(pin_override="f" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned writer source was refused", result.stdout + result.stderr)

    def test_the_gate_refuses_a_half_writer_pair(self):
        """One of the pair present and the other empty is refused explicitly.

        The block is executed directly with an empty root and a real pin, so the
        pair check runs (rather than a missing root silently falling back).
        """
        result = self._gate_exit(root_override="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("are a pair and must both", result.stdout + result.stderr)

    def test_the_gate_refuses_a_missing_writer_source_record(self):
        """报告的 writer_source 缺失即拒绝（不从 legacy fixture 缺字段推兼容）。"""
        def build(sha):
            report = good_report(sha)
            report.pop("writer_source")
            return report
        result = self._gate_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no writer_source record", result.stdout + result.stderr)

    def test_the_gate_refuses_a_non_pinned_or_unverified_writer_source(self):
        """kind 非 pinned、或 verified 非 True，一律 fail closed。"""
        for kind, verified in (("main", False), ("pinned", False), ("pinned", "yes"),
                               ("main", True), ("main", "yes")):
            with self.subTest(kind=kind, verified=verified):
                def build(sha, kind=kind, verified=verified):
                    return good_report(sha, writer_source={
                        "kind": kind, "verified": verified, "sha256": sha})
                result = self._gate_exit(build_report=build)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("did not publish through a pinned, verified",
                              result.stdout + result.stderr)

    def test_the_gate_refuses_a_report_naming_a_different_writer_sha(self):
        """pin 与磁盘字节一致，但报告声明了另一个 sha -> 报告字段被拒。"""
        def build(sha):
            return good_report(sha, writer_source={
                "kind": "pinned", "verified": True, "sha256": "b" * 64})
        result = self._gate_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("but this step pinned", result.stdout + result.stderr)

    def test_the_gate_refuses_a_report_that_pruned_orphans(self):
        """报告声明候选事务清理过孤儿即拒绝：本 workflow 不做自动 GC。"""
        def build(sha):
            return good_report(sha, store={
                "accepted": 1, "verify_ok": True, "verify_failures": [],
                "transaction": {"staged": True, "pruned_orphans": True, "promoted": True}})
        result = self._gate_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pruned orphans", result.stdout + result.stderr)

    def test_the_gate_refuses_a_report_without_a_transaction(self):
        def build(sha):
            return good_report(sha, store={"accepted": 1, "verify_ok": True,
                                           "verify_failures": []})
        result = self._gate_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no staged-store transaction", result.stdout + result.stderr)

    def test_the_gate_refuses_a_report_without_a_staged_promotion(self):
        for transaction in ({"staged": True, "pruned_orphans": False, "promoted": False},
                            {"staged": False, "pruned_orphans": False, "promoted": True}):
            with self.subTest(transaction=transaction):
                def build(sha, transaction=transaction):
                    return good_report(sha, store={
                        "accepted": 1, "verify_ok": True, "verify_failures": [],
                        "transaction": transaction})
                result = self._gate_exit(build_report=build)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("staged-and-promoted", result.stdout + result.stderr)

    def test_a_run_that_exited_zero_without_writing_is_refused(self):
        """`generated_written` is what admits the release, not the exit code."""
        def build(sha):
            return good_report(sha, generated_written=False,
                               store={"accepted": 0, "verify_ok": True, "verify_failures": []},
                               failure_reason="no surface produced an entry")
        result = self._gate_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generated_written=False", result.stdout + result.stderr)

    def test_a_written_but_unverified_store_is_refused(self):
        def build(sha):
            return good_report(sha, store={
                "accepted": 1, "verify_ok": False,
                "verify_failures": ["objects/sha256/aa/dead hashes to ..."]})
        result = self._gate_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)

    def test_a_failed_build_report_is_refused(self):
        def build(sha):
            return good_report(sha, build_status="failed", generated_written=False, store=None,
                               failure_reason="one or more surfaces did not produce a "
                                              "publishable bundle")
        result = self._gate_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)

    def test_a_foreign_report_kind_is_refused(self):
        result = self._gate_exit(build_report=lambda sha: good_report(sha, kind="something-else"))
        self.assertNotEqual(result.returncode, 0)

    def test_a_missing_report_is_refused(self):
        result = self._gate_exit(write=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no machine-readable report", result.stdout + result.stderr)


class TestStepElevenReverifiesThroughThePinnedWriter(WorkflowTemplateCase):
    """Step 11 re-verifies the release through the SAME pinned writer, and never prunes.

    Two claims, each executed against the workflow's own block rather than
    pattern-matched:

    * 复验用真正的中央 ``resolve_writer_source`` 与解析出的模块自身的
      ``GeneratedStore.verify_release``——重新哈希 store 的字节就是 step 10 发布
      所用字节，错 pin 在任何 store 读之前被拒；
    * 本 step 不执行 prune：破坏性的 ``prune --apply`` 与 store CLI 都已移除，且
      step 10 传了 ``--no-prune-orphans``，因此活的 ``generated/`` 与候选事务都不
      做 GC。
    """

    def guard_block(self) -> str:
        blocks = [block for block in self.heredocs(self.run_text(11))
                  if REPORT_PATH in block[1]]
        self.assertEqual(len(blocks), 1,
                         "step 11 must read back the step-10 report before re-verifying")
        return blocks[0][1]

    def test_the_guard_is_the_first_executable_block_and_the_only_store_command(self):
        # Comment lines are excluded first: prose about pruning is not a prune.
        executable = self.executable_lines(self.run_text(11))
        marker = "<<'PY'"
        guard_start = executable.index(marker)
        guard_end = executable.index("\nPY", guard_start) + 3
        self.assertIn(REPORT_PATH, executable[guard_start:guard_end],
                      "the first executable block in step 11 must be the report guard")
        # No store CLI subcommand runs at all: the verify goes through the
        # resolved module's API inside the guard, not `assets_generated_index.py`.
        self.assertNotIn("assets_generated_index.py verify", executable)
        self.assertNotIn("assets_generated_index.py prune", executable)

    def test_step_eleven_never_prunes(self):
        """本 workflow 不对 store 做任何 GC。

        step 11 的可执行行里不得出现 prune 子命令、``--apply`` 或
        ``prune_orphans(`` 调用，也不得调用 store CLI。``pruned_orphans`` 只是报告
        里被检查的字段名（用于确认清理已关闭），不是命令。
        """
        executable = self.executable_lines(self.run_text(11))
        self.assertNotIn("assets_generated_index.py", executable)
        self.assertNotIn("--apply", executable)
        self.assertNotIn(".prune_orphans(", executable)
        self.assertNotIn("prune_orphans(", executable)
        self.assertNotIn(" prune ", executable)

    def test_the_guard_uses_the_central_resolver_and_the_writer_verify_api(self):
        body = self.guard_block()
        self.assertIn("resolve_writer_source", body)
        self.assertIn("from scripts.materialize_generated_release import", body)
        self.assertIn('source.kind != "pinned"', body)
        self.assertIn("source.module_sha256 != writer_pin", body)
        self.assertIn("GeneratedStore", body)
        self.assertIn("verify_release", body)
        self.assertIn("verify.ok is not True", body)
        # 报告的 writer_source 与调用方的 AV 也在这里一并绑定。
        self.assertIn('declared.get("sha256") != writer_pin', body)
        self.assertIn('str(report.get("asset_version")) != str(caller_av)', body)
        self.assertIn('transaction.get("pruned_orphans") is not False', body)
        # It does not write a second loader or re-implement hashing.
        self.assertNotIn("exec(", body)
        self.assertNotIn("hashlib", body)

    def test_the_guard_declares_the_same_writer_pair_step_ten_used(self):
        """Same source of truth: both steps read the same two variables."""
        for number in (10, 11):
            env = self.step(number).get("env") or {}
            self.assertEqual(env.get("ASSETS_WRITER_ROOT"), "${{ vars.ASSETS_WRITER_ROOT }}")
            self.assertEqual(env.get("ASSETS_WRITER_PIN"), "${{ vars.ASSETS_WRITER_PIN }}")

    def _guard_exit(self, *, build_report=None, write: bool = True,
                    store_ok: bool = True, pin_override: str | None = None,
                    root_override: str | None = None,
                    caller_av: str = "1077100") -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            root, real_sha, _ = self.writer_root_with_pin(cwd)
            if write:
                report = (build_report or good_report)(real_sha)
                if report is not None:
                    target = cwd / REPORT_PATH
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
            _seed_store(cwd / "generated", "1077100", ok=store_ok)
            pin = real_sha if pin_override is None else pin_override
            root_arg = root_override if root_override is not None else str(root)
            return self.run_script(self.guard_block(), [root_arg, pin, caller_av], cwd)

    def test_a_proven_release_reverifies_through_the_pinned_writer(self):
        result = self._guard_exit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("re-verified", result.stdout)

    def test_a_wrong_pin_is_refused_before_any_store_read(self):
        result = self._guard_exit(pin_override="f" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned writer source was refused", result.stdout + result.stderr)
        self.assertNotIn("re-verified", result.stdout)

    def test_a_missing_writer_pair_is_refused(self):
        result = self._guard_exit(root_override="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("are a pair and must both", result.stdout + result.stderr)

    def test_a_wrong_caller_version_blocks_the_reverify(self):
        """step 11 也必须把报告的 asset_version 与调用方 AV 严格对比。"""
        result = self._guard_exit(
            build_report=lambda sha: good_report(sha, asset_version="1077101"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("but this run is", result.stdout + result.stderr)
        self.assertNotIn("re-verified", result.stdout)

    def test_a_missing_writer_source_record_blocks_the_reverify(self):
        def build(sha):
            report = good_report(sha)
            report.pop("writer_source")
            return report
        result = self._guard_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no writer_source record", result.stdout + result.stderr)

    def test_a_non_pinned_or_unverified_writer_source_blocks_the_reverify(self):
        for kind, verified in (("main", True), ("pinned", False), ("pinned", "yes"),
                               ("main", "yes")):
            with self.subTest(kind=kind, verified=verified):
                def build(sha, kind=kind, verified=verified):
                    return good_report(sha, writer_source={
                        "kind": kind, "verified": verified, "sha256": sha})
                result = self._guard_exit(build_report=build)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("did not publish through the pinned writer",
                              result.stdout + result.stderr)

    def test_a_report_that_pruned_orphans_blocks_the_reverify(self):
        def build(sha):
            return good_report(sha, store={
                "accepted": 1, "verify_ok": True, "verify_failures": [],
                "transaction": {"staged": True, "pruned_orphans": True, "promoted": True}})
        result = self._guard_exit(build_report=build)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pruned orphans", result.stdout + result.stderr)

    def test_no_report_blocks_the_reverify(self):
        result = self._guard_exit(write=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to re-verify", result.stdout + result.stderr)

    def test_generated_written_false_blocks_the_reverify(self):
        result = self._guard_exit(
            build_report=lambda sha: good_report(sha, generated_written=False, store=None))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to re-verify", result.stdout + result.stderr)

    def test_an_unverified_store_blocks_the_reverify(self):
        result = self._guard_exit(
            build_report=lambda sha: good_report(
                sha, store={"verify_ok": False, "verify_failures": ["x"]}))
        self.assertNotEqual(result.returncode, 0)

    def test_a_report_without_the_staged_promotion_blocks_the_reverify(self):
        """store 只能作为本次 run 自己提升的后果被复验。

        报告的 transaction 记录区分"本次 run 提升了发布"与"磁盘上恰有一份发布"——
        只有前者之后才允许复验。每个用例都带已复验的 store 且 ``pruned_orphans``
        为 False，因此唯一能拒绝它的只有 staged/promoted 记录；否则会被更早的检查
        （verify_ok）掩盖。
        """
        for transaction in (None, {"staged": True, "pruned_orphans": False, "promoted": False},
                            {"staged": False, "pruned_orphans": False, "promoted": True},
                            {"staged": True, "pruned_orphans": False, "promoted": "yes"}):
            with self.subTest(transaction=transaction):
                def build(sha, transaction=transaction):
                    store = {"accepted": 1, "verify_ok": True, "verify_failures": []}
                    if transaction is not None:
                        store["transaction"] = transaction
                    return good_report(sha, store=store)
                result = self._guard_exit(build_report=build)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("staged-store", result.stdout + result.stderr)

    def test_a_staged_promotion_without_gc_passes_the_reverify(self):
        result = self._guard_exit(build_report=lambda sha: good_report(sha, store={
            "accepted": 1, "verify_ok": True, "verify_failures": [],
            "transaction": {"staged": True, "pruned_orphans": False, "promoted": True}}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_store_that_fails_the_writer_verify_is_refused(self):
        """The pinned writer's own verify_release decides, not the report.

        The step-10 report claims success, but the store on disk fails the
        writer's re-hash -- so the step fails closed.
        """
        result = self._guard_exit(store_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not re-verify", result.stdout + result.stderr)


class TestStepSevenToleranceMatchesThePortal(WorkflowTemplateCase):
    """CI is the release gate and the portal is the UX gate; same number."""

    def workflow_tolerance(self) -> float:
        run = self.run_text(7)
        match = re.search(r"RATIO_TOLERANCE\s*=\s*([0-9]*\.?[0-9]+)", run)
        self.assertIsNotNone(match, "step 7 must define the tolerance it applies")
        # The literal must actually be used by the comparison below it.
        self.assertIn('width * declared["height"] != declared["width"] * height', run)
        return float(match.group(1))

    def portal_tolerance(self) -> float:
        source = PORTAL_RATIO_JS.read_text(encoding="utf-8")
        match = re.search(r"export const DEFAULT_RATIO_TOLERANCE\s*=\s*([0-9]*\.?[0-9]+)", source)
        self.assertIsNotNone(match, "the portal must export DEFAULT_RATIO_TOLERANCE")
        return float(match.group(1))

    def test_the_two_tolerances_are_the_same_number(self):
        self.assertEqual(self.workflow_tolerance(), self.portal_tolerance())

    def test_actual_ci_geometry_rejects_stretch_and_accepts_high_resolution(self):
        from PIL import Image
        run = self.run_text(7)
        script = re.search(r"python - <<'PY'\n(.*?)\nPY", run, re.S).group(1)
        for width, height, expected in ((2000, 1000, 0), (1003, 500, 1), (999, 500, 1)):
            with self.subTest(size=(width, height)), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "manifests").mkdir()
                Image.new("RGBA", (width, height)).save(root / "upload.png")
                (root / "manifests/images.manifest.json").write_text(json.dumps({"images": [{
                    "id": "unit-fixture", "dimensions": {"width": 1000, "height": 500},
                    "original": {"relative_path": "upload.png"}}]}), encoding="utf-8")
                result = subprocess.run([sys.executable, "-c", script], cwd=root,
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_both_sides_refuse_a_downscaled_upload(self):
        run = self.run_text(7)
        self.assertRegex(run, r"width\s*<\s*declared\[.width.\]")
        portal = PORTAL_RATIO_JS.read_text(encoding="utf-8")
        self.assertIn("resolution_below_original", portal)

    def test_the_portal_module_agrees_about_both_rules(self):
        """Execute the portal gate: even 0.3% stretching fails; exact upscale passes.

        The portal's numbers are far from the 0.005 boundary on purpose, so the
        check pins the rule rather than the float formatting.
        """
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; the portal side cannot be executed offline")
        cases = {
            "inside": {"actual": {"width": 1003, "height": 500},
                       "original": {"width": 1000, "height": 500}},
            "outside": {"actual": {"width": 1016, "height": 500},
                        "original": {"width": 1000, "height": 500}},
            "downscaled": {"actual": {"width": 64, "height": 64},
                           "original": {"width": 128, "height": 128}},
        }
        script = f"""
import {{ checkAspectRatio, DEFAULT_RATIO_TOLERANCE, parseImageSize }} from {json.dumps(_module_url(PORTAL_RATIO_JS))};
const fixture = Buffer.from({json.dumps(_png_header(128, 64).hex())}, "hex");
const cases = {json.dumps(cases)};
const out = {{tolerance: DEFAULT_RATIO_TOLERANCE, parsed: parseImageSize(fixture)}};
for (const [name, body] of Object.entries(cases)) {{
  out[name] = checkAspectRatio(body);
}}
console.log(JSON.stringify(out));
"""
        # The scratch file is named `.mjs`, so Node parses it as ESM without any
        # help from a package.json `type` field.
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "check.mjs"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, str(path)], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tolerance"], self.workflow_tolerance())
        # The fixture is a real header, not a hand-waved one.
        self.assertEqual(payload["parsed"], {"width": 128, "height": 64, "format": "png"})
        self.assertFalse(payload["inside"]["ok"], payload["inside"])
        self.assertFalse(payload["outside"]["ok"])
        self.assertEqual(payload["outside"]["reason"], "aspect_ratio_mismatch")
        self.assertFalse(payload["downscaled"]["ok"])
        self.assertEqual(payload["downscaled"]["reason"], "resolution_below_original")


class TestStepTwelvePublishesOnlyGenerated(WorkflowTemplateCase):
    """The only git write, on `generated` only, and it must not retrigger itself."""

    def test_default_branch_checkout_and_head_drift_are_explicit(self):
        self.assertEqual(self.steps[0]["with"]["ref"], "${{ github.event.repository.default_branch }}")
        run = self.run_text(12)
        self.assertIn('git ls-remote --exit-code origin "refs/heads/$DEFAULT_BRANCH"', run)
        self.assertIn('"$remote_head" != "$EXPECTED_SOURCE_COMMIT"', run)
        self.assertIn('git push origin "HEAD:refs/heads/$DEFAULT_BRANCH"', run)
        self.assertNotIn("--force", run)

    def test_git_add_targets_generated_only(self):
        run = self.executable_lines(self.run_text(12))
        adds = re.findall(r"git add[^\n]*", run)
        self.assertEqual(len(adds), 1, f"expected one `git add`, got {adds}")
        self.assertRegex(adds[0], r"^git add\s+generated\s*$")
        self.assertNotIn("git add -A", run)
        self.assertNotIn("git add .", run)

    def test_the_commit_message_carries_skip_ci_and_the_asset_version(self):
        run = self.run_text(12)
        commits = re.findall(r'git commit -m "([^"]*)"', run)
        self.assertEqual(len(commits), 1, f"expected one commit, got {commits}")
        message = commits[0]
        self.assertIn("[skip ci]", message)
        self.assertIn("${{ steps.identity.outputs.asset_version }}", message)
        self.assertIn("generated", message)

    def test_no_other_step_runs_git_add_or_commit(self):
        for number in range(1, 13):
            if number == 12:
                continue
            run = self.executable_lines(self.run_text(number))
            self.assertNotIn("git add", run, f"step {number} must not stage files")
            self.assertNotIn("git commit", run, f"step {number} must not commit")
            self.assertNotIn("git push", run, f"step {number} must not push")

    def test_the_job_guard_still_refuses_bot_commits(self):
        guard = str(self.job.get("if", ""))
        for marker in ("[skip ci]", "[ci skip]", "[skip actions]"):
            self.assertIn(marker, guard)


class TestTemplateShape(WorkflowTemplateCase):
    """Cheap invariants that keep the file honest about itself."""

    def test_workflow_parses_as_yaml(self):
        self.assertEqual(self.doc["name"], "Assets Generated Release")
        self.assertIn("on", self.doc)
        self.assertEqual(list(self.doc["jobs"]), ["generate"])
        self.assertEqual(len(self.steps), 12, "the template is a 12-step contract")

    def test_the_run_report_never_lands_inside_generated(self):
        """A report written under `generated/` would be committed by step 12.

        The report lives in `work/`, which no step stages; step 12 stages
        `generated` only, so the machine-readable report can never become a
        published artifact.
        """
        self.assertFalse(REPORT_PATH.startswith("generated/"))
        for number in (10, 11):
            self.assertIn(REPORT_PATH, self.run_text(number))
        self.assertNotIn("work/", self.executable_lines(self.run_text(12)))

    def test_the_template_still_declares_the_external_blockers(self):
        """The four honest gaps stay documented in the header.

        The assertions reflow the comments first, so rewrapping a paragraph
        cannot fail them -- only deleting a fact does.
        """
        head = self.text.split("name: Assets Generated Release")[0]
        prose = " ".join(row.lstrip("# ").strip() for row in head.splitlines())
        self.assertIn("External-blocked", prose)
        self.assertIn("Nothing here has been run on a real GitHub runner", prose)
        self.assertIn("inject_reviewed_textures.py", prose)
        self.assertIn("ledgers/*.jsonl", prose, "the missing ledger input stays documented")


if __name__ == "__main__":
    unittest.main(verbosity=2)
