#!/usr/bin/env bash
#
# 备份 ScheduleKit 数据库。
#
# 用 Python 的 sqlite3 backup API 而不是 sqlite3 命令行：
#   * 不依赖额外安装 sqlite3 包
#   * backup API 在 WAL 模式下是一致性快照，不需要停服务
#   * 保留份数直接读 config.toml 的 [backup].keep
#
# 由 systemd timer 每天调用，也可以手动跑。

set -euo pipefail

OPT_DIR="/opt/schedulekit"
VENV_PY="${OPT_DIR}/.venv/bin/python"

[[ -x "${VENV_PY}" ]] || { echo "找不到 ${VENV_PY}，服务端可能尚未部署" >&2; exit 1; }

cd "${OPT_DIR}"
export SK_CONFIG="${SK_CONFIG:-/etc/schedulekit/config.toml}"

exec "${VENV_PY}" -m app.cli backup
