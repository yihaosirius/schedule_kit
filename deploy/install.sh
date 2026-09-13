#!/usr/bin/env bash
#
# ScheduleKit 一键部署（Ubuntu 22.04 / 24.04）
#
#   sudo bash deploy/install.sh --init    首次：交互式生成 config.toml 并部署
#   sudo bash deploy/install.sh           幂等重部署（改完 config.toml 后重跑即可）
#
# 设计要点：
#   * 本脚本**不自行解析 TOML**。所有配置都通过 `python -m app.cli show --json`
#     读取，保证 config.toml 是唯一真相（见 PLAN.md §4）。
#   * 不开 80/443：证书走 DNS-01，HTTPS 跑在非标准端口，规避大陆备案触发条件。
#   * 全程幂等：重复执行只会把状态收敛到目标，不会重复建用户或重复签证书。

set -euo pipefail

APP_USER="schedulekit"
APP_GROUP="schedulekit"
OPT_DIR="/opt/schedulekit"
ETC_DIR="/etc/schedulekit"
CONFIG_FILE="${ETC_DIR}/config.toml"
CERT_DIR="${ETC_DIR}/certs"
DATA_DIR="/var/lib/schedulekit"
VENV_DIR="${OPT_DIR}/.venv"
SERVICE="schedulekit"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DO_INIT=0
SKIP_TLS=0
DOMAIN=""
ACME_EMAIL=""

# ── 输出 ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_END=""
fi
step() { printf '%s==>%s %s\n' "${C_OK}" "${C_END}" "$*"; }
info() { printf '    %s%s%s\n' "${C_DIM}" "$*" "${C_END}"; }
warn() { printf '%s[!]%s %s\n' "${C_WARN}" "${C_END}" "$*" >&2; }
fail() { printf '%s[x]%s %s\n' "${C_ERR}" "${C_END}" "$*" >&2; exit 1; }

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

选项：
  --init                交互式生成 config.toml（已存在时会先备份）
  --domain DOMAIN       非交互指定域名（跳过询问）
  --email EMAIL         ACME 账户邮箱
  --no-tls              跳过 Caddy 与证书，仅装服务端（调试用）
  -h, --help            显示本帮助
EOF
}

# ── 参数 ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --init) DO_INIT=1; shift ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) ACME_EMAIL="${2:-}"; shift 2 ;;
    --no-tls) SKIP_TLS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数：$1（用 --help 查看用法）" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "需要 root 权限，请用 sudo 运行"
[[ -f "${REPO_DIR}/pyproject.toml" ]] || fail "在 ${REPO_DIR} 找不到 pyproject.toml，请在项目根目录下运行"

# ── 配置读取（唯一真相是 config.toml）───────────────────────────────
# 所有 app.cli 调用都必须切到 ${OPT_DIR}：`python -m app` 靠 cwd 找包，
# 而本脚本是在仓库目录被调用的，不切就会 ImportError。
sk_cli() {
  ( cd "${OPT_DIR}" && SK_CONFIG="${CONFIG_FILE}" "${VENV_DIR}/bin/python" -m app.cli "$@" )
}

cfg_field() {
  "${VENV_DIR}/bin/python" - "$1" "${OPT_DIR}" <<'PY'
import json, subprocess, sys
field, cwd = sys.argv[1], sys.argv[2]
raw = subprocess.run(
    [sys.executable, "-m", "app.cli", "show", "--json"],
    capture_output=True, text=True, check=True, cwd=cwd,
).stdout
node = json.loads(raw)
for part in field.split("."):
    node = node[part]
print(node)
PY
}

cfg_secret() {
  "${VENV_DIR}/bin/python" - "$1" "${OPT_DIR}" <<'PY'
import json, subprocess, sys
field, cwd = sys.argv[1], sys.argv[2]
raw = subprocess.run(
    [sys.executable, "-m", "app.cli", "show", "--json", "--secrets"],
    capture_output=True, text=True, check=True, cwd=cwd,
).stdout
node = json.loads(raw)
for part in field.split("."):
    node = node[part]
print(node)
PY
}

# ── 1. 基础依赖 ─────────────────────────────────────────────────────
step "安装基础依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates ufw >/dev/null
info "curl / ca-certificates / ufw 就绪"

# ── 2. 服务账号与目录 ───────────────────────────────────────────────
step "准备服务账号与目录"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
  info "已创建系统账号 ${APP_USER}"
else
  info "账号 ${APP_USER} 已存在"
fi

install -d -m 0755 "${OPT_DIR}"
install -d -m 0755 "${ETC_DIR}"          # 需可遍历，Caddy 才能读到 certs/
install -d -m 0755 "${CERT_DIR}"
install -d -m 0750 -o "${APP_USER}" -g "${APP_GROUP}" "${DATA_DIR}"

# ── 3. 同步代码 ─────────────────────────────────────────────────────
step "同步代码到 ${OPT_DIR}"
for item in app pyproject.toml uv.lock config.toml.example deploy clients docs README.md; do
  [[ -e "${REPO_DIR}/${item}" ]] || continue
  rm -rf "${OPT_DIR:?}/${item}"
  cp -r "${REPO_DIR}/${item}" "${OPT_DIR}/${item}"
done
info "已复制 app / pyproject.toml / uv.lock / deploy / clients / docs"

# ── 4. 安装 uv 与依赖 ───────────────────────────────────────────────
step "安装 uv 与 Python 依赖"
if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null
  info "uv 已安装到 /root/.local/bin"
fi
export PATH="/root/.local/bin:${PATH}"

pushd "${OPT_DIR}" >/dev/null
uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev
"${VENV_DIR}/bin/python" -m compileall -q app >/dev/null   # 预编译，strict 模式下无需写 pycache
popd >/dev/null
info "依赖就绪：$("${VENV_DIR}/bin/python" --version)"

# ── 5. 生成配置 ─────────────────────────────────────────────────────
if [[ "${DO_INIT}" -eq 1 || ! -f "${CONFIG_FILE}" ]]; then
  step "生成 ${CONFIG_FILE}"
  if [[ -f "${CONFIG_FILE}" ]]; then
    backup="${CONFIG_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
    cp "${CONFIG_FILE}" "${backup}"
    warn "已备份原配置到 ${backup}"
  fi

  if [[ -z "${DOMAIN}" ]]; then
    read -rp "    对外域名（如 schedulekit.duckdns.org）： " DOMAIN
  fi
  [[ -n "${DOMAIN}" ]] || fail "域名不能为空"

  if [[ -z "${ACME_EMAIL}" ]]; then
    read -rp "    ACME 账户邮箱（可用任意可达邮箱）： " ACME_EMAIL
  fi

  duckdns_token=""
  if [[ "${DOMAIN}" == *.duckdns.org ]]; then
    read -rsp "    DuckDNS Token（输入时不显示）： " duckdns_token; echo
  fi

  tls_port="8443"
  read -rp "    HTTPS 端口 [${tls_port}]： " input_port
  tls_port="${input_port:-${tls_port}}"

  cp "${OPT_DIR}/config.toml.example" "${CONFIG_FILE}"

  DOMAIN="${DOMAIN}" ACME_EMAIL="${ACME_EMAIL}" DUCKDNS_TOKEN="${duckdns_token}" \
  TLS_PORT="${tls_port}" PUBLIC_URL="https://${DOMAIN}:${tls_port}" \
  "${VENV_DIR}/bin/python" - "${CONFIG_FILE}" <<'PY'
import os, sys
sys.path.insert(0, "/opt/schedulekit")
from app.config import Config

cfg = Config(sys.argv[1])
cfg.update_section("server", {"public_url": os.environ["PUBLIC_URL"], "data_dir": "/var/lib/schedulekit"})
cfg.update_section("tls", {
    "provider": "duckdns" if os.environ["DUCKDNS_TOKEN"] else "manual",
    "domain": os.environ["DOMAIN"],
    "port": int(os.environ["TLS_PORT"]),
    "acme_email": os.environ["ACME_EMAIL"],
    "duckdns_token": os.environ["DUCKDNS_TOKEN"],
    "cert_dir": "/etc/schedulekit/certs",
})
print("    config.toml 已写入")
PY

  # 先生成签名密钥：为空时应用会拒绝启动（fail-closed），
  # 所以这一步必须显式成功，不能被 `|| true` 吞掉失败。
  step "生成会话签名密钥"
  "${VENV_DIR}/bin/python" - "${CONFIG_FILE}" <<'PY'
import sys
sys.path.insert(0, "/opt/schedulekit")
from app.config import Config

cfg = Config(sys.argv[1])
if cfg.auth.secret_key:
    print("    已存在，保持不变")
else:
    cfg.generate_secret_key()
    print("    已生成 [auth].secret_key")
PY

  step "设置管理员密码"
  sk_cli set-password
else
  step "沿用已有 ${CONFIG_FILE}"
fi

chown "${APP_USER}:${APP_GROUP}" "${CONFIG_FILE}"
chmod 0600 "${CONFIG_FILE}"
info "config.toml 权限：$(stat -c '%a %U:%G' "${CONFIG_FILE}")"

# 非 --init 路径下配置可能是手写的，仍要确保密钥不缺失
if [[ -z "$(cfg_secret auth.secret_key)" ]]; then
  step "生成会话签名密钥"
  "${VENV_DIR}/bin/python" - "${CONFIG_FILE}" <<'PY'
import sys
sys.path.insert(0, "/opt/schedulekit")
from app.config import Config

cfg = Config(sys.argv[1])
cfg.generate_secret_key()
print("    已生成 [auth].secret_key")
PY
  chown "${APP_USER}:${APP_GROUP}" "${CONFIG_FILE}"
  chmod 0600 "${CONFIG_FILE}"
fi

step "应用数据库迁移"
sk_cli migrate
chown -R "${APP_USER}:${APP_GROUP}" "${DATA_DIR}"

# ── 6. systemd 服务 ─────────────────────────────────────────────────
step "安装 systemd 服务"
install -m 0644 "${OPT_DIR}/deploy/schedulekit.service" "/etc/systemd/system/${SERVICE}.service"

install -m 0755 "${OPT_DIR}/deploy/backup.sh" "/usr/local/bin/schedulekit-backup"
cat > "/etc/systemd/system/schedulekit-backup.service" <<'EOF'
[Unit]
Description=ScheduleKit 数据库备份

[Service]
Type=oneshot
User=schedulekit
Group=schedulekit
WorkingDirectory=/opt/schedulekit
Environment=SK_CONFIG=/etc/schedulekit/config.toml
ExecStart=/usr/local/bin/schedulekit-backup
EOF

cat > "/etc/systemd/system/schedulekit-backup.timer" <<'EOF'
[Unit]
Description=每天备份 ScheduleKit 数据库

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now schedulekit-backup.timer >/dev/null
info "备份定时器已启用（每天 04:30，保留份数由 [backup].keep 决定）"

systemctl enable "${SERVICE}" >/dev/null
systemctl restart "${SERVICE}"
sleep 2
systemctl is-active --quiet "${SERVICE}" || {
  journalctl -u "${SERVICE}" -n 40 --no-pager >&2
  fail "服务启动失败，上面是最近日志"
}
info "服务已启动"

# ── 7. TLS：Caddy + acme.sh DNS-01 ──────────────────────────────────
if [[ "${SKIP_TLS}" -eq 1 ]]; then
  warn "已跳过 TLS 配置（--no-tls）"
else
  TLS_DOMAIN="$(cfg_field tls.domain)"
  TLS_PORT="$(cfg_field tls.port)"
  CERT_PATH="${CERT_DIR}/fullchain.pem"
  KEY_PATH="${CERT_DIR}/privkey.pem"

  step "安装 Caddy"
  if ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
  fi
  info "caddy：$(caddy version | head -1)"

  # 证书要先存在，Caddy 才能用显式路径启动；这里先签证书再写配置
  if [[ ! -f "${CERT_PATH}" ]]; then
    step "签发证书（DNS-01，不需要 80/443 入站）"
    if ! [[ -x /root/.acme.sh/acme.sh ]]; then
      curl -fsSL https://get.acme.sh | sh -s email="${ACME_EMAIL}" >/dev/null
    fi
    ACME="/root/.acme.sh/acme.sh"
    "${ACME}" --set-default-ca --server letsencrypt >/dev/null

    TLS_PROVIDER="$(cfg_field tls.provider)"
    if [[ "${TLS_PROVIDER}" == "duckdns" ]]; then
      export DuckDNS_Token
      DuckDNS_Token="$(cfg_secret tls.duckdns_token)"
      [[ -n "${DuckDNS_Token}" ]] || fail "tls.provider=duckdns 但 duckdns_token 为空"
      info "用 DuckDNS API 写 TXT 记录…"
      if ! "${ACME}" --issue --dns dns_duckdns -d "${TLS_DOMAIN}" --keylength ec-256; then
        fail "证书签发失败。常见原因：① VPS 无法出站访问 www.duckdns.org（改 TXT 失败）；② 域名解析尚未指向本机。请按上面的报错判断是哪一段。"
      fi
    else
      fail "tls.provider=${TLS_PROVIDER} 需要手工签发证书并放到 ${CERT_DIR}，然后重跑本脚本"
    fi

    install -d -m 0755 "${CERT_DIR}"
    "${ACME}" --install-cert -d "${TLS_DOMAIN}" --ecc \
      --key-file "${KEY_PATH}" \
      --fullchain-file "${CERT_PATH}" \
      --reloadcmd "systemctl reload caddy" >/dev/null
    info "证书已安装到 ${CERT_DIR}（acme.sh 会自动续期）"
  else
    info "证书已存在，跳过签发（续期由 acme.sh 的 cron 负责）"
  fi

  chmod 0644 "${CERT_PATH}"
  chmod 0640 "${KEY_PATH}"
  chgrp caddy "${KEY_PATH}" 2>/dev/null || warn "找不到 caddy 组，请检查 Caddy 是否安装成功"
  chgrp caddy "${CERT_DIR}" 2>/dev/null || true

  step "渲染 Caddyfile"
  sed -e "s|__DOMAIN__|${TLS_DOMAIN}|g" \
      -e "s|__PORT__|${TLS_PORT}|g" \
      -e "s|__CERT__|${CERT_PATH}|g" \
      -e "s|__KEY__|${KEY_PATH}|g" \
      -e "s|__BACKEND__|$(cfg_field server.listen_host):$(cfg_field server.listen_port)|g" \
      "${OPT_DIR}/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile >/dev/null || fail "Caddyfile 校验失败"
  systemctl enable caddy >/dev/null 2>&1 || true
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  info "Caddy 已按 ${TLS_DOMAIN}:${TLS_PORT} 配置"

  step "配置防火墙"
  ufw allow "${TLS_PORT}/tcp" >/dev/null 2>&1 || true
  info "已放行 ${TLS_PORT}/tcp（未开启 80/443）"

  # DuckDNS A 记录自更新：VPS 换了 IP 也能自动跟上
  if [[ "$(cfg_field tls.provider)" == "duckdns" ]]; then
    step "安装 DuckDNS A 记录自更新"
    {
      echo '#!/usr/bin/env bash'
      echo 'set -euo pipefail'
      printf 'curl -fsS "https://www.duckdns.org/update?domains=%s&token=%s&ip=" >/dev/null\n' \
        "${TLS_DOMAIN%%.duckdns.org}" "$(cfg_secret tls.duckdns_token)"
    } > /etc/schedulekit/duckdns-update.sh
    chmod 0700 /etc/schedulekit/duckdns-update.sh
    cat > /etc/systemd/system/schedulekit-duckdns.service <<'EOF'
[Unit]
Description=同步 DuckDNS A 记录

[Service]
Type=oneshot
ExecStart=/etc/schedulekit/duckdns-update.sh
EOF
    cat > /etc/systemd/system/schedulekit-duckdns.timer <<'EOF'
[Unit]
Description=每天同步 DuckDNS A 记录

[Timer]
OnBootSec=2min
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now schedulekit-duckdns.timer >/dev/null
    info "已启用（token 只写在 0700 的脚本里，不落 crontab）"
  fi
fi

# ── 8. 健康检查 ─────────────────────────────────────────────────────
step "健康检查"
LISTEN="$(cfg_field server.listen_host):$(cfg_field server.listen_port)"
if curl -fsS "http://${LISTEN}/healthz" >/dev/null; then
  info "本机 /healthz 正常"
else
  journalctl -u "${SERVICE}" -n 40 --no-pager >&2
  fail "本机健康检查失败"
fi

PUBLIC_URL="$(cfg_field server.public_url)"
cat <<EOF

${C_OK}部署完成${C_END}

  应用目录   ${OPT_DIR}
  配置文件   ${CONFIG_FILE}   （唯一配置来源）
  数据目录   ${DATA_DIR}
  对外地址   ${PUBLIC_URL}

下一步：
  1. 浏览器打开 ${PUBLIC_URL}/settings 填入 LLM 的 base_url / model / API Key
  2. 打开 ${PUBLIC_URL}/courses 粘贴课表
  3. 在 ${PUBLIC_URL}/settings 创建 API Key 给快捷指令与 Windows 悬浮窗用

常用命令：
  systemctl status ${SERVICE}         查看服务
  journalctl -u ${SERVICE} -f         跟踪日志（含 trace id）
  systemctl restart ${SERVICE}        重启（改 [server]/[tls] 后需要）
  /usr/local/bin/schedulekit-backup   手动备份一次

注意：系统的安全组/防火墙（服务商网页控制台那一层）也要放行 ${TLS_PORT}/tcp，
      它与服务器内的 ufw 是两道独立的墙。
EOF
