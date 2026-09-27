# MLTD 自动化汉化流水线工具集 (MLTD Localization Pipelines)

本目录承载《偶像大师 百万现场 剧场时光》完整的自动化汉化流水线工具链，为项目与社区提供从数据提取、AI 翻译、视觉审校到资产分发的一站式基础设施。

## 子目录导航

- [**text/**](./text/README.md)：文本自动化汉化流水线
  - 核心功能：GTX 加解密、多模型并发翻译池、自动化质检与控制符保护、加密 Overlay 构建。
- [**image/**](./image/README.md)：图像与纹理汉化流水线
  - 核心功能：Unity Sprite Atlas 解析与整帧重组、`gpt-image-2.5-sunburst` 图像重绘、`gpt-5.6-luna` 视觉分类、ASTC 纹理切片与无损回填。
- [**export/**](./export/README.md)：仓库数据导出与同步工具
  - 核心功能：按游戏业务分类导出 JSONL、歌词双语对齐与纯英文 Bypass、媒体清单生成。

## 安全与凭据规范

为保护开发者与协同者安全，本目录下所有脚本：
1. **绝不硬编码任何 API 密钥或个人凭据**。
2. 统一通过环境变量（如 `MLTD_API_KEY`, `CLIPROXY_API_KEY`, `OPENAI_API_KEY`）或本地未跟踪的 `*.local.json` 注入配置。
3. 仓库内仅提供带有详尽注释的 `.example.json` 模板。
