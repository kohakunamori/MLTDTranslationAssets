# MLTD 资产服务（本仓自足闭包）

本目录是 MLTD 汉化 Assets 通道的**最小运行闭包**：镜像、CLI 与只读路由只依赖
本仓（`D:\Project\_MLTDTranslationAssets`）内的源码，不依赖主仓 mltd-current、
不依赖任何兄弟仓库或 `PYTHONPATH` 回退。

> 状态（2026-10-08 只读核对）：
> - **已在生产运行的**：`generated-assets/` 的 `mltd-generated-assets`（loopback 只读路由
>   `127.0.0.1:18765`）与 `mltd-generated-assets-sync`（每 6h 自动同步 `generated/`），
>   经共享 `on-demand-nginx:18443` 以 `/generated-assets/<ver>/…` 对外提供；
> - 本目录的官方归档侧模板（`docker-compose.yml` / `Dockerfile` 的
>   `mltd-asset-updater`、`mltd-assets-mirror`）仍是**候选**，NAS 上跑的是归档镜像
>   `local/imas-mltd-asset:20260914-static`；
> - `generated/` 生成侧 CI（`assets-generated.yml` → `scripts/build_generated_release.py`）
>   仍是唯一的正式 writer，本目录只是它的消费/分发端，不写 `generated/`。

## 组成

| 路径 | 角色 |
| --- | --- |
| `assets_route.py` | 只读 HTTP 分发（`serve`/`list`），按 manifest 的 `runtime_path` / `logical_path` 读取 CAS 对象，逐请求校验字节哈希；默认不出站（官方回退只有显式给 `--official-base-url` / `--official-root` 才启用，且 local root 优先） |
| `Dockerfile` | 逐文件 COPY 的自足镜像（见下「闭包清单」） |
| `docker-compose.yml` | 三个服务的静态部署模板：`mltd-asset-tools`（profile `tools`）、`mltd-asset-updater`、`mltd-assets-mirror`；`ASSETS_MIRROR_VERSIONS` 缺失即拒绝启动 |
| `asset-version` | 官方归档控制 wrapper（`tools/asset_version.py`，经 docker compose `tools` profile） |
| `switch-version.sh` | 兼容 wrapper：`activate --version <v>` |
| `requirements.txt` | `msgpack`、`requests` |
| `generated-assets/` | NAS 已部署的 generated 分发闭包：`sync_loop.py`（自动发现版本）、`Dockerfile`、`docker-compose.yml`、`deployed.json`（部署字节哈希记录）、`test_closure.py`；详见该目录 README |
| `nginx-vhost.conf` | 共享 nginx 的项目 vhost 副本（`/assets/`、`/cn/`、`/generated-assets/`） |
| `Deploy-GeneratedAssets.ps1` | 项目侧部署/核对入口（默认只计划；`-Apply` 才收敛） |

服务入口（compose）：

- `mltd-assets-mirror` → `scripts/assets_mirror.py watch --asset-version …`
  （只同步列出的版本；不跟随 latest、不写 `current.json`、不删除任何版本）
- `mltd-asset-updater` → `tools/archive_controller.py watch`
- `mltd-asset-tools` → `tools/versioned_assets.py`（按需 run）

## 归档绑定阶段的性能约定

一个版本要绑定 ~168,390 个对象，绑定阶段曾是整条同步链的瓶颈（实测 3.8 → 19 → 21 对象/秒；
1077720 这一版全流程 `duration_seconds: 1155`）。先记住结论：**这一阶段受限于归档池的小随机
写，不是 SQLite**——同样 4,000 条绑定在 tmpfs 里只要 0.06–0.10 s（40,912–67,394 对象/秒），
在池子上要 33–1080 s。因此所有优化都围绕「减少写出去的页数」：

- `sync` 把下载/复用池拆成「并行 `Client.resolve()` + 主线程批量落库」：worker 只做探测、
  跨版本复用判定与 CAS 发布（`commit_part`），**不写库**；`fetch()` 仍是「resolve + 立刻 bind」
  的单对象封装，`serve`/`verify`/一次性调用不受影响。
- 落库顺序按主键递增：work list 先排序，按 `--bind-window`（默认 20000）分窗口并行解析，
  **整窗收齐后排序**再按 `--bind-batch`（默认 2000）连续下发。**不要**按完成顺序切批：下一批
  会重新弄脏上一批刚写过的叶子页（实测 ~18.7 KB 物理写/行 vs 排序窗口 ~5.4 KB/行；tmpfs 里
  40,912 vs 67,394 对象/秒）。`SyncWindowTests` 在 `bind_objects` 真正执行的 UPDATE 顺序上
  钉住这条不变式。
- `bind_objects()` 在**一个事务**里按主键顺序 `executemany` UPDATE（外加同事务的 checksum
  UPSERT）。逐对象一个事务时每个对象要付 35 KB 物理写（一版 ~5.9 GB），而池子随机写只有
  ~1 MB/s —— 这才是「~105 分钟」的来源。
- `entries` 上**没有二级索引**（`UNUSED_ENTRY_INDEXES`）。绑定会改写 `sha256`，而每个以内容
  哈希为键的索引都意味着「每行一次随机叶子页写」（多百万条目的索引），这正是批量排序之后
  仍然只有 ~34 对象/秒的原因。三个索引在本树中都没有查询使用者（`object_by_md5()` 读
  `object_checksums`；`stats()` 的 DISTINCT 走主键前缀）。`_init_db` 不再创建它们，
  已有库用下面的维护命令清理。
- 绑定期 `bulk_writes()` 把 WAL autocheckpoint 阈值抬到 `BULK_AUTOCHECKPOINT_PAGES`
  （10000 页 / 40 MB），写连接另设 `JOURNAL_SIZE_LIMIT`（64 MB）。**注意**：并发只读连接会一直
  握着读标记，PASSIVE checkpoint 无法回收 WAL，因此 WAL 可能长期停在阈值之上；索引 DDL 之类
  的大事务更容易把它推到几百 MB（实测 310–673 MB），之后每次 commit 都要做一次巨额
  checkpoint，速率会掉回 ~20 对象/秒。遇到这种情况先停 updater 跑
  `PRAGMA wal_checkpoint(TRUNCATE)`（实测 310 MB → 0，0.1–30 s）。

一次性索引维护（**不要**放进 store 构造路径：在现网 2 GB 库上它会读几十 GB、跑十几分钟，
并阻塞每一次 sync）：

```bash
docker run --rm --entrypoint python -v /vol2/1000/imas-asset-archive/mltd:/data \
  local/imas-mltd-asset:20260914-static \
  /app/tools/versioned_assets.py maintenance --root /data            # 只看，不删（dry run）
docker run -d --name mltd-drop-index --entrypoint python \
  -v /vol2/1000/imas-asset-archive/mltd:/data \
  local/imas-mltd-asset:20260914-static \
  /app/tools/versioned_assets.py maintenance --root /data --drop-unused-entry-indexes
```

现网实测：`idx_entries_scope_name_version` 150.4 s、`idx_entries_sha256` +
`idx_entries_verify_cover` 1024.8 s（读 25 GB），删完 `freelist` 回收 ~857 MB
（`quick_check` 仍为 `ok`，解析器只读视图目录/`manifest.json`，不看 `entries`）。

## 数据库需要定期 VACUUM

绑定是**原地 UPDATE**，每个版本都给同一个版本的 168k 行做一次改写，加上索引删除留下的
空闲页，`entries` 的行很快就不再连续：`stats()` 里那条「扫一遍该版本的行」的查询会退化成
**每行一次随机页读**。现网实测（1077720）：

| | 维护前 | `VACUUM` 后 |
| --- | --- | --- |
| `page_count` / `freelist` | 535,823 / 216,700 | 297,572 / **0** |
| 文件大小 | 2093 MB | **1162 MB** |
| `store.stats()` | ~50–108 s | **0.82 s** |
| `sync` 固定开销（500 条 smoke） | 159 s | **0.735 s** |

`VACUUM` 现网耗时 **203.8 s**，`quick_check` 仍为 `ok`。它只阻塞归档工具（updater 必须停），
对外分发走 nginx `alias` 直读 `views/`，不受影响。碎片会随版本继续累积，所以这不是一次性
操作——`sync` 的固定开销再次抬头（或 `freelist` 又到几百 MB）时重跑即可。

```bash
cd /vol1/1000/appdata/imas/mltd-asset/asset-server && docker compose stop mltd-asset-updater
docker run --rm --entrypoint python -v /vol2/1000/imas-asset-archive/mltd:/data \
  local/imas-mltd-asset:20260914-static -c "
import sqlite3, time, os
con = sqlite3.connect('/data/index.sqlite3', timeout=3600); con.execute('PRAGMA busy_timeout=3600000')
con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
t = time.time(); con.execute('VACUUM'); print('vacuum %.1fs' % (time.time() - t))
print('page_count=%d freelist=%d size=%.0fMB' % (con.execute('PRAGMA page_count').fetchone()[0],
      con.execute('PRAGMA freelist_count').fetchone()[0], os.path.getsize('/data/index.sqlite3')/1048576))
print('quick_check:', con.execute('PRAGMA quick_check').fetchone()[0])
"
docker compose start mltd-asset-updater
```

部署提醒：本地镜像换了 tag 不变时，`docker compose up -d` 和 **`docker compose start`
都不会重建容器**（后者会继续用旧容器里的旧代码，`_init_db` 会把删掉的索引建回来）。
必须 `docker compose up -d --force-recreate mltd-asset-updater`，并用
`docker inspect imas-mltd-asset-updater --format '{{.Image}}'` 与
`docker images --no-trunc local/imas-mltd-asset:20260914-static` 核对镜像 ID。

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
