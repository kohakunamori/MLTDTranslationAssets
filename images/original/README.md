# 日文原图

这些原图按 `manifests/images.manifest.json` 的 `original.relative_path` 发布，逐文件校验对应的 `original.sha256`，用于查阅站的原图与汉化图对照。

文件保留源 Texture2D 的像素和尺寸。纹理中可能包含旋转后的 Sprite 碎片，查阅站通过源图 SHA-256 绑定的布局拼接为完整画面后展示、放大及下载，不直接把整张纹理图集当作完整图片。

此目录提供源图，不属于审核通过的汉化图片，也不参与汉化替换包的发布选择。
