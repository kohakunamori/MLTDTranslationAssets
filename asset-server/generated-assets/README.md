# MLTD generated-assets 分发闭包（NAS 侧，已部署）

本目录是 NAS 上 `mltd-generated-assets` 两个服务的**源码闭包**：它把本仓
`generated/<asset_version>/` 变成客户端可直接请求的只读 HTTP 面。

> 状态：**已部署并在跑**（2026-09-30 上线，2026-10-08 只读核对）。
> 与 `asset-server/` 的官方归档 updater 完全不相交：这里只读 `generated/`，
> 不写 `views/`、`current`、`index.sqlite3`，也不参与官方资源归档。

## 两个服务

| 服务 | 容器 | 作用 |
| --- | --- | --- |
| `generated-assets` | `mltd-generated-assets` | 只读路由：`assets_route.py serve`，只监听 `127.0.0.1:18765`；按 manifest 的 `runtime_path`/`logical_path` 读 CAS 对象，逐请求复验字节；未命中的对象只读回源同版本官方 CDN（缓存写在 CAS 之外） |
| `generated-assets-sync` | `mltd-generated-assets-sync` | 常驻同步：`sync_loop.py` 每 6 小时按当前 `main` HEAD 枚举 `generated/<数字版本>/`，校验 manifest 与 checksums 后写入 `published/<ver>/` 与 `objects/sha256/` |

公开路由（复用共享 `on-demand-nginx:18443`，不新开端口）：

```text
https://mltd-asset.nyaneko.cn:18443/generated-assets/<asset_version>/<logical_path>
   -> shared on-demand-nginx
   -> http://127.0.0.1:18765/assets/<asset_version>/<logical_path>
   -> /vol2/1000/imas-asset-archive/mltd/generated/published/<asset_version>/
```

## 为什么新版本会自动出现

`sync_loop.py` 不维护任何版本清单，也**不跟随任何 `current` 指针**：它每个 tick 在钉住的
head 上重新枚举 `generated/` 下的纯数字目录，因此一笔新版本只要构建成功并推到 `main`，
下一个 tick（≤6h）就会自动进入 `published/<asset_version>/`。已存在对象按 SHA-256 跳过下载，
所以重复 tick 是廉价的幂等操作。

## 文件与部署记录

| 本仓文件 | NAS 路径 | 说明 |
| --- | --- | --- |
| `sync_loop.py` | `<project_dir>/sync_loop.py` | 同步循环；只调用 `scripts/assets_mirror.py` 的实现，不复制第二实现 |
| `Dockerfile` | `<project_dir>/Dockerfile` | 镜像配方；构建上下文就是 NAS 上的 `<project_dir>` |
| `docker-compose.yml` | `<project_dir>/docker-compose.yml` | 两个服务的卷/环境/命令 |
| `../assets_route.py` | `<project_dir>/assets_route.py` | 只读路由（本仓 `asset-server/` 同一文件） |
| `../../scripts/assets_mirror.py` | `<project_dir>/scripts/assets_mirror.py` | mirror/校验实现（本仓唯一来源） |
| `../nginx-vhost.conf` | `<vhost_dir>/nginx-vhost.conf` | 共享 nginx 的项目 vhost（单文件 bind，需保 inode） |

`deployed.json` 逐文件记录**部署时**的 NAS SHA-256 与本仓 SHA-256，`test_closure.py`
离线校验两者一致、并拒绝没有说明的漂移。改这些文件后必须重新部署并更新记录，否则回归会红。

当前已记录的一处真实漂移：NAS 镜像里的 `scripts/assets_mirror.py` 是旧副本
（`08c82125…`），本仓是较新的 `1e39ebaa…`；用本仓模块驱动同一份 `sync_loop.py` 的
只读探针（枚举 head + `1077710` dry-run，零写入）通过，但**收敛这处漂移是一次显式部署**，
不属于本记录。

## 部署与核对

`asset-server/Deploy-GeneratedAssets.ps1` 是项目侧的部署/核对入口（默认只计划）：

```powershell
# 计划：对比两侧 SHA-256、检查容器与 nginx 配置，不写任何东西
pwsh -NoLogo -NoProfile -File asset-server/Deploy-GeneratedAssets.ps1

# 收敛：备份 → 上传并复验 → 重建镜像 → 用一次性容器在临时根上跑 --once 自证 →
#       nginx -t 后 reload → 重建两个服务 → loopback 探针
pwsh -NoLogo -NoProfile -File asset-server/Deploy-GeneratedAssets.ps1 -Apply
```

## 离线回归

```bash
python -m pytest asset-server/generated-assets -q
```

断言覆盖：不写 `current` 指针、版本发现来自 `generated/` 目录、只有 `build_status=success`
才镜像、非阻塞单写者锁、tick 失败不退出循环、路由只监听回环且挂载只读、官方回退缓存
不进 CAS、vhost 的 `/generated-assets/` 只代理回环且不接受写方法与 `current`。
