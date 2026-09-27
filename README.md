# MLTD 简体中文汉化开源资源库 (THE IDOLM@STER MILLION LIVE! THEATER DAYS Localization)

本项目为《偶像大师 百万现场 剧场时光》(MLTD) 简体中文本地化个人整理与社区协作数据仓库。
本仓库沉淀了日文原文与整理的简体中文译文，并统一维护术语表、歌词与多媒体资源 Manifest。

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
- `glossary/`：翻译规范与参考术语
  - `glossary/authoritative-terms.json`：90 个参考术语表（固定译名与避免词）
  - `glossary/idols.json`：52 偶像译名对照与声优名录
- `manifests/`：富媒体与贴图元数据清单（文字在库，多媒体外链）
  - `manifests/images.manifest.json`：937 张已汉化贴图与底栏贴图的 SHA-256 索引
  - `manifests/bottom-bar.manifest.json`：底栏 7 标签图文与 Sprite 坐标
- `schema/`：数据规范与 JSON Schema 定义

## 版本分支与标签

`main` 始终跟踪**最新**资源版本；每条已经离开主线的资源版本另以两个 ref 冻结，供仍固定在旧版本的客户端复现：

- 标签 `assets-<资源版本>`（如 `assets-1077500`）—— 该资源版本对应的仓库状态。
- 分支 `release/<客户端版本>+<资源版本>`（如 `release/9.0.200+1077500`）—— 同一状态的长期分支。

`auto-track-jp-assets.yml` 检测到新资源版本时，会先给**即将被取代**的版本打标签、建分支，再更新 `manifests/asset-version.json` 并合并。已存在的标签/分支不会被覆盖。

## 条目格式规范

每一行 JSONL 严格遵循以下规范：

```json
{
  "base_version": "9.0.200+1077500",
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
1. **防止版本漂移**：`source_sha256` 必须与 `ja` 原文字符串的 SHA-256 强校验匹配。
2. **安全隔离控制符**：客户端引擎使用 `|` 和 `^` 作为底层控制分隔符。**严禁在译文 `zh` 中输入半角 `|` 或 `^`**（可使用全角 `｜` 或 `＾`）。
3. **状态说明**：
   - `untranslated`：待翻译条目，`zh` 为空字符串。
   - `pending`：已生成初稿或机器翻译，等待人工审校。
   - `accepted`：已通过质量审校的正式译文。

## Web 翻译门户与在线协同

您可以通过社区翻译门户直接在线认领翻译与审校：
- **Web 门户**：https://mltd-translate.nyaneko.cn
- **提交 PR**：欢迎在 GitHub 直接提交 Pull Request，CI 机器人将对每一行的数据完整性进行自动化检测。

## 许可证与致谢

- 游戏日版原始文本、角色、图片与音频著作权均归 株式会社万代南梦宫娱乐 (Bandai Namco Entertainment Inc.) 所有。
- 本仓库汉化翻译成果遵循 [CC-BY-NC-SA 4.0](LICENSE) (知识共享署名-非商业性使用-相同方式共享 4.0 国际许可协议) 开源共享，严禁用于任何商业营利目的。
