# Cloudflare 版 assets 网关（Worker 直取 GitHub）

线上地址：**https://mltd-asset-cf.nyaneko.cn**（同一个 Worker 也在 `mltd-assets.<sub>.workers.dev`，
但那个域名在中国大陆被污染，只有能解析它的网络才通）。

一个 Cloudflare Worker：**命中汉化包的请求，直接去 GitHub 仓库按提交号取（在 Cloudflare 边缘缓存住）；
没命中的请求，原样转发官方 CDN。** 它是 `asset-server/serve_release.py` + NAS nginx 那套的云端替身，
规矩一样：只保留最新版、命中给汉化、未命中给官方原版、永不拿别的版本顶替。

## 1. 为什么不存副本

翻译产物的字节本来就在 GitHub（这个仓库 `generated/objects/sha256/<digest>` 就是产物本身）。
Cloudflare 没法把仓库"挂"上去当硬盘，所以只有两种选择：**先搬一份**到自己这边，或者**用时去取**。
这里选后者——Worker 里带一张对照表，按需要去取，取过的由 Cloudflare 缓存。

| | 直取（本方案） | 先搬一份到 R2 |
| --- | --- | --- |
| 换一版要做什么 | 重新生成对照表 + 部署（几秒） | 把变化的包重新传一遍 |
| 存储 | 不占 | 每个版本一份 |
| 首次命中的延迟 | 多一次 GitHub 往返（几十到几百毫秒） | 直接命中 |
| 源站 | GitHub | Cloudflare R2 |

## 2. 它怎么决定"有没有"

Worker 里带一张表（`src/index.json`），内容是**请求路径 → 这个文件在仓库里的哈希名**。

| 步骤 | 行为 |
| --- | --- |
| 1 | 解析 URL 里的 `<version>`（没有就当作当前版本） |
| 2 | `<version>` 不等于表里那一版 → **直接转发官方**（绝不把 A 版的汉化包发给 B 版客户端） |
| 3 | 查表：命中 → 去 `raw.githubusercontent.com/<owner>/<repo>/<pinned_commit>/generated/objects/sha256/<digest>` 取 |
| 4 | 取回来的字节交给客户端，同时写进 Cloudflare 的边缘缓存（Cache API，1 年） |
| 5 | 没命中（或 GitHub 取不到）→ 转发 `https://td-assets.bn765.com/<version>/<path>` |

接受的 URL 形状（与现有 nginx vhost、`serve_release.py` 兼容）：

```text
/<version>/production/2018/Android/<name>   官方 CDN 形状
/assets/<version|current>/<path>            serve_release.py 形状
/generated-assets/<version>/<path>          现在对外公布的镜子形状
/cn/<version>/<path>                        CN 命名空间（仅路由别名，不含 legacy overlay 回落）
/<path>                                     当前版本
/healthz                                    自述（版本、提交、表大小、上游基址）
```

细节：

* **`HEAD` 不碰网络**：大小写在表里，直接回答。
* **`Range` 透传**：交给 GitHub 处理，不进缓存（避免把 206 当成完整对象缓存下来）。
* **`If-None-Match`**：ETag 就是内容的哈希，命中就回 304，不花任何上游请求。
* **逻辑名别名**：表里同时收了"哈希名"和"可读名"，指向同一份字节，两种请求都能命中。
* 断言：表里只会出现 `production/2018/Android/` 前缀下的路径；一条路径被两份不同内容同时认领会直接拒绝。

## 3. 缓存策略（汉化资源会更新，所以分三层）

| 层 | 策略 | 为什么 |
| --- | --- | --- |
| 客户端 ↔ 边缘 | `public, max-age=0, must-revalidate` + 强 ETag | 对外的 URL 不是永久的：**同一个资源版本号下可能重新发布汉化**（2026-10-11 就发生过，491 个歌词包换了字节）。所以带 HTTP 缓存的客户端每次都会问一句；没变就 304（不碰上游、不碰仓库），变了立刻拿新的 |
| 边缘 ↔ GitHub | Cache API，`max-age=31536000, immutable` | 取的地址里带着提交号，内容永不可能变；一个对象在一个数据中心只读一次 |
| 客户端本地 | **不归我们管** | 游戏按"资源版本 + 文件名"判断要不要重下。同一版本号下换了内容，已经下过的玩家不会自动重下——要清游戏缓存，或等官方资源版本号变化 |

更新一版汉化资源之后，线上要多久生效：**重新构建对照表 + 部署**（见下）之后立刻生效，不需要等缓存过期。

## 4. 怎么跟上更新

**自动（推荐）**：`.github/workflows/cloudflare-gateway.yml`。上游 "Build generated Assets" 一跑完，
它就重建对照表、部署、再跑一遍线上逐字节验收。发布提交带 `[skip ci]`，所以它挂在"上游流程完成"上，
而不是挂在 push 上。需要仓库机密 `CLOUDFLARE_API_TOKEN`（权限：Edit Cloudflare Workers），
建议一并加 `CLOUDFLARE_ACCOUNT_ID`；**没配令牌时它只打一条警告并跳过，不会把 CI 变红**。
也可以手动点 "Run workflow"。

**手动**：

```bash
git pull                                   # 先让本地和 origin/main 一致
python asset-server/cloudflare/build_index.py            # 先看计划（只读，不写任何东西）
python asset-server/cloudflare/build_index.py --apply    # 写 src/index.json
cd asset-server/cloudflare && npx wrangler deploy        # 部署
```

生成前逐条校验：manifest 必须 `build_status=success`、每个 `artifact_sha256` 必须在本地对象库里存在且
**哈希对得上**、路径不能越界、不能一条路径对应两份内容；任何一条不满足就整体退出、不写文件。
锚定的提交号取"最后一次改动 `generated/<version>/manifest.json` 的那个提交"（用 `git log` 解析）。

## 5. 验收

```bash
# 直连自定义域（大陆可用）
python asset-server/cloudflare/check_gateway.py --base https://mltd-asset-cf.nyaneko.cn

# 从大陆的机器上验 workers.dev 需要代理（域名被污染）
python asset-server/cloudflare/check_gateway.py --base https://mltd-assets.<sub>.workers.dev \
    --proxy http://127.0.0.1:7890
```

覆盖：`/healthz`；**新鲜度**（线上提交 vs 本地对照表 vs 仓库最新提交——不一致就报错提醒"改了没部署"）；
抽查汉化包字节与 `artifact_sha256` 一致；第二次读必须命中边缘缓存；逻辑名别名同字节；`HEAD`/`Range`；
**从官方目录里挑本版没有的文件**，比对直连官方拿到的字节一致；别的版本不被本地命中；路径穿越/写请求被拒。

## 6. 边界与风险

* **`*.workers.dev` 在中国大陆不可用**（DNS 被污染）。自定义域 `mltd-asset-cf.nyaneko.cn` 直连实测可用，
  客户端应当用这个域名。
* **GitHub 是源站**：仓库限速或抖动时请求会**自动回落官方原版**——客户端不会坏，只是那一刻拿到未汉化文件
  （响应头 `x-mltd-asset-source: official-repository-unavailable`）。
* **官方转发不落盘、不缓存**：这个 Worker 只决定"哪一端来答"。
* **边缘缓存按数据中心独立**：不同地区各会向 GitHub 取一次（每个对象、每个节点一次），之后长期命中。
* **免费额度**：Worker 100,000 请求/天；本方案不占 R2 存储。Cloudflare 条款允许把大文件放在自家服务
  （R2/Stream/Images）再经 CDN 分发，而"缓存在 Cloudflare 之外的大文件"属于受限的那一类；本项目量级很小，
  但这条要知道。
* **环境依赖**：`check_gateway.py` 的"官方目录"抽查需要 `msgpack`（`asset-server/requirements.txt` 已列）。
* 响应头自查：`x-mltd-asset-source`（哪一端答的）、`x-mltd-cache`（hit/miss/bypass）、`x-mltd-build`（部署的提交号）。

## 7. 本次实测（2026-10-11）

* 对照表：**22,495 条路径**（11,248 哈希名 + 11,247 逻辑名），描述 **672,569,567 字节（约 641 MiB）** 对象载荷；
  `src/index.json` 3.68 MiB（压缩后约 1.10 MiB，免费版上限 3 MiB），Worker 启动 10–19 ms。
* 锚定提交：`eae9a5ede1d836acf8a416738b180fd09a763973`（`origin/main`）。
* 验收 26 项全过；另单独验过 10.5 MB 大对象（先 miss 后 hit，字节与哈希一致）。
* 缓存实测：直连自定义域第一次 `x-mltd-cache: miss`，第二次 `hit`；带相同 ETag 再问 → `304 Not Modified`。
