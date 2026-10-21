# syntax=docker/dockerfile:1
#
# WebHub 单容器镜像（Q49 / 2026-10 D7）：同一容器内运行 Next.js 网站（0.0.0.0:3100）与 FastAPI
# （127.0.0.1:8100，单 worker）。API 只在容器回环地址可达，Origin 校验也只信任来自回环的转发头，
# 所以刻意不拆成两个容器。进程编排见 deploy/entrypoint.sh，部署步骤见 README「部署到自己的服务器」。
#
# 只用官方多架构基础镜像，linux/arm64（Oracle Ampere）与 linux/amd64 都在本机原生构建：
#   docker compose -f deploy/compose.yaml up -d --build
# 默认跟随 Node 24 / Python 3.13 的最新补丁版（升级时加 --pull 即可拿到基础镜像的安全更新）。
# 需要完全可复现时固定补丁版，例如与开发机一致的 .node-version / services/api/.python-version：
#   docker compose -f deploy/compose.yaml build --build-arg NODE_VERSION=24.18.0 --build-arg PYTHON_VERSION=3.13.12
ARG NODE_VERSION=24
ARG PYTHON_VERSION=3.13
ARG DEBIAN_SUITE=trixie
ARG UV_VERSION=0.11.32

# ---------------------------------------------------------------------------------------------
# 网站依赖：corepack 按 package.json 的 packageManager 安装 pnpm 11.1.3，再按锁文件安装。
# CI=true 让 pnpm 不等待任何交互确认。
FROM node:${NODE_VERSION}-${DEBIAN_SUITE}-slim AS web-base
ENV CI=true \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN corepack enable && corepack install && pnpm --version

# 运行时只要生产依赖。单独一次干净的 --prod 安装：在完整安装上原地裁剪时，pnpm 会把开发依赖
# 留在 node_modules/.pnpm 里（实测多出约 120 MB 的 typescript / eslint / tailwind 等）。
FROM web-base AS web-deps
RUN --mount=type=cache,id=webhub-pnpm-store,target=/pnpm-store \
    pnpm install --frozen-lockfile --prod --store-dir /pnpm-store

# 构建需要完整依赖。WEBHUB_API_INTERNAL_URL 在构建期固化进 /api/backend/* 的重写规则：
# 只能指向同容器回环上的 API。构建完删掉依赖与构建缓存，运行镜像改用 web-deps 的生产依赖。
FROM web-base AS web-build
ENV WEBHUB_API_INTERNAL_URL=http://127.0.0.1:8100
RUN --mount=type=cache,id=webhub-pnpm-store,target=/pnpm-store \
    pnpm install --frozen-lockfile --store-dir /pnpm-store
COPY apps/web/ apps/web/
RUN pnpm --filter @webhub/web build \
    && rm -rf apps/web/node_modules apps/web/.next/cache

# ---------------------------------------------------------------------------------------------
# API：uv 按 uv.lock 只装生产依赖（--no-dev）。项目保持 uv 默认的可编辑安装——config.py 与迁移模块
# 按源码位置定位仓库根和 alembic.ini，所以运行镜像保留同样的 /app/services/api 布局。
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_SUITE} AS api-build
ARG UV_VERSION
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /app/services/api
COPY services/api/pyproject.toml services/api/uv.lock ./
RUN --mount=type=cache,id=webhub-uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY services/api/README.md services/api/alembic.ini ./
COPY services/api/alembic/ alembic/
COPY services/api/src/ src/
RUN --mount=type=cache,id=webhub-uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------------------------
# 只取官方镜像里的 Node 运行时（单个二进制），不带 npm / corepack / pnpm。
FROM node:${NODE_VERSION}-${DEBIAN_SUITE}-slim AS node-runtime

# ---------------------------------------------------------------------------------------------
# 运行镜像：以 Python 官方镜像为底（虚拟环境里的解释器路径与 api-build 完全一致），叠加 Node 二进制。
# 两个基础镜像同为 Debian trixie，Node 依赖的 glibc / libstdc++ 版本一致。
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_SUITE} AS runtime
LABEL org.opencontainers.image.title="WebHub" \
      org.opencontainers.image.description="WebHub 单容器（Next.js 网站 + 回环 FastAPI），可信私网部署"
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEBHUB_ENVIRONMENT=production \
    WEBHUB_DATA_DIR=/data \
    WEBHUB_API_INTERNAL_URL=http://127.0.0.1:8100 \
    PATH=/app/services/api/.venv/bin:$PATH
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
# 官方 Node 二进制只依赖 glibc / libstdc++（Debian 基础镜像自带）；个别架构还需要 libatomic，
# 缺了才补（跟随基础镜像的 Debian 版本，不另外钉版本），并在构建期验证 node 能运行。
# hadolint ignore=DL3008
RUN if ! node --version >/dev/null 2>&1; then \
        apt-get update \
        && apt-get install -y --no-install-recommends libatomic1 \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && node --version \
    && groupadd --gid 10001 webhub \
    && useradd --uid 10001 --gid webhub --home-dir /home/webhub --create-home --no-log-init \
        --shell /usr/sbin/nologin webhub \
    && install -d -o webhub -g webhub -m 0700 /data
WORKDIR /app
# 代码、构建产物与依赖都归 root、对运行用户只读；运行用户只能写 /data 与 Next.js 的运行时缓存目录
# .next/cache（图片优化、fetch 缓存）。
COPY --from=api-build /app/services/api /app/services/api
COPY --from=web-deps /app/package.json /app/pnpm-workspace.yaml /app/
COPY --from=web-deps /app/node_modules /app/node_modules
COPY --from=web-build /app/apps/web /app/apps/web
COPY --from=web-deps /app/apps/web/node_modules /app/apps/web/node_modules
COPY --chmod=0755 deploy/entrypoint.sh /app/deploy/entrypoint.sh
RUN install -d -o 10001 -g 10001 -m 0755 /app/apps/web/.next/cache

# 用数字 uid:gid，编排工具不必解析镜像内的用户名就能确认以非 root 运行。
USER 10001:10001
VOLUME ["/data"]
EXPOSE 3100
# 经网站访问 /api/health：它会再调用回环上的 API，所以两个进程任一不可用都会判为不健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3100/api/health',{signal:AbortSignal.timeout(4000)}).then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
