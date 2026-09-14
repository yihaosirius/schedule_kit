"""接口文档与实现的同步守卫。

`docs/api.md` 是手写的，很容易在改路由时忘记同步——这一版文档的来历就是
"上一版已经和实现对不上了"。所以这里把文档里的**端点清单**与**权限矩阵**
钉成断言：加了端点、删了端点、换了鉴权依赖，这里都会失败。

这不是在测文档的措辞，只测那些"改了就一定得改文档"的事实。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC = PROJECT_ROOT / "docs" / "api.md"

#: 文档 §0.2 端点总表里承诺的 (方法, 路径, 最低鉴权)。
#: 鉴权取值为路由依赖名的简写：
#:   public  = 无依赖（自己判断或不需要）
#:   read    = current_auth（任何凭据）
#:   write   = require_write（读写密钥或会话 + CSRF）
#:   session = require_session / require_session_write（仅网页会话）
DOCUMENTED_ROUTES: set[tuple[str, str, str]] = {
    ("POST", "/api/login", "public"),
    ("POST", "/api/logout", "public"),
    ("GET", "/api/tasks", "read"),
    ("POST", "/api/tasks", "write"),
    ("PATCH", "/api/tasks/{item_id}", "write"),
    ("DELETE", "/api/tasks/{item_id}", "write"),
    ("POST", "/api/ingest", "write"),
    ("GET", "/api/ingest/{draft_id}", "read"),
    ("GET", "/api/ingest/{draft_id}/image", "read"),
    ("POST", "/api/ingest/{draft_id}/confirm", "write"),
    ("POST", "/api/ingest/{draft_id}/discard", "write"),
    ("GET", "/api/courses", "read"),
    ("PUT", "/api/courses", "write"),
    ("GET", "/api/status", "read"),
    ("GET", "/api/settings", "session"),
    ("PUT", "/api/settings", "session"),
    ("GET", "/api/keys", "session"),
    ("POST", "/api/keys", "session"),
    ("DELETE", "/api/keys/{key_id}", "session"),
    ("GET", "/healthz", "public"),
}

#: 文档 §7 页面路由表。这些不走依赖注入，未认证时 303 而非 401。
DOCUMENTED_PAGES: set[tuple[str, str]] = {
    ("GET", "/"),
    ("GET", "/login"),
    ("GET", "/courses"),
    ("GET", "/settings"),
    ("GET", "/drafts/{draft_id}"),
    ("GET", "/sw.js"),
}

DEPENDENCY_TO_LEVEL = {
    "current_auth": "read",
    "require_write": "write",
    "require_session": "session",
    "require_session_write": "session",
}


def _flatten(routes):
    """这个 FastAPI 版本把 include_router 的结果包成 _IncludedRouter。

    真正的 APIRouter 挂在 ``original_router`` 上，包装层没有 ``routes`` 属性。
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _flatten(getattr(inner, "routes", []) or [])
            continue
        if getattr(route, "methods", None):
            yield route


def _route_level(route) -> str:
    names = set()
    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        for sub in dependant.dependencies:
            names.add(getattr(sub.call, "__name__", ""))
    levels = {DEPENDENCY_TO_LEVEL[name] for name in names if name in DEPENDENCY_TO_LEVEL}
    if not levels:
        return "public"
    assert len(levels) == 1, f"{route.path} 同时挂了两级鉴权：{levels}"
    return levels.pop()


@pytest.fixture(scope="module")
def actual_api_routes(app_module) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    flat = list(_flatten(app_module.routes))
    for route in flat:
        path = route.path
        if not (path.startswith("/api/") or path == "/healthz"):
            continue
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, path, _route_level(route)))
    return found


@pytest.fixture(scope="module")
def app_module():
    """按真实装配路径建一次应用，供整个模块复用。"""
    import shutil
    import uuid

    from app.config import Config
    from app.security import hash_password

    run_root = PROJECT_ROOT / ".pytest-run"
    run_root.mkdir(parents=True, exist_ok=True)
    work = run_root / uuid.uuid4().hex[:12]
    work.mkdir()
    config_path = work / "config.toml"
    shutil.copyfile(PROJECT_ROOT / "config.toml.example", config_path)

    cfg = Config(config_path)
    cfg.update_section("server", {"data_dir": str(work / "data"),
                                  "public_url": "http://testserver"})
    cfg.update_section("auth", {"secret_key": "api-docs-guard",
                                "password_hash": hash_password("x")})
    cfg.update_section("llm", {"provider": "mock", "model": "mock-model", "api_key": "k"})

    from app.main import create_app

    return create_app(Config(config_path))


# --------------------------------------------------------------------------- #
# 端点清单
# --------------------------------------------------------------------------- #
def test_api_route_set_matches_the_document(actual_api_routes) -> None:
    """多一个、少一个、换个鉴权级别，都会在这里失败。"""
    missing = DOCUMENTED_ROUTES - actual_api_routes
    undocumented = actual_api_routes - DOCUMENTED_ROUTES

    assert not missing, (
        "文档里写了但代码里没有（路由被删或改名了）：\n  "
        + "\n  ".join(f"{m} {p} ({l})" for m, p, l in sorted(missing))
    )
    assert not undocumented, (
        "代码里有但 docs/api.md §0.2 没写（新增端点要同步文档）：\n  "
        + "\n  ".join(f"{m} {p} ({l})" for m, p, l in sorted(undocumented))
    )


def test_healthz_is_the_only_unauthenticated_api(app_module) -> None:
    levels = {
        (method, route.path, _route_level(route))
        for route in _flatten(app_module.routes)
        for method in (route.methods or ())
        if route.path.startswith("/api/") and method not in {"HEAD", "OPTIONS"}
    }
    public = {path for method, path, level in levels if level == "public"}
    # 只有登录/登出可以没有鉴权：它们本身就是取得凭据的入口。
    assert public == {"/api/login", "/api/logout"}, f"这些端点没有鉴权：{public}"


def test_page_routes_do_not_go_through_the_api_guard(app_module) -> None:
    """页面路由自己判断登录状态并 303，不挂依赖——文档 §7 是这么写的。"""
    pages = {
        (method, route.path)
        for route in _flatten(app_module.routes)
        for method in (route.methods or ())
        if not route.path.startswith("/api/") and route.path != "/healthz"
        and method not in {"HEAD", "OPTIONS"}
    }
    assert pages == DOCUMENTED_PAGES, (
        f"页面路由变了，docs/api.md §7 要同步。多出：{pages - DOCUMENTED_PAGES}；"
        f"缺少：{DOCUMENTED_PAGES - pages}"
    )
    for route in _flatten(app_module.routes):
        if route.path in {path for _, path in DOCUMENTED_PAGES}:
            assert _route_level(route) == "public", (
                f"页面路由 {route.path} 挂上了 API 鉴权依赖，"
                "会返回 401 而不是文档写的 303 重定向"
            )


# --------------------------------------------------------------------------- #
# 文档自身
# --------------------------------------------------------------------------- #
def test_document_actually_lists_every_endpoint() -> None:
    """上面那张表是"我以为文档写了什么"，这里确认文档正文真的写了。"""
    text = DOC.read_text(encoding="utf-8")
    for method, path, _level in sorted(DOCUMENTED_ROUTES):
        # 正文里以表格行或小节标题的形式出现
        pattern = re.compile(rf"`{re.escape(path)}`")
        assert pattern.search(text), f"docs/api.md 正文里找不到 {path}"
        assert method in text, f"docs/api.md 里找不到方法 {method}"


def test_document_has_no_stale_course_count_claims() -> None:
    """回归：课表计数曾经与课程数组撞名，文档也写错过。"""
    text = DOC.read_text(encoding="utf-8")
    assert "course_count" in text and "session_count" in text
    # 文档里必须给出可清空课表的写法
    assert '{"courses": []}' in text
