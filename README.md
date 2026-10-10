# MLTD 简体中文汉化开源资源库 (THE IDOLM@STER MILLION LIVE! THEATER DAYS Localization Assets)

这是一个个人业余项目，用于整理《偶像大师 百万现场 剧场时光》(MLTD) 的简体中文汉化资源。
仓库中的文本、术语表、歌词和贴图 Manifest 会持续更新，欢迎通过 GitHub PR 参与修订。

本仓库只承载**经 assets 服务器下发的面**。正式生成物通过显式版本的 `/generated-assets/<asset>/` 路由提供，历史 `/cn/<asset>/` overlay 仍保留用于兼容与回滚。底栏贴图、BI 文案与字体属于
APK 内置面，由配套仓库 [MLTDTranslationClient](https://github.com/kohakunamori/MLTDTranslationClient)
维护。

仓库同时保存可审阅的翻译源和 CI 生成的 Unity3D 二进制。官方日版资源不直接复制进 Git：
公开 GitHub Actions 在构建时按 manifests/asset-version.json 从官方 assets-server 下载基线，
只把经过源哈希校验的汉化结果写入 generated/。生成物使用内容寻址存储，跨版本相同二进制只保留一个对象。
构建失败不会创建或覆盖 generated/<asset_version>/。

## 目录结构

- `locales/`：核心业务文本库（UTF-8 JSONL，单行精确定位）
  - `locales/story/`：活动剧情、主线剧情、特别剧情
  - `locales/card/`：卡片觉醒剧情、通常剧情、卡片短信
  - `locales/dialogue/`：偶像触碰台词、常驻问候、工作对话、演出结算台词
  - `locales/birth/`：偶像生日剧情与白板问候
  - `locales/master/`：Master 核心主数据表、菜单UI、卡片技能、系统提示
- `lyrics/`：全曲目歌词库（491 首歌曲对齐双语歌词与时间戳；官方 `scrobj_*` 包共 492 个，其中开发自测包 `scrobj_00test` 不含歌词行、已排除）
  - `lyrics/songs/`：按歌曲独立分轨 JSONL
  - `lyrics/all_lyrics.jsonl`：全曲歌词总汇
  - 说明：歌词源库随官方更新自动扩充（`scripts/refresh_lyrics_catalogue.py`）；**已采纳中文的歌曲会在发布构建时被写回资源包并随版本发布**（`scripts/build_lyric_overlay.py`），未翻译的行保持官方日文不动。
- `glossary/`：翻译规范与标准术语
  - `glossary/authoritative-terms.json`：项目当前采用的固定译名与避免词
  - `glossary/idols.json`：项目整理的 52 名偶像与声优名录
- `manifests/`：贴图元数据清单（文字在库，多媒体外链）
  - `manifests/images.manifest.json`：937 张已汉化贴图的 SHA-256 索引
  - `manifests/localizable-bundle-families.json`：哪些官方资源包属于「可汉化面」（文本包、歌词包），新面必须先在这里声明
  - `manifests/official-asset-inventory.json`：官方 16.8 万个包的家族签名基线，用于告警「出现了从未见过的资源类型」
- `pipelines/`：自动化汉化与生成流水线工具集
  - `pipelines/text/`：全量文本提取、加密 GTX 解密/回写、多模型并发翻译与自动化质检流水线
  - `pipelines/image/`：Sprite Atlas 几何重组、`gpt-image-2.5-sunburst` 图像重绘、`gpt-5.6-luna` 视觉审查与 ASTC 纹理回填流水线
  - `pipelines/export/`：数据分发与 GitHub 规范导出工具
- `schema/`：数据规范与 JSON Schema 定义

generated/ 保存 CI 生成的 Unity3D：objects/sha256/ 负责跨版本内容寻址去重，generated/<asset_version>/manifest.json 与 checksums.txt 描述可分发对象。每个翻译 bundle 同时记录 `logical_path` 与可选的 `runtime_path`；后者来自官方 `.data` 目录中的哈希文件名，是客户端实际请求的路径。`.data` 官方目录本身也会进入生成发布，未翻译的其他运行时 bundle 由 NAS 只读回源官方 assets-server。

## 条目格式规范

每一行 JSONL 严格遵循以下规范：

```json
{
  "asset_version": "1077500",
  "client_version": null,
  "source_client_version": "9.0.200",
  "bundle": "event_0448_story_06_jp.gtx",
  "item_key": "event_0448_story_06_title",
  "source_sha256": "ea4cef9ff36d07f10f6bd00f4163edfa882ccd469392bc96127a9b2b6b45ae7f",
  "ja": "本領発揮",
  "zh": "大显身手",
  "status": "accepted",
  "updated_at": "2026-09-27T00:00:00Z"
}
```

### 关键约束
1. **独立版本轴**：条目的身份是纯数字的 `asset_version`（assets 轴），`client_version` 在本轴恒为 `null`；`source_client_version`（`X.Y.Z`）只作溯源，不是身份的一部分。三者不得拼接成 `9.0.200+1077500`、`client-9.0.200-assets-1077500` 之类的组合串，组合字段 `base_version` 已废除。
2. **防止版本漂移**：`source_sha256` 必须与 `ja` 原文字符串的 SHA-256 强校验匹配。
3. **安全隔离控制符**：客户端引擎使用 `|` 和 `^` 作为底层控制分隔符。**严禁在译文 `zh` 中输入半角 `|` 或 `^`**（可使用全角 `｜` 或 `＾`）。
4. **状态说明**：
   - `untranslated`：待翻译条目，`zh` 为空字符串。
   - `pending`：已生成初稿或机器翻译，尚未进入构建。
   - `accepted`：允许进入 `generated/<asset_version>/` 构建的译文。

`status` 只回答「这一行能不能进构建」，**文本出处**由 `translation_stage` 记录：
`untranslated` → `llm_translated` → `human_translated`。因此 `accepted` 有两种合法
出处：`accepted + human_translated`（人工审校）与 `accepted + llm_translated`（机翻
直接发布）。后者由 `scripts/llm_translate_untranslated.py publish` 产生，**不会**被
改写成 `human_translated`，发布产物始终可追溯到「这段文字是机器产出的」。

## Web 翻译门户与在线协同

您可以通过社区翻译门户直接在线认领翻译与审校：
- **Web 门户**：https://mltd-translate.nyaneko.cn
- **提交 PR**：欢迎在 GitHub 直接提交 Pull Request，CI 机器人将对每一行的数据完整性进行自动化检测。

合并后的资源会自动进入发布链路：

1. `validate-localization.yml` 在 PR 检查工作区预演状态晋级，并验证源 hash、控制符和占位符；
2. PR 合并到 `main` 后，`assets-generated.yml` 只对本次 diff 中有译文的 `pending/untranslated` 行标记为 `accepted`；
3. 同一次 CI 运行调用 `scripts/build_generated_release.py`，生成并校验 `generated/<asset_version>/`；
4. 机器人把状态晋级和 `generated/` 一起提交回 `main`，提交带 `[skip ci]`，不会自触发循环；
5. `sync-to-portal.yml` 随 `locales/` 变更同步 Portal 索引。

定时 LLM 路径（`llm-translate-assets.yml`）走同一条构建链路，只是晋级与派发都是它
自己做的：`apply`（写 `pending/llm_translated`）→ `publish`（晋级为 `accepted`，
保留出处）→ 提交 `main` → `workflow_dispatch` 触发 `assets-generated.yml`。两条路径
共用同一个 `generated/` writer，因此仍只有一个 writer。

同一支工作流还负责「把新资源纳入范围」。每天按官方索引做三步：

1. `discover_official_bundles.py` 报告「属于已声明家族、但仓库还没有」的包与新出现的
   资源类型，并把家族基线写进 `manifests/official-asset-inventory.json`；
2. `refresh_latest_official_catalogue.py` 按 `manifests/localizable-bundle-families.json`
   的家族匹配下载新文本包（不再从 `locales/` 反推，避免「自己证明自己」）；
3. `refresh_lyrics_catalogue.py` 抽取新歌歌词到 `lyrics/songs/`，并重算
   `all_lyrics.jsonl` 与 `lyrics_manifest.json`。

翻译完成后，发布构建（`assets-generated.yml`）除了把文本包写回，还会调用
`build_lyric_overlay.py`：把所有「有已采纳中文」的歌曲的官方歌词包改写中文、放进同一个
覆盖层并追加到发布清单。因此手机端拿到的歌词包和文本包走完全相同的分发路径，
新版本发布时自动更新，不需要人工步骤。

这些步骤都 fail-closed：单次数量或字节数超过上限（发现 400 个/1 GiB，歌词刷新 200 个/
512 MiB，歌词打包 600 首/256 MiB）就报错退出，不提交；未声明家族的包只报告、不下载。
官方 index 里占了绝大多数包数的家族（语音音频、动画曲线、图片，共 326 个家族 / 1.88 万个包）
已抽样确认不含可翻译文本，登记在 `reviewed_unclassified` 后不再计入告警；其余长尾家族仍照旧
计数，清单与证据见 `docs/ARCHITECTURE.md` 的「未纳入面审计」。

普通协作翻译仍以 GitHub PR 合并作为审核入口；LLM CI 是独立路径：它先把结果直接
写入 `main` 并保留 `llm_translated` 标记，随后由 `llm_translate_untranslated.py
publish` 把 `pending + llm_translated` 的行原地晋级为 `accepted`（出处仍是
`llm_translated`），提交后用 `workflow_dispatch` 显式触发 `assets-generated.yml`
——`GITHUB_TOKEN` 的 push 不会触发其他工作流，不派发的话新译文会一直等到某次无关
推送才被构建。想回到「只人工审校」：把仓库变量 `MLTD_PUBLISH_LLM_DRAFTS` 设为
`false`，或手动触发时传 `publish_drafts=false`。构建或源校验失败时不会提交
`generated/`。

## 配套资产服务（asset-server/）

`asset-server/` 是本仓**自足**的最小运行闭包（本仓内唯一 writer 的消费端）：

- `assets_mirror.py`（本仓 `scripts/`）：把 `generated/` 的已发布版本显式同步到独立
  mirror root（`objects/sha256/<digest>` + `published/<ver>/…`）；只同步，不激活、不清理
  （`activate`/`prune` 是单独的显式动作）。
- `assets_route.py`：只读 HTTP 分发（loopback），按 manifest 的 `runtime_path` /
  `logical_path` 读取并逐请求复验对象字节。
- `generated-assets/`：NAS 上**已部署**的 generated 分发服务闭包
  （`mltd-generated-assets` 只读路由 + `mltd-generated-assets-sync` 常驻同步）。
  同步每 6 小时按 `main` HEAD 自动发现 `generated/<asset_version>/`，所以新版本不需要
  任何人工登记；`deployed.json` 记录部署的 NAS 字节哈希，`test_closure.py` 离线钉住契约。
- `nginx-vhost.conf`：共享 nginx（`on-demand-nginx:18443`）的项目 vhost 副本，含
  `/generated-assets/<ver>/…` 与 `/cn/<ver>/…` 到 loopback resolver 的映射。
- `docker-compose.yml` / `Dockerfile`：官方归档侧三个服务的静态部署模板；构建
  上下文为本仓根，不依赖任何兄弟仓库。运行与回归入口见
  [`asset-server/README.md`](asset-server/README.md)。

## 许可证与致谢

本仓库文本基于游戏日版文本翻译与整理，版权归 Bandai Namco Entertainment Inc. 所有。
汉化成果遵循社区开源共享协议，严禁用于任何商业用途。
