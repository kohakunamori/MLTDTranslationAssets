# 日文原图

这些原图按 `manifests/images.manifest.json` 的 `original.relative_path` 发布，逐文件校验对应的 `original.sha256`，用于查阅站的原图与汉化图对照。

历史 `original/` 子目录保留源 Texture2D 的像素和尺寸。纹理中可能包含旋转后的 Sprite 碎片，查阅站通过源图 SHA-256 绑定的布局拼接为完整画面后展示、放大及下载，不直接把整张纹理图集当作完整图片。

`complete/` 保存从尚无审核译图的源资源导出的完整原图，使用逐像素验证的无损 WebP 和 SHA-256 文件名去重。来源、版本、原始资源包哈希、Sprite 顺序与布局见 `manifests/image-originals.manifest.json`；多个包可共用同一文件，但保留各自任务身份。

更新命令（需要 Pillow 和 UnityPy；仅解包，不生成译图）：

```sh
python scripts/export_portal_originals.py --work local-data/work/portal-originals-1077750 --workers 4
```

工作目录是可复用的官方资源下载与导出缓存，完整运行后原子更新原图目录。`--limit N` 仅生成缓存中的样本目录，不覆盖发布目录。无法确认的布局保留在 `unresolved`，不使用原始图集冒充完整图。发布时提交目录中引用的文件和清单，并由查阅站重建索引。

此目录提供源图，不属于审核通过的汉化图片，也不参与汉化替换包的发布选择。
