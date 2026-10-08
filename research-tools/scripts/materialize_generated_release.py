#!/usr/bin/env python3
"""One fail-closed entry point that materialises every generated bundle for one asset_version.

Why this exists
---------------
The workflow template used to fail closed at a step of its own because no single
entry point emitted *every* generated bundle at once: the text-side
materializers each cover a single surface, the image-side injector lived outside
the commit contract, and nothing assembled a content-addressed release from
their output.  This module is that entry point, and it now drives all three
surfaces including the image injector and its independent re-audit.

Contract
--------
Inputs are explicit and the two version axes stay independent:

* ``--asset-version`` — assets axis, digits only (``1077100``).
* ``--source-client-version`` — client axis, ``X.Y.Z`` (``9.0.200``).
* ``--source-commit`` / ``--translation-commit`` / ``--generated-commit`` — 40-hex.
* ``--input-root`` — where the translated text inputs live.
* ``--output-root`` — where ``generated/`` goes (the store root is ``<output-root>/generated``).
* ``--repository-root`` — the root the surface scripts are resolved under.
* ``--preflight-only`` — validate inputs and the release gate, produce no bundle.
* ``--image-install-manifest`` / ``--image-original-root`` — the image surface's
  reviewed inputs.  Required when the image surface runs; absent, the run is
  refused with the missing metadata named rather than injected from a guess.

A composite identity such as ``9.0.200+1077100`` is refused outright (same rule
and phrasing as ``scripts/export_localization_for_github.py::validate_asset_axis``
and ``scripts/assets_generated_index.py``).

Surfaces
--------
===============  ============================================================
text-event-unit  ``scripts/materialize_event_unit_90200.py`` (852 UnityFS bundles)
text-mld         ``scripts/materialize_mld_90200.py`` (1 encrypted MD.mld bundle)
image            ``tools/mltd_image_localization/inject_reviewed_textures.py``
                 (this repository's home for the source-bound Texture2D
                 injector) plus its independent re-audit
                 ``tools/mltd_image_localization/verify_bundle_repack.py``.
                 The Assets repository ships the same two files under
                 ``pipelines/image/``; that layout is passed explicitly with
                 ``--image-entry`` and is never a default, because no
                 ``pipelines/`` directory exists in this repository.
===============  ============================================================

A surface whose entry point is missing is reported ``not_implemented`` and the
whole run FAILS CLOSED: no bundle is staged into ``generated/``.  A surface is
``success`` only when its child exited 0 *and* the child's own release gates
report release-ready, roundtrip-verified output; anything else is ``failed``.

Failure semantics
-----------------
``generated/`` (objects + ``<asset_version>/manifest.json`` + ``checksums.txt``)
is written only after **every** requested surface succeeded.  A failed run
therefore leaves an existing successful build byte-identical.  The store API is
reused unchanged: this module never deletes or rewrites objects itself.

This script is offline: it never contacts the asset server and never publishes.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 公开 wire 标签只描述载荷；身份、准入、复用与存储规则均取本次已核 pin 的写者。
RESOURCE_KIND_BUNDLE = "bundle"
RESOURCE_KIND_OTHER = "other"
RESOURCE_KIND_TEXTURE = "texture"

REPORT_KIND = "mltd-generated-release-materialize-report"
REPORT_SCHEMA_VERSION = 1
STORE_DIRNAME = "generated"

# Surfaces
SURFACE_TEXT_EVENT_UNIT = "text-event-unit"
SURFACE_TEXT_MLD = "text-mld"
SURFACE_IMAGE = "image"
DEFAULT_SURFACES = (SURFACE_TEXT_EVENT_UNIT, SURFACE_TEXT_MLD, SURFACE_IMAGE)
SURFACE_CHOICES = DEFAULT_SURFACES

# What each surface's payload is.  The logical path cannot say it: every one of
# these surfaces ships a UnityFS container under
# `production/2018/Android/<remote>.unity3d`, and the store refuses an entry
# whose producer did not classify its own output.
SURFACE_RESOURCE_KIND = {
    SURFACE_TEXT_EVENT_UNIT: RESOURCE_KIND_BUNDLE,
    SURFACE_TEXT_MLD: RESOURCE_KIND_BUNDLE,
    SURFACE_IMAGE: RESOURCE_KIND_TEXTURE,
}

# Surface entry points, relative to --repository-root.
EVENT_UNIT_SCRIPT = "scripts/materialize_event_unit_90200.py"
EVENT_UNIT_MANIFEST = "event-unit-90200-manifest.json"
MLD_SCRIPT = "scripts/materialize_mld_90200.py"
MLD_MANIFEST = "mld-90200-manifest.json"
# The image surface's entry point at its repository-home path.  The Assets
# repository layout (``pipelines/image/...``) is an explicit --image-entry
# override, never the default: this repository has no ``pipelines/`` directory,
# so a default pointing there resolved to nothing and the surface could only
# ever report not_implemented.
IMAGE_SCRIPT_DEFAULT = "tools/mltd_image_localization/inject_reviewed_textures.py"
IMAGE_AUDIT_SCRIPT_DEFAULT = "tools/mltd_image_localization/verify_bundle_repack.py"
IMAGE_INVENTORY = "inventory.json"
IMAGE_REPORT = "inject-report.json"
IMAGE_AUDIT_REPORT = "repack-independent-audit.json"
# The documents' own kinds, as cross-checked below: a file that merely sits at
# the expected path is not evidence that the expected program wrote it.
IMAGE_INJECT_REPORT_KIND = "reviewed-texture-injection-report"
IMAGE_AUDIT_KIND = "independent-reviewed-image-unity-repack-audit"
IMAGE_INPUT_CONTEXT_KIND = "mltd-image-injection-input-context"

# The path the official asset host serves inside one asset_version:
# https://<host>/<asset_version>/production/2018/Android/<remote>.unity3d
LOGICAL_PATH_PREFIX_DEFAULT = "production/2018/Android"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_NOT_IMPLEMENTED = "not_implemented"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_FAILED_CLOSED = 2
EXIT_PREFLIGHT_NOT_READY = 3

# Exit code the text materializers use when --preflight-only finds the release
# gate not ready (release ledger coverage incomplete).
CHILD_EXIT_PREFLIGHT_NOT_READY = 3

TAIL_LIMIT = 4000
LEDGERS_DIRNAME = "ledgers"


class RefusedInput(ValueError):
    """An input, identity or gate check refused the run; nothing was written."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tail(text: str, limit: int = TAIL_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else "...\n" + text[-limit:]


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sha256_file(path: Path) -> str:
    """入口证据文件的字节摘要，不依赖任何写者实现。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_file_sha(path: Path) -> str | None:
    return _sha256_file(path) if path.is_file() else None


# --------------------------------------------------------------------------- #
# pinned writer source（写者来源）
# --------------------------------------------------------------------------- #
# 入口发布始终落在同一 store 家族：``--assets-writer-root``/``--assets-writer-pin``
# 指向单个固定文件（产品 owner 的 ``scripts/assets_generated_index.py``）并给出其
# 原始字节 SHA-256；不开插件 loader，也不扫目录发现模块。所有模式均必须显式
# 给出这对参数，包括 ``--preflight-only``；没有主仓 reader/writer 回退。
ASSETS_WRITER_RELATIVE = "scripts/assets_generated_index.py"
ASSETS_WRITER_SOURCE_PINNED = "pinned"
HEX64_MODULE_RE = re.compile(r"^[0-9a-f]{64}$")


def _load_pinned_writer_module(root: Path, pin: str) -> Any:
    """用已核验字节加载 pinned 写者模块，不产生 import 副作用。

    文件只读一次，校验 SHA-256==``pin``，再编译**同一份已核验字节**。模块在
    唯一命名的命名空间内执行，只设 ``__name__``/``__file__``/``__package__``：
    不解析 ``scripts/__init__.py``、不读 root 的 ``__pycache__``、不扫目录发现
    其它模块。写者自身 import 全部来自标准库，与解释器可信边界一致。
    """
    if not HEX64_MODULE_RE.match(str(pin)):
        raise RefusedInput(
            "--assets-writer-pin must be the 64-hex SHA-256 of "
            f"{ASSETS_WRITER_RELATIVE}; got {pin!r}")
    root = Path(root).resolve()
    if not root.is_dir():
        raise RefusedInput(f"--assets-writer-root is not a directory: {root}")
    target = root / ASSETS_WRITER_RELATIVE
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise RefusedInput(
            f"the pinned writer file resolves outside --assets-writer-root: {resolved}")
    if not resolved.is_file():
        raise RefusedInput(f"the pinned writer file does not exist: {resolved}")
    raw = resolved.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != pin:
        raise RefusedInput(
            f"the pinned writer file does not match --assets-writer-pin: {resolved} "
            f"hashes to {actual}, not {pin}")
    module_name = f"_mltd_pinned_assets_writer_{actual[:16]}"
    module = types.ModuleType(module_name)
    module.__file__ = str(resolved)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        exec(compile(raw, str(resolved), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


@dataclass
class WriterSource:
    """本次显式指定、原始字节 SHA-256 与 pin 一致的写者来源。"""

    kind: str
    module: Any
    module_path: Path | None = None
    module_sha256: str | None = None
    root: Path | None = None
    pin: str | None = None
    unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "module": getattr(self.module, "__name__", None),
            "path": str(self.module_path) if self.module_path else None,
            "sha256": self.module_sha256,  # 已核验源码字节（非 commit）
            "pin": self.pin,
            "verified": self.kind == ASSETS_WRITER_SOURCE_PINNED,
            "unknown": self.unknown,
        }


def resolve_writer_source(module: Any, *, writer_root: Path | None,
                          writer_pin: str | None, require_pair: bool) -> WriterSource:
    """所有模式只解析显式 pinned 写者，缺对在任何 store/producer 操作前拒绝。

    ``module`` / ``require_pair`` 仅保留外部调用形状；即使传旧模块或 False，
    也不能把它们作为未核 pin 的来源。模板的 ``resolve_writer_source(None,
    ..., require_pair=True)`` 继续使用同一 loader。
    """
    if (writer_root is None) != (writer_pin is None):
        missing = "--assets-writer-pin" if writer_pin is None else "--assets-writer-root"
        raise RefusedInput(
            "--assets-writer-root and --assets-writer-pin are a pair and must be given "
            f"together; {missing} is missing while its partner was supplied. A pinned "
            "writer source is explicit or absent, never half-specified")
    if writer_root is not None:
        pinned_module = _load_pinned_writer_module(Path(writer_root), str(writer_pin))
        _require_writer_api(pinned_module, ASSETS_WRITER_SOURCE_PINNED)
        _require_writer_api_store(pinned_module)
        return WriterSource(kind=ASSETS_WRITER_SOURCE_PINNED, module=pinned_module,
                            module_path=Path(pinned_module.__file__),
                            module_sha256=str(writer_pin),
                            root=Path(writer_root).resolve(), pin=str(writer_pin))
    raise RefusedInput(
        "所有模式（包括 --preflight-only）都必须显式给出 "
        "--assets-writer-root 和 --assets-writer-pin；不回退主仓 reader/writer")


# 模块级 API：这些名字必须是**可调用**对象，一个 ``None`` 占位不得放行。
WRITER_MODULE_CALLABLES = (
    "GeneratedStore", "GeneratedStoreError", "ReuseLedger", "sha256_file",
    "validate_asset_version", "validate_ci_run_id", "validate_commit",
    "validate_source_client_version",
)
# 模块级常量：存在即可（不是 callable），但必须存在。
WRITER_MODULE_CONSTANTS = (
    "ADMISSIBLE_REUSE_STATUSES", "ADMISSIBLE_TRANSLATION_STATUSES",
    "RESOURCE_KIND_BUNDLE", "RESOURCE_KIND_OTHER", "RESOURCE_KIND_TEXTURE",
)


def _require_writer_api(module: Any, source_label: str) -> None:
    """写者模块缺本入口所需 API（可调用/常量）时 fail closed。"""
    missing = [name for name in WRITER_MODULE_CALLABLES
               if not callable(getattr(module, name, None))]
    missing += [name for name in WRITER_MODULE_CONSTANTS
                if not hasattr(module, name)]
    if missing:
        raise RefusedInput(
            f"the {source_label} writer module does not implement the store API this "
            f"entry point publishes through: missing/not-callable {missing}")
    error_cls = module.GeneratedStoreError
    if not isinstance(error_cls, type) or not issubclass(error_cls, Exception):
        raise RefusedInput("pinned 写者的 GeneratedStoreError 必须是 Exception 子类")


# 本入口实际在 store 类上调用的方法（不只是模块级函数）。写者可能导出一个
# 缺这些方法的 ``GeneratedStore``；不预先检查的话，失败会拖到子进程跑完之后。
# 每一项都必须是**可调用**的：一个 ``build_release = None`` 不得通过。
WRITER_STORE_METHODS = (
    "manifest_path", "checksums_path", "load_manifest",
    "build_release", "put_object", "verify_release", "transaction",
)


def _class_transaction_supported(cls: Any) -> bool:
    """在**类**层面核 ``transaction(*, prune=...)`` 签名，不必实例化坏 store。

    与 ``_transaction_supported``（实例层面，run 中仍保留）同义，但可在
    ``GeneratedStore(...)`` 构造 / mkdtemp / 起 surface 之前就判定。
    """
    factory = getattr(cls, "transaction", None)
    if not callable(factory):
        return False
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    return "prune" in parameters


def _require_writer_api_store(module: Any) -> None:
    """pinned 写者的 ``GeneratedStore`` 缺方法或坏 transaction 签名时 fail closed。"""
    store_cls = getattr(module, "GeneratedStore", None)
    not_callable = [name for name in WRITER_STORE_METHODS
                    if not callable(getattr(store_cls, name, None))]
    if not_callable:
        raise RefusedInput(
            "the pinned writer's GeneratedStore does not implement the store API this "
            f"entry point publishes through: missing/not-callable {not_callable}")
    if not _class_transaction_supported(store_cls):
        raise RefusedInput(
            "the pinned writer's GeneratedStore has no staged-store transaction "
            "(GeneratedStore.transaction(prune=...)): the release is staged and "
            "independently verified before it is switched in, so a writer without that "
            "class API cannot both publish and keep a failed run non-destructive")


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def validate_axes(asset_version: str, source_client_version: str, store_module: Any) -> tuple[str, str]:
    """身份校验只取本次已核 pin 的写者，不绑定主仓校验器。"""
    try:
        asset = store_module.validate_asset_version(asset_version)
        client = store_module.validate_source_client_version(source_client_version)
    except store_module.GeneratedStoreError as exc:
        raise RefusedInput(
            f"{exc}; composite versions are forbidden: asset_version and "
            "source_client_version are independent axes (assets: digits only; client: X.Y.Z)") from exc
    # 写者负责身份规则；入口的公开 CLI 另要求三段 X.Y.Z，保留原有安全边界。
    # 某些 legacy fixture 的校验器接受两段版本，不能因此放宽本入口的参数形状。
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", client):
        raise RefusedInput("--source-client-version 必须为 X.Y.Z")
    return asset, client


def validate_commits(source_commit: str, translation_commit: str,
                     generated_commit: str, store_module: Any) -> tuple[str, str, str]:
    out = []
    for name, value in (("source_commit", source_commit),
                        ("translation_commit", translation_commit),
                        ("generated_commit", generated_commit)):
        try:
            out.append(store_module.validate_commit(value, name))
        except store_module.GeneratedStoreError as exc:
            raise RefusedInput(str(exc)) from exc
    return out[0], out[1], out[2]


# --------------------------------------------------------------------------- #
# surface results
# --------------------------------------------------------------------------- #
@dataclass
class EntryRow:
    """One materialised bundle plus the decision that admits it to the store."""

    logical_key: str
    logical_path: str
    declared_logical_path: str | None
    declared_remote: str | None
    source_sha256: str
    translated_sha256: str
    artifact_path: Path
    bytes: int
    surface: str
    resource_kind: str = RESOURCE_KIND_OTHER
    reuse_status: str = ""
    translation_status: str = ""
    reuse_reason: str = ""
    declared_reuse_status: str | None = None
    declared_translation_status: str | None = None
    object_path: str | None = None
    deduped: bool | None = None

    def admission_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": "assets",
            "logical_key": self.logical_key,
            "logical_path": self.logical_path,
            "resource_kind": self.resource_kind,
            "source_sha256": self.source_sha256,
            "translated_sha256": self.translated_sha256,
            "reuse_status": self.reuse_status,
            "translation_status": self.translation_status,
            "artifact_file": str(self.artifact_path),
            "artifact_sha256": self.translated_sha256,
        }
        # runtime_path 就是客户端在一个 asset_version 内实际请求的服务路径，与
        # logical_path 同义——这里显式带上这条**已核验**的现有路径，交由产品
        # validator 校验（不新增映射规则，也不从 CAS 摘要/artifact 名推断）。
        # logical_path 为空（无 declared 来源）时该条已不可发布，不带 runtime。
        if self.logical_path:
            payload["runtime_path"] = self.logical_path
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "logical_key": self.logical_key,
            "logical_path": self.logical_path,
            "runtime_path": self.logical_path or None,
            "declared_logical_path": self.declared_logical_path,
            "declared_remote": self.declared_remote,
            "resource_kind": self.resource_kind,
            "source_sha256": self.source_sha256,
            "translated_sha256": self.translated_sha256,
            "artifact_sha256": self.translated_sha256,
            "object_path": self.object_path,
            "reuse_status": self.reuse_status,
            "translation_status": self.translation_status,
            "reuse_reason": self.reuse_reason,
            "artifact_path": str(self.artifact_path),
            "bytes": self.bytes,
            "deduped": self.deduped,
        }


@dataclass
class SurfaceResult:
    surface: str
    build_status: str
    reason: str | None = None
    entry_point_path: str | None = None
    entry_point_sha256: str | None = None
    argv: list[str] = field(default_factory=list)
    exit_code: int | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    entries: list[EntryRow] = field(default_factory=list)
    preflight_checked: str = "executed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "build_status": self.build_status,
            "reason": self.reason,
            "entry_point": {
                "path": self.entry_point_path,
                "sha256": self.entry_point_sha256,
                "exists": self.entry_point_path is not None,
                "argv": self.argv,
                "exit_code": self.exit_code,
                "stdout_tail": self.stdout_tail,
                "stderr_tail": self.stderr_tail,
            },
            "inputs": self.inputs,
            "provenance": self.provenance,
            "outputs": self.outputs,
            "preflight_checked": self.preflight_checked,
            "entry_count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
        }


# --------------------------------------------------------------------------- #
# child invocation
# --------------------------------------------------------------------------- #
def _resolve_under(root: Path, rel: str) -> Path:
    candidate = Path(rel)
    return candidate if candidate.is_absolute() else (root / candidate)


def _run_child(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        list(argv), cwd=str(cwd), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
    )


def _child_json(stdout: str) -> Mapping[str, Any] | None:
    """The child's last JSON object on stdout.

    The text materializers print a preflight document *and* a result document, so
    a whole-stream ``json.loads`` sees "Extra data".  The last complete object is
    the one that carries ``status``/``reason``.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        document = json.loads(text)
        return document if isinstance(document, Mapping) else None
    except json.JSONDecodeError:
        pass
    lines = text.splitlines()
    for start in range(len(lines) - 1, -1, -1):
        try:
            document = json.loads("\n".join(lines[start:]))
        except json.JSONDecodeError:
            continue
        if isinstance(document, Mapping):
            return document
    return None


def _load_json_document(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RefusedInput(f"a child did not write the document it was asked for: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RefusedInput(f"a child's document is not valid JSON: {path}: {exc}") from exc


def _first_existing(paths: Sequence[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


# --------------------------------------------------------------------------- #
# text surfaces
# --------------------------------------------------------------------------- #
def _run_text_surface(
    *,
    surface: str,
    script_rel: str,
    manifest_name: str,
    repository_root: Path,
    ledgers: Sequence[Path],
    staging: Path,
    asset_version: str,
    client_version: str,
    preflight_only: bool,
) -> SurfaceResult:
    result = SurfaceResult(surface=surface, build_status=STATUS_NOT_IMPLEMENTED)
    script = _resolve_under(repository_root, script_rel)
    result.inputs.append({"path": str(script), "role": "surface entry point",
                          "sha256": _json_file_sha(script)})
    if not script.is_file():
        result.reason = (
            f"surface entry point is not part of the commit contract: {script} does not exist; "
            "this surface is not implemented, so the run fails closed instead of skipping it"
        )
        return result

    result.entry_point_path = str(script)
    result.entry_point_sha256 = _sha256_file(script)
    out_root = staging / surface
    argv = [
        sys.executable, str(script),
        "--client-version", client_version,
        "--asset-version", asset_version,
        "--output-root", str(out_root),
    ]
    for ledger in ledgers:
        argv.extend(["--translations", str(ledger)])
    if preflight_only:
        argv.append("--preflight-only")
    result.argv = argv
    for ledger in ledgers:
        result.inputs.append({"path": str(ledger), "role": "accepted text release ledger",
                              "sha256": _json_file_sha(ledger)})

    process = _run_child(argv, cwd=repository_root if repository_root.is_dir() else REPO)
    result.exit_code = process.returncode
    result.stdout_tail = _tail(process.stdout)
    result.stderr_tail = _tail(process.stderr)
    child_report = _child_json(process.stdout)

    if preflight_only:
        if process.returncode == 0:
            result.build_status = STATUS_SUCCESS
            result.preflight_checked = "child --preflight-only exit 0 (release gate ready)"
            result.provenance = {"child_preflight": child_report}
        else:
            result.build_status = STATUS_FAILED
            refusal = (child_report or {}).get("reason") if child_report else None
            result.reason = (
                f"child --preflight-only exited {process.returncode}"
                + (f": {refusal}" if refusal else "")
                + f" (raw stderr: {_tail(process.stderr, 600)})"
            )
            result.provenance = {"child_preflight": child_report}
        return result

    if process.returncode != 0:
        result.build_status = STATUS_FAILED
        refusal = (child_report or {}).get("reason") if child_report else None
        result.reason = (
            f"materializer exited {process.returncode}"
            + (f": {refusal}" if refusal else "")
            + f" (raw stderr: {_tail(process.stderr, 600)})"
        )
        return result

    manifest_path = out_root / manifest_name
    if not manifest_path.is_file():
        result.build_status = STATUS_FAILED
        result.reason = f"materializer exited 0 but wrote no manifest: {manifest_path}"
        return result
    manifest = _load_json_document(manifest_path)
    if not isinstance(manifest, Mapping):
        result.build_status = STATUS_FAILED
        result.reason = f"materializer manifest is not a JSON object: {manifest_path}"
        return result

    bundles = manifest.get("bundles")
    if not isinstance(bundles, list) or not bundles:
        result.build_status = STATUS_FAILED
        result.reason = f"materializer manifest carries no bundles: {manifest_path}"
        return result

    # The child's own release gates, re-checked here: a partial/trial run must
    # never be promoted by this entry point.
    checks = {
        "release_ready": manifest.get("release_ready"),
        "all_roundtrip_verified": manifest.get("all_roundtrip_verified"),
        "isolated_partial_trial": manifest.get("isolated_partial_trial"),
        "asset_version": str(manifest.get("asset_version") or ""),
        "app_version": str(manifest.get("app_version") or ""),
    }
    failures: list[str] = []
    if checks["release_ready"] is not True:
        failures.append(f"release_ready is {checks['release_ready']!r}")
    if checks["all_roundtrip_verified"] is not True:
        failures.append(f"all_roundtrip_verified is {checks['all_roundtrip_verified']!r}")
    if checks["isolated_partial_trial"] is not False:
        failures.append(f"isolated_partial_trial is {checks['isolated_partial_trial']!r}")
    if checks["asset_version"] and checks["asset_version"] != asset_version:
        failures.append(f"manifest asset_version {checks['asset_version']!r} != {asset_version!r}")
    if checks["app_version"] and checks["app_version"] != client_version:
        failures.append(f"manifest app_version {checks['app_version']!r} != {client_version!r}")
    if failures:
        result.build_status = STATUS_FAILED
        result.reason = "materializer output is not release-ready: " + "; ".join(failures)
        return result

    scope = str(manifest.get("scope") or "jp-android")
    ledger_identity = {
        "kind": manifest.get("kind"),
        "scope": scope,
        "asset_version": manifest.get("asset_version"),
        "app_version": manifest.get("app_version"),
        "bundles_written": manifest.get("bundles_written"),
        "unique_source_values": manifest.get("unique_source_values"),
        "release_accepted_source_values": manifest.get("release_accepted_source_values"),
        "release_ready": manifest.get("release_ready"),
        "release_ready_semantics": manifest.get("release_ready_semantics"),
        "all_roundtrip_verified": manifest.get("all_roundtrip_verified"),
        "isolated_partial_trial": manifest.get("isolated_partial_trial"),
        "status": manifest.get("status"),
        "source_index_sha256": manifest.get("source_index_sha256"),
        "source_queue_sha256": manifest.get("source_queue_sha256"),
        "source_cohort_index_sha256": manifest.get("source_cohort_index_sha256"),
        "source_audit_sha256": manifest.get("source_audit_sha256")
        or manifest.get("event_unit_source_audit_sha256"),
        "snapshot_sha256": manifest.get("snapshot_sha256"),
        "plain_snapshot_sha256": manifest.get("plain_snapshot_sha256"),
        "decoded_snapshot_sha256": manifest.get("decoded_snapshot_sha256"),
        "unityfs_packer": manifest.get("unityfs_packer"),
        "translations": manifest.get("translations"),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
    }
    result.provenance = ledger_identity

    for index, bundle in enumerate(bundles):
        if not isinstance(bundle, Mapping):
            result.build_status = STATUS_FAILED
            result.reason = f"manifest bundle #{index} is not an object"
            return result
        logical = str(bundle.get("logical") or "")
        remote = str(bundle.get("remote") or "")
        source_sha = str(bundle.get("source_sha256") or "")
        localized_sha = str(bundle.get("localized_sha256") or "")
        if not logical or not remote or not source_sha or not localized_sha:
            result.build_status = STATUS_FAILED
            result.reason = (f"manifest bundle #{index} lacks logical/remote/"
                             "source_sha256/localized_sha256")
            return result
        artifact = out_root / scope / remote
        if not artifact.is_file():
            result.build_status = STATUS_FAILED
            result.reason = f"materializer reported a bundle it did not write: {artifact}"
            return result
        actual = _sha256_file(artifact)
        if actual != localized_sha:
            result.build_status = STATUS_FAILED
            result.reason = (f"{logical}: staged bundle {artifact} hashes to {actual}, "
                             f"manifest declares {localized_sha}")
            return result
        result.entries.append(EntryRow(
            logical_key=logical,
            logical_path="",  # the caller resolves --logical-path-prefix + remote
            declared_logical_path=logical,
            declared_remote=remote,
            source_sha256=source_sha,
            translated_sha256=actual,
            artifact_path=artifact,
            bytes=artifact.stat().st_size,
            surface=surface,
            resource_kind=SURFACE_RESOURCE_KIND.get(surface, RESOURCE_KIND_OTHER),
            declared_reuse_status=bundle.get("reuse_status"),
            declared_translation_status=bundle.get("translation_status"),
        ))
        result.outputs.append({"path": str(artifact), "sha256": actual,
                               "bytes": artifact.stat().st_size, "logical": logical,
                               "remote": remote})
    result.build_status = STATUS_SUCCESS
    return result


# --------------------------------------------------------------------------- #
# image surface
# --------------------------------------------------------------------------- #
def _image_rows(document: Any, inventory_path: Path) -> list[Mapping[str, Any]]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, Mapping):
        rows = document.get("bundles") or document.get("entries")
        if rows is None:
            raise RefusedInput(
                f"image inventory {inventory_path} must be a list or an object with "
                "'bundles'/'entries'"
            )
    else:
        raise RefusedInput(f"image inventory {inventory_path} is not a JSON document")
    if not isinstance(rows, list) or not rows:
        raise RefusedInput(f"image inventory {inventory_path} carries no bundles")
    if any(not isinstance(row, Mapping) for row in rows):
        raise RefusedInput(f"image inventory {inventory_path} has a non-object row")
    return rows


def _resolve_inventory_artifact(row: Mapping[str, Any], inventory_path: Path,
                                staging: Path) -> Path | None:
    value = row.get("artifact_file") or row.get("bundle_file") or row.get("output_path")
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for base in (inventory_path.parent, staging):
        resolved = base / candidate
        if resolved.is_file():
            return resolved
    return None


def _manifest_sources(context: Mapping[str, Any] | None) -> str:
    sources = (context or {}).get("manifest_sources") or []
    if not sources:
        return "the entry point's own default location"
    return "; ".join(f"{item.get('source')} -> {item.get('path')}" for item in sources)


@dataclass
class ImageInputProbe:
    """The verdict of the entry point's read-only input probe.

    ``answered`` is True only when the child exited 0 **and** printed the
    documented document with a usable reviewed manifest.  A crash, a foreign
    document or a "no manifest" answer is never treated as success: the whole
    point of the probe is to tell missing metadata apart from a failed
    injection, and a probe that could not answer cannot tell either.
    """

    answered: bool = False
    context: Mapping[str, Any] | None = None
    refusal: str | None = None


def _image_input_context(
    script: Path,
    repository_root: Path,
    out_root: Path,
    install_manifest: Path | None,
    original_root: Path | None,
    report_path: Path,
    preflight_only: bool,
) -> ImageInputProbe:
    """Ask the entry point which inputs resolve, without producing a bundle.

    The entry point's ``--preflight-context`` writes nothing, so this is safe in
    both ``--preflight-only`` and a real run; it answers "which manifest source
    and original root were resolved, and is the manifest usable?" so a missing
    reviewed cohort is reported as missing metadata instead of as a failed
    injection.
    """
    argv = [sys.executable, str(script), "--preflight-context", "--out", str(out_root),
            "--report", str(report_path)]
    # ``--install-manifest`` is always passed, even when no manifest was
    # supplied.  An injector that declares it ``required=True`` (the Assets
    # candidate does) otherwise dies in argparse with exit 2 and a usage dump,
    # so the caller would report "nothing resolved" instead of forwarding the
    # injector's own refusal document naming the source it consulted.  An empty
    # value is exactly equivalent to omitting the flag for an injector that
    # falls back to $MLTD_IMAGE_INSTALL_MANIFEST and then a repository default.
    argv.extend(
        ["--install-manifest", "" if install_manifest is None else str(install_manifest)]
    )
    # ``--original-root`` stays conditional: both injectors declare it
    # ``default=None`` and "no root" is a documented valid outcome (a manifest
    # whose ``original_png`` values are all absolute needs none), so an empty
    # value would convey nothing the omission does not.
    if original_root is not None:
        argv.extend(["--original-root", str(original_root)])
    process = _run_child(argv, cwd=repository_root if repository_root.is_dir() else REPO)
    document = _child_json(process.stdout)
    mapping = document if isinstance(document, Mapping) else None
    if process.returncode != 0:
        refused = mapping.get("refused") if mapping is not None else None
        return ImageInputProbe(
            answered=False, context=mapping,
            refusal=(f"the entry point's input probe (--preflight-context) exited "
                     f"{process.returncode}"
                     + (f": {refused}" if refused else
                        f" (raw stderr: {_tail(process.stderr, 300)})")))
    if mapping is None:
        return ImageInputProbe(
            answered=False,
            refusal=("the entry point's input probe exited 0 but printed no JSON document "
                     f"(raw stdout: {_tail(process.stdout, 300)})"))
    if str(mapping.get("kind") or "") != IMAGE_INPUT_CONTEXT_KIND:
        return ImageInputProbe(
            answered=False, context=mapping,
            refusal=(f"the entry point's input probe printed an unexpected document kind "
                     f"{mapping.get('kind')!r}, expected {IMAGE_INPUT_CONTEXT_KIND!r}"))
    counts = mapping.get("manifest_counts")
    if mapping.get("refused") or not mapping.get("manifest") \
            or not mapping.get("manifest_sha256") or not isinstance(counts, Mapping) \
            or not int(counts.get("rows") or 0):
        return ImageInputProbe(
            answered=False, context=mapping,
            refusal=("the entry point's input probe reports no usable reviewed manifest: "
                     + (str(mapping.get("refused")) if mapping.get("refused")
                        else json.dumps(counts, ensure_ascii=False))))
    if not preflight_only and (install_manifest is None
                               or Path(str(mapping["manifest"])).resolve()
                               != install_manifest.resolve()):
        # On a real (non-preflight) run the cohort is never whatever the entry
        # point happened to resolve: it is the file this run was given.
        return ImageInputProbe(
            answered=False, context=mapping,
            refusal=("the entry point's input probe resolved "
                     f"{mapping.get('manifest')!r}, not the reviewed cohort this run was "
                     f"given ({install_manifest}); the run is refused instead of injecting "
                     "some other cohort"))
    return ImageInputProbe(answered=True, context=mapping)


def _int_field(document: Mapping[str, Any], name: str, source: str) -> int:
    value = document.get(name)
    if isinstance(value, bool) or value is None:
        raise RefusedInput(f"{source}: {name} is {value!r}, not a count")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RefusedInput(f"{source}: {name} is {value!r}, not a count") from None


def _image_report_records(report_path: Path) -> list[Mapping[str, Any]]:
    """The injector's own run report, validated as the audit's only input."""
    document = _load_json_document(report_path)
    if not isinstance(document, Mapping):
        raise RefusedInput(f"image run report is not a JSON object: {report_path}")
    if str(document.get("kind") or "") != IMAGE_INJECT_REPORT_KIND:
        raise RefusedInput(
            f"image run report has kind {document.get('kind')!r}, expected "
            f"{IMAGE_INJECT_REPORT_KIND!r}: {report_path}")
    records = document.get("bundles")
    if not isinstance(records, list) or not records:
        raise RefusedInput(f"image run report carries no bundles: {report_path}")
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise RefusedInput(f"image run report bundle #{index} is not an object")
        missing = [field for field in ("remote", "output_bundle", "output_sha256",
                                       "source_sha256") if not record.get(field)]
        if missing:
            raise RefusedInput(
                f"image run report bundle #{index} lacks {', '.join(missing)}: {report_path}")
    remotes = [str(record["remote"]) for record in records]
    if len(set(remotes)) != len(remotes):
        raise RefusedInput(f"image run report repeats a remote: {report_path}")
    return records


def _validate_image_audit(document: Mapping[str, Any], report_path: Path,
                          records: Sequence[Mapping[str, Any]],
                          install_manifest_hint: Path) -> tuple[int, int, int]:
    """The audit must be *this* run's audit, complete, and pass.

    An empty document, a foreign kind or an audit of some other report can never
    authorise the bundles: the re-audit is the only thing standing between the
    injector's own report and a published repack, so its answer is checked
    field by field and bound back to the exact report file it audited.
    """
    if str(document.get("kind") or "") != IMAGE_AUDIT_KIND:
        raise RefusedInput(
            f"the independent re-audit report has kind {document.get('kind')!r}, expected "
            f"{IMAGE_AUDIT_KIND!r}")
    audited_path = document.get("report_audited")
    if not audited_path or Path(str(audited_path)).resolve() != report_path.resolve():
        raise RefusedInput(
            f"the independent re-audit audited {audited_path!r}, not this run's report "
            f"{report_path}")
    expected_sha = str(document.get("report_audited_sha256") or "")
    if expected_sha != _sha256_file(report_path):
        raise RefusedInput("the independent re-audit's report_audited_sha256 does not match "
                           "this run's report bytes")
    audited = _int_field(document, "audited_bundles", "audit report")
    passed = _int_field(document, "passed_bundles", "audit report")
    failed = _int_field(document, "failed_bundles", "audit report")
    textures = _int_field(document, "target_textures", "audit report")
    errors = document.get("errors")
    if not isinstance(errors, list):
        raise RefusedInput("the independent re-audit report has no errors list")
    if failed != 0 or errors:
        raise RefusedInput(
            "the independent re-audit did not pass every bundle: "
            + json.dumps(errors, ensure_ascii=False)[:900])
    if audited != len(records):
        raise RefusedInput(
            f"the independent re-audit covered {audited} bundle(s) but this run's report "
            f"carries {len(records)}; the audit does not cover the whole run, so a partial "
            "(or a stale) audit would be authorising bundles it never read")
    if passed != audited:
        raise RefusedInput(
            f"the independent re-audit reports {passed} passed bundle(s) out of {audited} "
            "audited and no errors; a partial pass is not a pass")
    if textures <= 0:
        raise RefusedInput(
            f"the independent re-audit reports {textures} audited texture(s) across {passed} "
            "bundle(s); a repack with no audited texture is not a backfill")
    manifest_path = document.get("install_manifest")
    if not manifest_path or Path(str(manifest_path)).resolve() != install_manifest_hint.resolve():
        raise RefusedInput(
            f"the independent re-audit audited manifest {manifest_path!r}, not the reviewed "
            f"cohort this run injected: {install_manifest_hint}")
    return passed, failed, textures


def _run_image_surface(
    *,
    script_rel: str,
    audit_rel: str,
    repository_root: Path,
    staging: Path,
    preflight_only: bool,
    install_manifest: Path | None,
    original_root: Path | None,
) -> SurfaceResult:
    surface = SURFACE_IMAGE
    result = SurfaceResult(surface=surface, build_status=STATUS_NOT_IMPLEMENTED)
    script = _resolve_under(repository_root, script_rel)
    audit_script = _resolve_under(repository_root, audit_rel)
    result.inputs.append({"path": str(script), "role": "surface entry point",
                          "sha256": _json_file_sha(script)})
    result.inputs.append({"path": str(audit_script), "role": "independent re-audit",
                          "sha256": _json_file_sha(audit_script)})
    if install_manifest is not None:
        result.inputs.append({"path": str(install_manifest),
                              "role": "reviewed texture-install manifest",
                              "sha256": _json_file_sha(install_manifest)})
    if original_root is not None:
        result.inputs.append({"path": str(original_root), "role": "original PNG root",
                              "sha256": None})
    if not script.is_file():
        result.reason = (
            "the image-side entry point is absent: expected "
            f"{script}. The source-bound Texture2D injector (and its independent re-audit "
            f"{audit_script}) are what this surface must run; without them it is "
            "not_implemented and the run fails closed instead of publishing a release with "
            "un-backfilled images"
        )
        return result
    if not audit_script.is_file():
        # No fallback on purpose: re-hashing the inventory is not an independent
        # audit of the repack, and this surface must not be published on its own
        # word alone.
        result.reason = (
            f"the independent re-audit is absent: expected {audit_script}. The injector "
            "alone is not enough: its inventory carries paths and SHA-256 values, and only "
            "a re-read of the archives can confirm the injected textures, so this surface "
            "is not_implemented and the run fails closed"
        )
        return result

    result.entry_point_path = str(script)
    result.entry_point_sha256 = _sha256_file(script)
    out_root = staging / surface
    report_path = staging / f"{surface}-{IMAGE_REPORT}"
    audit_report = staging / f"{surface}-{IMAGE_AUDIT_REPORT}"
    probe = _image_input_context(script, repository_root, out_root, install_manifest,
                                 original_root, report_path, preflight_only)
    if probe.context is not None:
        result.provenance = {"input_context": probe.context}
    if not probe.answered:
        # No fallback: a probe that could not answer (a crash, a foreign
        # document, a "no manifest" refusal) leaves the surface unimplemented,
        # never preflight-success.  --image-install-manifest is named because
        # that is the input the probe exists to check.
        result.build_status = STATUS_NOT_IMPLEMENTED
        result.reason = (
            "the image surface's reviewed inputs could not be established (pass "
            "--image-install-manifest <reviewed JSONL>, and --image-original-root when its "
            f"original_png values are relative): {probe.refusal}. Sources consulted: "
            + _manifest_sources(probe.context)
        )
        return result
    if install_manifest is None or install_manifest.is_dir() or not install_manifest.is_file():
        result.build_status = STATUS_NOT_IMPLEMENTED
        result.reason = (f"--image-install-manifest is not a file: {install_manifest}; the "
                         "reviewed cohort this surface injects is not available")
        return result

    argv = [sys.executable, str(script), "--all", "--out", str(out_root),
            "--install-manifest", str(install_manifest),
            "--report", str(report_path)]
    if original_root is not None:
        argv.extend(["--original-root", str(original_root)])
    argv.extend(["--expect-manifest-sha256", str(probe.context["manifest_sha256"])])
    result.argv = argv
    if preflight_only:
        # The answered probe above is the whole proof available without running
        # the injector: the reviewed inputs resolve and the entry point confirmed
        # them, so nothing is executed (running it would produce bundles).
        result.build_status = STATUS_SUCCESS
        result.preflight_checked = ("input_context_only: the entry point confirmed the reviewed "
                                    "inputs resolve; the injector was not executed")
        return result

    process = _run_child(argv, cwd=repository_root if repository_root.is_dir() else REPO)
    result.exit_code = process.returncode
    result.stdout_tail = _tail(process.stdout)
    result.stderr_tail = _tail(process.stderr)
    child_report = _child_json(process.stdout) or {}
    if process.returncode != 0:
        # A non-zero exit is a failure even when the child printed a JSON
        # document: the document is where the reason is read from, never a
        # substitute for the exit code.
        result.build_status = STATUS_FAILED
        result.reason = (f"image entry point exited {process.returncode}"
                         + (f": {child_report.get('reason')}" if child_report.get("reason")
                            else "")
                         + f" (raw stderr: {_tail(process.stderr, 600)})")
        return result
    try:
        records = _image_report_records(report_path)
    except RefusedInput as error:
        result.build_status = STATUS_FAILED
        result.reason = str(error)
        return result

    # The independent re-audit, always on the real archives.
    audit_argv = [sys.executable, str(audit_script), "--report", str(report_path),
                  "--install-manifest", str(install_manifest),
                  "--audit", str(audit_report)]
    process = _run_child(audit_argv, cwd=repository_root if repository_root.is_dir() else REPO)
    result.stderr_tail = _tail(process.stderr)
    if process.returncode != 0:
        result.build_status = STATUS_FAILED
        result.reason = (f"the independent re-audit refused the run (exit {process.returncode}); "
                         "the repacked archives were not published: "
                         + _tail(process.stderr or process.stdout, 900))
        return result
    try:
        audit_document = _load_json_document(audit_report)
        if not isinstance(audit_document, Mapping):
            raise RefusedInput(f"the independent re-audit wrote no JSON object: {audit_report}")
        passed, failed, textures = _validate_image_audit(audit_document, report_path, records,
                                                        install_manifest)
    except RefusedInput as error:
        result.build_status = STATUS_FAILED
        result.reason = str(error)
        return result
    result.provenance = {
        **(result.provenance or {}),
        "input_context": probe.context,
        "audit_path": str(audit_report),
        "audit_sha256": _sha256_file(audit_report),
        "audit_passed_bundles": passed,
        "audit_failed_bundles": failed,
        "audit_target_textures": textures,
        "audited_inventory_bundles": len(records),
    }

    inventory_path = out_root / IMAGE_INVENTORY
    if not inventory_path.is_file():
        result.build_status = STATUS_FAILED
        result.reason = f"image entry point exited 0 but wrote no {IMAGE_INVENTORY}: {inventory_path}"
        return result
    rows = _image_rows(_load_json_document(inventory_path), inventory_path)
    result.provenance = {
        **(result.provenance or {}),
        "inventory_path": str(inventory_path),
        "inventory_sha256": _sha256_file(inventory_path),
        "rows": len(rows),
    }
    # The audited set and the published set must be the same set: the audit
    # covered `records` (tied to this exact report by SHA), and the store is
    # about to be fed from `rows`.  A bundle the audit never read must not slip
    # in through the inventory, and a bundle the inventory never mentions must
    # not be counted as covered.
    def _remote_of(row: Mapping[str, Any]) -> str:
        return str(row.get("remote") or row.get("logical_key") or row.get("logical")
                   or row.get("bundle") or "")

    audited_remotes = {str(record["remote"]) for record in records}
    inventory_remotes = {_remote_of(row) for row in rows}
    if "" in inventory_remotes or audited_remotes != inventory_remotes:
        missing = sorted(audited_remotes - inventory_remotes)[:5]
        extra = sorted(inventory_remotes - audited_remotes)[:5]
        result.build_status = STATUS_FAILED
        result.reason = (
            "the independently audited bundles and the inventory disagree: the audit read "
            f"{len(audited_remotes)} bundle(s), the inventory declares {len(inventory_remotes)}"
            + (f"; not in the inventory: {missing}" if missing else "")
            + (f"; never audited: {extra}" if extra else "")
        )
        return result
    for index, row in enumerate(rows):
        artifact = _resolve_inventory_artifact(row, inventory_path, staging)
        if artifact is None:
            result.build_status = STATUS_FAILED
            result.reason = (f"image inventory row #{index} has no resolvable artifact_file "
                             f"({row.get('artifact_file')!r})")
            return result
        declared_source = str(row.get("source_sha256") or "")
        if not declared_source:
            result.build_status = STATUS_FAILED
            result.reason = f"image inventory row #{index} carries no source_sha256"
            return result
        actual = _sha256_file(artifact)
        declared_artifact = row.get("artifact_sha256")
        if declared_artifact is not None and str(declared_artifact) != actual:
            result.build_status = STATUS_FAILED
            result.reason = (f"image inventory row #{index}: artifact_sha256 "
                             f"{declared_artifact!r} does not match the bytes ({actual})")
            return result
        logical_key = str(row.get("logical_key") or row.get("logical")
                          or row.get("bundle") or artifact.name)
        declared_path = row.get("logical_path") or row.get("remote")
        result.entries.append(EntryRow(
            logical_key=logical_key,
            # The caller places the declaration under --logical-path-prefix; it is
            # the bundle's own name, not a server path yet.
            logical_path="",
            declared_logical_path=str(declared_path) if declared_path else logical_key,
            declared_remote=str(row.get("remote")) if row.get("remote") else None,
            source_sha256=declared_source,
            translated_sha256=actual,
            artifact_path=artifact,
            bytes=artifact.stat().st_size,
            surface=surface,
            resource_kind=SURFACE_RESOURCE_KIND.get(surface, RESOURCE_KIND_OTHER),
            declared_reuse_status=row.get("reuse_status"),
            declared_translation_status=row.get("translation_status"),
        ))
        result.outputs.append({"path": str(artifact), "sha256": actual,
                               "bytes": artifact.stat().st_size,
                               "logical_key": logical_key,
                               "logical_path": str(declared_path or "")})
    result.build_status = STATUS_SUCCESS
    return result


# --------------------------------------------------------------------------- #
# reuse decision
# --------------------------------------------------------------------------- #
def decide_admission(
    store: Any,
    asset_version: str,
    surfaces: Sequence[SurfaceResult],
    verified_records: Mapping[str, Mapping[str, Any]] | None,
    reuse_ledger_cls: Any,
) -> tuple[bool, list[str]]:
    """Classify every produced entry; return (baseline, failure reasons).

    * No retained manifest for this ``asset_version``: this build establishes the
      baseline.  Every entry's official source was re-verified byte-for-byte by
      its materializer against the frozen audited cohort / pinned bundle SHA, so
      the source dimension is ``exact`` ("automatic reuse of the official source
      baseline") and the translated bytes are a first release (``modified``).
    * A retained manifest exists: the store's own ``ReuseLedger`` decides, and
      anything it marks ``suggested``/``blocked`` or ``untranslated``/``pending``
      fails the run closed.

    ``entry.logical_path`` must already be set by the caller.
    """
    entries = [entry for surface in surfaces for entry in surface.entries]

    if not store.manifest_path(asset_version).is_file():
        for entry in entries:
            entry.reuse_status = "exact"
            entry.translation_status = "modified"
            entry.reuse_reason = (
                "baseline: no retained manifest for asset_version "
                f"{asset_version}; the materializer re-verified this bundle's official source "
                "against the frozen audited cohort, so the source dimension is exact and the "
                "translated bytes are a first release"
            )
        return True, []

    previous = store.load_manifest(asset_version).get("entries") or []
    ledger = reuse_ledger_cls(previous, verified_records)
    sources = [{"logical_key": entry.logical_key, "logical_path": entry.logical_path,
                "source_sha256": entry.source_sha256,
                "translated_sha256": entry.translated_sha256} for entry in entries]
    decisions = ledger.decide(sources)
    failures: list[str] = []
    for entry, decision in zip(entries, decisions):
        entry.reuse_status = decision.reuse_status
        entry.translation_status = decision.translation_status
        entry.reuse_reason = decision.reason
        if not decision.eligible:
            failures.append(f"{entry.logical_key}: {decision.reason}")
    return False, failures


def check_declared_statuses(surfaces: Sequence[SurfaceResult], store_module: Any) -> list[str]:
    """子进程不得交回不可发布的状态。

    可准入集合取自 ``store_module``——即稍后会拒绝该条目的同一写者——因此由
    pinned 来源的规则主导该检查，而非本仓库副本。
    """
    failures: list[str] = []
    admissible_reuse = store_module.ADMISSIBLE_REUSE_STATUSES
    admissible_translation = store_module.ADMISSIBLE_TRANSLATION_STATUSES
    for surface in surfaces:
        for entry in surface.entries:
            if entry.declared_reuse_status is not None and \
                    str(entry.declared_reuse_status) not in admissible_reuse:
                failures.append(
                    f"{entry.logical_key}: child declared reuse_status "
                    f"{entry.declared_reuse_status!r}, which may never be published"
                )
            if entry.declared_translation_status is not None and \
                    str(entry.declared_translation_status) not in admissible_translation:
                failures.append(
                    f"{entry.logical_key}: child declared translation_status "
                    f"{entry.declared_translation_status!r}, which may never be published"
                )
    return failures


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def _surface_specs(args: argparse.Namespace) -> list[tuple[str, str, str | None]]:
    specs: list[tuple[str, str, str | None]] = []
    for name in args.surfaces:
        if name == SURFACE_TEXT_EVENT_UNIT:
            specs.append((name, EVENT_UNIT_SCRIPT, None))
        elif name == SURFACE_TEXT_MLD:
            specs.append((name, MLD_SCRIPT, None))
        elif name == SURFACE_IMAGE:
            specs.append((name, args.image_entry, args.image_audit))
    return specs


def resolve_ledgers(input_root: Path, explicit: Sequence[Path]) -> list[Path]:
    if explicit:
        return [Path(path) for path in explicit]
    ledger_dir = input_root / LEDGERS_DIRNAME
    discovered = sorted(ledger_dir.glob("*.jsonl")) if ledger_dir.is_dir() else []
    if not discovered:
        raise RefusedInput(
            "no accepted translation ledger was supplied: pass --translated-ledger "
            f"(repeatable) or place ledger JSONL files under {ledger_dir}"
        )
    return discovered


def build_report(args: argparse.Namespace, *, mode: str, build_status: str,
                 surfaces: Sequence[SurfaceResult], store_root: Path,
                 failure_reason: str | None, store_payload: Mapping[str, Any] | None,
                 reuse_baseline: bool | None) -> dict[str, Any]:
    return {
        "kind": REPORT_KIND,
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": mode,
        "build_status": build_status,
        "generated_written": bool(store_payload),
        "generated_at_utc": _utc_now(),
        "asset_version": args.asset_version,
        "source_client_version": args.source_client_version,
        "source_commit": args.source_commit,
        "translation_commit": args.translation_commit,
        "generated_commit": args.generated_commit,
        "ci_run_id": getattr(args, "ci_run_id", None),
        "repository_root": str(args.repository_root),
        "input_root": str(args.input_root),
        "output_root": str(args.output_root),
        "store_root": str(store_root),
        "logical_path_prefix": args.logical_path_prefix,
        "surfaces_requested": list(args.surfaces),
        "reuse_baseline": reuse_baseline,
        "surfaces": [surface.to_dict() for surface in surfaces],
        "store": dict(store_payload) if store_payload else None,
        "writer_source": getattr(args, "writer_source", None),
        "failure_reason": failure_reason,
    }


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    # 所有模式先核来源与 API；旧 module 入参不再参与来源选择。
    writer_row = resolve_writer_source(
        None, writer_root=getattr(args, "assets_writer_root", None),
        writer_pin=getattr(args, "assets_writer_pin", None), require_pair=True)
    writer_module = writer_row.module
    args.writer_source = writer_row.to_dict()
    try:
        return _run_pinned(args, writer_module)
    except writer_module.GeneratedStoreError as exc:
        # 产品异常类属于本次已核字节的命名空间，不能靠主仓同名异常捕获。
        # 仅转换写者声明的错误；编程错误及其它异常继续向外抛出。
        raise RefusedInput(str(exc)) from exc


def _run_pinned(args: argparse.Namespace, writer_module: Any) -> tuple[int, dict[str, Any]]:
    repository_root = Path(args.repository_root).resolve()
    input_root = Path(args.input_root).resolve()
    output_root = Path(args.output_root).resolve()
    store_root = output_root / STORE_DIRNAME

    asset_version, client_version = validate_axes(args.asset_version,
                                                  args.source_client_version, writer_module)
    source_commit, translation_commit, generated_commit = validate_commits(
        args.source_commit, args.translation_commit or args.source_commit,
        args.generated_commit or args.source_commit, writer_module,
    )
    try:
        ci_run_id = writer_module.validate_ci_run_id(args.ci_run_id)
    except writer_module.GeneratedStoreError as exc:
        raise RefusedInput(str(exc)) from exc
    args.asset_version = asset_version
    args.source_client_version = client_version
    args.source_commit = source_commit
    args.translation_commit = translation_commit
    args.generated_commit = generated_commit
    args.ci_run_id = ci_run_id
    args.repository_root = repository_root
    args.input_root = input_root
    args.output_root = output_root

    if not input_root.is_dir():
        raise RefusedInput(f"--input-root is not a directory: {input_root}")
    if not repository_root.is_dir():
        raise RefusedInput(f"--repository-root is not a directory: {repository_root}")
    ledgers = resolve_ledgers(input_root, args.translated_ledger)
    for ledger in ledgers:
        if not ledger.is_file():
            raise RefusedInput(f"translation ledger does not exist: {ledger}")
    prefix = str(args.logical_path_prefix or "").strip().strip("/")
    args.logical_path_prefix = prefix

    store = writer_module.GeneratedStore(store_root)
    if store.manifest_path(asset_version).is_file():
        store.load_manifest(asset_version)  # fail closed on a corrupt retained manifest
    if not _transaction_supported(store):
        # Checked before any surface runs: a store whose release cannot be staged
        # and switched in one step must not be discovered after the children have
        # already produced bundles.
        raise RefusedInput(
            "this store has no staged-store transaction "
            "(GeneratedStore.transaction(prune=...)): "
            f"{type(store).__module__}.{type(store).__name__} at {getattr(store, 'root', '?')}. A release is "
            "staged and independently verified before it is switched in; without that API a "
            "failed run could not leave the previous release byte-identical"
        )

    staging = Path(tempfile.mkdtemp(prefix="mltd-materialize-generated-"))
    surfaces: list[SurfaceResult] = []
    reuse_baseline: bool | None = None
    store_payload: dict[str, Any] | None = None
    try:
        for name, script_rel, audit_rel in _surface_specs(args):
            if name == SURFACE_IMAGE:
                surfaces.append(_run_image_surface(
                    script_rel=script_rel, audit_rel=audit_rel or IMAGE_AUDIT_SCRIPT_DEFAULT,
                    repository_root=repository_root,
                    staging=staging, preflight_only=args.preflight_only,
                    install_manifest=Path(args.image_install_manifest)
                    if args.image_install_manifest else None,
                    original_root=Path(args.image_original_root)
                    if args.image_original_root else None))
            else:
                surfaces.append(_run_text_surface(
                    surface=name, script_rel=script_rel,
                    manifest_name=(EVENT_UNIT_MANIFEST if name == SURFACE_TEXT_EVENT_UNIT
                                   else MLD_MANIFEST),
                    repository_root=repository_root, ledgers=ledgers, staging=staging,
                    asset_version=asset_version, client_version=client_version,
                    preflight_only=args.preflight_only))

        # 服务端在一个 asset_version 内仍按原资源路径提供服务，故 logical_path 落在
        # 该命名空间：配置前缀 + 官方主机实际提供的名字。子进程已给出带前缀路径
        # 就保留；裸 bundle 名（图片 inventory 的情况）放到前缀下；子进程声明
        # 的相对路径同理。**不以 CAS 摘要形状的 artifact 文件名充当路径**——那样
        # 只会造出一个无映射证据、解析不到的假 runtime 路径。因此无 declared
        # remote/logical 来源时**拒绝该条**，而不是用 artifact 名兜底。
        path_problems: list[str] = []
        for surface in surfaces:
            for entry in surface.entries:
                if entry.logical_path:
                    continue
                declared = entry.declared_remote or entry.declared_logical_path or ""
                if prefix and declared.startswith(f"{prefix}/"):
                    entry.logical_path = declared
                    continue
                if not declared:
                    path_problems.append(
                        f"{surface.surface}:{entry.logical_key}: 无 declared remote/logical 来源，"
                        "无法在官方服务路径命名空间内给出可解析的 logical_path/runtime_path")
                    entry.logical_path = ""  # 该条不可发布
                    continue
                entry.logical_path = f"{prefix}/{declared}" if prefix else declared

        problems = path_problems + [
            f"{surface.surface}: {surface.reason or surface.build_status}"
            for surface in surfaces if surface.build_status != STATUS_SUCCESS]
        if problems:
            status = (STATUS_NOT_IMPLEMENTED
                      if any(surface.build_status == STATUS_NOT_IMPLEMENTED for surface in surfaces)
                      else STATUS_FAILED)
            reason = ("one or more surfaces did not produce a publishable bundle; "
                      "generated/ was not touched: " + "; ".join(problems))
            report = build_report(args, mode=("preflight-only" if args.preflight_only
                                              else "materialize"),
                                  build_status=status, surfaces=surfaces,
                                  store_root=store_root, failure_reason=reason,
                                  store_payload=None, reuse_baseline=None)
            return (EXIT_PREFLIGHT_NOT_READY if args.preflight_only else EXIT_FAILED_CLOSED,
                    report)

        problems = check_declared_statuses(surfaces, writer_module)
        if problems:
            reason = ("a child declared a non-publishable status; generated/ was not touched: "
                      + "; ".join(problems))
            report = build_report(args, mode="materialize", build_status=STATUS_FAILED,
                                  surfaces=surfaces, store_root=store_root,
                                  failure_reason=reason, store_payload=None,
                                  reuse_baseline=None)
            return EXIT_FAILED_CLOSED, report

        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for surface in surfaces:
            for entry in surface.entries:
                key = entry.logical_path
                if key in seen:
                    duplicates.append(f"{key} ({seen[key]} and {surface.surface})")
                else:
                    seen[key] = surface.surface
        if duplicates:
            reason = ("duplicate logical_path across/inside surfaces; generated/ was not "
                      "touched: " + "; ".join(duplicates))
            report = build_report(args, mode="materialize", build_status=STATUS_FAILED,
                                  surfaces=surfaces, store_root=store_root,
                                  failure_reason=reason, store_payload=None,
                                  reuse_baseline=None)
            return EXIT_FAILED_CLOSED, report

        reuse_baseline, inadmissible = decide_admission(
            store, asset_version, surfaces, args.verified_compatible_records,
            reuse_ledger_cls=writer_module.ReuseLedger)
        if inadmissible:
            reason = ("the reuse ledger refused entries; generated/ was not touched: "
                      + "; ".join(inadmissible))
            report = build_report(args, mode="materialize", build_status=STATUS_FAILED,
                                  surfaces=surfaces, store_root=store_root,
                                  failure_reason=reason, store_payload=None,
                                  reuse_baseline=reuse_baseline)
            return EXIT_FAILED_CLOSED, report

        if args.preflight_only:
            report = build_report(args, mode="preflight-only", build_status=STATUS_SUCCESS,
                                  surfaces=surfaces, store_root=store_root,
                                  failure_reason=None, store_payload=None,
                                  reuse_baseline=reuse_baseline)
            return EXIT_OK, report

        entries = [entry for surface in surfaces for entry in surface.entries]
        if not entries:
            reason = ("no surface produced an entry; an empty release is not publishable and "
                      "generated/ was not touched")
            report = build_report(args, mode="materialize", build_status=STATUS_FAILED,
                                  surfaces=surfaces, store_root=store_root,
                                  failure_reason=reason, store_payload=None,
                                  reuse_baseline=reuse_baseline)
            return EXIT_FAILED_CLOSED, report

        store_payload = _materialize_entries(
            store=store, staging=staging, entries=entries, asset_version=asset_version,
            client_version=client_version, source_commit=source_commit,
            translation_commit=translation_commit, generated_commit=generated_commit,
            ci_run_id=ci_run_id, prune=args.prune_orphans)
        report = build_report(args, mode="materialize", build_status=STATUS_SUCCESS,
                              surfaces=surfaces, store_root=store_root, failure_reason=None,
                              store_payload=store_payload, reuse_baseline=reuse_baseline)
        return EXIT_OK, report
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _transaction_supported(store: Any) -> bool:
    """Whether this store's ``transaction`` matches the staged-store contract.

    The entry point talks to *some* copy of ``scripts/assets_generated_index.py``
    (its own, or the one a runner supplies), so the API it relies on is checked
    instead of assumed: a store without a transaction, or one from before the
    prune flag existed, is detected and reported.
    """
    factory = getattr(store, "transaction", None)
    if not callable(factory):
        return False
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    return "prune" in parameters


def _materialize_entries(*, store: Any, staging: Path,
                         entries: Sequence[EntryRow], asset_version: str,
                         client_version: str, source_commit: str,
                         translation_commit: str, generated_commit: str,
                         ci_run_id: str | None,
                         prune: bool) -> dict[str, Any]:
    """Stage the release, verify it, then switch it in; return the store payload.

    Everything the release consists of is written into a **staging store** and
    independently verified there.  The live ``generated/`` is not written inside
    that block at all -- so a failure raises out of the context manager, the
    staged root is discarded, and the live store is left exactly as it was (an
    existing successful build stays byte-identical).  Only a staged root that
    passed its own verification is switched in, by the store's transaction
    (which itself prunes the staged root when ``prune`` is set and restores the
    previous root if the switch fails).
    """
    def build(staged: Any) -> dict[str, Any]:
        # 1. Objects first (content addressing dedupes identical bytes), so the
        #    report can name every object_path before the manifest exists.
        objects_written = 0
        objects_deduped = 0
        for entry in entries:
            declared = _sha256_file(entry.artifact_path)
            if declared != entry.translated_sha256:
                raise RefusedInput(
                    f"{entry.logical_key}: staged bundle changed between staging and store "
                    f"({declared} != {entry.translated_sha256})"
                )
            stored = staged.put_object(entry.artifact_path)
            entry.object_path = stored.rel_path
            entry.deduped = stored.deduped
            objects_written += 0 if stored.deduped else 1
            objects_deduped += 1 if stored.deduped else 0

        # 2. The release manifest + checksums.txt, written atomically by the store.
        result = staged.build_release(
            asset_version,
            [entry.admission_dict() for entry in entries],
            source_client_version=client_version,
            source_commit=source_commit,
            translation_commit=translation_commit,
            generated_commit=generated_commit,
            ci_run_id=ci_run_id,
            build_status="success",
            entries_base=staging,
        )
        if not result.written:
            raise RefusedInput(f"store refused the release: {result.note}")

        # 3. Re-hash the release independently, *inside the staging root*: a
        #    failure here must not reach the live store, so it raises (the
        #    context manager then discards the staged root) instead of being
        #    turned into a report about a release that was in fact promoted.
        verification = staged.verify_release(asset_version)
        if not verification.ok:
            raise RefusedInput(
                "the staged release failed independent verification; the live store was not "
                "touched: " + "; ".join(verification.failures))
        return {
            "manifest_path": str(result.manifest_path),
            "checksums_path": str(result.checksums_path),
            "manifest_sha256": _json_file_sha(result.manifest_path),
            "checksums_sha256": _json_file_sha(result.checksums_path),
            "written": result.written,
            "accepted": len(result.accepted),
            "rejected": result.rejected,
            # Counted at put_object time: build_release sees the objects already
            # in the store and therefore always reports 0/0 itself.
            "objects_written": objects_written,
            "objects_deduped": objects_deduped,
            "counts": result.counts,
            "verify_ok": verification.ok,
            "verify_checked_objects": verification.checked_objects,
            "verify_failures": verification.failures,
            "note": result.note,
        }

    if not _transaction_supported(store):
        # No staged-store fallback on purpose: writing the live store in place
        # would make "a failed run leaves the previous release byte-identical"
        # untrue, and that guarantee is why the CI may commit this tree at all.
        raise RefusedInput(
            "this store has no staged-store transaction "
            "(GeneratedStore.transaction(prune=...)): "
            f"{type(store).__module__}.{type(store).__name__} at {getattr(store, 'root', '?')}. The release is "
            "written into a staging root and switched in as one step, so an entry point "
            "without that API cannot both publish and keep a failed run non-destructive"
        )

    with store.transaction(prune=prune) as staged:
        payload = build(staged)
    payload["transaction"] = {
        "staged": True,
        "pruned_orphans": bool(prune),
        "promoted": True,
    }
    # The paths above point into the staging root, which the switch has just
    # renamed to the live root -- they would name a directory that no longer
    # exists.  Re-point them at the live store and re-hash the two files there,
    # so the report describes the tree the CI is about to commit (and a
    # discrepancy fails the run instead of shipping a stale path).
    return _repoint_to_live(store, asset_version, payload)


def _repoint_to_live(store: Any, asset_version: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
    """Point the store payload at the live root after the promotion.

    Every path and digest in the payload is re-derived from the live store: the
    promotion moved the staged tree into place, so a payload that still named
    the staging paths would be provenance for a directory that is gone.  The
    manifest and checksums are re-hashed here; the object digests inside them
    were already re-verified by ``verify_release`` in the block.
    """
    manifest_path = store.manifest_path(asset_version)
    checksums_path = store.checksums_path(asset_version)
    if not manifest_path.is_file():
        raise RefusedInput(
            f"the promotion reported success but the live store has no manifest at "
            f"{manifest_path}")
    if not checksums_path.is_file():
        raise RefusedInput(
            f"the promotion reported success but the live store has no checksums at "
            f"{checksums_path}")
    verification = store.verify_release(asset_version)
    if not verification.ok:
        raise RefusedInput(
            "the promoted release does not re-verify in the live store: "
            + "; ".join(verification.failures))
    payload = dict(payload)
    payload.update({
        "manifest_path": str(manifest_path),
        "checksums_path": str(checksums_path),
        "manifest_sha256": _json_file_sha(manifest_path),
        "checksums_sha256": _json_file_sha(checksums_path),
        "verify_ok": verification.ok,
        "verify_checked_objects": verification.checked_objects,
        "verify_failures": verification.failures,
        "paths_repointed_to_live_root": True,
    })
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="materialize_generated_release.py",
        description="One fail-closed entry point that materialises every generated bundle for "
                    "one asset_version into a content-addressed generated/ store. Client and "
                    "assets versions are independent axes; composite identities are refused.",
    )
    parser.add_argument("--asset-version", required=True,
                        help="assets axis: digits only, e.g. 1077100")
    parser.add_argument("--source-client-version", required=True,
                        help="client axis: X.Y.Z, e.g. 9.0.200")
    parser.add_argument("--source-commit", required=True,
                        help="40-hex commit of the official/pristine source snapshot")
    parser.add_argument("--translation-commit", default=None,
                        help="40-hex commit of the applied translations (default: --source-commit)")
    parser.add_argument("--generated-commit", default=None,
                        help="40-hex commit this build ran at (default: --source-commit)")
    parser.add_argument("--ci-run-id", default=None,
                        help="CI run identity, digits or '<run>.<attempt>' (default: the runner's "
                             "own $MLTD_CI_RUN_ID/$GITHUB_RUN_ID; recorded as null when neither "
                             "is set -- it is provenance and is never invented)")
    parser.add_argument("--input-root", required=True, type=Path,
                        help="where translated text/images live; <input-root>/ledgers/*.jsonl is "
                             "used when --translated-ledger is not given")
    parser.add_argument("--output-root", required=True, type=Path,
                        help="where generated/ goes; the store root is <output-root>/generated")
    parser.add_argument("--repository-root", default=REPO, type=Path,
                        help="root the surface entry points resolve under (default: this repo)")
    parser.add_argument("--assets-writer-root", default=None, type=Path,
                        help="pinned store 写者副本的根；须包含 "
                             f"{ASSETS_WRITER_RELATIVE}。与 --assets-writer-pin 成对给出，"
                             "所有模式（包括 --preflight-only）必填")
    parser.add_argument("--assets-writer-pin", default=None,
                        help=f"pinned 副本 {ASSETS_WRITER_RELATIVE} 的 64-hex SHA-256；"
                             "须与 --assets-writer-root 处字节一致，否则在任何写入前拒绝。"
                             "两参数成对，只给其一即拒；pin 是源码哈希，绝不用 commit id")
    parser.add_argument("--preflight-only", action="store_true",
                        help="validate inputs and the release gate; produce no bundle")
    parser.add_argument("--translated-ledger", action="append", default=[], type=Path,
                        help="accepted (release_gate=accepted) text ledger; repeatable")
    parser.add_argument("--surfaces", default=",".join(DEFAULT_SURFACES),
                        help=f"comma-separated subset of {', '.join(SURFACE_CHOICES)}")
    parser.add_argument("--image-entry", default=IMAGE_SCRIPT_DEFAULT,
                        help="image-side entry point, relative to --repository-root "
                             f"(default: {IMAGE_SCRIPT_DEFAULT}, this repository's own copy; "
                             "the Assets repository passes its pipelines/image/ copy here)")
    parser.add_argument("--image-audit", default=IMAGE_AUDIT_SCRIPT_DEFAULT,
                        help="the injector's independent re-audit, relative to "
                             f"--repository-root (default: {IMAGE_AUDIT_SCRIPT_DEFAULT})")
    parser.add_argument("--image-install-manifest", default=None,
                        help="reviewed texture-install manifest JSONL for the image surface; "
                             "required when the image surface runs, because the reviewed cohort "
                             "is not in this repository's commit contract")
    parser.add_argument("--image-original-root", default=None,
                        help="root the reviewed manifest's relative original_png values resolve "
                             "under")
    parser.add_argument("--logical-path-prefix", default=LOGICAL_PATH_PREFIX_DEFAULT,
                        help="prefix for logical_path (the server path inside one asset_version); "
                             "pass an empty string to publish the bare bundle file name")
    parser.add_argument("--verified-compatible-records", type=Path, default=None,
                        help="JSON {logical_key: {from_sha256, to_sha256, evidence}} authorising "
                             "reuse across a changed official source")
    prune_group = parser.add_mutually_exclusive_group()
    prune_group.add_argument("--prune-orphans", dest="prune_orphans", action="store_true",
                             default=False,
                             help="显式请求在候选事务内清理所有保留清单均未引用的对象")
    prune_group.add_argument("--no-prune-orphans", dest="prune_orphans",
                             action="store_false", default=False,
                             help="保留孤儿对象（兼容参数；默认行为）")
    parser.add_argument("--report", type=Path, default=None,
                        help="also write the machine-readable build report here")
    return parser


def _refusal_payload(args: argparse.Namespace, reason: str, *,
                     mode: str | None = None) -> dict[str, Any]:
    """The CI still gets a machine-readable report for a refused identity."""
    return {
        "kind": REPORT_KIND, "schema_version": REPORT_SCHEMA_VERSION,
        "mode": mode or ("preflight-only" if getattr(args, "preflight_only", False)
                         else "materialize"),
        "build_status": "failed", "generated_written": False,
        "asset_version": str(getattr(args, "asset_version", "")),
        "source_client_version": str(getattr(args, "source_client_version", "")),
        "source_commit": str(getattr(args, "source_commit", "")),
        "translation_commit": str(getattr(args, "translation_commit", "")),
        "generated_commit": str(getattr(args, "generated_commit", "")),
        "surfaces": [],
        "store": None,
        "failure_reason": reason,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    requested = [name.strip() for name in str(args.surfaces).split(",") if name.strip()]
    unknown = [name for name in requested if name not in SURFACE_CHOICES]
    if unknown or not requested:
        reason = (f"unknown --surfaces {unknown or '(none)'}; "
                  f"choose from {', '.join(SURFACE_CHOICES)}")
        print(f"ERROR: {reason}", file=sys.stderr)
        if args.report:
            _atomic_write_text(Path(args.report), json.dumps(
                _refusal_payload(args, reason, mode="refused"), ensure_ascii=False,
                indent=2) + "\n")
        return EXIT_REFUSED
    args.surfaces = tuple(requested)
    records: Mapping[str, Mapping[str, Any]] | None = None
    try:
        args.repository_root = Path(args.repository_root)
        if args.verified_compatible_records is not None:
            records = _load_json_document(Path(args.verified_compatible_records))
        code, report = run(args)
    except RefusedInput as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        payload = _refusal_payload(args, str(exc), mode="refused")
        if args.report:
            _atomic_write_text(Path(args.report),
                               json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return EXIT_REFUSED
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        _atomic_write_text(Path(args.report), text)
    print(text, end="", flush=True)
    if code == EXIT_OK:
        if args.preflight_only:
            print(f"OK: preflight ready for asset_version {report['asset_version']} "
                  f"({len(report['surfaces'])} surface(s)); no bundle was produced",
                  file=sys.stderr)
        else:
            store = report["store"]
            print(f"OK: wrote {store['manifest_path']} ({store['accepted']} entries, "
                  f"{store['objects_written']} new object(s), "
                  f"{store['objects_deduped']} deduplicated, verify_ok={store['verify_ok']})",
                  file=sys.stderr)
    else:
        print(f"FAIL: {report['failure_reason']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
