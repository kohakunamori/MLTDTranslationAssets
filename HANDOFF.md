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
- 当前总量：文本 394,959 行（已采纳 393,427 / 待译 1,532）；歌词 491 首 / 13,729 槽
  （已译 11,065 / 纯英文保留 1,258 / 日文待译 1,406）。原有 432 首的逐曲文件与
  `english_bypass=1066` 口径均未被改动。
- **歌词已接入发布**（2026-10-10 晚）：`scripts/build_lyric_overlay.py` 在每次发布构建里
  把「有已采纳中文」的歌曲的官方 `scrobj_*` 包改写中文，写进与文本包相同的覆盖层、
  发布清单与对象库，随版本自动更新。写入前逐位置回读校验；只有 `accepted` 且
  `source_sha256` 与原文一致的行会被使用，未翻译行保持官方日文。上限 600 首 / 256 MiB。
  真机效果仍需用户在手机上确认（本仓库无法在设备上验证）。
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
