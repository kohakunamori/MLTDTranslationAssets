# MLTD 资产服务镜像（候选闭包，逐文件 COPY；本仓自足，不依赖主仓）
#
# 上下文 = 本仓库根目录。只复制 compose 服务实际导入/调用的源码：
#   - mltd-assets-mirror:  scripts/assets_mirror.py + 它导入的 producer 校验模块
#     scripts/assets_generated_index.py（同仓生成侧 CI 的同一文件，非第二实现）
#   - mltd-asset-updater / mltd-asset-tools: tools/archive_controller.py
#     （+ 其经子进程调用的 tools/versioned_assets.py、
#      tools/materialize_versioned_assets.py、tools/asset_version.py）
#   - 共享依赖: server/asset_archive.py、server/versioned_asset_store.py
# 不整树 COPY，也不复制与本服务无关的生成/发布脚本（build_generated_release.py、
# sync_to_d1.py、track_jp_assets.py 等属于生成侧 CI 与门户同步，不进运行镜像）。
# 仓库没有 server/__init__.py、tools/__init__.py、scripts/__init__.py：这些目录
# 以 Python 3 命名空间包方式导入（各入口脚本都会把仓库根插入 sys.path），
# 因此这里不需要、也不复制 __init__.py。回归：asset-server/test_docker_context.py。
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY asset-server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# server/：versioned_asset_store 依赖 asset_archive，两个都在闭包里。
COPY server/asset_archive.py /app/server/asset_archive.py
COPY server/versioned_asset_store.py /app/server/versioned_asset_store.py

# tools/：updater 与 tools 服务的入口及其子进程调用的同级入口。
COPY tools/archive_controller.py /app/tools/archive_controller.py
COPY tools/versioned_assets.py /app/tools/versioned_assets.py
COPY tools/materialize_versioned_assets.py /app/tools/materialize_versioned_assets.py
COPY tools/asset_version.py /app/tools/asset_version.py

# scripts/：mirror 服务入口 + 它导入的 producer 校验模块。
# 镜像内以 `python scripts/assets_mirror.py` 直接运行（非包导入）时，
# sys.path[0] 是 /app/scripts，assets_mirror 的兼容层走「顶层模块名」分支；
# 以包方式导入（如 asset-server/assets_route.py）时走 `scripts.*` 分支。
# 两种布局都要求 producer 与 mirror 同目录/同包，因此这里的两个 COPY 都是
# 闭包必需项，删一不可。
COPY scripts/assets_generated_index.py /app/scripts/assets_generated_index.py
COPY scripts/assets_mirror.py /app/scripts/assets_mirror.py
