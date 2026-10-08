# Assets 生成产物的内容寻址存储与 CI 设计

> 适用仓库：`MLTDTranslationAssets`（assets 服务器通道）；中央入口以显式 root/pin 加载产品仓库的写者，
> 本仓 `scripts/assets_generated_index.py` 是 legacy 工具与离线测试 fixture，不是中央入口的默认 reader/writer。
> schema 见 `configs/schemas/assets-generated-manifest.schema.json`，workflow 模板见
> `.github/workflows/assets-generated.yml.example`。
> 归属 stream：`text-localization`。本文只描述稳定契约，不含进度/checkpoint（进度在 `docs/streams/text-localization/STATE.md`）。

## 1. 为什么需要 CAS

文本走 Git、二进制外部托管的规则（见 `docs/GITHUB_LOCALIZATION_REPO_SPEC.md` §1.2）对**生成产物**不成立：
`generated/` 里的 Unity3D 包正是 assets 服务器真正下发的字节，而同一份包会被多个 `asset_version` 共用，
"每个版本各存一份"会在每次构建时重复整份二进制。因此生成产物进入内容寻址存储（CAS）：

- 同一个 `asset_version` **只保留最新一次成功构建**；
- 不同 `asset_version` 目录**可以同时存在**；
- 相同二进制**只保存一个对象**。

## 2. 目录契约

以下分片目录与相关低层行为描述对应本仓 legacy 工具。中央入口的实际对象布局、准入和复用规则均由本次已核 pin 的产品写者决定；产品写者可使用 `objects/sha256/<digest>` 平铺路径。入口保留写者返回的 `object_path`，不强制转换为 legacy 分片布局。

```
generated/
├── objects/
│   └── sha256/
│       ├── aa/<完整 sha256-A>
│       └── bb/<完整 sha256-B>
├── 1077100/
│   ├── manifest.json
│   └── checksums.txt
├── 1077500/
│   ├── manifest.json
│   └── checksums.txt
└── …
```

- `objects/sha256/<前两位 hex>/<完整 64 位 hex>`：新对象使用两级分片路径；写入方式为**同目录临时文件 + `os.replace` 原子替换**，
  读侧永远看不到半个对象。目标已存在且 hash 一致时**不重写**（去重）；目标存在但 hash 不符视为存储损坏，**拒绝覆盖**并报错。
- `generated/<asset_version>/manifest.json`：该版本的正式清单（见 §4）。
- `generated/<asset_version>/checksums.txt`：每行 `<sha256>  objects/sha256/<前两位>/<sha256>`，
  与 `sha256sum -c` 兼容。注意相对路径是**从 store 根**（`generated/`）计算的，
  所以校验要在 store 根执行：`cd generated && sha256sum -c 1077100/checksums.txt`。
- 服务器继续使用**原始资源路径**（如 `/assets/1077100/event/001/title.unity3d`）；NAS 侧靠
  `asset_version + logical_path + manifest + object_path` 映射到真实对象。因此条目必须同时携带
  `logical_path`（原始资源路径）与 `object_path`（CAS 路径），二者不可互相替代。

## 3. 版本轴：client 与 assets 相互独立

本仓 legacy 工具将历史平铺清单 `objects/sha256/<digest>` 兼容读取，新写入使用 `objects/sha256/<前2位>/<digest>`；这不是中央入口对产品写者的布局要求。布局收敛只能在成功候选事务中校验所有保留清单、统一引用并去除重复对象；失败时旧清单、checksums 与对象必须保持原样，不改写 Git 历史。

`GeneratedStore.transaction(prune=True)` 提供候选发布边界：在 store 同文件系统的兄弟目录复制完整受管内容，候选内写入、校验和清理成功后才切换目录；异常回滚，恢复失败则保留 backup 并报出恢复位置。单个文件的原子替换不等于整个构建事务，producer 必须使用该事务接口；不使用硬链接隔离可写候选。该接口不替代发布单写窗口，也不表示 NAS 已部署。

`asset_version`（assets 轴，纯数字，如 `1077100`）与 `source_client_version`（client 轴，如 `9.0.200`）
是**两个独立字段**。

- 条目与清单中的 `client_version` 必须为 `null`；assets 通道不下发 APK 内置面（底栏贴图、BI 文案、字体由
  `MLTDTranslationClient` 负责）。
- **组合版本一律拒绝**：`asset_version` 或 `source_client_version` 含 `+` 或 `-assets-` 时，
  `build` 直接报错退出（`9.0.200+1077100`、`client-9.0.200-assets-1077100` 都是废身份）。
  schema 用 `pattern` + `not` 显式表达同一禁令。

## 4. manifest 字段语义

顶层（`kind = "mltd-assets-generated-manifest"`）：`asset_version`、`client_version`（`null`）、
`source_client_version`、`source_commit`、`translation_commit`、`generated_commit`、`ci_run_id`、
`build_status`、`generated_at_utc`、`entry_count`、`entries[]`、`reuse_summary`。

**commit 溯源**：`source_commit`、`translation_commit`、`generated_commit` 是生成时已存在的输入 commit；`generated_commit` 不是之后包含本 manifest 的 bot commit（不能自引用）。NAS 另在同步报告/state 记录实际解析到的 `snapshot_commit`，并在下载/写入前验证三个输入 commit 均为快照的祖先或相同；未知、分叉或无法查询均拒绝，不能仅检查 40-hex。后续文档提交不使旧成功版本失效。

**`ci_run_id`（顶层与条目都有）**：产生该产出的 CI 运行标识，形如 `<run_id>` 或 GitHub 的
`<run_id>.<run_attempt>`（`^[0-9]+(\.[0-9]+)?$`）。它是**溯源**字段：调用方未传时，生产方按顺序读
`$MLTD_CI_RUN_ID` → `$GITHUB_RUN_ID`；两者都没有就记为 `null`，**绝不编造**（时间戳、主机名、
随机数都不是运行身份，写进去就和真实值无法区分）。字段**必须存在**——`null` 合法，缺失不合法，
因为"没有运行上下文"和"值在传递中丢了"是两件不同的事。含 `+` 或 `-assets-` 的值按版本轴同款
规则 fail-closed 拒绝（运行标识永远不是版本串）。

条目（`channel = "assets"`）：

| 字段 | 语义 |
| --- | --- |
| `logical_key` | 该逻辑资源在翻译仓库中的稳定身份 |
| `logical_path` | 官方原始资源路径（服务器实际下发的路径），如 `event/001/title.unity3d` |
| `resource_kind` | 该载荷是什么：`bundle` / `texture` / `audio` / `other`。**必填且不默认**——每个 surface 在 `scripts/materialize_generated_release.py::SURFACE_RESOURCE_KIND` 里声明自己的产物类型（event-unit→`bundle`、mld→`bundle`、image→`texture`）；忘记分类必须让构建失败，而不是被静默标成 `other`，否则消费方分不清"真的是 other"和"漏填了" |
| `source_sha256` | **官方源资源**的 SHA-256；跨版本相等即 `exact` |
| `translated_sha256` | **生成（汉化）产物**的 SHA-256；与 `source_sha256` 正交 |
| `object_path` | `objects/sha256/<前两位>/<digest>`，新产物的分片 CAS 路径；历史平铺路径仅兼容读取 |
| `artifact_sha256` | 存储字节的 SHA-256；必须等于 `object_path` 内嵌 digest，且出现在 `checksums.txt` |
| `reuse_status` | `exact` / `verified-compatible`（正式清单只允许这两个） |
| `translation_status` | `accepted` / `modified` / `reused`（正式清单只允许这三个） |

`reuse_summary` 记录已发布条目的两个维度计数、被拒条目数与按原因分组的计数；被拒条目**带 reason 报告但绝不进入清单**。

**溯源元组完整性与两条轴的区别**：架构钉死的身份字段是
`client_version` / `asset_version` / `source_client_version` / `source_commit` / `translation_commit` /
`generated_commit` / `ci_run_id` / `artifact_sha256`。其中**版本轴**只有两条
（`asset_version` 本仓身份，`client_version` 在本仓恒为 `null`），其余六项**都是溯源**：
它们描述"这批字节是谁、在哪个 commit、哪次运行里产出的"，**不是身份的一部分**，任何一项都不得被并进
版本串。schema（`configs/schemas/assets-generated-manifest.schema.json`，`additionalProperties: false`）
与生产方（`scripts/assets_generated_index.py`）对每个字段都有显式校验，缺字段与非法取值一样 fail-closed；
schema 一致性由 `scripts/test_assets_generated_index.py` 在装了 `jsonschema` 时做 draft 2020-12 校验。

## 5. 两个正交状态维度

### 5.1 `reuse_status`：官方源资源是否仍兼容

| 值 | 判定 | 处置 |
| --- | --- | --- |
| `exact` | 与上一版本的 `source_sha256` 相同 | 自动复用 |
| `verified-compatible` | `source_sha256` 变了，且存在**显式授权记录**（`{logical_key: {from_sha256, to_sha256, evidence}}`） | 可复用 |
| `suggested` | 只有路径/文件名/文本/视觉相似，无授权记录 | **禁止自动进入正式 generated**；仅供人工参考 |
| `blocked` | 无法证明兼容 | 禁止复用 |

### 5.2 `translation_status`：译文内容是否变化

| 值 | 判定 | 处置 |
| --- | --- | --- |
| `untranslated` | 尚无译文（`zh` 为空） | 不得发布 |
| `pending` | 机翻初稿，待审 | 不得发布 |
| `accepted` | 已通过质量审校 | 可发布 |
| `modified` | 译文与上一版本不同 | 可发布 |
| `reused` | 译文与上一版本逐字节相同 | 可发布，且复用同一对象 |

### 5.3 准入规则与关键反例

**只有** `reuse_status ∈ {exact, verified-compatible}` **且** `translation_status ∈ {accepted, modified, reused}`
的条目可以进入 `generated/<asset_version>/`；其余四类（`suggested` / `blocked` / `untranslated` / `pending`）
一律拒绝并带明确 reason。

**必须钉死的反例**：`source_sha256` 不变、译文被修改时，是
`reuse_status = exact` + `translation_status = modified`，**不得**错标为 `verified-compatible`
（后者是"官方源变了但被验证过"的意思，与译文改动无关）。已发布清单中的 `verified-compatible` = 该来源变更经过专项验证；
译文改动永远不构成来源兼容性证据。

运行时语义：`translated_sha256` 变化 → 生成**新的内容寻址对象**；只有内容完全相同的二进制才共享对象。
"官方源未变但译文被改"是正常且常见的情形（重新翻译、t2s 修正），不应被拒绝，也不应触发生成新目录。

## 6. 对象生命周期

| 事件 | 对象后果 |
| --- | --- |
| `put_object` 首次写入 | 新对象产生 |
| 相同字节再次写入 | 去重：不重写、不新增 |
| 同 `asset_version` 再次成功构建 | 候选中替换 manifest，扫描所有保留版本并清理未引用对象，通过校验后才整体提升 |
| 不同 `asset_version` 引用了同一字节 | 同一对象被两份 manifest 引用，不复制 |
| 显式 `prune_orphans(dry_run=False)` | 删除 `objects/sha256/**` 中**未被任何保留版本 manifest 引用**的对象 |

原则：

- 入口 `materialize_generated_release.py` 的候选事务默认保留孤儿对象（`prune_orphans=False`）；只有显式传入 `--prune-orphans` 才请求事务内扫描全部保留版本的引用并清理孤儿，`--no-prune-orphans` 保留兼容。独立 CLI `prune` 仍默认 dry-run，必须显式 `--apply` 才删。任何人工清理仍须另行授权并取得单写窗口，显式参数不替代授权。低层 `put_object` 不是可单独宣称成功的发布入口。
- **中央 workflow 模板完全不做 GC**：`assets-generated.yml.example` 的 step 10 显式传 `--no-prune-orphans` 关闭候选事务内的清理，step 11 只做复验、不执行 `prune`（无 dry-run、无 `--apply`）。因此模板既不清理活的 `generated/`，也不在候选事务里清理。独立 CLI 的 `prune --apply` 仍存在，供人工在有单写窗口时按需使用；模板不替调用方做这件事。
- 扫描范围是 `generated/*/manifest.json` 的**全部保留版本**：只要还有任意版本引用，对象就不算孤儿。
- 引用判定同时采信 `artifact_sha256` 与 `object_path` 内嵌 digest，手工改过的 manifest 不会导致活对象被误删。
- prune 只动 `objects/`，**不碰 Git 历史**；同目录空壳（如被清空的两位分片目录）会被清理。
- 失败构建（`build_status != "success"`）**完全不触碰** `generated/`——连 store 根目录都不创建，
  也不做任何条目校验（参数错误不会留下半成品）。这一条由单测断言，不要为了让测试通过而放宽。

## 7. `[skip ci]` 与孤儿清理的交互

- bot 提交 `generated/` 时 commit message 末尾带 `[skip ci]`，因此该提交不会再次触发同一 workflow；
  workflow 还有一个 `if` 守卫显式拒绝 message 含 `[skip ci]`/`[ci skip]`/`[skip actions]` 的 push
  （`workflow_dispatch` 不受 skip 标记影响，守卫只拦自动触发）。
- **中央 workflow 模板不清理孤儿**：step 10 传 `--no-prune-orphans`，因此成功候选事务内的顺序是
  `stage → build → verify → 原子提升 → git add generated → commit/push`，中间没有清理一步；
  step 11 也不调用 `prune`。去掉 `--no-prune-orphans` 仍默认保留对象；只有在已授权的单写窗口内显式传入 `--prune-orphans`，
  才请求成功候选事务内、提交之前的清理，删除与 manifest 变更在同一个提交里落地，且不得把清理移到提交之后。
  独立 CLI `prune --apply` 是另行授权并取得单写窗口后的人工操作，不属于候选事务内步骤；中央模板仍完全不做 GC。
  应保留 `[skip ci]`、generated-only 路径过滤和单写者并发保护，防止回写自触发。
- `concurrency: assets-generated-release`（`cancel-in-progress: false`）保证一次只有一个发布写者，
  半途被取消不会留下"manifest 已更新、对象已删一半"的中间态。
- 失败语义：`verify` 失败时**绝不执行 prune**（不可重新校验的发布不是发布），更不会提交。

## 8. workflow 步骤与失败语义

中央全表面模板：`.github/workflows/assets-generated.yml.example`。公开仓库已另有真实 `.github/workflows/assets-generated.yml` 与 `scripts/build_generated_release.py`；两者的输入适配和表面覆盖必须分别验收，不能把中央模板的完整回填能力当作公开入口已全部接线。运行状态见 stream STATE 所链交接。

1. checkout（`fetch-depth: 0`，需要完整历史拿 commit id）→ 2. setup Python 3.11 → 3. 安装 Pillow；
4. 从**默认分支 HEAD** 识别版本轴（读 `manifests/asset-version.json`；**不提供** `asset_version`/`source_commit`
   派发输入——能手输的版本就会与分支漂移），并复核"非组合身份"；
5. 从公开 assets-server 取官方基线
   `https://td-assets.bn765.com/<asset_version>/production/2018/Android/<index>`，**fail-closed**：
   非 200 直接失败退出，失败信息写清"需要可用的出网路径"与覆盖变量、且**不断言原因**（403 不是诊断，见 §9 第 2 条），**不静默跳过、不悄悄换镜像**；
6. 校验文本与图片（`scripts/validate_repo.py`）；7. **图片比例检查**（同比例允许更高分辨率，比例偏差 >0.5% 即失败）；
   8. 缩放到游戏尺寸；9. 通过 `--preflight-context` 只读验证显式图片输入（不再重复产生 bundle）；
   10. `scripts/materialize_generated_release.py`（**唯一 materialize 入口**：调用各 surface producer，图片执行独立 repack audit；逐包复核 SHA-256、准入过滤、候选 CAS 去重/manifest/checksums/复验，候选内写入并复验后才事务提升；本模板用 `--no-prune-orphans` 关闭候选内的孤儿清理）。
   图片输入由 `--image-install-manifest`、可选 `--image-original-root` 明确传入；默认工具位于本仓 `tools/mltd_image_localization/`，其他仓布局用 `--image-entry` / `--image-audit` 显式指定。非零预检、缺字段/空审计、审计范围与本次报告不一致均拒绝。
   `assets_generated_index.py` 仍是存储层，不替代来源与图片回填验证。
   该步骤先从 `--input-root` 找已验收 ledger（`ledgers/*.jsonl`，行带 `release_gate=accepted`，由
   `build_translation_release_ledger.py` 产出），没有就直接 `mode="refused"`——该入口在这条路径上**根本不读**
   checkout 的 `locales/`，所以裸 checkout 会在此步 fail-closed 而不是发一份未过发布门的 `generated/`
   （2026-09-29 用布局副本真实执行过，输出 `mode=refused` / `generated_written=false` / 无 `generated/`，
   取证见 `build/runs/.../manifest-field-unification-20260929/runner-wiring-copycheck/copy-check.txt`）；
   **写者来源是显式输入对**：`--assets-writer-root` 指向产品写者仓库（`MLTDTranslationAssets`）的 checkout，
   `--assets-writer-pin` 是其中 `scripts/assets_generated_index.py` 的**原始字节 SHA-256**（不是 Git commit id）。
   **所有模式都必须同时给出，包括 `--preflight-only`**；两者都缺、半对、错 pin、不可调用 API 或坏事务签名，在构造 store、运行 producer 或创建输出根前拒绝。预检不再回退主仓 reader。模板用仓库变量 `ASSETS_WRITER_ROOT` / `ASSETS_WRITER_PIN`
   传入，**不在运行时对 checkout 现算"可信 pin"、不自动找兄弟路径、不下载 checkout、不硬编码机器路径**。
   该对参数由入口的 `resolve_writer_source` 解析：pin 与实际字节不符、或写者缺入口所需 API 时，在构造 store /
   写入前 `RefusedInput`。入口只编译本次读取并核验的同一份原始字节，不读写者 pycache、不执行 root 的包初始化器、不发现兄弟路径、不从环境选择来源。身份、commit、CI 溯源、准入集合、复用账本与 store 均来自该模块；入口证据 hash 使用标准库。写者自己的 `GeneratedStoreError` 转为明确拒绝，其它异常保留原类型。`resolve_writer_source(None, ..., require_pair=True)` 保留外部调用形状；旧 module 参数或 `require_pair=False` 也不能启用未 pin 回退。
   报告 gate 也重新经**同一个** `resolve_writer_source` 复核该对，并且要求报告的
   `writer_source` 是一个 `kind=pinned` / `verified=true` / `sha256==pin` 的字典，同时把报告的 `asset_version`
   与本 job 从 HEAD 读出的 AV 严格对比；缺字段、非 pinned、`false`、错 sha、版本不符或候选事务
   `transaction.pruned_orphans != false` 一律 fail closed。
   11. 复验：先按 step 10 的报告 gate 确认本次 run 真的写出（`generated_written`）、复验过（`verify_ok`）、并经
   staged-store 事务提升（`transaction.staged`/`promoted`）且未清理孤儿（`pruned_orphans=false`）、报告的
   `asset_version` 与 AV 一致；**通过后调用同一已核 pin 的写者模块的 `GeneratedStore.verify_release`** 重新哈希该版本。
   step 11 **不执行** `prune`（无 dry-run、无 `--apply`，不做 GC），也**不直接以 CLI 跑 checkout 里的
   `scripts/assets_generated_index.py`**——复验走已核 pin 的模块 API，与 step 10 发布所用字节一致。缺 pin、
   坏 source、坏报告都在 store 构造 / verify 前拒绝。12. **唯一的 Git 写入**：`git add generated` + bot 提交（含 `[skip ci]`），非快进或输入变化必须拒绝，不能强推或盲目 rebase 旧产物。

失败语义：任何一步失败 → **不提交、不修改 `generated/`**。权限最小化：`permissions: contents: write`，
且只用于提交 `generated/`。

**本模板当前非 active**：它是仓库内的中央全表面设计模板，**本轮未在真实 GitHub Actions 上运行过它**；
step 10/11 的写者输入（`ASSETS_WRITER_ROOT`/`ASSETS_WRITER_PIN`）、step 9 的 reviewed image 输入、
step 10 的 accepted ledger 都仍需外部提供。公开仓另有真实的文本入口，与这里的中央多 surface 设计**不等价**；
不能因为公开文本 run 成功就认为本模板的全部表面（尤其图片回填）已闭环。本节描述的接线不等于真实 CI 已迁移或发布，
也不表示已授权任何自动 GC（本模板不清理）。

## 9. 外部验收边界

运行状态以 stream STATE 链接的交接、真实 GitHub run 与精确输入 hash 为准，不以本模板是否启用推断整个公开仓库状态。下列验收分开记账：

1. **公开 Actions**：默认分支 HEAD 的自动和无参数手动运行走同一入口，最终成功产出并提交 generated；只读校验绿或 workflow 存在不等于生成成功。
2. **官方基线与源质量**：使用精确版本的 index/对象 URL，保留获取与 source hash 证据。真实 runner 已进入校验器但因占位符损坏失败，应记作源数据门禁失败，不误记为缺 runner；不得删减校验或静默过滤坏行使其变绿。
3. **图片和 Unity3D**：可编辑普通图片必须和 logical_key、尺寸、原始 bundle/Texture2D 身份及 hash 绑定。分发摘要不是逐纹理 install manifest；缺输入时标为 external-blocked，不伪造 ledger。历史手工回填、本地 fixture 与当前公开 Runner 的回填验收分开。
4. **发布**：成功的候选生成、bot 写回、NAS 同步/HTTP 和真机消费是不同证据。一次失败必须保留上一成功 generated，不能宣称生产闭环。

## 10. 本地可验证范围

`scripts/test_materialize_generated_release.py` 默认发现 70 项 unit，用显式 pin 的本仓 legacy 文件作为自足测试工具；这不构成入口的运行时依赖，也不冒称产品源码。干净子进程用 `meta_path` 阻断 `scripts.assets_generated_index` 导入并先执行负控，再验证入口导入、`--help`、预检与 materialize。默认 unittest/pytest 集合不包含外部产品 probe，也不因缺产品参数产生 skip；**默认 unit/CI 不能宣称已验证产品来源**。

显式 CLI 同时传 `--product-writer-root` / `--product-writer-pin` 时，先运行正常 unit 集合（或命令选择的 unit 项）；成功后另用一个独立 TestSuite 执行同文件的 `verify_explicit_product_rawpin_preflight_materialize_and_no_gc`，分别报告 unit 与 probe 结果，失败均非零退出。半对参数在 parser 处硬拒；直接运行 verify 方法而缺 pair 也失败，绝不返回通过。该 probe 用真实产品单文件与隔离文本 stub/ledger 检验产品布局、runtime_path、报告来源、默认 noGC 与损坏保留清单的拒绝，可用 `--probe-evidence-dir` 留存报告；不读取产品 generated 或生产输入，不执行真实 producer。

`scripts/assets_generated_index.py` 的模块级 API 与 CLI（`build` / `verify` / `prune` / `reuse` / `list`）
**不依赖网络**，可在临时目录完整跑通；回归见 `scripts/test_assets_generated_index.py`
（内容寻址去重、孤儿清理、失败构建早退、四类拒绝、`exact`+`modified` 反例、`verified-compatible` 授权门槛、
篡改/缺失对象校验失败、组合版本拒绝、同版本二次构建）。schema 用 draft 2020-12 校验，
测试在安装 `jsonschema` 时会额外验证"产出清单合规 + 组合身份被拒"。
