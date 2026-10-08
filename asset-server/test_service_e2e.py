#!/usr/bin/env python3
"""End-to-end service acceptance for the self-contained Assets closure (offline).

链路（全部在临时目录、loopback 随机端口、不触网络/NAS/设备）：

    本仓 flat writer（``scripts/assets_generated_index.py``，含 ``runtime_path``）
      -> 产出 manifest/checksums/objects 真实字节
      -> mirror（``scripts/assets_mirror.py``）sync 进独立 mirror root
      -> 只读 route（``asset-server/assets_route.py``）经 loopback HTTP 读回字节

同时验证：sharded 历史 manifest 只读兼容；坏 digest/路径/版本被拒且不落盘；
重复路径在写任何字节之前整体拒绝；``watch`` 只同步、不 activate、不 prune；
route 在默认配置下不发任何出站请求（本文件把 ``urllib.request.urlopen``
替换成会失败的探针来证明这一点，而不是靠 skip）。
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import msgpack  # noqa: E402

from scripts import assets_generated_index as producer  # noqa: E402
from scripts import assets_mirror as mirror_module  # noqa: E402
from scripts.assets_mirror import (  # noqa: E402
    AssetVersionMirror,
    ManifestValidationError,
    ObjectPool,
)
from scripts.test_assets_mirror import FakeSource, COMMIT_A, COMMIT_B, SOURCE_COMMIT  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "assets_route", Path(__file__).with_name("assets_route.py"))
route = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = route
_spec.loader.exec_module(route)

RUNTIME_NAME = "ab" * 20 + ".unity3d"  # 客户端实际请求的哈希文件名（40hex.unity3d）
RUNTIME_PATH = f"production/2018/Android/{RUNTIME_NAME}"
LOGICAL_PATH = "production/2018/Android/event/001/title.unity3d"
SECOND_LOGICAL = "production/2018/Android/master/BGM_001.unity3d"
PAYLOAD_RUNTIME = b"runtime-bundle-fixture" * 8
PAYLOAD_SECOND = b"second-bundle-fixture" * 8


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ServiceE2ETestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mltd-service-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.generated_root = self.base / "generated"
        self.mirror_root = self.base / "mirror"
        self.stage = self.base / "stage"
        self.stage.mkdir()
        (self.stage / "runtime.bundle").write_bytes(PAYLOAD_RUNTIME)
        (self.stage / "second.bundle").write_bytes(PAYLOAD_SECOND)
        self._block_public_network()

    def _block_public_network(self) -> None:
        """pytest 级别断公网：本模块所有用例只允许 loopback。

        不是靠 skip：任何对非 loopback 主机的 ``socket.connect`` 或
        ``urllib.request.urlopen`` 都会直接抛 AssertionError——
        包括被测代码「偷偷」尝试默认 fetch 的情况。
        """
        import socket
        import urllib.parse
        import urllib.request

        real_connect = socket.socket.connect

        def guarded_connect(sock, address):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in ("127.0.0.1", "localhost", "::1", ""):
                raise AssertionError(f"e2e 测试禁止访问非 loopback 地址: {address}")
            return real_connect(sock, address)

        connect_patch = mock.patch.object(socket.socket, "connect", guarded_connect)
        connect_patch.start()
        self.addCleanup(connect_patch.stop)

        real_urlopen = urllib.request.urlopen

        def guarded_urlopen(url, *args, **kwargs):
            target = url.full_url if hasattr(url, "full_url") else str(url)
            host = urllib.parse.urlsplit(target).hostname or ""
            if host not in ("127.0.0.1", "localhost", "::1", ""):
                raise AssertionError(f"e2e 测试禁止出站请求: {target}")
            return real_urlopen(url, *args, **kwargs)

        urlopen_patch = mock.patch("urllib.request.urlopen", guarded_urlopen)
        urlopen_patch.start()
        self.addCleanup(urlopen_patch.stop)

    # -- fixture: 用本仓 producer 真实产出一个 flat + runtime_path 版本 ---------------

    def _producer_entries(self, *, version: str = "1077100",
                          runtime_path: str | None = RUNTIME_PATH,
                          logical_path: str = LOGICAL_PATH,
                          second_logical: str = SECOND_LOGICAL) -> list[dict]:
        entries = [{
            "channel": "assets",
            "asset_version": version,
            "client_version": None,
            "source_client_version": "9.0.200",
            "logical_key": logical_path,
            "logical_path": logical_path,
            "resource_kind": "bundle",
            "source_sha256": sha256_hex(b"ja-source"),
            "artifact_file": "runtime.bundle",
            "reuse_status": "exact",
            "translation_status": "modified",
        }]
        if runtime_path is not None:
            entries[0]["runtime_path"] = runtime_path
        entries.append({
            "channel": "assets",
            "asset_version": version,
            "client_version": None,
            "source_client_version": "9.0.200",
            "logical_key": second_logical,
            "logical_path": second_logical,
            "resource_kind": "bundle",
            "source_sha256": sha256_hex(b"ja-source-2"),
            "artifact_file": "second.bundle",
            "reuse_status": "exact",
            "translation_status": "accepted",
        })
        return entries

    def _build(self, entries: list[dict], *, version: str = "1077100"):
        store = producer.GeneratedStore(self.generated_root)
        return store.build_release(
            version, entries,
            source_client_version="9.0.200",
            source_commit=SOURCE_COMMIT,
            translation_commit=COMMIT_B,
            generated_commit=COMMIT_A,
            entries_base=self.stage,
        )

    def _publish_to_source(self, manifest_path: Path, checksums_path: Path,
                           objects_root: Path, source: FakeSource) -> None:
        """把 producer 磁盘上的真实字节原样交给 source（不重构 manifest）。"""
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        objects = {}
        for entry in manifest["entries"]:
            objects[entry["artifact_sha256"]] = (objects_root / entry["object_path"]).read_bytes()
        source.publish(manifest, objects, commit=COMMIT_A)
        source.compare[SOURCE_COMMIT] = "ahead"
        source.compare[COMMIT_B] = "ahead"
        source.compare[COMMIT_A] = "identical"

    def _synced_mirror(self) -> tuple[AssetVersionMirror, FakeSource]:
        result = self._build(self._producer_entries())
        self.assertTrue(result.written, result)
        source = FakeSource(head=COMMIT_A)
        self._publish_to_source(result.manifest_path, result.checksums_path,
                                self.generated_root, source)
        mirror = AssetVersionMirror(source, ObjectPool(self.mirror_root), self.mirror_root)
        report = mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(report["sync_status"], "success", report)
        return mirror, source

    def _start_route(self, mirror: AssetVersionMirror, *, official_root: Path | None = None):
        server = route.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            route.handler_for(mirror, official_root=official_root),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, 3)
        return server

    @staticmethod
    def _get(server, path: str, method: str = "GET", headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    # -- 1. producer flat + runtime_path -> sync -> route 读回 ----------------------

    def test_writer_flat_release_syncs_and_route_serves_exact_bytes(self):
        mirror, _ = self._synced_mirror()

        # 产物是 flat 对象路径（本仓 writer 契约），且 runtime_path 被保留。
        manifest = json.loads(mirror.manifest_path("1077100").read_text(encoding="utf-8"))
        for entry in manifest["entries"]:
            self.assertEqual(entry["object_path"], f"objects/sha256/{entry['artifact_sha256']}")
        with_runtime = next(e for e in manifest["entries"] if e.get("runtime_path"))
        self.assertEqual(with_runtime["runtime_path"], RUNTIME_PATH)

        server = self._start_route(mirror)
        # 客户端原样请求的 runtime path（含 production/2018/Android/ 前缀，真实
        # manifest 的两条路径都带该前缀）与 logical path 都要命中同一字节。
        for path in (f"/assets/1077100/{RUNTIME_PATH}",
                     f"/assets/1077100/{LOGICAL_PATH}",
                     f"/assets/1077100/{SECOND_LOGICAL}"):
            status, headers, body = self._get(server, path)
            self.assertEqual(status, 200, path)
            expected = PAYLOAD_RUNTIME if path != f"/assets/1077100/{SECOND_LOGICAL}" else PAYLOAD_SECOND
            self.assertEqual(body, expected, path)
            self.assertEqual(headers["ETag"], '"' + sha256_hex(expected) + '"')
            self.assertEqual(headers["X-Asset-Source"], "translated")
        # 现有语义：entry 自带前缀时，去前缀请求形式不是别名（既不新增映射，
        # 也不在迁移中改动 route 的对外行为）；未发布版本与未声明对象 fail-closed。
        self.assertEqual(self._get(server, f"/assets/1077100/{RUNTIME_NAME}")[0], 404)
        self.assertEqual(self._get(server, f"/assets/1077999/{LOGICAL_PATH}")[0], 404)
        self.assertEqual(self._get(server, "/assets/1077100/production/2018/Android/nope.unity3d")[0], 404)

    # -- 2. sharded 历史 manifest 只读兼容 -----------------------------------------

    def test_historical_sharded_manifest_reads_back_from_legacy_shard(self):
        mirror, _ = self._synced_mirror()
        manifest_path = mirror.manifest_path("1077100")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(e for e in manifest["entries"] if e.get("runtime_path"))
        digest = entry["artifact_sha256"]
        shard_rel = f"objects/sha256/{digest[:2]}/{digest}"
        # 字节只留在旧 shard 位置（把 flat 对象改名到 fan-out 路径）。
        flat = mirror_root_file = self.mirror_root / entry["object_path"]
        shard = self.mirror_root / shard_rel
        shard.parent.mkdir(parents=True, exist_ok=True)
        flat.rename(shard)
        entry["object_path"] = shard_rel
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        mirror.checksums_path("1077100").write_text(
            f"{digest}  {shard_rel}\n", encoding="utf-8")

        server = self._start_route(mirror)
        status, _, body = self._get(server, f"/assets/1077100/{RUNTIME_PATH}")
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD_RUNTIME)

    # -- 3. 坏 digest / 坏路径 / 未知版本 / 重复路径 ---------------------------------

    def test_bad_digest_bad_path_unknown_version_and_duplicate_path_are_refused(self):
        # 坏 digest：manifest 声明与字节不符 -> 发布被拒（不写 published/），route 404。
        result = self._build(self._producer_entries())
        source = FakeSource(head=COMMIT_A)
        self._publish_to_source(result.manifest_path, result.checksums_path,
                                self.generated_root, source)
        digest = next(e["artifact_sha256"] for e in
                      json.loads(result.manifest_path.read_text(encoding="utf-8"))["entries"])
        source.objects[(digest, COMMIT_A)] = b"tampered-bytes"
        mirror = AssetVersionMirror(source, ObjectPool(self.mirror_root), self.mirror_root)
        with self.assertRaises(mirror_module.AssetsMirrorError):
            mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertFalse(mirror.manifest_path("1077100").exists())
        self.assertFalse(mirror.state_path("1077100").exists())

        # 坏路径：object_path 不是该 digest 的规范内容寻址路径 -> 整个版本被拒。
        bad = self._build(self._producer_entries())
        bad_manifest = json.loads(bad.manifest_path.read_text(encoding="utf-8"))
        bad_manifest["entries"][0]["object_path"] = "objects/sha256/zz/" + "0" * 64
        problems = mirror_module.validate_manifest(
            bad_manifest, asset_version="1077100", expected_head_commit=COMMIT_A)
        self.assertTrue(problems, "坏 object_path 必须被 validate_manifest 拒绝")

        # 重复路径：两个 entry 同一 logical_path -> 在任何 put_object 之前整体拒绝，
        # 且 mirror root 保持零字节。
        dup_entries = self._producer_entries()
        dup_entries[1]["logical_path"] = LOGICAL_PATH
        dup_entries[1]["logical_key"] = LOGICAL_PATH
        fresh_root = self.base / "dup-mirror"
        dup_source = FakeSource(head=COMMIT_A)
        dup_source.publish(
            {
                "kind": producer.MANIFEST_KIND,
                "schema_version": 1,
                "asset_version": "1077100",
                "client_version": None,
                "source_client_version": "9.0.200",
                "source_commit": SOURCE_COMMIT,
                "translation_commit": COMMIT_B,
                "generated_commit": COMMIT_A,
                "ci_run_id": None,
                "build_status": "success",
                "entries": [
                    {**e, "artifact_sha256": sha256_hex(b"x" + str(i).encode()),
                     "object_path": f"objects/sha256/{sha256_hex(b'x' + str(i).encode())}"}
                    for i, e in enumerate(dup_entries)
                ],
            },
            {sha256_hex(b"x0"): b"x0", sha256_hex(b"x1"): b"x1"}, commit=COMMIT_A)
        for field in ("source_commit", "translation_commit", "generated_commit"):
            dup_source.compare.setdefault(COMMIT_A, "identical")
        dup_source.compare[SOURCE_COMMIT] = "ahead"
        dup_source.compare[COMMIT_B] = "ahead"
        dup_mirror = AssetVersionMirror(dup_source, ObjectPool(fresh_root), fresh_root)
        with self.assertRaises(ManifestValidationError):
            dup_mirror.sync("1077100", commit=COMMIT_A, dry_run=False)
        self.assertEqual(list(fresh_root.rglob("*")), [],
                         "重复路径必须在校验阶段先写拒绝（mirror root 保持零字节）")

    # -- 4. watch 只同步：不 activate、不 prune ------------------------------------

    def test_watch_syncs_without_activating_or_pruning(self):
        mirror, source = self._synced_mirror()
        # 用同一套 stage 字节再发布一个 1077500（真实 producer 产物，
        # 版本号落在 manifest 与每个 entry 上），watch 之后它必须原样保留。
        other = self._build(
            self._producer_entries(version="1077500",
                                   logical_path="production/2018/Android/old.unity3d",
                                   runtime_path=None,
                                   second_logical="production/2018/Android/old2.unity3d"),
            version="1077500")
        self.assertTrue(other.written)
        other_manifest = json.loads(other.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(other_manifest["asset_version"], "1077500")
        source.publish(other_manifest,
                       {e["artifact_sha256"]: (self.generated_root / e["object_path"]).read_bytes()
                        for e in other_manifest["entries"]}, commit=COMMIT_A)
        self.assertEqual(mirror.sync("1077500", commit=COMMIT_A, dry_run=False)["sync_status"], "success")
        before = {str(p.relative_to(self.mirror_root)): p.read_bytes()
                  for p in self.mirror_root.rglob("*") if p.is_file()}

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mirror_module, "GitHubAssetsSource", lambda **kwargs: source):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = mirror_module.main(
                    ["--root", str(self.mirror_root), "watch",
                     "--asset-version", "1077100", "--once"])
        self.assertEqual(code, 0, err.getvalue())
        after = {str(p.relative_to(self.mirror_root)): p.read_bytes()
                 for p in self.mirror_root.rglob("*") if p.is_file()}
        self.assertEqual(set(after), set(before), "watch 不得新增/删除任何文件")
        for rel, payload in after.items():
            if rel.endswith("state.json"):
                continue  # state.json 是本次同步的回执（时间戳/计数），允许更新
            self.assertEqual(payload, before[rel], rel)
        # 幂等证据在回执里：对象全部已存在（downloaded=0），且版本集合不变。
        state = json.loads((self.mirror_root / "published" / "1077100" / "state.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["sync_status"], "success")
        self.assertEqual(state["downloaded"], 0)
        self.assertFalse((self.mirror_root / "current.json").exists())
        self.assertFalse((self.mirror_root / "retained.json").exists())
        self.assertTrue(mirror.manifest_path("1077500").is_file())  # 未被 prune

        # prune 默认 dry-run：本地文件不动。
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mirror_module.main(["--root", str(self.mirror_root), "prune"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("dry-run", out.getvalue())
        self.assertEqual({str(p.relative_to(self.mirror_root)) for p in self.mirror_root.rglob("*") if p.is_file()},
                         set(after))

    # -- 5. route 默认不出站；官方回退仅在显式配置时使用 ----------------------------

    def test_route_never_performs_network_io_without_explicit_official_base(self):
        mirror, _ = self._synced_mirror()
        server = self._start_route(mirror)
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("出站网络调用被触发")):
            # 命中翻译对象
            self.assertEqual(self._get(server, f"/assets/1077100/{LOGICAL_PATH}")[0], 200)
            # 未命中的对象在未配置 official_base_url 时直接 404，而不是去官方 CDN 拉取。
            self.assertEqual(
                self._get(server, "/assets/1077100/production/2018/Android/missing.unity3d")[0],
                404)

    def test_official_fallback_uses_only_configured_local_root(self):
        mirror, _ = self._synced_mirror()
        official_root = self.base / "official"
        # materialized official 视图布局：views/<ver>/jp-android/<resource>（不含
        # production/2018/Android 前缀；该前缀在回退查询时被剥离）。
        fallback = official_root / "views" / "1077100" / "jp-android" / "fallback.unity3d"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(b"official-japanese-fixture")
        server = self._start_route(mirror, official_root=official_root)
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("出站网络调用被触发")):
            status, headers, body = self._get(
                server, "/assets/1077100/production/2018/Android/fallback.unity3d")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"official-japanese-fixture")
        self.assertEqual(headers["X-Asset-Source"], "official-jp")

    def test_route_refuses_same_size_corruption_and_truncated_object(self):
        """逐请求复验：同长度改一字节（digest 变）与截断（size 变）都拒绝。"""
        mirror, _ = self._synced_mirror()
        server = self._start_route(mirror)
        self.assertEqual(self._get(server, f"/assets/1077100/{LOGICAL_PATH}")[0], 200)
        entry = next(e for e in json.loads(
            mirror.manifest_path("1077100").read_text(encoding="utf-8"))["entries"]
            if e.get("runtime_path"))
        target = self.mirror_root / entry["object_path"]

        original = target.read_bytes()
        target.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))  # 同长度、坏字节
        self.assertEqual(self._get(server, f"/assets/1077100/{LOGICAL_PATH}")[0], 404)

        target.write_bytes(original[: len(original) // 2])  # 截断、坏 size
        self.assertEqual(self._get(server, f"/assets/1077100/{LOGICAL_PATH}")[0], 404)

        target.write_bytes(original)  # 复原后重新可读（无副作用缓存）
        self.assertEqual(self._get(server, f"/assets/1077100/{LOGICAL_PATH}")[0], 200)

    # -- 6. CLI 从任意 CWD 以显式 --root 运行 --------------------------------------

    def test_cli_entries_work_from_a_foreign_cwd_with_explicit_root(self):
        import subprocess
        env = {k: v for k, v in __import__("os").environ.items()
               if k not in ("PYTHONPATH", "PYTHONSTARTUP")}
        cwd = self.base  # 与仓库无关的目录
        for script, argv in (
            (ROOT / "scripts" / "assets_mirror.py", ["--root", str(self.mirror_root), "list"]),
            (ROOT / "asset-server" / "assets_route.py", ["list", "--root", str(self.mirror_root)]),
        ):
            completed = subprocess.run([sys.executable, str(script), *argv],
                                       cwd=cwd, env=env, capture_output=True,
                                       text=True, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            json.loads(completed.stdout)  # list 输出必须是 JSON


if __name__ == "__main__":
    unittest.main(verbosity=2)
