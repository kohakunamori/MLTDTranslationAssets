# MLTD 简体中文汉化资源

这是《偶像大师 百万现场 剧场时光》（MLTD）的个人汉化项目，用于整理和完善剧情、台词、歌词、图片及术语等中文内容。

汉化内容会持续更新，部分内容可能仍有遗漏或需要修订。

可以在 [汉化查阅站](https://mltd-portal.nyaneko.cn) 浏览内容，安装汉化版请前往 [MLTDTranslationClient](https://github.com/kohakunamori/MLTDTranslationClient/releases)。

## 版权与鸣谢

游戏原作、人工译文、校对、图片及字体等成果的权利归相应权利人所有。本项目为非官方汉化，鸣谢与公开可访问不代表获得使用授权。
现有 LICENSE 仅及许可人有权许可的部分，不覆盖原游戏与无权转授的第三方内容。
请阅读 [版权与使用声明](COPYRIGHT.md)及[鸣谢与参考署名](ACKNOWLEDGEMENTS.md)，复用时保留作者、出处、适用许可及修改说明。

## 贡献

后续开放贡献仍在规划。

## 隐私与非公开信息

本仓库公开可见。请勿在代码、文档、Issue、PR、截图、日志或构建产物中提交自己或他人的个人联系方式、账号/玩家或设备标识、访问令牌、密码、私钥、Cookie、Authorization 请求头或用户数据。也不要公开非公开的 VPS/服务器地址、内网地址、管理端口、SSH/代理配置、本机用户名和真实部署目录。示例请使用占位符；真实配置放在 Git 忽略的本地文件或 CI Secrets，示例配置只放假值。业务需要的公开服务域名可以保留。

提交前请检查 `git diff --cached`，并遮盖截图和日志中的账号标识、个人信息及网络配置。发现凭证已经公开时，先撤销或轮换，再通过私下渠道告知维护者；不要把敏感值贴在公开 Issue 中。

## 资源发布与目录

重打包后的汉化资源由私有 Cloudflare R2 bucket 分发，Git 保留汉化源文件和发布元数据。
详见 [R2 发布及缓存机制](asset-server/cloudflare/README.md) 和 [汉化目录规划](docs/LOCALIZATION_LAYOUT.md)。
CI 已统一为质量检查、生产发布和模型冒烟三个入口，运行机制与阶段图见 [CI 说明](docs/CI.md)。
大模型译文效果、人工参考对照与分阶段优化见 [汉化质量优化](docs/TRANSLATION_QUALITY.md)。
