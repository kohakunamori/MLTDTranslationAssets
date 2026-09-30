# MLTD 简体中文汉化开源资源库 (THE IDOLM@STER MILLION LIVE! THEATER DAYS Localization Assets)

这是一个个人业余项目，用于整理《偶像大师 百万现场 剧场时光》(MLTD) 的简体中文汉化资源。
仓库中的文本、术语表、歌词和贴图 Manifest 会持续更新，欢迎通过 GitHub PR 参与修订。

本仓库只承载**经 assets 服务器下发的面**（`/cn/<asset>/` overlay）。底栏贴图、BI 文案与字体属于
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
- `lyrics/`：全曲目歌词库（432 首歌曲对齐双语歌词与时间戳）
  - `lyrics/songs/`：按歌曲独立分轨 JSONL
  - `lyrics/all_lyrics.jsonl`：全曲歌词总汇
- `glossary/`：翻译规范与标准术语
  - `glossary/authoritative-terms.json`：项目当前采用的固定译名与避免词
  - `glossary/idols.json`：项目整理的 52 名偶像与声优名录
- `manifests/`：贴图元数据清单（文字在库，多媒体外链）
  - `manifests/images.manifest.json`：937 张已汉化贴图的 SHA-256 索引
- `pipelines/`：自动化汉化与生成流水线工具集
  - `pipelines/text/`：全量文本提取、加密 GTX 解密/回写、多模型并发翻译与自动化质检流水线
  - `pipelines/image/`：Sprite Atlas 几何重组、`gpt-image-2.5-sunburst` 图像重绘、`gpt-5.6-luna` 视觉审查与 ASTC 纹理回填流水线
  - `pipelines/export/`：数据分发与 GitHub 规范导出工具
- `schema/`：数据规范与 JSON Schema 定义

generated/ 保存 CI 生成的 Unity3D：objects/sha256/ 负责跨版本内容寻址去重，generated/<asset_version>/manifest.json 与 checksums.txt 描述可分发对象。

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
   - `pending`：已生成初稿或机器翻译，等待人工审校。
   - `accepted`：已通过人工质量审校、可进入构建的译文。

`translation_stage`（新条目使用）进一步标记流程：`untranslated` →
`llm_translated` → `human_translated`。LLM 结果仍是 `pending`，不会绕过人工审核。

## Web 翻译门户与在线协同

您可以通过社区翻译门户直接在线认领翻译与审校：
- **Web 门户**：https://mltd-translate.nyaneko.cn
- **提交 PR**：欢迎在 GitHub 直接提交 Pull Request，CI 机器人将对每一行的数据完整性进行自动化检测。

## 许可证与致谢

本仓库文本基于游戏日版文本翻译与整理，版权归 Bandai Namco Entertainment Inc. 所有。
汉化成果遵循社区开源共享协议，严禁用于任何商业用途。
