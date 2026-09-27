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
## 3. 验证与后续使用
- 目录结构完全独立且干净，可直接在 `build\runs\text-localization\9.0.200\github-export-candidate` 下执行 `git init && git add . && git commit -m "feat: initial localization repository" && git push` 推送至 GitHub。
