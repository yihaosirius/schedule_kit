"""M6：部署产物的结构校验。

**这些脚本无法在本机执行**（Git Bash 在受限沙箱里起不来，也没有 Linux），
所以这里退而求其次，把"能在服务器上炸掉"的几类问题变成断言：

* 行尾符 —— Windows 上写出的 CRLF 会让 shell 脚本在 Linux 上直接失败，
  而且报错信息（``$'\\r': command not found``）常常被误读成别的问题。
* 模板占位符与替换项不一致 —— Caddyfile 里残留 ``__DOMAIN__`` 会导致
  Caddy 启动失败。
* systemd 加固与写入路径不匹配 —— ``ProtectSystem=strict`` 下没列进
  ``ReadWritePaths`` 的目录是只读的，控制台保存配置会静默失败。
* heredoc 未闭合 —— 最常见的语法错误，且 bash -n 在本地跑不了。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = PROJECT_ROOT / "deploy"

INSTALL_SH = DEPLOY / "install.sh"
BACKUP_SH = DEPLOY / "backup.sh"
CADDY_TEMPLATE = DEPLOY / "Caddyfile.template"
SERVICE_UNIT = DEPLOY / "schedulekit.service"
DEPLOY_README = DEPLOY / "README.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 基本存在性与行尾符
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path", [INSTALL_SH, BACKUP_SH, CADDY_TEMPLATE, SERVICE_UNIT, DEPLOY_README]
)
def test_deploy_artifact_exists(path: Path) -> None:
    assert path.is_file(), f"缺少部署产物 {path.name}"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("path", [INSTALL_SH, BACKUP_SH, CADDY_TEMPLATE, SERVICE_UNIT])
def test_no_crlf_and_no_bom(path: Path) -> None:
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"{path.name} 含 CRLF，在 Linux 上会执行失败"
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 含 UTF-8 BOM"
    assert raw.endswith(b"\n"), f"{path.name} 末尾应有换行"


# --------------------------------------------------------------------------- #
# shell 脚本结构
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [INSTALL_SH, BACKUP_SH])
def test_shell_scripts_are_strict(path: Path) -> None:
    text = read(path)
    assert text.startswith("#!/usr/bin/env bash"), f"{path.name} 缺少 bash shebang"
    assert "set -euo pipefail" in text, f"{path.name} 应开启严格模式"


@pytest.mark.parametrize("path", [INSTALL_SH, BACKUP_SH])
def test_heredocs_are_terminated(path: Path) -> None:
    """``<<'EOF'`` / ``<<'PY'`` 必须有独立成行的结束标记。

    这是最常见的语法错误，而本地没有 bash 可以跑 ``bash -n``。
    """
    lines = read(path).splitlines()
    pending: str | None = None
    for number, line in enumerate(lines, start=1):
        if pending is None:
            match = re.search(r"<<-?'([A-Za-z_][A-Za-z0-9_]*)'", line)
            if match:
                pending = match.group(1)
        elif line.strip() == pending:
            pending = None
    assert pending is None, f"{path.name} 的 heredoc {pending!r} 没有闭合"


def test_install_requires_root() -> None:
    text = read(INSTALL_SH)
    assert "EUID" in text and "root" in text.lower()


def test_install_is_idempotent_in_shape() -> None:
    """重跑不应重复建账号、重复签证书。"""
    text = read(INSTALL_SH)
    assert "id -u" in text, "建账号前应先检查是否已存在"
    assert "-f \"${CERT_PATH}\"" in text, "签证书前应先检查证书是否已存在"
    assert "install -d" in text, "建目录应使用 install -d（幂等且显式设权限）"


# --------------------------------------------------------------------------- #
# Caddyfile 模板与替换
# --------------------------------------------------------------------------- #
def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"__([A-Z_]+)__", text))


def test_every_template_placeholder_is_substituted() -> None:
    template_placeholders = _placeholders(read(CADDY_TEMPLATE))
    install_text = read(INSTALL_SH)
    substituted = set(re.findall(r"s\|\s*__([A-Z_]+)__\s*\|", install_text))

    missing = template_placeholders - substituted
    assert not missing, f"这些占位符没有被 install.sh 替换，Caddy 会启动失败：{sorted(missing)}"
    assert template_placeholders, "模板里应当有占位符"


def test_template_comments_do_not_contain_placeholders() -> None:
    """sed 会把注释里的占位符也替换掉，服务器上的说明就变成读不懂的样子。

    实测踩过：模板头部用 ``__DOMAIN__`` 标注含义，渲染后变成
    ``# canisa1ph.duckdns.org   [tls].domain``。
    """
    for line in read(CADDY_TEMPLATE).splitlines():
        if line.lstrip().startswith("#"):
            assert not re.search(r"__[A-Z_]+__", line), (
                f"注释里不该出现占位符字面量（会被 sed 替换掉）：{line.strip()}"
            )


def _strip_comments(text: str) -> str:
    """去掉整行注释，只留真正的配置。"""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_caddy_uses_explicit_certs_not_acme() -> None:
    """证书由 acme.sh 走 DNS-01 签发，Caddy 不该自己去跑 ACME（那会需要 80/443）。

    注意剥掉注释再断言——模板的注释里正是在解释"为什么不用 Caddy 自动 ACME"。
    """
    config = _strip_comments(read(CADDY_TEMPLATE))
    assert "tls __CERT__ __KEY__" in config
    assert "acme" not in config.lower()


def test_caddy_does_not_listen_on_80_or_443() -> None:
    config = _strip_comments(read(CADDY_TEMPLATE))
    assert "https://__DOMAIN__:__PORT__" in config
    assert ":80" not in config and ":443" not in config


def test_caddy_sets_security_headers() -> None:
    text = read(CADDY_TEMPLATE)
    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Content-Security-Policy",
        "Referrer-Policy",
    ):
        assert header in text, f"缺少安全响应头 {header}"


def test_caddy_forwards_the_real_client_ip() -> None:
    """登录限流按 IP 计数，反代必须把真实客户端 IP 传下去。"""
    assert "X-Real-IP" in read(CADDY_TEMPLATE)


#: 标准 Caddy 构建里**没有**、必须靠第三方插件的模块。
#: 用了它们会让 ``caddy validate`` 直接失败。
NON_BUILTIN_CADDY_MODULES = {
    "output journal": (
        "caddy.logging.writers.journal 属于第三方插件，标准构建未注册该模块。"
        "改用 output stdout —— Caddy 由 systemd 托管，stdout 就是 journald。"
    ),
}

#: 版本相关指令：能用但不值得赌。
VERSION_DEPENDENT_DIRECTIVES = {
    "request_body": "Caddy 2.10 才引入；应用侧已做大小限制并返回 413，无需重复",
}


def test_caddyfile_avoids_plugin_only_modules() -> None:
    """实际踩过：`output journal` 导致 caddy validate 失败，部署中断在最后一步。

    报错是 ``module not registered: caddy.logging.writers.journal``，
    看不出"这需要装插件"。
    """
    config = _strip_comments(read(CADDY_TEMPLATE))
    for needle, reason in NON_BUILTIN_CADDY_MODULES.items():
        assert needle not in config, f"Caddyfile 用了非内置模块 {needle!r}：{reason}"


def test_caddyfile_avoids_version_dependent_directives() -> None:
    config = _strip_comments(read(CADDY_TEMPLATE))
    for needle, reason in VERSION_DEPENDENT_DIRECTIVES.items():
        assert needle not in config, f"Caddyfile 用了版本相关指令 {needle!r}：{reason}"


def test_caddyfile_logs_to_stdout() -> None:
    config = _strip_comments(read(CADDY_TEMPLATE))
    assert "output stdout" in config


def test_caddyfile_keeps_health_check_logs_quiet() -> None:
    """健康检查很频繁，不该刷满日志。"""
    config = _strip_comments(read(CADDY_TEMPLATE))
    assert "log_skip" in config


# --------------------------------------------------------------------------- #
# systemd 单元
# --------------------------------------------------------------------------- #
def test_unit_runs_the_app_on_loopback() -> None:
    text = read(SERVICE_UNIT)
    assert "ExecStart=/opt/schedulekit/.venv/bin/python -m app.serve" in text
    assert "SK_CONFIG=/etc/schedulekit/config.toml" in text
    # 只监听回环这件事由 config.toml 决定，这里确认单元没有把它暴露出去
    assert "0.0.0.0" not in text


def test_unit_hardening_allows_the_paths_the_app_must_write() -> None:
    """strict 模式下只有 ReadWritePaths 里的目录可写。

    控制台要就地改写 ``config.toml``（保存 [llm] 配置），数据库与图片写在
    数据目录 —— 漏掉任何一个都会在运行时静默失败。
    """
    text = read(SERVICE_UNIT)
    assert "ProtectSystem=strict" in text
    match = re.search(r"^ReadWritePaths=(.+)$", text, re.MULTILINE)
    assert match, "strict 模式下必须显式列出可写路径"

    writable = match.group(1).split()
    assert "/var/lib/schedulekit" in writable, "数据库与图片目录必须可写"
    assert "/etc/schedulekit" in writable, "控制台保存配置需要可写 config.toml 所在目录"


def test_unit_sets_a_memory_cap() -> None:
    text = read(SERVICE_UNIT)
    assert "MemoryMax=" in text
    assert "MemoryHigh=" in text


def test_unit_restarts_on_failure_and_logs_to_journal() -> None:
    text = read(SERVICE_UNIT)
    assert "Restart=on-failure" in text
    assert "StandardOutput=journal" in text
    assert "SyslogIdentifier=schedulekit" in text


# --------------------------------------------------------------------------- #
# 安装脚本引用的文件都要存在
# --------------------------------------------------------------------------- #
def test_install_only_references_files_that_exist() -> None:
    text = read(INSTALL_SH)
    referenced = set(re.findall(r"\$\{OPT_DIR\}/([A-Za-z0-9_./-]+)", text))
    referenced |= set(re.findall(r"\$\{REPO_DIR\}/([A-Za-z0-9_./-]+)", text))

    known_ok = {"config.toml.example", "uv.lock", "pyproject.toml", "README.md"}
    for relative in referenced:
        candidate = DEPLOY.parent / relative
        assert candidate.exists() or relative in known_ok, f"install.sh 引用了不存在的 {relative}"


def test_install_installs_the_timer_units_it_defines() -> None:
    text = read(INSTALL_SH)
    for unit in ("schedulekit-backup.service", "schedulekit-backup.timer"):
        assert unit in text, f"install.sh 没有安装 {unit}"
    assert "systemctl enable --now schedulekit-backup.timer" in text


def test_install_precompiles_python() -> None:
    """ProtectSystem=strict 下 /opt 只读，运行时写不了 __pycache__，必须预编译。"""
    assert "compileall" in read(INSTALL_SH)


# --------------------------------------------------------------------------- #
# CLI 调用必须能找到 app 包
# --------------------------------------------------------------------------- #
def test_every_cli_call_runs_from_opt_dir() -> None:
    """``python -m app`` 靠 cwd 找包。

    本脚本是在**仓库目录**被调用的，而包安装在 ``/opt/schedulekit``；
    不切目录就会 ImportError。这个坑实际踩过：有一处还带着 ``|| true``，
    结果是"服务起不来，但日志里看不出为什么"（真正原因是 secret_key 没生成）。
    """
    offenders = []
    for line in read(INSTALL_SH).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "-m app." not in stripped:
            continue
        wrapped = (
            "sk_cli" in stripped
            or f'cd "${{OPT_DIR}}"' in stripped
            or "cwd=" in stripped
        )
        if not wrapped:
            offenders.append(stripped)
    assert not offenders, f"这些调用没有切到 ${{OPT_DIR}}：{offenders}"


def test_no_cli_call_swallows_its_failure() -> None:
    """``|| true`` 会把"密钥没生成"变成"服务莫名起不来"。"""
    offenders = [
        line.strip()
        for line in read(INSTALL_SH).splitlines()
        if "-m app." in line and "|| true" in line
    ]
    assert not offenders, f"CLI 调用失败被吞掉了：{offenders}"


def test_secret_key_is_generated_explicitly() -> None:
    """secret_key 为空时应用 fail-closed，必须有一段一定会成功的生成逻辑。"""
    text = read(INSTALL_SH)
    assert "generate_secret_key" in text
    assert "会话签名密钥" in text


def test_sk_cli_helper_sets_config_env() -> None:
    """除了切目录，还要显式给 SK_CONFIG，避免依赖路径解析的隐式行为。"""
    text = read(INSTALL_SH)
    assert "sk_cli()" in text
    assert 'SK_CONFIG="${CONFIG_FILE}"' in text


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def test_backup_uses_the_cli_not_the_sqlite3_binary() -> None:
    """sqlite3 命令行不一定装了；而且 backup API 在 WAL 下才是一致性快照。"""
    text = read(BACKUP_SH)
    assert "app.cli backup" in text
    assert not re.search(r"^\s*sqlite3\s", text, re.MULTILINE)


def test_backup_respects_sk_config() -> None:
    assert "SK_CONFIG" in read(BACKUP_SH)


# --------------------------------------------------------------------------- #
# 不泄露秘密、不开 80/443
# --------------------------------------------------------------------------- #
def test_no_hardcoded_secrets_in_deploy_artifacts() -> None:
    suspicious = re.compile(r"(sk_[A-Za-z0-9_-]{20,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
    for path in (INSTALL_SH, BACKUP_SH, CADDY_TEMPLATE, SERVICE_UNIT):
        text = read(path)
        hits = suspicious.findall(text)
        # 允许文档/注释里出现的示例 UUID 占位
        real = [hit for hit in hits if hit not in {"00000000-0000-0000-0000-000000000000"}]
        assert not real, f"{path.name} 里疑似写死了密钥：{real}"


def test_install_reads_secrets_without_echoing_or_argv() -> None:
    text = read(INSTALL_SH)
    assert "read -rsp" in text, "DuckDNS token 必须用隐藏输入，避免泄露到终端"
    assert "--secrets" in text, "需要取明文时要走 cli show --secrets"


def test_install_never_opens_80_or_443() -> None:
    text = read(INSTALL_SH)
    assert not re.search(r"ufw allow (80|443)\b", text), "不得开放 80/443（会触发备案条件）"
    assert "ufw allow" in text


def test_deploy_readme_documents_the_manual_steps() -> None:
    text = read(DEPLOY_README)
    assert "duckdns.org" in text
    assert "8443" in text
    assert "安全组" in text, "必须提醒用户服务商控制台那一层也要放行端口"
    assert "两道独立的墙" in text


# --------------------------------------------------------------------------- #
# 行尾符策略
# --------------------------------------------------------------------------- #
def test_gitattributes_forces_lf_everywhere() -> None:
    """开发机是 Windows，部署目标是 Linux，这条是硬要求。

    Git for Windows 默认 ``core.autocrlf=true``，会把检出的 ``install.sh``
    变成 CRLF。落到服务器上会以 ``$'\\r': command not found`` 或
    ``bad interpreter`` 失败 —— 报错信息完全指不到真正原因。

    实测踩过一次：``git add`` 时全部 95 个文件都提示
    "LF will be replaced by CRLF"，加了 ``.gitattributes`` 才压住。
    """
    attrs = PROJECT_ROOT / ".gitattributes"
    assert attrs.is_file(), "缺少 .gitattributes，部署脚本可能被检出成 CRLF"

    text = attrs.read_text(encoding="utf-8")
    assert "text=auto eol=lf" in text, "应设全局默认，避免新增文件漏网"
    for pattern in ("*.sh", "*.service", "*.timer", "*.sql"):
        assert pattern in text, f".gitattributes 没有显式覆盖 {pattern}"


def test_gitattributes_marks_images_binary() -> None:
    """PNG 之类不能被当文本处理，否则会被换行转换破坏。"""
    text = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.png", "*.jpg", "*.ico"):
        assert pattern in text, f".gitattributes 没有把 {pattern} 标为 binary"


# --------------------------------------------------------------------------- #
# 可执行位
# --------------------------------------------------------------------------- #
def test_shell_scripts_are_executable_in_git() -> None:
    """Windows 没有 POSIX 可执行位，git 会一律记录成 100644。

    后果：在 Linux 上 clone 下来后 ``deploy/install.sh`` 没有 x 权限，
    直接执行报 ``Permission denied``（而且 ``sudo`` 也救不了 —— sudo 同样
    要先 exec 这个文件）。这个坑实际踩过。

    注意 ``core.fileMode`` 在 Windows 上恒为 false，所以**必须**用
    ``git update-index --chmod=+x <file>`` 显式写进索引，靠本地 chmod 无效。
    """
    if not (PROJECT_ROOT / ".git").exists():
        pytest.skip("不在 git 仓库中（例如从 tarball 解压）")

    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files", "-s", "deploy/install.sh", "deploy/backup.sh"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"无法调用 git：{exc}")

    if result.returncode != 0:
        pytest.skip(f"git 不可用：{result.stderr.strip()}")

    modes = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            modes[parts[3]] = parts[0]

    for path in ("deploy/install.sh", "deploy/backup.sh"):
        assert modes.get(path) == "100755", (
            f"{path} 在 git 索引里的 mode 是 {modes.get(path)!r}，应为 '100755'。"
            " 修复：git update-index --chmod=+x " + path
        )


def test_install_sh_sets_modes_for_generated_scripts() -> None:
    """生成的脚本用 install -m / chmod 显式设权限，不依赖源文件的 mode。"""
    text = read(INSTALL_SH)
    assert "install -m 0755" in text, "backup.sh 安装到 /usr/local/bin 时应显式设 0755"
    assert "chmod 0700 /etc/schedulekit/duckdns-update.sh" in text, (
        "含 DuckDNS token 的脚本必须是 0700"
    )


# --------------------------------------------------------------------------- #
# Caddy 配置的替换必须"先校验后生效"
# --------------------------------------------------------------------------- #
def test_install_validates_caddyfile_before_replacing_it() -> None:
    """先写 /etc/caddy/Caddyfile 再校验，会把"重跑安装"变成"弄挂正常服务"。

    坏配置一旦落盘，Caddy 会一直起不来，而且旧配置已经没了。
    正确顺序是渲染到临时文件 → 校验 → 通过才 install 覆盖。
    """
    text = read(INSTALL_SH)
    assert "mktemp" in text, "应先渲染到临时文件"
    assert 'caddy validate --config "${tmp_caddyfile}"' in text, "应校验临时文件"

    validate_at = text.index('caddy validate --config "${tmp_caddyfile}"')
    install_at = text.index('install -m 0644 "${tmp_caddyfile}" /etc/caddy/Caddyfile')
    assert validate_at < install_at, "必须先校验，后覆盖 /etc/caddy/Caddyfile"


def test_validate_passes_the_adapter_explicitly() -> None:
    """Caddy 靠**文件名**判断配置格式。

    ``caddy validate --config /tmp/tmp.XXXX`` 会被当 JSON 解析，然后在
    Caddyfile 第一行注释上失败：
      ``config is not valid JSON: invalid character '#' ... did you mean to
      use a config adapter?``
    —— 这个坑实际踩过（是"先校验后落盘"那次改动引入的）。
    """
    text = read(INSTALL_SH)
    validate_line = next(
        (line for line in text.splitlines() if "caddy validate" in line and not line.strip().startswith("#")),
        None,
    )
    assert validate_line is not None, "找不到 caddy validate 调用"
    assert "--adapter caddyfile" in validate_line, (
        f"校验临时文件时必须显式指定 adapter，否则会被当 JSON 解析：{validate_line.strip()}"
    )


def test_install_confirms_caddy_actually_started() -> None:
    """reload 成功不代表进程活着 —— 配置有问题时 Caddy 会起来又退出。"""
    text = read(INSTALL_SH)
    assert "systemctl is-active --quiet caddy" in text
    assert "journalctl -u caddy" in text, "启动失败时要打印 Caddy 自己的日志"


def test_install_reports_the_caddy_version() -> None:
    """出问题时第一个要问的就是版本（指令可用性随版本变化）。"""
    assert "caddy version" in read(INSTALL_SH)


def test_install_probes_https_from_the_outside() -> None:
    """只检查本机 /healthz 会漏掉"安全组没放行"这类问题。"""
    text = read(INSTALL_SH)
    assert "外部 HTTPS 可达" in text
    assert "安全组" in text, "失败提示要指向服务商安全组那一层"


# --------------------------------------------------------------------------- #
# 服务账号必须能写它要写的目录
# --------------------------------------------------------------------------- #
def test_etc_dir_is_owned_by_the_service_user() -> None:
    """控制台保存 [llm] 配置要就地改写 config.toml。

    原子写需要在该**目录**里创建锁文件与临时文件 —— 这是目录写权限。
    只 chown 配置文件、目录仍是 root:root 0755 时，服务账号建不了任何文件，
    保存直接 500。这个坑实际踩过（用户点保存看到 Internal Server Error）。
    """
    text = read(INSTALL_SH)
    line = next(
        (l for l in text.splitlines() if "install -d" in l and "${ETC_DIR}" in l),
        None,
    )
    assert line is not None, "找不到创建 ${ETC_DIR} 的那一行"
    assert "-o \"${APP_USER}\"" in line, f"${{ETC_DIR}} 必须归服务账号所有：{line.strip()}"
    assert "-g \"${APP_GROUP}\"" in line, f"${{ETC_DIR}} 的属组也要设对：{line.strip()}"


def test_install_probes_writability_as_the_service_user() -> None:
    """权限位写对不等于真能写 —— 要拿服务账号实际试一次。"""
    text = read(INSTALL_SH)
    assert "sk_probe_write" in text, "应有一个以服务账号身份实际写入的探测函数"
    assert "runuser -u" in text, "探测必须以服务账号身份执行"
    # 两个关键目录都要探
    assert 'sk_probe_write "${ETC_DIR}"' in text, "要探测配置目录（控制台保存配置依赖它）"
    assert 'sk_probe_write "${DATA_DIR}"' in text, "要探测数据目录"


def test_install_handles_the_root_owned_lock_file() -> None:
    """实测踩过：目录权限改对了，保存仍报

        PermissionError: [Errno 13] Permission denied:
        '/etc/schedulekit/config.toml.lock'

    因为锁文件是本脚本以 root 身份跑 set-password / generate_secret_key 时
    创建的（属主 root:root 0644），服务账号只能读、不能以 "a+b" 打开。
    目录权限只管**新建**文件，管不到这个已存在的文件。
    """
    text = read(INSTALL_SH)

    assert "sk_fix_config_perms" in text, "应有一个统一修正配置权限的函数"
    body = text.split("sk_fix_config_perms() {")[1].split("\n}")[0]
    assert "config.toml.lock" in body or '"${CONFIG_FILE}.lock"' in body, (
        "修正函数必须处理锁文件，不能只 chown 主文件"
    )
    assert "rm -f" in body, "最省事的做法是删掉锁文件（缺失时会自动重建）"

    # 两个会以 root 写配置的地方之后都要调用它
    assert text.count("sk_fix_config_perms\n") >= 2, (
        "set-password 与 generate_secret_key 之后都要修正权限"
    )


def test_install_probes_the_lock_file_specifically() -> None:
    """锁文件的属主与目录权限无关，必须单独探。"""
    text = read(INSTALL_SH)
    assert "sk_probe_lock" in text
    assert 'sk_probe_lock\n' in text, "验证步骤里要真的调用它"

    body = text.split("sk_probe_lock() {")[1].split("\n}")[0]
    assert '>>' in body, "要模拟应用的真实操作（以追加方式打开锁文件）"


def test_etc_dir_stays_traversable_for_caddy() -> None:
    """Caddy 以另一个用户运行，要能穿到 certs/ 读证书 —— 别改成 0700。"""
    text = read(INSTALL_SH)
    for line in text.splitlines():
        if "install -d" in line and "${ETC_DIR}" in line:
            assert "0755" in line, f"${{ETC_DIR}} 需保持可遍历，Caddy 才能读到证书：{line.strip()}"
