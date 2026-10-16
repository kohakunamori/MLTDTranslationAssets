# MLTD generated-assets 历史分发闭包

本目录保留一个旧版只读分发方案的源码。当前生产链路为统一 CI → 私有 R2 → Cloudflare Worker，
此闭包已不属于当前发布路径。

## 服务契约

| 服务 | 作用 |
| --- | --- |
| `generated-assets` | 只读服务一个经过校验的版本；只监听 loopback，未命中时可从官方资源回退读取 |
| `generated-assets-sync` | 按固定间隔读取公开仓库中的发布清单，验证对象指纹后更新当前版本 |

服务使用的归档目录、发布目录、缓存目录和代理地址由本机 `.env` 提供。复制
`.env.example` 为 `.env`，按需填写本机路径；这些值不会提交到仓库。

逻辑路由形态：

```text
https://<asset-host>/generated-assets/<asset_version>/<logical_path>
   -> reverse proxy
   -> http://127.0.0.1:18765/assets/<asset_version>/<logical_path>
   -> configured published root
```

`deployed.json` 只记录仓库内的源文件和服务契约，不保存机器名、部署路径、网络测量或远端文件哈希。
`test_closure.py` 检查源文件存在、服务绑定和只读路由边界。

## 本机部署配置

`asset-server/Deploy-GeneratedAssets.ps1` 需要本机的 SSH alias、项目目录、镜像目录和 vhost 目录。
将 `Deploy-GeneratedAssets.local.ps1.example` 复制为同目录下的
`Deploy-GeneratedAssets.local.ps1` 并填写这些值，或通过 `MLTD_DEPLOY_*` 环境变量传入。
本机配置文件已加入 Git 忽略规则。

## 离线回归

```bash
python -m pytest asset-server/generated-assets -q
```

回归覆盖版本发现、失败时保留旧版本、单写者锁、loopback 只读服务、可配置数据卷、官方回退缓存
和 vhost 的只读代理规则。
