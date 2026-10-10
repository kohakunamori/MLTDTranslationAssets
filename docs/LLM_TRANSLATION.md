# LLM 翻译自动并入流程

本项目是个人业余项目。LLM 结果按项目约定自动提交到 Assets 默认分支，之后由维护者检查和修正。

## 流程

```text
官方最新资源中的新文本
  -> source_sha256 去重
  -> LLM 翻译
  -> pending + translation_stage=llm_translated
  -> publish：accepted + translation_stage=llm_translated（出处不变）
  -> 自动提交默认分支
  -> workflow_dispatch 触发 assets-generated.yml
  -> Assets CI 生成 Unity3D
  -> 人工检查/修正；确认后 translation_stage 才改为 human_translated
```

`untranslated`、`llm_translated`、`human_translated` 是翻译阶段；`status` 字段保持
兼容：未翻译使用 `untranslated`，机器草稿未放行时使用 `pending`，允许进入构建时使用
`accepted`。LLM 工作流不会覆盖 `accepted` 或已有 `pending` 行。

## 机翻直接发布（`publish`）

`scripts/llm_translate_untranslated.py publish` 是**唯一**把机器草稿放进
`generated/<asset_version>/` 的入口（`--scope lyrics` 是同一命令的第二个作用域，
见下节，它不写 `generated/`），语义严格限定为：

- 只处理 `status=pending` 且 `translation_stage=llm_translated` 且 `zh` 非空的行；
- 逐行重验 `source_sha256 == SHA256(ja)`、译文的保留控制符与 `validate_translation`
  的占位符约束，任一不过整轮拒绝，**先 dry-run 再落盘**；
- 把 `status` 改为 `accepted`，**保留** `translation_stage=llm_translated`；
- 不触碰 `human_translated`、已 `accepted`、`untranslated` 的行，未改动行逐字节保留；
- 幂等：重复运行不会二次改写。

两条不变量保证机器产出永远踩不到 generated writer 的歧义闸门（同一
`(bundle, item_key, source_sha256)` 出现两个不同译文的 `accepted` 行时，
`build_generated_release.py` 会拒绝构建）：

- **不制造**：若该 identity 已有 `accepted` 译文，草稿保持 `pending`（记入
  `skipped_duplicate_source`）——同一段原文在发布里已有译法，再加一份只会引入歧义；
- **自修复**：已 `accepted` 的机器行若与同源的另一份 `accepted` 译文冲突，会被降回
  `pending`（记入 `demoted_conflicting_duplicates`，`zh` 与出处保留待人工处理）。
  无 `translation_stage` 的历史行和 `human_translated` 行永不被降级；若冲突只发生在机器行
  之间，保留版本号最小的那一份，结果稳定可复现。

2026-10-08 首次机翻发布就是这样暴露问题的：`1077640` 的 4 行与 `1077500` 已有译文同源不同文，
构建直接报 `conflicting current-source translation for birth_bdl2_001har_005_1000_001har`。
修复后该轮自动降级这 4 行并正常构建。

`--draft <file>` 可把晋级范围限制在某一轮自己的产出（按 `source_sha256` 取交集）；
不带该参数时晋级全部符合条件的行，这也是首次为某个已翻译版本补发布的方式。

晋级前 CI 会先跑一次 `publish --dry-run`，晋级后再跑 `validate_repo.py`：因为
`accepted` 行会额外触发受保护占位符校验，这道校验必须发生在提交之前。

### 第二个作用域：歌曲歌词（`--scope lyrics`）

`collect` / `apply` / `publish` 都接受 `--scope {locales,lyrics,all}`，默认仍是 `locales`
（既有调用行为不变）。`lyrics` 作用域指向 `lyrics/songs/*.jsonl`，与文本面共用同一套
`status` / `translation_stage` 规则，差别只有两点：

- **纯英文行不翻译**：行内含拉丁字母且不含假名与汉字时，`collect` 不排队、`publish`
  不晋级。这是仓库 2026-09-27 起的既有政策（`english_bypass_slots`），规则逐字复制自
  `pipelines/export/export_localization_for_github.py` 的 `is_pure_english_lyric`，
  以保证 `lyrics_manifest.json` 的口径与已发布数字（原有 432 首 = 1,066 槽）完全一致。
- **同一首歌内同一日文行只允许一种中文**：若同一首歌里两行同源但译文不同，这些行
  全部留在 `pending`（记入 `skipped_conflicting_wording`）交人工。它们指向同一句歌词，
  将来回写进客户端后必然显示为同一行，措辞不一致比不翻译更糟。这里没有 `generated/`
  的歧义闸门可用，所以用这条更简单的规则代替。

歌词晋级**不直接**写 `generated/`：`publish --scope lyrics` 的终点是 `lyrics/` 源库
（门户与人工审校用），但源库不是终点——下一次发布构建会用
`scripts/build_lyric_overlay.py` 把有中文的歌曲写回资源包并随版本发布（见
`docs/ARCHITECTURE.md` 的发布流程）。所以「歌词被采纳」到「手机能看到」之间的
唯一动作就是等一次发布构建，没有人工步骤。
`lyrics/all_lyrics.jsonl` 是派生文件，collect 不读它（否则同一行会被翻译两次）。

`collect --scope all` 是工作流的实际用法，一次同时覆盖两个面。

### 关闭与回退

- 单次关闭：手动触发工作流时把 `publish_drafts` 设为 `false`。
- 全局关闭：在仓库 Settings → Variables 里把 `MLTD_PUBLISH_LLM_DRAFTS` 设为 `false`；
  关掉后 LLM 结果仍会提交到 `main`，但停留在 `pending`，回到「仅人工审校」的旧行为。
- 已发布的机翻回退：把对应行改回 `pending`（或直接修正译文后改为
  `accepted + human_translated`）并推送 `main`，再触发 `assets-generated.yml`
  重建；`generated/<asset_version>/` 会被整体替换，旧对象仍留在 CAS 池中。

## GitHub-hosted provider

`.github/workflows/llm-translate-assets.yml` 使用 GitHub-hosted `ubuntu-latest`，
通过公开 HTTPS provider gateway 运行，不依赖 NAS、Windows runner 或本地回环代理。
非敏感的模型/provider 参数在 `configs/api-models.example.json`；API key 只存为
仓库 Actions secret `MLTD_LLM_API_KEY`。

工作流开始时把这个 secret 注入一次性 `$RUNNER_TEMP` 文件，并生成相邻的
`api-runtime.local.json`，以复用现有 `translate_mltd_api_pool.py` 的配置加载器。
临时文件不会提交、不会写进工作区；日志和 PR 内容不包含 key。不要把本地
`api-runtime.local.json`、API key、代理凭据或任何 provider token 提交到仓库。

工作流可定时或手动触发。没有待翻译内容时直接退出，不调用 provider。

## 增量下载官方 bundle

`scripts/refresh_latest_official_catalogue.py` 不再每次重新下载全部约 11.8k 个官方
bundle。官方 index 里的 `remote` 是内容寻址对象名，因此
`manifests/official-bundle-index.json` 记录「上次已核验的 logical -> remote」：
remote 未变的 bundle 本轮直接跳过，只下载真正变动或首次出现的 bundle，再对这批
bundle 抽取源文。

- 该 memo 只在**同一次运行产出的行被提交时**一起提交；运行中途失败则 memo 不推进，
  下次重新核验，不会因为跳过下载而永久漏掉文本。
- `--max-bundles` 截断只影响本轮下载范围，未检查但 remote 未变的条目仍保留在 memo 中。
- 冷启动（首次运行、memo 丢失、`--full-rescan`）会重新核验全部 bundle，约 1 小时；
  正常增量运行只需数分钟。
- 手动触发时的 `full_rescan` 输入用于强制重新核验全部 bundle。

## 新包发现与歌词抽取（2026-10-10 新增）

上面那套增量下载有一个**盲区**：它能复验的只有「仓库里已经有行」的 bundle。官方
index 里从没进过 `locales/` 的包，既不会下载、不会抽取、也不会写进 memo，于是永远
不可见。asset 1077720 时这个盲区的实际代价是 77 个文本包与 59 个歌词包（含新歌
「一旦愛して」）从未进入过翻译队列。

现在每个定时运行按顺序做三件事，都读同一份官方 index：

1. `scripts/discover_official_bundles.py`（只读 + 更新基线）：按
   `manifests/localizable-bundle-families.json` 的**声明式家族**从官方 16.8 万个包里
   挑出「属于可汉化面、但仓库还没有」的包，报告数量、字节数，以及**全新的家族签名**
   （从未见过的资源类型）。家族基线写入 `manifests/official-asset-inventory.json`。
   「已跟踪」的判定包含三处：`locales/` 的行、`lyrics/songs/` 的文件、
   `manifests/official-bundle-index.json` 的 memo —— 第三处不可省：官方索引里真的
   有一个拼错的包 `pecial_108_fc_01_jp.gtx`，它的行与正确命名的兄弟包完全相同，
   会被身份去重掉、永远不产生 `locales/` 文件。
2. `refresh_latest_official_catalogue.py` 按同一份家族注册表选包（不再从 `locales/`
   反推），只下载 remote 变化的。
3. `refresh_lyrics_catalogue.py` 抽取新歌歌词（`scrobj_*` 的 `scenario[*].str`）到
   `lyrics/songs/`，按 `(bundle, index, source_sha256)` 合并既有译文，再重算
   `all_lyrics.jsonl` 与 `lyrics_manifest.json`。

三道 fail-closed 上限：发现 400 包 / 1 GiB、歌词 200 包 / 512 MiB（两者超过即以 exit 2
中止本次运行、不提交），文本追加仍受 `--max-new-rows`（默认 5000）约束。取值依据是
「常态一次更新只有个位数到几十个包」与「2026-10-01 的故障形态是无界 39.4 万行」之间
留两三个数量级余量；1077720 的补录（77 + 59 个包）没有触发任何上限。

「新家族签名」告警只对**没抽查过**的类型生效。2026-10-10 的抽样审计把 326 个家族
（18,800 个包）确认为图片 / 语音音频 / 动画曲线，列入
`manifests/localizable-bundle-families.json` 的 `reviewed_unclassified`，不再刷屏；
审计方法与逐项证据见 `docs/ARCHITECTURE.md` 的「未纳入面审计」。

`--scope all` 让同一次运行同时翻译文本面与歌词面；歌词面的晋级规则见上文
「第二个作用域」小节。

## 失败处理

- 少数条目耗尽所有 provider 重试（终态失败）不再丢弃已接受的结果：翻译步骤容忍非零
  退出码，应用/提交继续执行，Run 保持绿色并输出 `::warning::` 标注。
- 只有真正没有产出（接受条目 = 0 且失败条目 ≥ 50）或前置步骤失败才会让 Run 变红，并
  自动在 Issue 里登记/追评一条 `Auto-apply LLM translations failed`。数量少于一门槛的
  残余失败只警告，由下一次运行重试——否则十几行顽固条目会天天开 issue。provider 整体
  不可用时队列会随未翻译行累积迅速越过门槛。
- 每次运行都上传 `llm-translation-diagnostics` artifact，包含 `.llm-summary.json`、
  `.llm-queue.jsonl`、`.llm-failed.jsonl`、`.llm-draft.jsonl`、`.llm-publish.json`、
  `.llm-publish-lyrics.json`、`.llm-discovery.json`、`.llm-discovery-summary.json`、
  `.llm-lyrics.json`，保留 14 天。
- `publish` 失败时不会提交任何东西：提交步骤显式排除 `steps.publish.outcome ==
  'failure'`，避免把「本轮晋级失败」的工作区状态当成已审校状态推上去。

成功直接提交包含 `llm_translated` 标记的结果到默认分支，不创建审核 PR；提交后用
`gh workflow run assets-generated.yml --ref main` 触发构建（`GITHUB_TOKEN` 推送不会
触发其他工作流，必须显式派发）。`status=pending` 仍表示该结果尚未放行进构建；
`status=accepted + translation_stage=llm_translated` 表示它已经机翻发布但**未经人工
审校**，维护者可以直接修改后把 `translation_stage` 改为 `human_translated`。
