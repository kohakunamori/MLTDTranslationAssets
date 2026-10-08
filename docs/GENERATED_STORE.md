# generated writer 回归维护契约

Assets 产品仓的 `scripts/assets_generated_index.py`、`scripts/test_assets_generated_index.py` 和 `schema/assets-generated-manifest.schema.json` 是维护源。日后修改 writer 在本仓修实现和回归，无需进入 mltd-current。本文只记录回归迁移 candidate，不表示上线或业务验收。

本次来源边界：JP 9.0.200 arm64 / frozen asset 1077100；其他版本号仅是合成共存 fixture。对象新输出为 `objects/sha256/<digest>`（flat）；`objects/sha256/<aa>/<digest>`（sharded）仅保留旧产物读取兼容。schema 两侧差异仅是 object_path/runtime_path 描述，迁入断言以产品 schema 为准。

原有两个产品用例完整保留：重复 runtime_path 拒绝前不写字节，以及唯一 runtime_path 成功发布 flat 路径。新增用例来自主仓完整套件，适配仅涉及 schema 路径、布局 fixture 和显式关闭 GC。普通事务隔离、失败回滚、pair 原子性、状态门禁、哈希篡改、版本边界、provenance、run-id、resource_kind 安全断言归产品；旧布局全量改写与 GC 行为归档。本轮不修 writer、不新增行为，不要求全绿才迁移。

## 独立运行

在产品根执行：

```powershell
python -X utf8 -B scripts/test_assets_generated_index.py
```

只需本仓公开输入：上述 writer、test 和 schema 三个文件；所有资源 fixture 由 test 在系统临时目录生成。Python 的 jsonschema 是 schema 反例验证的可选依赖：缺包时原有 schema 用例会明确 skip，必须记录；其余用例仍逐次读取产品 schema，缺文件会失败。验证不访问网络/DB/NAS，不读真实 generated，也不跑 GC。`scripts/test_build_generated_release.py` 已有 flat writer/runtime_path 入口契约，本轮只读参考，不复制入口测试或修改其实现。

冷 context 验证应把三个文件按产品布局复制到仓外临时根，以该目录为 cwd，移除兄弟仓路径和 PYTHONPATH，通过 `python -I -X utf8 -B` 执行完整 writer suite；可对 socket 与 prune 入口设置拒绝探针。不得向主仓 fallback。迁移交接中记录唯一一次实际运行命令、全量 pass/fail/skip；失败交给 Assets 在迁移后修，不能靠删断言/静默 skip 求绿。

## 只读归档及逐用例归属

主仓 `scripts/test_assets_generated_index.py` 保留为历史 archive。本轮精确原始字节快照及 SHA-256 在迁移 run 的 `before/5-test_assets_generated_index.py` 和 `before.json`；这里只作来源说明，产品代码不得导入、读取或自动依赖该路径。未迁用例没有作为产品 skip 收集，不能计入 pass；下表逐项说明全部主仓用例及部分子用例的去向。普通事务中原 prune=True 的组合归档，产品只跑 prune=False；回滚断言保留。

| 主仓来源用例及原始行号 | 产品方法 / 归档 | 适配或未迁原因 |
| --- | --- | --- |
| `test_transaction_rewrites_all_retained_flat_references_and_stores_one_object`（120–133） | 只读归档 | 全量改写旧清单及删除旧布局/孤儿，与不迁移、不 GC 的产品边界冲突；保留完整原始源码。 |
| `test_failed_publication_keeps_all_legacy_bytes_and_pairs_unchanged`（135–147） | 只读归档 | 旧布局批量迁移且 prune=True 的失败回滚；该迁移流程归档，普通事务失败回滚另有产品用例。 |
| `test_identical_bytes_are_stored_once_and_never_rewritten`（210–228） | `test_identical_bytes_are_stored_once_and_never_rewritten` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_no_temporary_files_are_left_behind`（230–234） | `test_no_temporary_files_are_left_behind` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_prune_reports_orphans_and_only_deletes_when_applied`（237–262） | 只读归档 | 显式执行 GC；包含 dry-run 不删和已引用对象不删的安全断言，原样归档，不以 skip 冒充通过。 |
| `test_failed_build_does_not_create_or_modify_the_store`（265–272） | `test_failed_build_does_not_create_or_modify_the_store` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_failed_build_leaves_an_existing_store_byte_identical`（274–287） | `test_failed_build_leaves_an_existing_store_byte_identical` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_failed_build_never_validates_entries`（289–294） | `test_failed_build_never_validates_entries` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_inadmissible_statuses_are_rejected_with_reasons`（297–327） | `test_inadmissible_statuses_are_rejected_with_reasons` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_exact_reuse_is_admitted`（329–343） | `test_exact_reuse_is_admitted` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_changed_translation_on_unchanged_source_is_modified_not_verified`（346–393） | `test_changed_translation_on_unchanged_source_is_modified_not_verified` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_unchanged_translation_reuses_the_object`（395–420） | `test_unchanged_translation_reuses_the_object` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_verified_compatible_requires_an_explicit_record`（423–470） | `test_verified_compatible_requires_an_explicit_record` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_unknown_logical_key_is_blocked`（472–481） | `test_unknown_logical_key_is_blocked` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_verify_release_passes_then_fails_on_tamper_and_on_missing_object`（484–506） | `test_verify_release_passes_then_fails_on_tamper_and_on_missing_object` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_verify_fails_when_checksums_disagree_with_the_manifest`（508–520） | `test_verify_fails_when_checksums_disagree_with_the_manifest` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_verify_rejects_a_manifest_that_smuggled_in_a_non_admissible_status`（522–533） | `test_verify_rejects_a_manifest_that_smuggled_in_a_non_admissible_status` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_combined_version_identity_is_refused`（536–559） | `test_combined_version_identity_is_refused` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_second_successful_build_replaces_the_manifest_and_keeps_old_objects`（562–583） | `test_second_successful_build_replaces_the_manifest_and_keeps_old_objects` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_different_asset_versions_coexist`（585–599） | `test_different_asset_versions_coexist` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_cli_build_verify_prune_round_trip`（617–668） | `test_cli_build_verify_round_trip` | build/verify/list/篡改失败部分迁入；从 Repair the tamper 起的 GC dry-run、--apply 与不误删断言原样归档。 |
| `test_cli_failed_build_exits_non_zero_and_touches_nothing`（670–685） | `test_cli_failed_build_exits_non_zero_and_touches_nothing` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_cli_reuse_writes_the_decision_document`（687–712） | `test_cli_reuse_writes_the_decision_document` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_cli_missing_store_fails_closed`（714–721） | `test_cli_missing_store_fails_closed` | 缺失 manifest 的 verify fail-closed 保留；缺失 root 的 prune CLI 子用例归档，不调用 GC 入口。 |
| `test_produced_manifest_conforms_and_combined_version_is_refused`（725–789） | `test_produced_manifest_conforms_and_combined_version_is_refused` | 全部 schema 正反断言保留；从产品 schema/ 读取；新输出断言 flat，历史可读断言 sharded。原 jsonschema 缺包 skip 条件保留并如实计数。 |
| `test_objects_are_sharded_and_the_legacy_flat_path_is_read_only`（792–830） | `test_objects_are_flat_and_the_legacy_shard_is_read_only` | 布局方向翻转为产品 flat，新对象、空对象拒绝、旧 shard 查找/新对象复制且保留旧字节断言均保留；不改写旧清单，不批量迁移。 |
| `test_a_retained_flat_manifest_still_verifies_before_transaction_migration`（832–889） | `test_a_retained_sharded_manifest_verifies_without_rewriting` | 旧 shard 清单及 checksums fixture 同步适配，只读验证/notes 保留，新增字节未变和无 flat 副本断言；GC 半段及引用保护断言原样归档。 |
| `test_a_referenced_digest_keeps_both_layouts_and_an_unreferenced_one_loses_both`（891–925） | 只读归档 | 显式双布局 GC；保留完整已引用副本保护断言于归档，产品本轮不执行 GC。 |
| `test_a_release_whose_checksums_contradict_its_manifest_never_lands`（928–952） | `test_a_release_whose_checksums_contradict_its_manifest_never_lands` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_transaction_publishes_a_whole_candidate_and_leaves_the_live_root_alone`（959–986） | `test_transaction_publishes_a_whole_candidate_and_leaves_the_live_root_alone` | 事务隔离/回滚安全断言迁入；显式 prune=False，旧 prune=True 的 GC 组合仅归档。 |
| `test_transaction_rolls_back_on_an_exception_inside_the_block`（988–1001） | `test_transaction_rolls_back_on_an_exception_inside_the_block` | 事务隔离/回滚安全断言迁入；显式 prune=False，旧 prune=True 的 GC 组合仅归档。 |
| `test_transaction_refuses_a_bad_candidate_and_keeps_the_live_root`（1003–1016） | `test_transaction_refuses_a_bad_candidate_and_keeps_the_live_root` | 事务隔离/回滚安全断言迁入；显式 prune=False，旧 prune=True 的 GC 组合仅归档。 |
| `test_every_switch_failure_point_restores_the_previous_root`（1018–1065） | `test_every_switch_failure_point_restores_the_previous_root` | 事务隔离/回滚安全断言迁入；显式 prune=False，旧 prune=True 的 GC 组合仅归档。 copy_object/两次 switch 故障点保留；prune_object 故障子用例连同其孤儿 fixture 原样归档，不执行。 |
| `test_a_candidate_that_would_drop_a_release_is_refused`（1067–1077） | `test_a_candidate_that_would_drop_a_release_is_refused` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_the_candidate_is_isolated_from_write_through`（1079–1100） | `test_the_candidate_is_isolated_from_write_through` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_failure_before_the_first_move_does_not_touch_the_live_root`（1102–1127） | `test_a_failure_before_the_first_move_does_not_touch_the_live_root` | 事务隔离/回滚安全断言迁入；显式 prune=False，旧 prune=True 的 GC 组合仅归档。 |
| `test_a_first_build_that_fails_after_the_switch_leaves_no_root`（1129–1166） | `test_a_first_build_that_fails_after_the_switch_leaves_no_root` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_failure_while_seeding_removes_the_candidate`（1168–1184） | `test_a_failure_while_seeding_removes_the_candidate` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_input_commits_must_descend_from_the_snapshot_commit`（1187–1205） | `test_input_commits_must_descend_from_the_snapshot_commit` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_an_unanswerable_comparison_is_not_a_pass`（1207–1228） | `test_an_unanswerable_comparison_is_not_a_pass` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_later_snapshot_commit_is_accepted_for_an_earlier_generated_commit`（1230–1243） | `test_a_later_snapshot_commit_is_accepted_for_an_earlier_generated_commit` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_store_checks_the_provenance_of_the_snapshot_it_was_given`（1245–1267） | `test_a_store_checks_the_provenance_of_the_snapshot_it_was_given` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_run_context_is_recorded_and_never_invented`（1270–1297） | `test_a_run_context_is_recorded_and_never_invented` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_the_runner_variable_is_used_when_no_id_is_passed`（1299–1321） | `test_the_runner_variable_is_used_when_no_id_is_passed` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_combined_or_malformed_run_id_is_refused`（1323–1332） | `test_a_combined_or_malformed_run_id_is_refused` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_an_entry_must_declare_its_resource_kind`（1334–1347） | `test_an_entry_must_declare_its_resource_kind` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_corrupt_retained_manifest_missing_the_run_id_fails_verification`（1349–1361） | `test_a_corrupt_retained_manifest_missing_the_run_id_fails_verification` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_runtime_path_is_kept_verbatim_and_verified`（1364–1379） | `test_runtime_path_is_kept_verbatim_and_verified` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_a_manifest_without_runtime_path_stays_valid`（1381–1388） | `test_a_manifest_without_runtime_path_stays_valid` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_unsafe_runtime_path_is_refused_before_anything_is_written`（1390–1402） | `test_unsafe_runtime_path_is_refused_before_anything_is_written` | 原断言迁入；仅产品路径及 fixture 接线。 |
| `test_duplicate_runtime_path_fails_closed_before_any_store_change`（1404–1440） | `test_duplicate_runtime_path_fails_closed_before_any_store_change` | 保留产品现有完整方法；其断言比主仓原用例更强，未替换或弱化。 |

产品独有 `test_unique_runtime_paths_build_completes_to_flat_object_paths` 保持原样，来源是迁移前产品回归。

## 本轮实际结果

唯一一次仓外完整 writer suite：**48 run / 47 pass / 1 fail / 0 error / 0 skip**，测试子进程 exit **1**。冷根为 `C:/Users/gekdanhs/AppData/Local/Temp/assets-writer-cold-vezo1mh6`（结束后自动清理）。

实际子进程命令（cwd 为上述冷根）：

```powershell
C:/Users/gekdanhs/AppData/Local/Programs/Python/Python313/python.exe -I -X utf8 -B C:/Users/gekdanhs/AppData/Local/Temp/assets-writer-cold-vezo1mh6/run_owned_tests.py
```

该临时 runner 只加载复制的产品三个文件，阻断 socket 连接及 `GeneratedStore.prune_orphans`，全量逐用例结果已记录。没有主仓或兄弟树 fallback；jsonschema 本环境可用，schema 正反用例实际 pass，未跳过。未跑真实 generated、网络、DB、NAS 或 GC。48 个产品用例不包含 4 个完整归档用例；部分归档子用例也不计 pass。

仅一条未决项交给 Assets：`test_a_retained_sharded_manifest_verifies_without_rewriting` 的 notes 断言期待 `shard`，现有 writer 明确使用 `retired fan-out`。这是迁入后的措辞接线不匹配，不据此认定 writer 业务缺陷。该用例 `report.ok` 及 notes 非空已通过，失败之后的字节未变 / 未产生 flat 副本断言**未执行**；不能宣称该用例整体通过。保留红项及完整断言，不改 writer，不重跑求绿。

有限下一步：由 Assets 在本仓单独对齐上述历史布局 notes 断言并验证该用例；迁移 candidate 已交付，此问题不阻止废弃主仓维护入口。实现、schema、mirror、workflow 和入口测试均未修改，未提交、推送或部署。
