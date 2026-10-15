# 汉化源文件与发布文件的目录边界

建议把人工维护的汉化成果统一到一个语义明确的根目录，再按素材类型细分：

```text
localization/
  text/                 # 原 locales/，其下继续按业务类别分目录
  lyrics/               # 歌词及其翻译元数据
  images/
    localized/          # 已完成的汉化 PNG
    metadata/           # 源图映射、审核和来源信息
  glossary/             # 共享术语
```

不建议将文字放进 `images/`，也不建议把重打包 Bundle 放进源图片目录。
统一根目录便于区分“维护汉化成果”和“构建、发布资源”。文字和图片的流程不同，目录仍分开。

本次 R2 迁移暂时保留现有 `locales/`、`lyrics/`、`images/localized/`、`glossary/` 的源路径。
原因是配套查阅站 `MLTDTranslationPortal/public/lib/taxonomy.js` 等代码用这些路径生成 GitHub 写入位置，
仅在本仓库移动会使查阅站写入旧位置。实际改名应协调两个仓库，并同步迁移 CI 路径筛选、
输入指纹、schema、审核入口和清单生产者；不用符号链接维持两个可写入口。

发布端不按上述源目录存放，也不按游戏版本重复存储字节：

```text
R2 bucket: mltd-localized-assets（私有）
  objects/sha256/<artifact-hash>      # 文字、歌词、图片 Bundle 和统一索引共用
  build-cache/sha256/<archive-hash>   # 构建解析缓存

Git:
  generated/<asset-version>/manifest.json
  generated/<asset-version>/checksums.txt
  generated/r2-storage.json          # 已验证对象大小、缓存地址、旧覆盖包映射
  manifests/                        # 游戏资源版本和查阅站元数据
```

版本清单决定“某个游戏版本的某个路径引用哪个哈希”，因此相同成果跨版本共用一个对象。
这次迁移不改变兼容性规则：只有已经验证兼容的成果可复用，不能仅靠逻辑名相同跨版本套用。
具体缓存和发布机制见 [R2 网关说明](../asset-server/cloudflare/README.md)。
