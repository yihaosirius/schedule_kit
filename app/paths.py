"""集中定义资源路径，避免 ``main`` 与 ``routers`` 之间循环导入。"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
MIGRATIONS_DIR = BASE_DIR / "migrations"
