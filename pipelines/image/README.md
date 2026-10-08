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
| `stage_reviewed_images.py` | 将离线人工审核 CSV 转为 **SHA 绑定的待装审批清单**（`--review-csv` 必填，`--work`/`--output` 选填）。**不修改、不创建 Unity3D Bundle**；每一行的 `review_status` 固定写 `approved_for_staging_not_installed`，且不会自动接受模型结果 |
| `audit_reconstructed_release.py` | 安装**前**的镜像制备审计：校验冻结的重组图身份与全部生成 PNG 的尺寸/哈希/ROI 外像素，产出未审校 QA 清单与人工复核阻塞项；**不做任何 Unity 物化**，也不接受 non-PNG 源 |
| `inject_reviewed_textures.py` | 源绑定注入器：把已人工审核的 PNG 真注入 UnityFS Texture2D，产出 repack 后的 Bundle 与 `inventory.json`（`--install-manifest` 必填；见 **§4.1**） |
| `verify_bundle_repack.py` | 对上述注入 run 的**独立再审计**：读取报告的定位与 hash，独立重新比对源/输出对象和纹理（见 **§4.2**） |
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

### 步骤 3：暂存人工审批、注入与独立复核

```bash
# (a) 把离线人工审核 CSV 转为 SHA 绑定的待装审批清单。
#     只写清单，不触碰 Unity3D Bundle；--review-csv 必填。
#     输出为 SHA 绑定的待装清单；--work/--output 仅为示例路径。
python stage_reviewed_images.py --review-csv review.csv \
    --work /path/to/review-work --output /path/to/staging/approved-for-staging.jsonl
```

注入器/审计器的输入**全部显式**（不读环境变量、无默认路径、不搜索同级目录）。以下示例按 runner 自身规则给出：`--original-root` 仅为相对 `original_png` 提供；`--report`/`--out`/`--audit` 均须显式。

```bash
# (b) 源绑定注入（此处仅示意参数；不要在此真跑 --all 注入）。
python inject_reviewed_textures.py --all \
    --install-manifest /path/to/reviewed-cohort.jsonl \
    --original-root /path/to/originals \
    --out /path/to/injection-out \
    --report /path/to/injection-out/../image-inject-report.json

# (c) 独立再审计同一 run（输出写到显式 --audit）。
python verify_bundle_repack.py \
    --report /path/to/image-inject-report.json \
    --install-manifest /path/to/reviewed-cohort.jsonl \
    --audit /path/to/independent-audit.json

# (d) 只读预检：报告输入是否可解析（install manifest / SHA-256 / 行数、bundle 数、
#     original root），不产出 bundle、不创建 --out/--report。缺元数据以“缺元数据”报出，
#     而非被判为注入失败。
python inject_reviewed_textures.py --preflight-context \
    --install-manifest /path/to/reviewed-cohort.jsonl
```

以上各例中，(b)(c) 的真实运行模式**不要在本仓库未接入前执行**：注入器与审计器尚未接入 `assemble`/发布链，`audit_reconstructed_release.py` 仍是**安装前**审计（非 repack 后审计），旧的 `--approved-list`/`--target-bundles`/`--bundle-dir` 并不是这些工具的真实选项。



> **重要（未接入，勿直接串联）：** `stage_reviewed_images.py` 产出标签为 `approved_for_staging_not_installed`，而注入器只接受用户审批标签 `user_approved_for_isolated_install_staging`；两者**不兼容**。本仓库**未实现**两者之间的 source-bound 桥接，也未自动改写 review 标签或把模型图改标为 accepted。因此**不能**把 (a) 的输出直接喂给 (b)。桥接需要一个有文档化用户审批步骤的 source-bound 转换，属未完成工作。



> **未接入/边界声明：** 注入器/审计器**未**接入 `build_generated_release`（`--require-images` 仍按原样 fail-closed）；`assemble_frozen1077100_overlay.py` 尚未调用它们；旧 `tools/mltd_image_localization/` 消费方未切换、原工具未退役；两者**不含**任何 asset-version 权威证明（版本无关的 source/CLI 工具）。管道内**没有**根 `pyproject.toml` 与管道级 requirements，唯一的 `asset-server/requirements.txt` **不适用**于本模块的依赖。



### 依赖与测试

运行时依赖为 `inject_reviewed_textures.py`、`verify_bundle_repack.py` 与 `test_image_injection_contract.py` 三者共用的标准库之外库；`requirements-injection.txt` 仅列这三个（`numpy`/`Pillow`/`UnityPy`）。

```bash
# 安装运行时依赖（仅文档，未在此仓库执行）
python -m pip install -r requirements-injection.txt

# pytest 是独立的 test-time 依赖，不属运行时依赖，需单独显式安装
python -m pip install pytest==8.4.2

# 契约测试（用独立、明确的方式运行）
python -m pytest -p no:cacheprovider test_image_injection_contract.py
```
