#!/usr/bin/env bash
# WebHub 在线备份（Q49）：经 `docker compose exec` 在运行中的容器里调用 Python sqlite3 的在线备份接口
# （Connection.backup），导出一份一致的数据库快照——兼容 WAL，不停服、不阻塞写入——连同 Provider 主密钥
# 写入带 UTC 时间戳的目录，只保留最近 N 份。
#
# 用法（宿主机，在任意目录执行均可）：
#   deploy/backup.sh
#   WEBHUB_BACKUP_DIR=/srv/webhub-backups WEBHUB_BACKUP_KEEP=30 deploy/backup.sh
#
# 可选环境变量：
#   WEBHUB_BACKUP_DIR      备份根目录（默认 deploy/backups，已被 Git 与 Docker 构建上下文忽略；
#                          建议放到仓库之外，例如 /srv/webhub-backups，并定期同步到另一台机器）
#   WEBHUB_BACKUP_KEEP     保留最近几份（默认 14）
#   WEBHUB_BACKUP_INCLUDE_BOOKMARK_SOURCES=1
#                          同时打包 /data/bookmark-imports（导入书签时上传的原始 HTML，可能很大）
#   WEBHUB_COMPOSE_FILE    compose 文件（默认与本脚本同目录的 compose.yaml）
#   WEBHUB_BACKUP_SERVICE  compose 服务名（默认 webhub）
#   WEBHUB_BACKUP_LOCAL=1  不经 docker，直接在当前环境执行同一段 Python（容器内运行或测试用），
#                          此时按当前环境的 WEBHUB_DATA_DIR / WEBHUB_DATABASE_URL 定位数据库
#   WEBHUB_BACKUP_PYTHON   执行备份片段的 Python（默认 python3；片段只用标准库）
#
# cron 示例（crontab -e，每天 03:17 备份；执行用户需能运行 docker 并能写备份目录，日志追加到文件）：
#   17 3 * * * WEBHUB_BACKUP_DIR=/srv/webhub-backups /opt/WebsiteHub/deploy/backup.sh >> "$HOME/webhub-backup.log" 2>&1
#
# 每份备份目录 webhub-<UTC 时间戳>/ 包含：
#   main.sqlite3              一致快照（已转为单文件 DELETE 日志模式，并通过 PRAGMA integrity_check）
#   provider-master-key.env   Provider 主密钥两行；没有它，库里保存的 Provider API Key 永久无法解密
#   bookmark-imports.tar      （可选）书签导入源文件
#   SHA256SUMS                以上文件的校验和
# 备份含凭据与私人数据：目录权限 700、文件 600，异地保存时同样按凭据保护。
#
# 恢复步骤（在仓库根目录执行，以 /srv/webhub-backups/webhub-20261021T031700Z 为例）：
#   1. 校验：(cd /srv/webhub-backups/webhub-20261021T031700Z && sha256sum -c SHA256SUMS)
#   2. 停止服务（不要加 -v，否则会删除数据卷）：
#        docker compose -f deploy/compose.yaml down
#   3. 把 provider-master-key.env 里的两行写回 deploy/.env，替换原有同名行——主密钥必须与数据库配套。
#   4. 写回数据库，同时删除旧的 -wal / -shm（否则 SQLite 可能把旧日志回放到新文件上）：
#        docker compose -f deploy/compose.yaml run --rm -T --entrypoint sh webhub \
#          -c 'rm -f /data/main.sqlite3-wal /data/main.sqlite3-shm && cat > /data/main.sqlite3' \
#          < /srv/webhub-backups/webhub-20261021T031700Z/main.sqlite3
#   5. （可选）书签源文件：
#        docker compose -f deploy/compose.yaml run --rm -T --entrypoint sh webhub \
#          -c 'rm -rf /data/bookmark-imports && tar -C /data -xf -' \
#          < /srv/webhub-backups/webhub-20261021T031700Z/bookmark-imports.tar
#   6. 启动：docker compose -f deploy/compose.yaml up -d
#      入口脚本会先执行迁移，较旧的备份会自动升级到当前 schema；之后用原账号登录核对。
# 迁移到新服务器时步骤相同：先按 README 配好 deploy/.env 并构建一次镜像，再从第 3 步开始。
set -euo pipefail
umask 077

die() {
  printf 'backup: 错误：%s\n' "$*" >&2
  exit 1
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_file="${WEBHUB_COMPOSE_FILE:-$script_dir/compose.yaml}"
service="${WEBHUB_BACKUP_SERVICE:-webhub}"
backup_root="${WEBHUB_BACKUP_DIR:-$script_dir/backups}"
keep="${WEBHUB_BACKUP_KEEP:-14}"
python_command="${WEBHUB_BACKUP_PYTHON:-python3}"
local_mode="${WEBHUB_BACKUP_LOCAL:-0}"
include_sources="${WEBHUB_BACKUP_INCLUDE_BOOKMARK_SOURCES:-0}"

[[ "$keep" =~ ^[1-9][0-9]*$ ]] || die "WEBHUB_BACKUP_KEEP 必须是正整数，当前为 '$keep'"

# 在应用环境（容器内，或 WEBHUB_BACKUP_LOCAL=1 时的当前环境）里执行的片段，只依赖标准库。
#   snapshot  在线备份到临时文件 → integrity_check → 以二进制写到 stdout
#   key       输出 Provider 主密钥（优先环境变量，与 config.py 一致；开发环境回退到数据目录里的密钥文件）
#   sources   以 tar 流输出 bookmark-imports 目录；目录不存在时不输出任何字节
read -r -d '' BACKUP_PY <<'PY' || true
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path


def fail(message):
    print(f"backup: 错误：{message}", file=sys.stderr)
    sys.exit(1)


def data_directory():
    return Path(os.environ.get("WEBHUB_DATA_DIR") or "/data").expanduser().resolve()


def database_path():
    url = os.environ.get("WEBHUB_DATABASE_URL", "").strip()
    if not url:
        return data_directory() / "main.sqlite3"
    scheme, separator, rest = url.partition(":///")
    if not separator or scheme.split("+", 1)[0] != "sqlite":
        fail("只支持 SQLite 数据库 URL")
    return Path(rest.partition("?")[0]).expanduser().resolve()


def snapshot():
    source_path = database_path()
    if not source_path.is_file():
        fail(f"数据库不存在：{source_path}")
    work = Path(tempfile.mkdtemp(prefix="webhub-backup-"))
    try:
        target_path = work / "main.sqlite3"
        # mode=rw：数据库缺失时报错而不是悄悄新建空库；备份只读取源库，不写入。
        source = sqlite3.connect(f"{source_path.as_uri()}?mode=rw", uri=True, timeout=30)
        target = sqlite3.connect(target_path)
        try:
            # 在线备份接口一次复制完整快照：WAL 模式下读不阻塞写，快照停在开始时已提交的状态，
            # 包括仍在 -wal 文件里、尚未检查点的事务。直接 cp main.sqlite3 则会丢掉这部分。
            source.backup(target)
        finally:
            source.close()
        # 恢复时应用连接会重新切回 WAL；备份文件本身保持单文件，方便校验与搬运。
        target.execute("PRAGMA journal_mode=DELETE")
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        try:
            rows = target.execute("SELECT version_num FROM alembic_version")
            revisions = [row[0] for row in rows]
        except sqlite3.Error:
            revisions = []
        target.close()
        if result != "ok":
            fail(f"快照完整性检查失败：{result}")
        with target_path.open("rb") as handle:
            shutil.copyfileobj(handle, sys.stdout.buffer, 1024 * 1024)
        sys.stdout.buffer.flush()
        size = target_path.stat().st_size
        schema = ",".join(revisions) or "未初始化"
        print(
            f"backup: 数据库快照 {size} 字节，integrity_check=ok，schema={schema}",
            file=sys.stderr,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def key():
    value = os.environ.get("WEBHUB_PROVIDER_MASTER_KEY", "").strip()
    origin = "环境变量 WEBHUB_PROVIDER_MASTER_KEY"
    if not value:
        key_file = data_directory() / "provider-master.key"
        if key_file.is_file():
            value = key_file.read_text(encoding="ascii").strip()
            origin = str(key_file)
    if not value:
        fail("找不到 Provider 主密钥（环境变量与数据目录中都没有）")
    version = os.environ.get("WEBHUB_PROVIDER_MASTER_KEY_VERSION", "").strip() or "1"
    print(
        f"# WebHub Provider 主密钥，来源：{origin}。"
        "恢复时写回 deploy/.env，必须与同目录的 main.sqlite3 配套。"
    )
    print(f"WEBHUB_PROVIDER_MASTER_KEY={value}")
    print(f"WEBHUB_PROVIDER_MASTER_KEY_VERSION={version}")


def sources():
    directory = data_directory() / "bookmark-imports"
    if not directory.is_dir():
        print("backup: 没有 bookmark-imports 目录，跳过书签源文件", file=sys.stderr)
        return
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        archive.add(directory, arcname="bookmark-imports")
    sys.stdout.buffer.flush()


commands = {"snapshot": snapshot, "key": key, "sources": sources}
if len(sys.argv) != 2 or sys.argv[1] not in commands:
    fail("用法：snapshot | key | sources")
commands[sys.argv[1]]()
PY

run_in_app() {
  if [[ "$local_mode" == "1" ]]; then
    "$@"
  else
    docker compose -f "$compose_file" exec -T "$service" "$@"
  fi
}

backup_step() {
  # -I：隔离模式，不把当前目录加入 sys.path，也不读 PYTHON* 环境变量。
  run_in_app "$python_command" -I -c "$BACKUP_PY" "$1"
}

sha256_files() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$@"
  else
    shasum -a 256 -- "$@"
  fi
}

if [[ "$local_mode" == "1" ]]; then
  source_label="本机环境（WEBHUB_BACKUP_LOCAL=1）"
else
  command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
  [[ -f "$compose_file" ]] || die "找不到 compose 文件 $compose_file"
  running="$(docker compose -f "$compose_file" ps --status running --quiet "$service")" \
    || die "无法查询 compose 服务状态：Docker 是否在运行、当前用户能否访问 Docker（docker 组或 sudo）？"
  [[ -n "$running" ]] || die "服务 $service 没有在运行；在线备份需要容器处于运行状态（docker compose -f $compose_file up -d）"
  source_label="docker compose 服务 $service"
fi

mkdir -p -- "$backup_root"
chmod 700 -- "$backup_root"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final_dir="$backup_root/webhub-$stamp"
[[ ! -e "$final_dir" ]] || die "$final_dir 已存在，请稍后再试"

# 先写到隐藏的临时目录，全部成功后再原子改名；任何一步失败都不会留下半份“看起来完整”的备份。
work_dir="$(mktemp -d "$backup_root/.webhub-$stamp.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT

backup_step snapshot >"$work_dir/main.sqlite3"
[[ -s "$work_dir/main.sqlite3" ]] || die "数据库快照为空"
backup_step key >"$work_dir/provider-master-key.env"

if [[ "$include_sources" == "1" ]]; then
  backup_step sources >"$work_dir/bookmark-imports.tar"
  [[ -s "$work_dir/bookmark-imports.tar" ]] || rm -f -- "$work_dir/bookmark-imports.tar"
fi

(
  cd -- "$work_dir"
  files=(main.sqlite3 provider-master-key.env)
  [[ -f bookmark-imports.tar ]] && files+=(bookmark-imports.tar)
  sha256_files "${files[@]}" >SHA256SUMS
)
chmod 600 -- "$work_dir"/*
chmod 700 -- "$work_dir"
mv -- "$work_dir" "$final_dir"
trap - EXIT
printf 'backup: 已完成 %s（来源：%s）\n' "$final_dir" "$source_label" >&2

# 保留最近 N 份：目录名是 UTC 时间戳，字典序即时间序。
mapfile -t backups < <(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name 'webhub-*' | LC_ALL=C sort)
excess=$((${#backups[@]} - keep))
for ((index = 0; index < excess; index++)); do
  rm -rf -- "${backups[index]}"
  printf 'backup: 已按保留策略删除 %s\n' "${backups[index]}" >&2
done
