# LLM 翻译自动并入流程

本项目是个人业余项目。LLM 结果按项目约定自动提交到 Assets 默认分支，之后由维护者检查和修正。

## 流程

```text
官方最新资源中的新文本
  -> source_sha256 去重
  -> LLM 翻译
  -> pending + translation_stage=llm_translated
  -> 自动提交默认分支
  -> 人工检查/修正
  -> accepted + translation_stage=human_translated
  -> Assets CI 生成 Unity3D
```

`untranslated`、`llm_translated`、`human_translated` 是翻译阶段；现有
`status` 字段仍保持兼容：未翻译使用 `untranslated`，LLM 草稿使用
`pending`，人工确认后使用 `accepted`。LLM 工作流不会覆盖 `accepted` 或已有
`pending` 行。

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
  `.llm-queue.jsonl`、`.llm-failed.jsonl`、`.llm-draft.jsonl`，保留 14 天。

成功直接提交包含 `llm_translated` 标记的结果到默认分支，不创建审核 PR。
`status=pending` 仍表示该结果尚未被人工确认；维护者可以直接修改并改为 `accepted` /
`human_translated`。
