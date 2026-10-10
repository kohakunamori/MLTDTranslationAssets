# Cloudflare 版 assets 网关（Worker 直取 GitHub）

一个 Cloudflare Worker：**命中汉化包的请求，直接去 GitHub 仓库按提交号取（在 Cloudflare 边缘缓存住）；
没命中的请求，原样转发官方 CDN。** 它是 `asset-server/assets_route.py` + NAS nginx 那套的云端替身，
客户端把资源基址指到这个域名就能用。

> 状态：**已部署并实测**（2026-10-11）。实测结论见文末。

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

接受的 URL 形状（与现有 nginx vhost、`assets_route.py` 兼容）：

```text
/<version>/production/2018/Android/<name>   官方 CDN 形状
/assets/<version|current>/<path>            assets_route.py 形状
/generated-assets/<version>/<path>          现在对外公布的镜子形状
/cn/<version>/<path>                        CN 命名空间（仅路由别名，不含 legacy overlay 回落）
/<path>                                     当前版本
/healthz                                    自述（版本、提交、表大小、上游基址）
```

细节：

* **`HEAD` 不碰网络**：大小就写在表里，直接回答。
* **`Range` 透传**：交给 GitHub 处理，不进缓存（避免把 206 当成完整对象缓存下来）。
* **`If-None-Match`**：ETag 就是内容的哈希，命中就回 304，不花任何上游请求。
* **逻辑名别名**：表里同时收了"哈希名"和"可读名"两种路径，指向同一份字节，所以两种请求都能命中。
* 断言：表里只会出现 `production/2018/Android/` 前缀下的路径；一条路径被两个不同内容同时认领会直接拒绝。

## 3. 只保留最新版

`src/index.json` 只描述**一版**，这是"只保留最新版"的直接含义：

* 客户端请求别的版本 → 第 2 步直接转发官方（它拿到的是自己那一版的官方原版）。
* 换版流程：

```bash
python asset-server/cloudflare/build_index.py            # 先看计划（只读，不写任何东西）
python asset-server/cloudflare/build_index.py --apply    # 写 src/index.json
cd asset-server/cloudflare && npx wrangler deploy        # 部署
```

* 计划默认**只读**：`--apply` 之前不写文件。
* 生成前逐条校验：manifest 必须是 `build_status=success`、每个 `artifact_sha256` 必须在本地对象库里存在且**哈希对得上**、
  路径不能越界、不能一条路径对应两份内容。任何一条不满足就整体退出。
* 锚定的提交号取"最后一次改动 `generated/<version>/manifest.json` 的那个提交"（用 `git log` 解析），
  这样取字节的 URL 与表里描述的内容永远是同一份。**先在本地 `git pull` 到与 `origin/main` 一致再生成。**

## 4. 部署与验收

```bash
# 一次性：确认已登录且能拦住错误版本
npx wrangler whoami
cd asset-server/cloudflare && npx wrangler deploy

# 线上验收（真实 HTTPS，逐字节比对本仓对象库）
python asset-server/cloudflare/check_gateway.py --base https://mltd-assets.<subdomain>.workers.dev
```

在中国大陆的机器上跑验收要给 `--proxy`（`*.workers.dev` 的域名被污染，直连不通）：

```bash
python asset-server/cloudflare/check_gateway.py --base https://... --proxy http://127.0.0.1:7890
```

验收覆盖：`/healthz` 版本正确；抽查汉化包字节与 `artifact_sha256` 一致；第二次读必须命中边缘缓存；
逻辑名别名返回同样字节；`HEAD`/`Range` 正确；**从官方目录里挑本版没有的文件**，比对直连官方拿到的字节一致；
别的版本不会被本地命中；路径穿越/写请求被拒。

## 5. 边界与风险

* **`*.workers.dev` 在中国大陆不可用**：域名被 DNS 污染（实测解析到假 IP、TCP 超时）。
  要真给客户端用，必须绑定自有域名（Cloudflare 控制台 → Worker → Settings → Domains & Routes →
  Add custom domain），或者客户端侧走代理。
* **当前 OAuth 令牌没有 DNS 写权限**，所以 `wrangler` 不能替我们建自定义域；`wrangler.jsonc` 里留了注释掉的
  `routes` 写法，授权后取消注释即可。
* **GitHub 是源站**：仓库限速或抖动时，请求会**自动回落官方原版**——客户端不会坏，只是那一刻拿到未汉化的文件
  （响应头 `x-mltd-asset-source: official-repository-unavailable` 可以看出来）。
* **官方转发不落盘、不缓存**：这个 Worker 只决定"哪一端来答"，不在中间存官方字节。
* **边缘缓存是按数据中心的**：不同地区的节点各自会向 GitHub 取一次（每个对象、每个节点一次），之后长期命中。
* **首次命中慢一点**：某个对象第一次被访问时多一次 GitHub 往返；已经被缓存过的就快。
* **免费额度**：Worker 100,000 请求/天；本方案不占 R2 存储。Cloudflare 条款允许把大文件放在自家服务（R2/Stream/Images）
  再经 CDN 分发，而"缓存在 Cloudflare 之外的大文件"属于受限的那一类；本项目量级很小，但这条要知道。

## 6. 本次实测（2026-10-11）

* 对照表：**22,495 条路径**（11,248 哈希名 + 11,247 逻辑名），描述 **661 MB** 对象载荷；
  `src/index.json` 3.68 MiB（压缩后约 1.10 MiB，免费版上限 3 MiB），Worker 启动 10 ms。
* 锚定提交：`eae9a5ede1d836acf8a416738b180fd09a763973`（`origin/main`）。
* 验收 26 项全过：抽查汉化包字节与哈希一致；第二次读 `x-mltd-cache: hit`；冷对象先 `miss` 后 `hit`；
  逻辑名别名同字节；`HEAD` 长度正确；`Range` 返回 206 且 `Content-Range` 正确；
  从官方目录挑 3 个未汉化文件，经网关取到的字节与直连官方**逐字节相同**；别的版本被转发官方；写请求被拒。
