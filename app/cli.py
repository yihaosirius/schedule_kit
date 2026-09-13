"""命令行工具。

    python -m app.cli init            生成 secret_key、设置管理员密码、建库
    python -m app.cli set-password    修改密码（session_epoch 自增，旧会话全部失效）
    python -m app.cli migrate         应用数据库迁移
    python -m app.cli show --json     输出配置（密钥脱敏），供部署脚本渲染 Caddyfile
    python -m app.cli new-key         创建 API Key
    python -m app.cli backup          备份数据库
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from app import __version__
from app.config import DEV_PATH, Config, ConfigError, resolve_config_path
from app.db import Database
from app.migrations.runner import apply_migrations
from app.security import hash_password
from app.timeutil import now_utc, utc_iso

EXAMPLE_NAME = "config.toml.example"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _database(config: Config) -> Database:
    return Database(config.server.data_dir / "schedulekit.db")


def _prompt_password(use_stdin: bool, *, confirm: bool = True) -> str:
    if use_stdin:
        value = sys.stdin.readline().rstrip("\n")
        if not value:
            raise SystemExit("错误：--stdin 模式下未从标准输入读到密码")
        return value
    first = getpass.getpass("新密码: ")
    if not first:
        raise SystemExit("错误：密码不能为空")
    if confirm:
        second = getpass.getpass("再输入一次: ")
        if first != second:
            raise SystemExit("错误：两次输入不一致")
    return first


def _ensure_config_file(path: Path) -> None:
    """配置文件缺失时，尝试从同目录的模板复制一份（开发便利）。"""
    if path.exists():
        return
    candidates = [Path.cwd() / EXAMPLE_NAME, Path(__file__).resolve().parent.parent / EXAMPLE_NAME]
    for candidate in candidates:
        if candidate.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate, path)
            print(f"已从 {candidate.name} 创建 {path}")
            return
    raise SystemExit(
        f"错误：找不到配置文件 {path}，也找不到 {EXAMPLE_NAME}。请先复制模板再运行 init。"
    )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    path = resolve_config_path(args.config)
    _ensure_config_file(path)

    config = Config(path)
    print(f"配置文件：{config.path}")

    if not config.auth.secret_key:
        config.generate_secret_key()
        print("已生成 [auth].secret_key")
    else:
        print("[auth].secret_key 已存在，保持不变")

    if config.auth.password_hash:
        print("[auth].password_hash 已存在，跳过设置密码（如需修改请用 set-password）")
    else:
        password = _prompt_password(args.stdin)
        config.update_section(
            "auth",
            {"password_hash": hash_password(password), "session_epoch": config.auth.session_epoch},
        )
        print("已设置管理员密码")

    db = _database(config)
    applied = apply_migrations(db)
    print(f"数据库：{db.path}")
    print("已应用迁移：" + (", ".join(applied) if applied else "无（已是最新）"))

    config.assert_ready()
    print("\n初始化完成。运行 `python -m app.serve` 启动服务。")
    return 0


def cmd_set_password(args: argparse.Namespace) -> int:
    config = Config(args.config)
    password = _prompt_password(args.stdin)
    new_epoch = config.auth.session_epoch + 1
    config.update_section(
        "auth", {"password_hash": hash_password(password), "session_epoch": new_epoch}
    )
    print(f"密码已更新；session_epoch 提升为 {new_epoch}，所有既有会话已失效。")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    config = Config(args.config)
    db = _database(config)
    applied = apply_migrations(db)
    print(f"数据库：{db.path}")
    print("已应用迁移：" + (", ".join(applied) if applied else "无（已是最新）"))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    config = Config(args.config)
    payload = {
        "version": __version__,
        "config_path": str(config.path),
        "secrets_included": bool(args.secrets),
        **config.as_dict(include_secrets=args.secrets),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_new_key(args: argparse.Namespace) -> int:
    config = Config(args.config)
    db = _database(config)
    apply_migrations(db)

    from app.services.apikeys import create_key

    key_id, plaintext = create_key(db, name=args.name, read_only=args.read_only)
    print("API Key（只在本次显示，请立即保存）：")
    print(plaintext)
    print(f"\n编号：{key_id}    名称：{args.name}    权限：{'只读' if args.read_only else '读写'}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    config = Config(args.config)
    data_dir = config.server.data_dir
    source = data_dir / "schedulekit.db"
    if not source.exists():
        raise SystemExit(f"错误：找不到数据库 {source}")

    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"schedulekit-{stamp}.db"

    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)

    keep = max(1, config.backup.keep)
    existing = sorted(backup_dir.glob("schedulekit-*.db"), reverse=True)
    for stale in existing[keep:]:
        stale.unlink(missing_ok=True)

    print(f"已备份到 {target}（保留最近 {keep} 份）")
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="ScheduleKit 命令行工具")
    parser.add_argument("-c", "--config", help="配置文件路径（默认按 SK_CONFIG → 生产路径 → ./config.toml 解析）")
    parser.add_argument("--version", action="version", version=f"ScheduleKit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成 secret_key、设置管理员密码并建库")
    p_init.add_argument("--stdin", action="store_true", help="从标准输入读取密码（避免进入 shell 历史）")
    p_init.set_defaults(func=cmd_init)

    p_pwd = sub.add_parser("set-password", help="修改管理员密码")
    p_pwd.add_argument("--stdin", action="store_true", help="从标准输入读取密码")
    p_pwd.set_defaults(func=cmd_set_password)

    sub.add_parser("migrate", help="应用数据库迁移").set_defaults(func=cmd_migrate)

    p_show = sub.add_parser("show", help="输出配置")
    p_show.add_argument("--json", action="store_true", help="以 JSON 输出（默认即 JSON）")
    p_show.add_argument(
        "--secrets",
        action="store_true",
        help="包含密钥明文；仅供 root 部署脚本使用",
    )
    p_show.set_defaults(func=cmd_show)

    p_key = sub.add_parser("new-key", help="创建 API Key")
    p_key.add_argument("--name", required=True, help="密钥名称，用于在控制台识别")
    p_key.add_argument("--read-only", action="store_true", help="只读密钥（仅允许 GET）")
    p_key.set_defaults(func=cmd_new_key)

    sub.add_parser("backup", help="备份数据库").set_defaults(func=cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认用代码页编码，会让中文 JSON 变成乱码。
    # cli show --json 会被部署脚本解析，必须固定为 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - 非文本流
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
