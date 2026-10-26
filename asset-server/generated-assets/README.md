# MLTD generated-assets 历史分发闭包

本目录保留一个旧版只读分发方案的源码原型。当前生产链路为统一 CI → 私有 R2 →
Cloudflare Worker，此闭包已不属于当前发布路径，也不再提供部署或运行说明。

`deployed.json` 只记录仓库内的源文件清单，不保存机器名、部署路径、网络测量或远端
文件哈希。`test_closure.py` 只做离线契约检查（源文件存在、只读路由边界）。

## 离线回归

```bash
python -m pytest asset-server/generated-assets -q
```
