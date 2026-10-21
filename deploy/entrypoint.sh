#!/usr/bin/env bash
# WebHub 单容器进程入口（Q49 / 2026-10 D7）。
#
# 顺序：先跑数据库迁移 → 同时启动 FastAPI（只监听 127.0.0.1，单 worker）与 Next.js 网站 →
# 任一进程退出就停止另一个，整个容器以非零码退出，交给 Compose 的 restart 策略整体重启。
#
# 为什么是这几十行 bash，而不是根包已有的 `pnpm start`（concurrently --kill-others-on-fail）：
#   1. `--kill-others-on-fail` 只在子进程以非零码退出时才停其它进程；某个进程以 0 退出时容器会
#      “半活”（只剩网站或只剩 API），只能等健康检查连续失败才被发现。这里任一退出都整体退出。
#   2. `pnpm start:api` 用 `uv run`：它在启动时会按锁文件同步环境（默认连开发依赖组一起），
#      而运行镜像里既没有 uv 也没有 pnpm，虚拟环境只装了生产依赖且对运行用户只读；
#      这里直接运行虚拟环境里的可执行文件。
#   3. pnpm → shell → uv run → uvicorn 的多层包装让信号转发和退出码都不可靠；这里 SIGTERM 只转发一跳。
#   4. `pnpm start` 里网站与迁移并行；这里迁移成功之后才启动任何服务，迁移失败则什么都不启动。
#   5. concurrently 是根包开发依赖，生产镜像只装生产依赖，不必为它多带一套开发依赖和 pnpm。
# 僵尸进程回收交给 Compose 的 `init: true`（tini 作 PID 1）；没有 init 时本脚本作为 PID 1 同样处理了
# TERM / INT / HUP，`docker run` 直接运行也能正常停止。
#
# 不变量：API 只监听回环地址、只跑单 worker（限流与书签上传准入是进程内状态）；`--no-proxy-headers`
# 让 FastAPI 看到真实的回环对端，Origin 校验因此只信任同容器网站转发的头。这几项刻意不开放覆盖。
#
# 也可以在容器外运行（部署验证就是这样做的）：只依赖 bash ≥ 5.1、node、services/api/.venv
# 与一次已完成的网站生产构建。可选环境变量：
#   WEBHUB_API_INTERNAL_URL      默认 http://127.0.0.1:8100；必须是 127.0.0.1 上的 http 地址，API 端口
#                                由它决定，并且必须与构建网站时固化的值一致。
#   WEBHUB_WEB_PORT              网站端口，默认 3100（监听 0.0.0.0）。容器内不要改：健康检查固定 3100，
#                                宿主侧端口在 compose.yaml 里映射；容器外验证时可换成空闲端口。
#   WEBHUB_STOP_TIMEOUT_SECONDS  停止时等待两个进程退出的秒数，超时强杀，默认 20
#                                （小于 compose.yaml 的 stop_grace_period 30 秒）。
#   WEBHUB_APP_ROOT              应用根目录，默认本脚本的上一级（镜像里是 /app，仓库里是仓库根）。
set -euo pipefail

log() { printf '[webhub-entrypoint] %s\n' "$*" >&2; }
die() {
  local code=$1
  shift
  log "错误：$*"
  exit "$code"
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
app_root=${WEBHUB_APP_ROOT:-$(dirname -- "$script_dir")}
venv_bin=$app_root/services/api/.venv/bin
web_dir=$app_root/apps/web
web_port=${WEBHUB_WEB_PORT:-3100}
stop_timeout=${WEBHUB_STOP_TIMEOUT_SECONDS:-20}
api_url=${WEBHUB_API_INTERNAL_URL:-http://127.0.0.1:8100}
api_url=${api_url%/}

# ---- 启动前检查：配置错误给出能照做的提示，而不是一段 Python 回溯（78 = EX_CONFIG）----

if ((BASH_VERSINFO[0] < 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] < 1))); then
  die 78 "需要 bash 5.1 或更高版本（当前 $BASH_VERSION）"
fi

# API 只能监听回环地址：Origin 校验只信任来自回环的转发头，这是单容器设计的前提。
if [[ ! $api_url =~ ^http://127\.0\.0\.1:([0-9]{1,5})$ ]]; then
  die 78 "WEBHUB_API_INTERNAL_URL 必须是 http://127.0.0.1:<端口>（API 只允许监听回环地址），当前为：$api_url"
fi
api_port=${BASH_REMATCH[1]}
export WEBHUB_API_INTERNAL_URL=$api_url

for port in "$web_port" "$api_port"; do
  if [[ ! $port =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1 || 10#$port > 65535)); then
    die 78 "端口必须是 1–65535 的整数：$port"
  fi
done
if ((10#$web_port == 10#$api_port)); then
  die 78 "网站端口与 API 端口不能相同：$web_port"
fi
if [[ ! $stop_timeout =~ ^[1-9][0-9]{0,3}$ ]]; then
  die 78 "WEBHUB_STOP_TIMEOUT_SECONDS 必须是正整数：$stop_timeout"
fi

[[ -x $venv_bin/uvicorn && -x $venv_bin/webhub-db ]] \
  || die 78 "找不到 API 虚拟环境：$venv_bin（容器外请先执行 uv sync --project services/api）"
[[ -f $web_dir/server.mjs && -f $web_dir/.next/BUILD_ID ]] \
  || die 78 "找不到已构建的网站：$web_dir/.next（容器外请先执行 pnpm build）"
command -v node >/dev/null 2>&1 || die 78 "找不到 node"

environment=${WEBHUB_ENVIRONMENT:-development}
master_key=${WEBHUB_PROVIDER_MASTER_KEY:-}
if [[ ${environment,,} =~ ^[[:space:]]*(prod|production)[[:space:]]*$ && -z ${master_key//[[:space:]]/} ]]; then
  die 78 "生产环境必须设置 WEBHUB_PROVIDER_MASTER_KEY（32 字节随机数的 Base64）。生成：openssl rand -base64 32，写入 deploy/.env 后重新 up -d。"
fi
unset master_key

if [[ -n ${WEBHUB_DATA_DIR:-} && $WEBHUB_DATA_DIR == /* ]]; then
  mkdir -p -- "$WEBHUB_DATA_DIR" 2>/dev/null || true
  if [[ ! -d $WEBHUB_DATA_DIR || ! -w $WEBHUB_DATA_DIR ]]; then
    die 73 "数据目录 $WEBHUB_DATA_DIR 不可写（当前 uid=$(id -u) gid=$(id -g)）。若改成了绑定挂载，请先在宿主机执行 chown -R $(id -u):$(id -g) <目录>。"
  fi
fi

export NODE_ENV=${NODE_ENV:-production}

# ---- 信号：任何时候收到 TERM / INT / HUP 都记下“要停”，并把 TERM 立即转给已启动的子进程 ----
# 只转发 TERM：非交互 bash 的后台子进程会忽略 INT，INT 转过去不会生效。
# 立即转发而不是只设标记：信号若恰好落在启动子进程与 `wait -n` 之间，标记会被错过。

shutdown_requested=0
api_pid=
web_pid=

stop_children() {
  local pid
  for pid in $api_pid $web_pid; do
    kill -TERM "$pid" 2>/dev/null || true
  done
}

# shellcheck disable=SC2329  # 由下面的 trap 调用
request_shutdown() {
  shutdown_requested=1
  stop_children
}

trap request_shutdown TERM INT HUP

# ---- 1. 迁移：失败则不启动任何服务；迁移期间收到的停止信号等迁移跑完再生效，不打断写库 ----

log "环境 $environment；数据目录 ${WEBHUB_DATA_DIR:-<默认>}；执行数据库迁移（webhub-db upgrade）"
if ! "$venv_bin/webhub-db" upgrade; then
  die 1 "数据库迁移失败，服务未启动"
fi
if ((shutdown_requested)); then
  log "迁移完成时已收到停止信号，不再启动服务"
  exit 0
fi

# ---- 2. 同时启动 API 与网站 ----

log "启动 API：http://127.0.0.1:$api_port（单 worker）"
"$venv_bin/uvicorn" webhub.main:app \
  --workers 1 \
  --no-proxy-headers \
  --host 127.0.0.1 \
  --port "$api_port" \
  --timeout-graceful-shutdown 10 &
api_pid=$!

log "启动网站：http://0.0.0.0:$web_port"
# 显式传 --hostname：Docker 会把 HOSTNAME 设成容器 ID，server.mjs 默认读它，就只绑定容器网卡，
# 容器内经回环的健康检查会连不上。
(cd -- "$web_dir" && exec node server.mjs --hostname 0.0.0.0 --port "$web_port") &
web_pid=$!

# 两次启动之间若已收到信号，当时可能只拿到了部分 PID，这里补发一次。
if ((shutdown_requested)); then
  stop_children
fi

# ---- 3. 等任意一个先结束（或被信号打断），然后收尾 ----

exited_pid=
first_status=0
wait -n -p exited_pid "$api_pid" "$web_pid" || first_status=$?

if ((shutdown_requested)); then
  log "收到停止信号，正在停止网站与 API"
else
  if [[ $exited_pid == "$api_pid" ]]; then
    exited="API 进程"
  elif [[ $exited_pid == "$web_pid" ]]; then
    exited=网站进程
  else
    exited=子进程
  fi
  log "${exited}意外退出（状态 $first_status），停止另一个进程并退出容器"
  stop_children
fi

deadline=$((SECONDS + stop_timeout))
for pid in "$api_pid" "$web_pid"; do
  while kill -0 "$pid" 2>/dev/null; do
    if ((SECONDS >= deadline)); then
      log "进程 $pid 在 ${stop_timeout} 秒内没有退出，强制结束"
      kill -KILL "$pid" 2>/dev/null || true
      break
    fi
    sleep 0.2
  done
done
wait || true

if ((shutdown_requested)); then
  log "已停止"
  exit 0
fi
# 子进程意外退出一律视为失败，即使它的退出码是 0。
if ((first_status == 0)); then
  first_status=1
fi
exit "$first_status"
