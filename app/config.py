"""统一配置：``config.toml`` 是全服务器唯一的配置来源。

设计约束（PLAN.md §4）：

* **唯一来源**。读取顺序（``--config`` → ``SK_CONFIG`` → 生产路径 → 开发路径）
  只决定"读哪个文件"，不做多来源合并、没有优先级规则。
* **保留注释地写入**。用 tomlkit 读-改-写，控制台改 ``[llm]`` 不会破坏用户注释。
* **原子替换 + 排他锁**。避免保存过程中崩溃损坏配置或并发写入互相覆盖。
* **fail-closed**。``secret_key`` 为空时拒绝启动（见 :meth:`Config.assert_ready`）。
"""

from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import tomlkit
from tomlkit import TOMLDocument

PRODUCTION_PATH = Path("/etc/schedulekit/config.toml")
DEV_PATH = Path("config.toml")
ENV_VAR = "SK_CONFIG"

#: 需要在对外输出时脱敏的键，元素为 ``(section, key)``。
SECRET_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        ("auth", "password_hash"),
        ("auth", "secret_key"),
        ("llm", "api_key"),
        ("tls", "duckdns_token"),
    }
)


class ConfigError(RuntimeError):
    """配置缺失、非法或无法安全写入。"""


# --------------------------------------------------------------------------- #
# 跨平台排他锁（Windows 用 msvcrt，POSIX 用 fcntl）
# --------------------------------------------------------------------------- #
if os.name == "nt":  # pragma: no cover - 平台相关分支

    def _lock(handle: Any) -> None:
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle: Any) -> None:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - 平台相关分支

    def _lock(handle: Any) -> None:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle: Any) -> None:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------- #
# 分段数据类
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ServerSection:
    public_url: str
    timezone: str
    listen_host: str
    listen_port: int
    data_dir: Path


@dataclass(frozen=True)
class AuthSection:
    password_hash: str
    secret_key: str
    session_ttl_days: int
    session_epoch: int


@dataclass(frozen=True)
class TermSection:
    start_date: str
    total_weeks: int


@dataclass(frozen=True)
class LLMSection:
    provider: str
    base_url: str
    model: str
    api_key: str
    temperature: float
    timeout_seconds: int
    max_tokens: int
    max_image_bytes: int
    system_prompt: str
    #: 临时性失败（连接、超时、429、5xx）在放弃前的**重试次数**，
    #: 不含首次尝试。所以总请求数最多是 ``retry_count + 1``。
    retry_count: int
    #: 退避基数（秒），按指数增长并加抖动。
    retry_backoff_seconds: float

    @property
    def api_key_set(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class IngestSection:
    confirm_ttl_hours: int


@dataclass(frozen=True)
class TLSSection:
    provider: str
    domain: str
    port: int
    acme_email: str
    duckdns_token: str
    cert_dir: Path


@dataclass(frozen=True)
class BackupSection:
    enabled: bool
    hour: int
    keep: int
    upload_retention_days: int


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #
def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """按固定顺序解析配置文件路径（不合并内容）。"""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    if PRODUCTION_PATH.exists():
        return PRODUCTION_PATH
    return DEV_PATH


def _atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + fsync + os.replace，保证替换是原子的。"""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 权限问题
        raise ConfigError(f"无法创建配置目录 {parent}：{exc}") from exc

    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".config-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class Config:
    """配置文件的内存表示，支持分段热加载与原子写回。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path: Path = resolve_config_path(path)
        self._doc: TOMLDocument = self._read()
        self._build_sections()

    # -- 读取 ------------------------------------------------------------- #
    def _read(self) -> TOMLDocument:
        if not self.path.exists():
            raise ConfigError(
                f"找不到配置文件 {self.path}。"
                "请先运行 `python -m app.cli init`，或从 config.toml.example 复制一份。"
            )
        try:
            return tomlkit.parse(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigError(f"配置文件 {self.path} 解析失败：{exc}") from exc

    def _build_sections(self) -> None:
        doc = self._doc
        server = doc.get("server", {})
        auth = doc.get("auth", {})
        term = doc.get("term", {})
        llm = doc.get("llm", {})
        ingest = doc.get("ingest", {})
        tls = doc.get("tls", {})
        backup = doc.get("backup", {})

        # data_dir 与 cert_dir：绝对路径直接用，相对路径相对配置文件所在目录。
        base = self.path.parent
        data_dir = Path(str(server.get("data_dir", "data")))
        cert_dir = Path(str(tls.get("cert_dir", "certs")))

        self.server = ServerSection(
            public_url=str(server.get("public_url", "")).rstrip("/"),
            timezone=str(server.get("timezone", "Asia/Shanghai")),
            listen_host=str(server.get("listen_host", "127.0.0.1")),
            listen_port=int(server.get("listen_port", 8000)),
            data_dir=data_dir if data_dir.is_absolute() else (base / data_dir),
        )
        self.auth = AuthSection(
            password_hash=str(auth.get("password_hash", "")),
            secret_key=str(auth.get("secret_key", "")),
            session_ttl_days=int(auth.get("session_ttl_days", 30)),
            session_epoch=int(auth.get("session_epoch", 1)),
        )
        self.term = TermSection(
            start_date=str(term.get("start_date", "")),
            total_weeks=int(term.get("total_weeks", 18)),
        )
        self.llm = LLMSection(
            provider=str(llm.get("provider", "responses")),
            base_url=str(llm.get("base_url", "")).rstrip("/"),
            model=str(llm.get("model", "")),
            api_key=str(llm.get("api_key", "")),
            temperature=float(llm.get("temperature", 0.0)),
            timeout_seconds=int(llm.get("timeout_seconds", 60)),
            max_tokens=int(llm.get("max_tokens", 1024)),
            max_image_bytes=int(llm.get("max_image_bytes", 8 * 1024 * 1024)),
            system_prompt=str(llm.get("system_prompt", "")),
            retry_count=int(llm.get("retry_count", 3)),
            retry_backoff_seconds=float(llm.get("retry_backoff_seconds", 0.8)),
        )
        self.ingest = IngestSection(
            confirm_ttl_hours=int(ingest.get("confirm_ttl_hours", 48)),
        )
        self.tls = TLSSection(
            provider=str(tls.get("provider", "manual")),
            domain=str(tls.get("domain", "")),
            port=int(tls.get("port", 8443)),
            acme_email=str(tls.get("acme_email", "")),
            duckdns_token=str(tls.get("duckdns_token", "")),
            cert_dir=cert_dir if cert_dir.is_absolute() else (base / cert_dir),
        )
        self.backup = BackupSection(
            enabled=bool(backup.get("enabled", True)),
            hour=int(backup.get("hour", 4)),
            keep=int(backup.get("keep", 7)),
            upload_retention_days=int(backup.get("upload_retention_days", 30)),
        )

    # -- 热加载 ----------------------------------------------------------- #
    def reload_llm(self) -> LLMSection:
        """重新读盘并仅刷新 ``[llm]`` 段（控制台保存后调用，立即生效）。"""
        self._doc = self._read()
        self._build_sections()
        return self.llm

    def reload_all(self) -> None:
        """整份重读（除 ``[llm]`` 外的段落需重启才真正生效）。"""
        self._doc = self._read()
        self._build_sections()

    # -- 写入 ------------------------------------------------------------- #
    def update_section(self, section: str, values: dict[str, Any]) -> None:
        """合并写入某个分段，保留文件中其余内容与全部注释。

        写入失败一律转成带可操作提示的 :class:`ConfigError`。

        这不是防御性编程而是踩过的坑：``config.toml`` 所在**目录**如果属主不是
        运行服务的账号，创建锁文件与原子写临时文件都会失败。原本抛的是裸
        ``PermissionError``，在网页上表现为一个没有任何线索的 500 ——
        用户只能看到 "Internal Server Error"，完全无从下手。
        """
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            with open(lock_path, "a+b") as lock_handle:
                _lock(lock_handle)
                try:
                    doc = self._read()
                    table = doc.get(section)
                    if table is None:
                        doc[section] = tomlkit.table()
                        table = doc[section]
                    for key, value in values.items():
                        table[key] = value
                    _atomic_write(self.path, tomlkit.dumps(doc))
                    self._doc = doc
                finally:
                    _unlock(lock_handle)
        except OSError as exc:
            parent = self.path.parent
            raise ConfigError(
                f"无法写入配置文件 {self.path}：{exc}。"
                f"请确认目录 {parent} 的属主是运行服务的账号，且允许创建文件："
                f"chown schedulekit:schedulekit {parent} && chmod 0755 {parent}"
            ) from exc
        self._build_sections()

    def generate_secret_key(self) -> str:
        """生成并写入新的签名根密钥，返回该值。"""
        key = secrets.token_urlsafe(48)
        self.update_section("auth", {"secret_key": key, "session_epoch": self.auth.session_epoch})
        return key

    # -- 不变量 ----------------------------------------------------------- #
    def assert_ready(self) -> None:
        """启动前校验，失败即拒绝启动（fail-closed）。"""
        problems: list[str] = []
        if not self.auth.secret_key:
            problems.append(
                "[auth].secret_key 为空。运行 `python -m app.cli init` 生成，"
                "否则会话 Cookie 无法安全签名。"
            )
        if not self.auth.password_hash:
            problems.append(
                "[auth].password_hash 为空。运行 `python -m app.cli set-password` 设置管理员密码。"
            )
        if not self.server.public_url:
            problems.append("[server].public_url 为空，无法生成 confirm_url 等绝对链接。")
        if problems:
            raise ConfigError("配置未就绪：\n  - " + "\n  - ".join(problems))

    # -- 输出 ------------------------------------------------------------- #
    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """导出为 JSON 友好结构。

        ``include_secrets=False``（默认）时密钥字段替换为 ``"***"`` / 空串，
        供 ``cli show --json`` 与设置页使用；只有 root 部署脚本才用 ``--secrets``。
        """
        raw = {
            "server": {
                "public_url": self.server.public_url,
                "timezone": self.server.timezone,
                "listen_host": self.server.listen_host,
                "listen_port": self.server.listen_port,
                "data_dir": str(self.server.data_dir),
            },
            "auth": {
                "password_hash": self.auth.password_hash,
                "secret_key": self.auth.secret_key,
                "session_ttl_days": self.auth.session_ttl_days,
                "session_epoch": self.auth.session_epoch,
            },
            "term": {
                "start_date": self.term.start_date,
                "total_weeks": self.term.total_weeks,
            },
            "llm": {
                "provider": self.llm.provider,
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "api_key": self.llm.api_key,
                "temperature": self.llm.temperature,
                "timeout_seconds": self.llm.timeout_seconds,
                "max_tokens": self.llm.max_tokens,
                "max_image_bytes": self.llm.max_image_bytes,
                "system_prompt": self.llm.system_prompt,
                "retry_count": self.llm.retry_count,
                "retry_backoff_seconds": self.llm.retry_backoff_seconds,
            },
            "ingest": {"confirm_ttl_hours": self.ingest.confirm_ttl_hours},
            "tls": {
                "provider": self.tls.provider,
                "domain": self.tls.domain,
                "port": self.tls.port,
                "acme_email": self.tls.acme_email,
                "duckdns_token": self.tls.duckdns_token,
                "cert_dir": str(self.tls.cert_dir),
            },
            "backup": {
                "enabled": self.backup.enabled,
                "hour": self.backup.hour,
                "keep": self.backup.keep,
                "upload_retention_days": self.backup.upload_retention_days,
            },
        }
        if include_secrets:
            return raw
        for section, key in SECRET_KEYS:
            if section in raw and key in raw[section]:
                raw[section][key] = ""
        return raw

    def secret_presence(self) -> dict[str, bool]:
        """密钥是否已设置（供控制台显示，永不回显明文）。"""
        return {f"{section}.{key}": bool(self._doc.get(section, {}).get(key, "")) for section, key in sorted(SECRET_KEYS)}


def load_config(explicit: str | os.PathLike[str] | None = None) -> Config:
    """便捷入口。"""
    return Config(explicit)
