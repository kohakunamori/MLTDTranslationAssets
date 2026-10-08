# MLTD 资产服务（本仓自足闭包，候选）

本目录是 MLTD 汉化 Assets 通道的**最小运行闭包**：镜像、CLI 与只读路由只依赖
本仓（`D:\Project\_MLTDTranslationAssets`）内的源码，不依赖主仓 mltd-current、
不依赖任何兄弟仓库或 `PYTHONPATH` 回退。

> 状态：**候选（未部署）**。本仓仓内的 `generated/` 生成侧 CI（`assets-generated.yml`
> → `scripts/build_generated_release.py`）是当前唯一的正式 writer；本目录只是它的
> 消费/分发端。上线切换、NAS/D1 指向、单写者转移都需要单独的人工窗口，本目录不做任何
> 生产切换。

## 组成

| 路径 | 角色 |
| --- | --- |
| `assets_route.py` | 只读 HTTP 分发（`serve`/`list`），按 manifest 的 `runtime_path` / `logical_path` 读取 CAS 对象，逐请求校验字节哈希；默认不出站（官方回退只有显式给 `--official-base-url` / `--official-root` 才启用，且 local root 优先） |
| `Dockerfile` | 逐文件 COPY 的自足镜像（见下「闭包清单」） |
| `docker-compose.yml` | 三个服务的静态部署模板：`mltd-asset-tools`（profile `tools`）、`mltd-asset-updater`、`mltd-assets-mirror`；`ASSETS_MIRROR_VERSIONS` 缺失即拒绝启动 |
| `asset-version` | 官方归档控制 wrapper（`tools/asset_version.py`，经 docker compose `tools` profile） |
| `switch-version.sh` | 兼容 wrapper：`activate --version <v>` |
| `requirements.txt` | `msgpack`、`requests` |

服务入口（compose）：

- `mltd-assets-mirror` → `scripts/assets_mirror.py watch --asset-version …`
  （只同步列出的版本；不跟随 latest、不写 `current.json`、不删除任何版本）
- `mltd-asset-updater` → `tools/archive_controller.py watch`
- `mltd-asset-tools` → `tools/versioned_assets.py`（按需 run）

## 闭包清单（镜像只装这些）

`Dockerfile` 逐文件 COPY（不整树）：

```text
server/asset_archive.py               # versioned_asset_store 的依赖
server/versioned_asset_store.py
tools/archive_controller.py           # updater 入口；子进程调用下面三个同级入口
tools/versioned_assets.py
tools/materialize_versioned_assets.py
tools/asset_version.py
scripts/assets_generated_index.py     # producer（本仓同一文件的唯一正式 writer 实现）
scripts/assets_mirror.py              # mirror 入口
asset-server/requirements.txt
```

仓库没有 `scripts/__init__.py`/`server/__init__.py`/`tools/__init__.py`：这些目录以
Python 3 命名空间包方式导入（各入口脚本都会把仓库根插入 `sys.path`），因此不需要
`__init__.py`。`scripts/assets_mirror.py` 的 producer 导入有两条兼容分支：

- 顶层名 `assets_generated_index`（镜像内 `python scripts/assets_mirror.py` 的真实形态，
  以及本仓 CI `python scripts/assets_generated_index.py …` 的形态）；
- 包名 `scripts.assets_generated_index`（以包布局消费本仓时的形态）。

两条分支解析到**同一份 producer 字节**（`test_docker_context.py` 用双分支探针钉住）；
两个模块都缺失时直接 `ImportError`，不会静默降级为第二实现。

## 本地运行（不联网）

```bash
# 只读路由（loopback，显式 mirror root）
python asset-server/assets_route.py list  --root /path/to/verified-mirror
python asset-server/assets_route.py serve --root /path/to/verified-mirror --bind 127.0.0.1 --port 8765

# mirror：显式版本、显式同步（默认 dry-run）
python scripts/assets_mirror.py --root /path/to/mirror sync --asset-version 1077100
python scripts/assets_mirror.py --root /path/to/mirror watch --asset-version 1077100 --once

# 官方归档控制（NAS 场景经 compose tools profile）
./asset-server/asset-version list
./asset-server/asset-version progress <ver>
./asset-server/switch-version.sh <ver>
```

## 回归（全部离线）

```bash
python -m pytest scripts/test_assets_mirror.py server/tests asset-server/ -q
```

- `scripts/test_assets_mirror.py`：mirror 契约（显式版本 watch、activate/prune 分离、
  fail-closed、幂等、legacy shard 读取、producer 输出直通）。
- `server/tests/`：store/materialize/controller/CLI 单元测试（自上游迁入，字节未改；
  本地版本只作本仓闭包回归，master 语义仍是上游）。
- `asset-server/test_assets_route.py`：只读 route 的 loopback fixture（runtime_path 别名、
  官方回退、`/cn` 兼容桥、路径穿越/坏校验拒绝）。
- `asset-server/test_docker_context.py`：镜像上下文闭包（逐文件 COPY、无跨仓引用、
  import 闭包、双分支 producer 同字节、临时上下文导入与 `--help`）。
- `asset-server/test_service_e2e.py`：writer(flat+runtime_path) → sync → route 读回字节；
  sharded 历史读回；坏 digest/路径/版本拒绝；重复路径先写拒绝；watch 不 activate/prune；
  route 默认不出站；CLI 从任意 CWD 显式 `--root` 运行。测试级断公网（非 loopback 直接
  失败），只用 tmp 目录与 loopback 随机端口。

静态 compose 检查（不 pull/build/up）：

```bash
cd asset-server && ASSETS_MIRROR_VERSIONS=1077100,1077600 docker compose config --quiet
```

## 边界

- 扁平 `objects/sha256/<digest>` 与历史 fan-out 路径**都可读**；本仓 writer 产出 flat +
  `runtime_path`，不迁移对象、不做 GC。
- 本目录不写 `generated/`、不写 `manifests/`、不碰 D1/NAS；`mltd-asset-updater` 只管理
  官方归档（`views/` + `current`），与 generated mirror 的树完全不相交。
- 生产切换（NAS 指向、单写者交接）未做，也不在本文件的范围内。
