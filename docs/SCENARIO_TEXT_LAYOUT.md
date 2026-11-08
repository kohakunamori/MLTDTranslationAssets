# Scenario 候选排版门禁

本规则是 EAW 估算，不是 9.0.350 字体实测。50/54 显示单位分别是目标/候选上限，不能据此宣称客户端不会截断。

- 全角/W/F 字符计 2，其他字符计 1，组合附加符计 0。
- 动态占位符暂预留 20 单位（10 全角）；这是保守假设，后续必须核验真实玩家名限制。
- card、event-unit 最多 2 行；story、birth、dialogue 保留源文本 3–5 行例外。
- synopsis 使用 40 单位/5 行；chat 使用 34 单位/3 行；title、master、未知类别不自动处理。
- 行数少于源文本记为 `line_count_mismatch`，仅作线索，不单独判错；重排时优先保持源文本行数，避免把官方两行又合并成一行。
- 句首省略号允许，续行遵守中文禁则。
- ASCII 词/数字、占位符为不可分割原子。未知控制符/标签禁止自动搬动。
- DP 优先原换行和标点，再按宽度均衡。字符级兜底不是中文分词，也不是经过模型验证的语义切分。
- 超长只报告，不截断、不自动新增消息/分页。这样不改语音、时序、消息边界。

## 入口

```powershell
python scripts/validate_repo.py --layout-report <report.json>
python scripts/scenario_text_layout.py --category event-unit --strict
python scripts/scenario_text_layout.py --category event-unit --apply
python -m pytest scripts/test_scenario_text_layout.py -q -x --maxfail=3
```

`--apply` 在所有候选预检通过后才写文件；单文件采用原子替换。多个文件不是文件系统事务，中途 IO 故障会显式抛出，已写内容可通过 Git 审核/恢复。保留原 JSONL 压缩格式和 LF/CRLF；只更改 zh 换行，其他字段不变。

CI 全仓输出 artifact（report，不阻断历史问题）；首批 event-unit 已清零候选硬问题，单独启用 `--strict`。其他类别暂不强制自动规整，也尚未接入草稿生成前的统一预算；全仓防退化仍是后续工作，不能宣称全部完成。

## 首批结果（2026-11-08）

基线 HEAD 01553f422。event-unit 628 条、325 个文件规整；对照原 main 审计 628 条旧/新，非换行字符、源文本、源哈希、状态、时间戳等字段变化为 0，且无超过 2 行的结果。抽样 `event_unit_talk_1640`：`[12,33]` 两行 → 自动重排为 `[20,25]` 两行，保持两行结构。

全仓最终 report：51,192 条有候选发现，其中 34,026 条有阻断型硬问题；46,728 条 `line_count_mismatch`、5,264 条 `over_target` 仅报告。按类别 story 30,387 / card 18,652 / birth 1,492 / event-unit 541 / dialogue 120。event-unit 剩余 504 条短译文行数失配与 140 条 51–54 单位预警，阻断型问题 0；短句不强行拆成两行，需视觉复核，尚未真机确认。

第二批（基线 c5999eaba）先做 birth：1,201 条、807 个文件，0 条阻断；对照 c5999eaba 审计仅 `zh` 换行变化，其他字段 0 变化。birth 剩余 260 条行数失配、144 条 51–54 单位预警，阻断型 0，已加入 `--strict`。card 预检 12,954 条候选、8 条超出候选预算（如 `card_blst_004yuk0144_1010_004yuk`），整批 all-or-nothing 未写入，留待单独处理。story、dialogue 尚未规整。

## 发布前/后边界

包内 round-trip、9.0.350 实际字体/框宽/行间距/截断行为仍需客户端侧核验。尤其动态名字、emoji、富文本不能仅凭 EAW 推导。未验证前不自动分页、不改客户端字号、不把 54 当作官方硬上限。
