# LLM 翻译草稿流程

本项目是个人业余项目。LLM 只负责生成可审阅的草稿，不自动把结果当作最终译文。

## 流程

```text
官方最新资源中的新文本
  -> source_sha256 去重
  -> LLM 翻译
  -> pending + translation_stage=llm_translated
  -> GitHub Pull Request
  -> 人工修改/审核
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

工作流可定时或手动触发。失败不会提交半成品；成功只创建包含待人工审核草稿的
PR。没有待翻译内容时直接退出，不调用 provider。
