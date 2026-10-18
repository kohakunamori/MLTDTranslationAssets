# R2 与 Gateway 消耗监控

## 数据源 A：R2 Data Access Logs（Bucket 层，计费相关）

Cloudflare 官方文档：https://developers.cloudflare.com/r2/buckets/data-access-logs/

需要在 Cloudflare Dashboard 手动确认启用，这是 Bucket 开关，与 Wrangler Worker
Observability 不同。当前没有文档支持的自动配置方式，也尚未确认你账户中是否已经开启。

1. 打开 https://dash.cloudflare.com/ → R2 Object Storage。
2. 选择 mltd-localized-assets → Settings → Data Access Logs → Enabled。
3. 点击 View logs in Workers Observability，切换 r2 dataset。
4. 按 bucket、interface=Workers、scriptName、action、requestMetadata.colo 分组。
5. 对 mltd-assets（客户端网关）和 mltd-localized-publisher（CI）分别核对
   GetObject、HeadObject、ListObjectsV2、PutObject。

R2 Data Access Logs 只能看到开启后的操作，异步、尽力投递，不记录 HTTP 400+
失败操作，保留七天。2026-12-01 起适用 Cloudflare Observability 定价。
R2 Metrics/Analytics 仍然是账户费用的权威对照。

## 数据源 B：Gateway 低开销缓存采样日志

网关 Worker 在每次普通请求后打印一条标记为 mltd_gateway_metrics 的结构化 JSON，
Cloudflare Workers Observability head sampling 设置为 5%，Publisher Worker 同样是 5%。
Gateway 日志不包含玩家 IP、完整 URL、文件名、哈希或 Ray ID，仅包含：

- colo、method、status、range
- asset_source、cache（pack-hit、pack-miss、hit、miss、hit-range、miss-range、bypass）
- r2_pack_gets、r2_object_gets、r2_get_attempts
- shared_pack_inflight（是否并发共享同一 pack GET）

使用 GATEWAY_METRICS_ENABLED=true 的 Worker 环境变量开关。
HEAD/304、同 pack 并发、官方 CDN 回落不会人为伪造成新的 R2 GET。
注意 pack-miss 响应也可能是同一次 GET 的并发等待者，必须同时观察
shared_pack_inflight 和 r2_get_attempts。

Workers Logs 中搜索 mltd_gateway_metrics。
这些记录只代表抽样，不等于总请求量；实际 R2 操作按 Data Access Logs
和 R2 Billing Metrics 对照，不要将 HTTP 次数直接计为 Class B。

## 安全汇总两个导出文件

在 Observability 导出同一时间窗的 r2 dataset 与 cloudflare-workers dataset，
JSON 数组或 JSONL 都支持：

    python scripts/analyze_r2_observability.py r2-logs.json worker-logs.json
    python scripts/analyze_r2_observability.py --json r2-logs.json worker-logs.json

输出仅按 Worker、操作类型、colo 和 cache 类型汇总，不输出 IP、URL 或资源 Key。
没有某一种数据源时，该源的统计为零，而非真正的使用量为零。

## CI 的统计

CI 已有每个脚本的 R2 CI operations 及 Step Summary，
参见 docs/R2_CI_COST_OPERATIONS.md。Publisher HTTP /batch 不等于单次 R2
GetObject，批量中每个对象都计一次 Class B。
