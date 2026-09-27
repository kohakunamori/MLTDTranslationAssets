# MLTD 图像汉化自动化流水线 (MLTD Image Localization Pipeline)

本模块包含《偶像大师 百万现场 剧场时光》游戏图像与纹理资源的自动化重组、视觉翻译与无损回填工具链。

## 1. 架构与模型规范

- **图像生成与编辑模型**：`gpt-image-2.5-sunburst`（通过 `images_edits` 端点，支持透明通道与高保真文字重绘）
- **视觉审校与布局识别模型**：`gpt-5.6-luna`（用于识别多 Sprite 布局、文本区域分类与质量检测）
- **底层纹理编码**：ASTC (Adaptive Scalable Texture Compression) 与 RGB24/RGBA32，保持 UnityFS 资源包非目标像素完全不变。

## 2. 环境配置与凭证脱敏

为了确保安全，本流水线**严禁硬编码任何 API 密钥**。
默认配置文件为 `config.example.json`，您可以复制为 `config.json` 或直接设置环境变量：

```bash
# 复制配置模板（可选）
cp config.example.json config.json

# 或直接通过环境变量注入凭据
export CLIPROXY_API_KEY="your_api_key_here"
# 或
export OPENAI_API_KEY="your_api_key_here"
```

### `config.example.json` 字段说明
- `image_provider.base_url`: 上游代理网关地址（默认 `http://127.0.0.1:15721/v1` 或兼容 OpenAI images 接口的端点）
- `image_provider.model`: 固定为 `gpt-image-2.5-sunburst`
- `image_provider.api_key_env`: 读取 API 密钥的环境变量名（默认 `CLIPROXY_API_KEY`）
- `vision.model`: 固定为 `gpt-5.6-luna`

## 3. 核心工具说明

| 脚本 | 功能说明 |
|---|---|
| `preprocess_mltd_image25.py` | 提取并解析 Unity Sprite 图集，根据 Sprite 几何结构重组整幅原始画面 |
| `run_mltd_image25_batch.py` | 批量调度 `gpt-image-2.5-sunburst` 进行图像去日文与简中重绘 |
| `build_mltd_image25_review_gallery.py` | 生成用于人工审查的 Web HTML 对照画廊 (Original vs Localized) |
| `stage_reviewed_images.py` | 将人工审核通过的 PNG 纹理重新切片并精准写回 Unity3D Bundle |
| `audit_reconstructed_release.py` | 校验回填后的 Unity3D Bundle，确保 ASTC 纹理尺寸、哈希和平台一致性 |
| `provider_config.py` | 统一的凭据安全管理、代理与超时重试配置模块 |

## 4. 运行示例

### 步骤 1：批量生成汉化图像
```bash
python run_mltd_image25_batch.py --input-dir /path/to/reconstructed --output-dir /path/to/localized
```

### 步骤 2：生成审校画廊
```bash
python build_mltd_image25_review_gallery.py --pairs /path/to/review_pairs.json --output gallery.html
```

### 步骤 3：回填并通过质检
```bash
python stage_reviewed_images.py --approved-list approved.txt --target-bundles /path/to/bundles
python audit_reconstructed_release.py --bundle-dir /path/to/bundles
```
