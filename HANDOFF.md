# HANDOFF — MLTD 汉化资源 GitHub 公开仓库分类导出 (Phase 1)

- **stream**: text-localization
- **run**: `build\runs\text-localization\9.0.200\github-export-candidate`
- **artifact_status**: **candidate**
- **base_version**: `9.0.200+1077500`
- **导出时间**: `2026-09-27T00:00:00Z`

## 1. 导出概览
- **待译全集总行数**: 74569 行，覆盖 5 大业务分类（51 个 Bundle）。
  - `locales/master/`: 73715 行 (5 bundles)
  - `locales/card/`: 349 行 (18 bundles)
  - `locales/story/`: 456 行 (13 bundles)
  - `locales/birth/`: 42 行 (10 bundles)
  - `locales/dialogue/`: 7 行 (5 bundles)
- **翻译覆盖度**:
  - `accepted` (已采纳正式译文): 50421
  - `pending` (待人工审校机翻初稿): 2332
  - `untranslated` (待翻译空槽): 21816
- **控制符清洗**:
  - 自动拦截/转义保留控制符 `|` 与 `^` 共计 0 行。
- **歌词与曲目**:
  - 432 首歌曲分轨，12131 个歌词槽，已翻译 12131 槽。
- **多媒体资产 Manifest**:
  - 939 张贴图索引（937 张已审校贴图 + 底栏贴图），支持 GitHub Release Assets / Cloudflare R2。
- **名词表**:
  - 90 个参考术语表 + 52 偶像译名名录。

## 2. 验证与后续使用
- 目录结构完全独立且干净，可直接在 `build\runs\text-localization\9.0.200\github-export-candidate` 下执行 `git init && git add . && git commit -m "feat: initial localization repository" && git push` 推送至 GitHub。
