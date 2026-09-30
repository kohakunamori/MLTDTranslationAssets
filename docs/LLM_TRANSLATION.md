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

## Provider 配置

`.github/workflows/llm-translate-assets.yml` 只在自托管 runner 上运行，因为本地
provider 可能位于 `127.0.0.1` 或本地代理之后。runner 必须通过环境变量
`MLTD_LLM_CONFIG_PATH` 指向现有的 `api-models.local.json`；相邻的
`api-runtime.local.json` 会由现有池加载。配置和 API key 不得提交到仓库。

工作流可定时或手动触发。失败不会提交半成品；成功只创建包含待人工审核草稿的
PR。真实 runner、provider、GitHub 权限未配置前，这个工作流只是可审计的部署模板。
