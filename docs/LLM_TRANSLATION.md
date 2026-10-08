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
`generated/<asset_version>/` 的入口，语义严格限定为：

- 只处理 `status=pending` 且 `translation_stage=llm_translated` 且 `zh` 非空的行；
- 逐行重验 `source_sha256 == SHA256(ja)`、译文的保留控制符与 `validate_translation`
  的占位符约束，任一不过整轮拒绝，**先 dry-run 再落盘**；
- 把 `status` 改为 `accepted`，**保留** `translation_stage=llm_translated`；
- 不触碰 `human_translated`、已 `accepted`、`untranslated` 的行，未改动行逐字节保留；
- 幂等：重复运行不会二次改写。

`--draft <file>` 可把晋级范围限制在某一轮自己的产出（按 `source_sha256` 取交集）；
不带该参数时晋级全部符合条件的行，这也是首次为某个已翻译版本补发布的方式。

晋级前 CI 会先跑一次 `publish --dry-run`，晋级后再跑 `validate_repo.py`：因为
`accepted` 行会额外触发受保护占位符校验，这道校验必须发生在提交之前。

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

## 失败处理

- 少数条目耗尽所有 provider 重试（终态失败）不再丢弃已接受的结果：翻译步骤容忍非零
  退出码，应用/提交继续执行，Run 保持绿色并输出 `::warning::` 标注。
- 只有真正没有产出（接受条目 = 0 且失败条目 ≥ 50）或前置步骤失败才会让 Run 变红，并
  自动在 Issue 里登记/追评一条 `Auto-apply LLM translations failed`。数量少于一门槛的
  残余失败只警告，由下一次运行重试——否则十几行顽固条目会天天开 issue。provider 整体
  不可用时队列会随未翻译行累积迅速越过门槛。
- 每次运行都上传 `llm-translation-diagnostics` artifact，包含 `.llm-summary.json`、
  `.llm-queue.jsonl`、`.llm-failed.jsonl`、`.llm-draft.jsonl`、`.llm-publish.json`，
  保留 14 天。
- `publish` 失败时不会提交任何东西：提交步骤显式排除 `steps.publish.outcome ==
  'failure'`，避免把「本轮晋级失败」的工作区状态当成已审校状态推上去。

成功直接提交包含 `llm_translated` 标记的结果到默认分支，不创建审核 PR；提交后用
`gh workflow run assets-generated.yml --ref main` 触发构建（`GITHUB_TOKEN` 推送不会
触发其他工作流，必须显式派发）。`status=pending` 仍表示该结果尚未放行进构建；
`status=accepted + translation_stage=llm_translated` 表示它已经机翻发布但**未经人工
审校**，维护者可以直接修改后把 `translation_stage` 改为 `human_translated`。
