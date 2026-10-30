# 第二轮：大模型汉化提示词与生产实测

日期：2026-10-29。接续[人工参考调研与优化计划](TRANSLATION_QUALITY.md)。本轮已把精简中文提示词、较完整的日文场景、说话者信息及有界异常重译接入生产入口，并修订三个有明确机器来源的歌词槽。小样本结果支持继续使用和复核，尚不能证明全库准确率提升或消除语义错误。

## 实际改变的模型输入

- 默认系统提示词改为 `mltd-zhcn-natural-v2`，要求先理解话语功能，再组织自然中文，保留主客体、否定、条件、意象、语气和未完句。核心规则由 11,835 字符精简为 1,462 字符；附当前术语与身份资料后的完整文本由 13,375 降至 3,002 字符。这是字符数，不是 token、费用或翻译准确率。
- 每个场景提供前后三个日文槽及各句的说话者代码，最多三个复用用例。连续编号中断时不跨缺口，master 增量清单不冒充完整对话，邮件不猜测回复分支。场景仅用于理解，答案只能包含当前 SOURCE 的语义。
- 歌词允许自然短句和留白，避免机械直译、解释性扩写及强行押韵；剧情和邮件按当前话语保留礼貌、犹豫、调侃及段落。已有权威术语仍经过确定性掩码保护。
- 非歌词显式提示保留真实换行及空段落；歌词仍按运行时要求每槽一行。模型若照抄日文展示换行，由既有 `join_lyric_lines` 规则衔接，再用与应用、发布相同的歌词校验器检查，不能为凑一行丢掉内容。
- 本地比较工具只允许日文场景字段进入请求，剔除隐藏参考及旧译注释；生产修复任务仍可显式携带待修旧译作为负面证据。第三方参考全文没有进入本轮翻译或审校请求。

主要代码在 `pipelines/text/mltd_natural_prompt.py`、`asset_translation_context.py` 与 `translate_mltd_api_pool.py`。保留 `policy_style="legacy"` 和 `build_legacy_user_prompt` 供固定对照使用；命令行生产入口默认采用新方案。

## 修复实际调用不兼容

生产配置的模型别名为 `imas-trans`，并非固定底层型号。本轮实测低推理档 `low` 返回 HTTP 400，错误明确列出启用推理时支持 `high / xhigh / max`；`none` 可正常调用。

因此公共模型配置增加 `min_reasoning_effort="high"`：仅把已启用但低于下限的档位提高到 high，显式 none 仍保持关闭。该规则同时覆盖按任务选择、带上下文的短标题、空响应后的自适应重试和真实请求；保存的 provenance 与发出的参数一致。通用 preset 的 low 仍可供其他兼容模型使用。

返回值中有型号时另存 `reported_model`，不能把请求别名当成已验证的底层身份。180 次留出集请求中，164 次返回 `gpt-5.6-luna` 字符串，16 次未返回型号及有效用量；这只是网关的报告，未独立核实其真实性。缺失用量不能当零成本，不据此宣称节费比例。

## 对照设计与原始结果

先用 12 条开发样本迭代提示词，再冻结 v4 比较组合；开发集与留出集按歌曲、剧情文件分组排除重叠。留出集为 60 条歌词、30 条剧情/对话/邮件，后者覆盖 main / event / card / birth / MB / job 六类，每类五条。按原文哈希去重，每条均保留相邻日文语境。

参考材料来自前轮核验的本地歌词缓存，以及单独保存的历史官方繁中剧情译文。现代剧情视频字幕没有完整逐句对齐，因此这不是全新的现代人译金标准；历史参考也可能有错译、风格与版本差异。留出只针对本轮开发分组，不保证项目历史工作从未看过这些文本。

旧组合采用冻结的旧系统提示词和旧用户提示词、较短语境；新组合采用冻结的精简中文提示词和较长语境。两组使用同一生产别名、温度及有效任务参数，随机交错调用，每组每条各一次；候选另行随机分配 A/B，参考译文保持隐藏。**本实验同时改变提示词与上下文，不能单独归因于其中一项。**

| 记录 | 旧组合 | 新组合 | 含义 |
| --- | ---: | ---: | --- |
| 完成生成请求 | 90/90 | 90/90 | 无传输失败，不表示语义全对 |
| Agent 按日文与语境逐条盲读偏好 | 13 | 18 | 其余 59 组接近；不是独立人工审校或准确率 |
| 非歌词真实换行计数不一致 | 15 | 2 | 格式诊断，不自动等于显示故障或语义错误 |
| 歌词原始返回多行 | 0 | 1 | 应用前按单槽规则衔接；不能混算为剧情换行 |

Agent 审读在查看 A/B 对应关系前记录，逐条保留理由，格式另行统计。偏好有主观性，不能把 18 对 13 写成提升百分比。旧、新方案均仍存在省略、语气及主体判断问题。

独立模型审校遇到额度与可用性问题：Gemini 在开发轮后耗尽额度；随后使用另一网关别名 `deepseek-v4.1-flash` 仅完成 7/90 组（新胜 1、旧胜 1、接近 5），之后出现 429/503。该数据不完整，未据此判定质量通过。实验工具现遇 429 停止排队调用，连续三次其他失败也停止，保留已完成结果；不完整运行返回失败，汇总须显式 `--allow-partial`，不能静默冒充完整评测。

## 观察到的退步及处理

原始留出结果发现一次新引入的未分配 Unicode 字符、一次西里尔文字母尾巴、一次播放器称呼重复，以及一次歌词把前一句解释进当前槽。其中三项出现在新组合、一项出现在旧组合。新增规则为本次观察后的修复，**不属于预先登记的盲测指标**。

新增未分配字符/代理码点、西里尔文字母和非原文有意重复的 `{$P$}先生先生` 会被拒收。源文已有相同字符时不因字符集本身拒收。

对有语境的歌词，去除保护标记后，译文长度超过 `max(26, 2 × 原文长度)` 时标记 `possible_context_spill`，提示仅重译 SOURCE、保留指代及未完句，不展开前后槽。它只是保守的源文范围复核启发式，可能拦下合法长译，也会漏掉短小增译；不能标成已确认误译。通过已有次数上限定向重译，仍触发规则的草稿留待复核，不靠无限调用放行。

将规则事后应用到原始 180 条输出，正好标出上述四条，未在该样本额外拦下其他条目。这不是全库误报率。用生产接受/重试路径另取这四条及两条实际机器译文问题做定向试译，6/6 首次生成通过规则，逐条复读也未再出现串入邻句、异常字符或重复称呼；另外用模拟传输测试证实首稿被拒后会收到针对 SOURCE 范围的反馈，第二稿合格才被接受。定向复测不能与原始盲测混合计分。

后续独立审校又发现两项门禁缺口并已修复：歌词若显式带 `translation_stage`，现在与 locale 一样拒绝无效来源组合（`pending` 必须是 `llm_translated`，声明来源的 `accepted` 必须是 `llm_translated` 或 `human_translated`）；历史歌词中大量没有该字段的旧行继续按既有兼容规则保留，不能据此倒推其来源。歌词角括号现在同时检查源/译符号顺序和两侧平衡，防止同样数量但顺序错乱的 Unity 标记进入发布。

## 实际歌词修订与人工成果保护

只修改原有 `translation_stage=llm_translated` 的 6 个槽位、5 个原文身份：

| 位置 | 旧译 | 本轮采用的机器候选 |
| --- | --- | --- |
| `scrobj_bntht+` index 729、`scrobj_newbn+` index 482 | 大家一起 手工制作舞台 | 大家一起亲手打造舞台 |
| `scrobj_everl+` index 32 | 一边确认着你的睡脸 | 望着你的睡颜 |
| `locales/master/official-1077790-untranslated.jsonl` `igp_shop_order_warning_message_tail_02` | となります | 成为 |
| `locales/master/official-1077750-untranslated.jsonl` `igp_021mat_chat_12108_121083_2001_p_a` | 含西里尔垃圾的未完句 | 对于茉莉来说，不是我强加给她的偶像形象，\n而是 |
| `scrobj_lmlad+` index 1069 | 就这样　卸下螺丝のまま……不对，就这样　保持卸下螺丝的状态 | 就这样　保持螺丝拆下的状态 |

候选仅根据日文与日文语境或同一项目已有确定术语重新审读；输入未包含网络人译全文。`translation_stage` 继续保留机器来源，更新日期并重建 `all_lyrics.jsonl`。没有把这次 Agent 复读标为人工翻译。来源未明及原有人工条目未自动覆盖。

其中 `scrobj_everl+` index 32 的“望着你的睡颜”中文更自然，但比日文 `確かめながら` 弱化了“确认/一边……一边……”的持续承接；本轮没有把这个可争议的风格取舍伪装成确定性修复，保留为人工复核项。

网络译文只用于本地比较和发现问题，没有把第三方译文全文提交公开仓库或用模型改写掩盖其来源。原有[版权声明](../COPYRIGHT.md)和[鸣谢](../ACKNOWLEDGEMENTS.md)继续适用，鸣谢不能替代许可。

## 重现与后续验收

`scripts/translation_quality_experiment.py` 提供有界生成、固定输入续跑、独立 A/B 映射和部分审校汇总。最多 120 条、生成并发不超过五；输出路径必须在仓库外，不直接写正式译文。恢复时验证原文、提示词、型号与请求参数，发生变化须换结果文件，防止新旧实验混杂。

```powershell
python scripts/translation_quality_experiment.py run --input '<study>/holdout-source.jsonl' --config '<local-config>' --production --system-prompt '<study>/baseline-system.md' --candidate-system-prompt '<study>/candidate-natural-system-v4.md' --output '<study>/results.jsonl'
python scripts/translation_quality_experiment.py blind --input '<study>/holdout-source.jsonl' --results '<study>/results.jsonl' --output '<study>/blind.jsonl' --mapping '<study>/mapping.jsonl'
python scripts/translation_quality_experiment.py judge --input '<study>/blind.jsonl' --config '<local-config>' --model '<available-judge-alias>' --output '<study>/votes.jsonl'
python scripts/translation_quality_experiment.py summarize --mapping '<study>/mapping.jsonl' --votes '<study>/votes.jsonl' --output '<study>/summary.json'
```

`<study>` 是未提交的本地研究目录，包含原文身份、原始输出、提示词、盲读理由及单独的隐藏参考。公开只登记下列文件字节哈希，供持有同一本地证据的人核验；不以公开链接替代第三方使用许可：

| 证据文件 | SHA-256 |
| --- | --- |
| `holdout-source.jsonl` | `a841b62d20205d69c88479e67ac1e20a536ea072e1c150925def0dbdb8306bda` |
| `baseline-system.md` | `befee167c923164d8d1549007b17f3898674e4d6ee0f86886c496971e53fe88b` |
| `candidate-natural-system-v4.md` | `bf82923410bee1144612cca8954bd5a87232709e3f8635278ecc9228f64db14f` |
| `holdout-results-v4.jsonl` | `752cf7b5379757a84e1bcfd0d92b7be8dba21f97a49a1e4fa0899e4e642f4493` |

`validate_repo.py` 是结构发布门禁：它检查 JSONL、source hash、状态、保留标记、保留分隔符和歌词显示约束，但不会对全部历史 accepted 行重放 `mltd_translation_quality.evaluate_row` 的语义启发式。新模型草稿在队列接受前仍会经过该质量评估；历史 accepted 行的重放结果只应作为人工复核队列，不能直接变成全库硬拒收。修复前的一次本地扫描覆盖 27,061 条 machine-stage locale 和 1,487 条 machine-stage lyric：locale 有 1 条确定性 REJECT、1,403 条 REVIEW，lyrics 有 33 条 REVIEW；其中许多 REVIEW 是主体、省略、口吻或术语候选，不能自动判错。代码验证覆盖生产请求参数、实际保存的来源、邻句与说话者、异常拒收后的定向重译、无参考泄漏、恢复一致性和额度熔断，已加入发布离线门禁；仓库数据另跑 `validate_repo.py`。这些验证证明链路与数据契约，不证明全部翻译正确。

独立人工验收、完整模型审校、提示词/上下文贡献拆分，以及完整邮件分支语境仍是后续工作。尤其省略主体、双关和多场景复用的语义问题，不能只靠长度或字符规则解决；本轮没有为此改动跨仓库的译文身份契约。
