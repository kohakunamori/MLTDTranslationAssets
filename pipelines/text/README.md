# MLTD 文本自动汉化流水线 (MLTD Text Localization Pipeline)

本模块包含《偶像大师 百万现场 剧场时光》游戏全量文本的提取、机器翻译调度、质量审校与本地加密 Overlay 生成的完整工具链。

## 1. 架构与设计原则

- **源哈希强绑定 (Source-SHA Binding)**：所有文本行均计算 SHA-256 原文指纹，杜绝幽灵翻译与跨版本漂移。
- **底层控制符安全保护 (Delimiters & Control Codes)**：
  - 严格保护游戏引擎内部控制码（如 `\`, `\`, `{0}`, `{$P$}` 等），自动掩码与还原。
  - 阻断破坏客户端底层解析的半角保留分隔符（`|` 与 `^`）。
- **多模型并发池调度 (Model Pool & Rate Limiting)**：支持 OpenAI / Claude / Gemini 多 Provider、动态分组批处理（Batch V2）与 RPM 速率控制。
- **自动化质量把关 (Automated QA Gate)**：提供针对未转义日文汉字、平假名/片假名残留、数字错位的多级质量质检。

## 2. 环境配置与凭证脱敏

本流水线**绝不硬编码任何 API 密钥**。在 `configs/` 目录下提供了脱敏配置范本：

```bash
# 复制模型配置文件
cp configs/api-models.example.json configs/api-models.local.json

# 设置对应的环境变量（推荐）
export MLTD_API_KEY="your_api_key_here"
```

## 3. 核心工具说明

| 脚本 | 功能说明 |
|---|---|
| `mltd_localization_pipeline.py` | 游戏资源包全量提取、翻译记忆库构建、覆盖率审计与 UnityFS Overlay 合流 |
| `mltd_localize_gtx.py` | 底层加密 `TextAsset` (GTX) 加解密、UnityPy 解析、行内控制符保护与验证 |
| `translate_mltd_api_pool.py` | 多模型并发翻译池控制器（支持动态 Batch V2、缓存预热与失败回退） |
| `review_gtx_translations.py` | 译文自动化 QA 审校器（假名残留、术语一致性、格式标记校验） |
| `run_dual_track_pipeline.py` | 双轨并发流水线编排器（快速小包与主文本池并行推进） |
| `report_localization_progress.py` | 统计当前汉化总覆盖率与进度报告 |

## 4. 运行示例

### 步骤 1：从游戏资产提取待翻译文本
```bash
python mltd_localization_pipeline.py extract-snapshot   --snapshot /path/to/asset_snapshot.json   --archive-root /path/to/raw_assets   --output catalogue.jsonl   --memory translation-memory.jsonl
```

### 步骤 2：启动并发机器翻译
```bash
python translate_mltd_api_pool.py   --queue queue.jsonl   --output machine-translations.jsonl   --config configs/api-models.local.json
```

### 步骤 3：翻译质量自检与审计
```bash
python review_gtx_translations.py   --input machine-translations.jsonl   --output reviewed.jsonl
```

### 步骤 4：构建本地加密 Overlay Bundle
```bash
python mltd_localization_pipeline.py build-overlay   --snapshot /path/to/asset_snapshot.json   --archive-root /path/to/raw_assets   --translations reviewed.jsonl   --output-dir /path/to/cn_overlay
```
