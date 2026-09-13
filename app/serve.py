"""服务入口：读取 ``config.toml`` 后按其中的 host/port 启动 uvicorn。

生产环境由 systemd 执行本模块；对外流量一律经 Caddy 反代，
uvicorn 只应监听回环地址。
日志格式与约定见 ``app/logging.py`` 与 ``docs/dev-notes.md``。
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from app.config import ENV_VAR, Config, ConfigError
from app.logging import setup_logging


def build_app():
    """给 ``uvicorn --reload`` 用的工厂。"""
    from app.main import create_app

    return create_app(Config(os.environ.get(ENV_VAR)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.serve", description="启动 ScheduleKit 服务")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("--host", help="覆盖 config.toml 的 listen_host")
    parser.add_argument("--port", type=int, help="覆盖 config.toml 的 listen_port")
    parser.add_argument("--reload", action="store_true", help="开发模式：改动自动重载")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（DEBUG 会打印更细的追踪）",
    )
    parser.add_argument("--log-file", help="同时写入文件（生产由 journald 收集，通常不需要）")
    args = parser.parse_args(argv)

    setup_logging(args.log_level, log_file=args.log_file)

    try:
        config = Config(args.config)
        config.assert_ready()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    host = args.host or config.server.listen_host
    port = args.port or config.server.listen_port

    if args.reload:
        # reload 模式必须给 uvicorn 一个 import 字符串，配置通过环境变量传递。
        if args.config:
            os.environ[ENV_VAR] = str(config.path)
        uvicorn.run(
            "app.serve:build_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=["app"],
            log_config=None,
        )
    else:
        from app.main import create_app

        uvicorn.run(create_app(config), host=host, port=port, log_config=None)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
