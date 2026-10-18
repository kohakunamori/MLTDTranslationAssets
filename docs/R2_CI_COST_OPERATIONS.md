# CI 对 R2 的操作费用：和客户端流量分开统计

## 哪些操作计费

| CI 行为 | R2 实际操作 | 计费 |
| --- | --- | --- |
| 资源恢复 / pack 读取 / 解析缓存分片读取 | GetObject | Class B；一个 HTTP batch 中每个对象各算一次 |
| 上传前检查远端已存在对象 | ListObjects，每页最多 1000 对象 | Class A；不是 Class B |
| 新对象与 pack 上传 | PutObject | Class A；已有对象仅本地 SHA-256 和库存对照，不重复 HEAD |
| 少数并发上传冲突验证 | HeadObject | Class B；由 Publisher 内部执行，不包含在 CLI 估算 |
| 成功上线后的 GC | ListObjects 与 DeleteObject | LIST 为 Class A，DeleteObject 免费 |
| 客户端请求线上资产 | Worker R2 GET | 单独的线上访问统计，不属于 CI |

R2 Standard 当前免费额度：Class A 100 万次/月，Class B 1000 万次/月。
脚本的本地统计不等于 Cloudflare 账单。

## 实际已知成本

2026-10-18 第一次升级前完整恢复日志：53 个大对象单独下载，
426 个批量 HTTP 请求，其内部逐个 GetObject。HTTP 数量不是 Class B 数量。
当前清单约 13,525 个对象，新的 256 KiB 打包阈值预计形成约 101 个 pack、
218 个未打包对象，加上 13 个解析缓存分片。缺失全部本地对象时，
一次完整恢复粗估 332 次 Class B（101+218+13），随版本、overlay 和重试变化。

未变更构建输入时，已有 should_build_release gate 完全跳过 R2 restore、publish。
Gateway 发布仅抽样恢复 3 个文件和官方索引；GC 执行安全库存检查。

## 预算保护与逐次统计

每次运行脚本会打印 R2 CI operations 并写入 GitHub Actions Step Summary：

- GET：批量请求按内部对象个数统计，重复尝试同样计入；
- LIST 按每一分页记录；PUT 按实际请求记录；
- DeleteObject 为免费操作，单独记录；
- 已存在并通过库存检查跳过 PUT 的数量单列；
- batch HTTP 数量仅做诊断，不能伪装成节省的 Class B 数量。

R2_CI_CLASS_B_LIMIT 是可选的单次 CLI 上限，超限会在下一次 GET 前停止。
工作流设 restore 3000，publish 100，Gateway sample 100，GC 100。
它不是月度配额，不能拦截其他 Worker 或全账户操作。
真正的费用需在 Cloudflare Dashboard R2 Analytics 查看。
