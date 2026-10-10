# HANDOFF — MLTD 汉化资源 GitHub 公开仓库分类导出 (Phase 1)

- **stream**: text-localization
- **run**: `build\runs\text-localization\9.0.200\github-export-candidate`
- **artifact_status**: **candidate**
- **base_version**: `9.0.200+1077500`
- **导出时间**: `2026-09-27T00:00:00Z`

## 1. 导出概览
- **待译全集总行数**: 391618 行，覆盖 5 大业务分类（51 个 Bundle）。
  - `locales/master/`: 206136 行 (2480 bundles)
  - `locales/card/`: 58132 行 (2997 bundles)
  - `locales/story/`: 119229 行 (3657 bundles)
  - `locales/birth/`: 6647 行 (1605 bundles)
  - `locales/dialogue/`: 1474 行 (1077 bundles)
- **翻译覆盖度**:
  - `accepted` (已采纳正式译文): 391618
  - `pending` (待人工审校机翻初稿): 0
  - `untranslated` (待翻译空槽): 0
- **控制符清洗**:
  - 自动拦截/转义保留控制符 `|` 与 `^` 共计 0 行。
- **歌词与曲目**:
  - 432 首歌曲分轨，12131 个歌词槽，已翻译 11065 槽（纯英文保留不翻 1066 槽）。
- **贴图 Manifest**:
  - 937 张贴图索引，支持 GitHub Release Assets / Cloudflare R2。
- **名词表**:
  - 90 个官方权威术语 + 52 偶像标准名录。

## 2. 2026-10-10 更新：新包发现 + 歌词自动抽取已接入

本节是**当前状态**，上面第 1 节是 2026-09-27 那次导出的历史快照，两者数字不同属正常。

- 新增能力：`scripts/discover_official_bundles.py`（新包发现，读官方索引，按声明式家族匹配）、
  `scripts/refresh_lyrics_catalogue.py` + `pipelines/text/mltd_localize_scrobj.py`（歌词抽取与合并）。
  每日工作流 `llm-translate-assets.yml` 依次跑：发现 → 抽文本 → 抽歌词 → 翻译 → 发布。
- 已补录 asset 1077720 漏掉的内容（此前从 `locales/` 反推范围，导致新包永远不可见）：
  - 文本：新增 **77 个 bundle、1,532 行**（`locales/master/official-1077720-untranslated.jsonl`），全部为 `untranslated` 待译。
  - 歌词：新增 **59 首歌曲、1,598 槽**（其中日文 1,406 槽待译、纯英文保留 192 槽）。
- 当前总量：文本 395,673 行（已采纳 **395,673** / 待译 0）；歌词 491 首 / 13,729 槽
  （已采纳 **12,457** / 日文待译 1,272；其中 13 行是模型返回不可用草稿被规则拒收，留待下次运行）。
  原有 432 首的逐曲文件与 `english_bypass` 口径均未被改动。
- **歌词已接入发布**（2026-10-10 晚）：`scripts/build_lyric_overlay.py` 在每次发布构建里
  把「有已采纳中文」的歌曲的官方 `scrobj_*` 包改写中文，写进与文本包相同的覆盖层、
  发布清单与对象库，随版本自动更新。写入前逐位置回读校验；只有 `accepted` 且
  `source_sha256` 与原文一致的行会被使用，未翻译行保持官方日文。上限 600 首 / 256 MiB。
- **实测结果（asset 1077741 已发布）**：`bundles_patched=491`、`slots_patched=12,447`、
  `songs_without_translation=0`、`accepted_rows_not_applied=0`；发布清单 11,249 条，
  其中 `scrobj_*` **491 个**、texture 231 个。此前的发布里 `scrobj_*` 为 **0 个**。
  用户最初报的 `scrobj_ittana`（「一旦愛して」）现在指向
  `production/2018/Android/310c6889a525095e3cd3455aee18305d3594abec.unity3d`。
- **图片面（同一套发布路径）**：`image-localize-assets.yml` 产出的覆盖层被发布构建合并
  （`--require-images` + `--image-overlay-manifest/--image-overlay-root/--image-index`），
  1077741 发布 **231 个图片包**（全部 `exact` 复用），覆盖 `manifests/images.manifest.json`
  里 937 张已审图的 191 个包，**图片侧无积压**。注意两件事：① 图片**生产**仍是离线人工
  （发现/重绘/审校都不自动）；② 覆盖层的 `real_client_verified=False` + `client_cache_cleared=False`
  是实话——真机效果仍未验证，且「同名清单 + 同名资源名」策略可能命中客户端旧缓存。
- **`client_version` 的读法**（2026-10-10 澄清）：`manifests/asset-version.json` 里的
  `9.0.200` **不是**「应该跟着客户端升级的版本号」，而是「`locales/` 里的行是从哪个客户端抽出来的」
  这条来路记录。资源轴与客户端轴相互独立：发布清单写 `client_version: null`，只把该值作为
  `source_client_version` 附带；拼接式身份（如 `9.0.200+1077100`）会被直接拒绝。
  当天官方客户端已发到 **9.0.300**，但 395,673 行文本全部来自 9.0.200，所以清单写 9.0.200 是对的。
  只有**重新抽取文案**时才应更新它。缺一处：13,729 行歌词没有这个字段（文本行有）。
- **只能由真机确认的部分**：发布清单、字节哈希、逐槽回读只能证明「包是对的」，**不能**证明
  「手机上看到的是新的」——歌词/图片是否显示中文、客户端是否真的取到新包，都要在手机上看；
  图片那侧还要先清游戏缓存（覆盖层用的是「同名清单 + 同名资源名」策略，可能命中旧缓存）。
  镜像另有时间差：NAS 每 6 小时才重新发现 `generated/<version>/`，`activate` / `prune` 是显式动作。
  判定表见 `docs/ARCHITECTURE.md` 8.5。
- 待译内容会在下一次定时任务被自动翻译（`--scope all` 同时覆盖文本与歌词）。
- 覆盖面审计（2026-10-10）：官方 16.8 万个包里，除上述两个文本面外**没有**其他可翻译文本。
  抽样解包证据：`.acb` 家族（约 2,700 包 / 3 GB）是 Criware 语音音频（`@UTF` 头）；
  `fhout_event_*_story_*.json`（1,515 包）是「TextId 引用 + 口型动画曲线」，12 个样本
  0 个假名/汉字；`blog#`、`*_info`、`tutorialinfo#`、`titlebg_#` 等图片家族只有
  Sprite/Texture2D、0 个字符串（画面文字来自已覆盖的主数据表）。这些家族已登记进
  `manifests/localizable-bundle-families.json` 的 `reviewed_unclassified`，不再计入
  「新资源类型」告警；逐项证据见 `docs/ARCHITECTURE.md` 的「未纳入面审计」。
  边界：只抽查了包数占多数的 326 个家族 / 18,800 个包，其余 17,583 个长尾签名未开箱，
  仍照旧计数与告警。
- 已知遗留：门户资源清单（`scripts/build_portal_resource_manifest.py`）只统计 `locales/`，
  从不读 `lyrics/`，所以歌曲不会出现在门户的分类计数里（历史行为，与本次改动无关）。

## 3. 验证与后续使用
- 目录结构完全独立且干净，可直接在 `build\runs\text-localization\9.0.200\github-export-candidate` 下执行 `git init && git add . && git commit -m "feat: initial localization repository" && git push` 推送至 GitHub。
