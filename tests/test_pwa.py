"""M8：PWA 外壳（manifest / Service Worker / 图标）与对外文档。

Service Worker 有一条关键红线要测：**绝不能缓存 `/api/` 响应**。
缓存了任务列表就会出现"勾选完刷新又变回来"这类错觉，而且极难排查。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx import AsyncClient
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC = PROJECT_ROOT / "app" / "static"
ICONS = STATIC / "icons"
DOCS = PROJECT_ROOT / "docs"

MANIFEST = STATIC / "manifest.webmanifest"
SW_JS = STATIC / "sw.js"
BASE_HTML = PROJECT_ROOT / "app" / "templates" / "base.html"
APP_JS = STATIC / "js" / "app.js"


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def test_manifest_is_valid_and_complete() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("name", "short_name", "start_url", "scope", "display", "icons"):
        assert key in data, f"manifest 缺少 {key}"
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"
    assert data["lang"] == "zh-CN"


def test_manifest_has_both_any_and_maskable_icons() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    purposes = {icon.get("purpose") for icon in data["icons"]}
    assert "any" in purposes
    assert "maskable" in purposes, "缺少 maskable 图标，安卓上会被裁得难看"


def test_manifest_referenced_icons_exist_with_right_sizes() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for icon in data["icons"]:
        path = PROJECT_ROOT / "app" / icon["src"].lstrip("/")
        assert path.is_file(), f"manifest 引用了不存在的图标 {icon['src']}"
        with Image.open(path) as image:
            expected = {int(part) for part in icon["sizes"].split("x")}
            assert set(image.size) == expected, f"{icon['src']} 尺寸应为 {expected}，实际 {image.size}"


@pytest.mark.parametrize("name", ["icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon-32.png"])
def test_icons_are_real_pngs(name: str) -> None:
    path = ICONS / name
    assert path.is_file()
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.width == image.height


# --------------------------------------------------------------------------- #
# Service Worker
# --------------------------------------------------------------------------- #
def test_service_worker_never_caches_api_responses() -> None:
    """核心红线：接口数据必须实时。

    断言方式刻意做成"顺序检查"而不是匹配某行文本：真正要保证的是
    **放行 /api/ 的判断发生在任何缓存操作之前**，中间多几个条件都不影响。
    """
    text = SW_JS.read_text(encoding="utf-8")
    handler = text.split("addEventListener('fetch'")[1]

    guard = handler.index("startsWith('/api/')")
    first_cache_use = handler.index("caches.")
    assert guard < first_cache_use, "对 /api/ 的放行必须早于任何缓存读写"
    assert "return" in handler[guard : guard + 160], "放行 /api/ 后必须直接 return"


def test_service_worker_uses_network_first_for_navigation() -> None:
    """页面是服务端渲染数据的，缓存优先会看到旧任务。"""
    text = SW_JS.read_text(encoding="utf-8")
    assert "request.mode === 'navigate'" in text
    assert "fetch(request).catch(" in text, "导航应 network-first，断网才退回外壳"


def test_service_worker_uses_network_first_for_static_assets() -> None:
    """静态资源也必须 network-first。

    这条是**花了一整轮才查出来**的：原来是 cache-first，注释里写的理由是
    "它们有版本号或很少变"——可这个前提不成立。项目没有构建步骤，就没有
    文件名指纹；VERSION 是手写常量，没人会记得改。于是任何 CSS / JS 改动
    都到不了已经装过 SW 的浏览器。

    实测后果：新写的备注组件样式一直不生效，看到的是旧样式，还以为是 CSS
    本身写错了（三角没隐藏、字号过大、文案重复）——全是缓存造成的假象。

    所以这条用例防的不是性能回退，是"改了看不到"。
    """
    text = SW_JS.read_text(encoding="utf-8")
    handler = text.split("addEventListener('fetch'")[1]
    static_branch = handler.split("startsWith('/static/')")[1]

    assert "networkFirst(request)" in static_branch, (
        "静态资源应当走 network-first。改回 caches.match 优先会让"
        "所有前端改动在装过 SW 的浏览器上永远不生效。"
    )
    # network-first 的实现里必须先 fetch、后落缓存
    helper = text.split("async function networkFirst")[1].split("\n}")[0]
    assert helper.index("await fetch(request)") < helper.index("cache.put")
    assert "caches.match(request)" in helper, "网络失败时要能退回缓存"


def test_service_worker_versions_its_cache() -> None:
    """版本号决定了旧外壳能否被清掉。"""
    text = SW_JS.read_text(encoding="utf-8")
    assert "const VERSION" in text
    assert "caches.delete" in text, "activate 时应清理旧版本缓存"


# --------------------------------------------------------------------------- #
# 静态资源的缓存破坏串
# --------------------------------------------------------------------------- #
async def test_static_assets_carry_a_content_hash(session_client: AsyncClient) -> None:
    """模板引静态资源必须带 ?v=<内容哈希>。

    这是给"SW 缓存让改动不生效"兜底的**第二道**保险：SW 已经改成
    network-first，但**已经装过旧 SW 的设备**仍然会命中旧缓存，而旧 SW
    要等下一次导航才被替换。带版本串的 URL 是不同的缓存键，`caches.match()`
    匹配不到，直接就绕过去了 —— 用户不需要手动清缓存。
    """
    from app.templating import ASSET_VERSION

    assert ASSET_VERSION, "版本串不该为空"
    # 用首页取样：已登录时 /login 会 303 到 /，响应体是空的
    html = (await session_client.get("/")).text
    assert f"/static/css/app.css?v={ASSET_VERSION}" in html
    assert f"/static/js/app.js?v={ASSET_VERSION}" in html

    # 逐个检查渲染出来的资源引用：漏一个就有一份资源会走旧缓存
    urls = re.findall(r'(?:src|href)="(/static/(?:css|js)/[^"]+)"', html)
    assert urls, "首页没引用任何 CSS/JS，用例失去意义"
    for url in urls:
        assert "?v=" in url, f"{url} 没有版本串，会走旧缓存"


async def test_every_template_versions_its_assets() -> None:
    """所有模板都要带版本串——漏一个就等于那个文件仍然会被旧缓存钉住。"""
    templates_dir = PROJECT_ROOT / "app" / "templates"
    for template in sorted(templates_dir.glob("*.html")):
        for match in re.finditer(
            r'(?:src|href)="(/static/(?:css|js)/[^"]+)"', template.read_text(encoding="utf-8")
        ):
            url = match.group(1)
            assert "?v=" in url, f"{template.name} 里 {url} 没有版本串"


def test_asset_version_changes_with_content() -> None:
    """版本串必须由内容决定 —— 手工维护版本号迟早会忘。"""
    import app.templating as templating

    before = templating.ASSET_VERSION
    original = templating._static_version
    try:
        # 内容没变 → 版本不变
        assert original() == before
    finally:
        templating._static_version = original
    assert len(before) == 10, "取哈希前 10 位，够短也够区分"


def test_service_worker_does_not_fail_install_on_one_bad_asset() -> None:
    text = SW_JS.read_text(encoding="utf-8")
    assert "allSettled" in text, "单个外壳资源缺失不应让整个 SW 安装失败"


async def test_sw_is_served_from_root_with_correct_scope(session_client: AsyncClient) -> None:
    """从 /static/ 提供的话作用域只有 /static/，管不到页面导航。"""
    response = await session_client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert "javascript" in response.headers["content-type"]
    assert "no-cache" in response.headers.get("cache-control", "")


async def test_sw_is_reachable_without_login(client: AsyncClient) -> None:
    """浏览器注册 SW 时不一定带 Cookie，且它本身不含敏感信息。"""
    assert (await client.get("/sw.js")).status_code == 200


# --------------------------------------------------------------------------- #
# 页面接线
# --------------------------------------------------------------------------- #
async def test_base_template_links_pwa_assets(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert 'rel="manifest"' in html
    assert 'rel="apple-touch-icon"' in html
    assert 'name="theme-color"' in html
    assert 'name="apple-mobile-web-app-capable"' in html


def test_service_worker_registration_is_https_only() -> None:
    """http 页面里 navigator.serviceWorker 不可用，必须先判协议。"""
    text = APP_JS.read_text(encoding="utf-8")
    assert "serviceWorker" in text
    assert "location.protocol === \"https:\"" in text


async def test_manifest_is_served(session_client: AsyncClient) -> None:
    response = await session_client.get("/static/manifest.webmanifest")
    assert response.status_code == 200
    json.loads(response.text)


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["api.md", "shortcuts.md", "widget-v2.md", "dev-notes.md"])
def test_docs_exist(name: str) -> None:
    path = DOCS / name
    assert path.is_file(), f"缺少文档 {name}"
    assert path.stat().st_size > 500, f"{name} 内容过少"


def test_api_doc_covers_every_route() -> None:
    """文档漏写接口比写错更麻烦 —— 用真实路由表核对一遍。"""
    from app.main import create_app

    text = (DOCS / "api.md").read_text(encoding="utf-8")
    # 只核对约定好的公开接口清单
    expected = [
        "/api/login", "/api/logout",
        "/api/tasks", "/api/ingest", "/api/courses",
        "/api/settings", "/api/keys", "/api/status", "/healthz",
    ]
    for path in expected:
        assert path in text, f"api.md 没有提到 {path}"


def test_shortcuts_doc_covers_the_heic_trap() -> None:
    """HEIC 是最容易踩且最难看懂的一脚，文档必须写清楚。"""
    text = (DOCS / "shortcuts.md").read_text(encoding="utf-8")
    assert "HEIC" in text
    assert "转换图像" in text
    assert "换行符" in text, "Base64 的换行符选项也必须提醒"
    assert "confirm_url" in text


def test_widget_doc_records_the_refresh_limitation() -> None:
    """避免将来对小组件的实时性抱有不切实际的期待。"""
    text = (DOCS / "widget-v2.md").read_text(encoding="utf-8")
    assert "15–60 分钟" in text or "15-60 分钟" in text
    assert "refreshAfterDate" in text
    assert "只是提示" in text or "提示" in text


def test_readme_documents_the_whole_delivery() -> None:
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "PLAN.md" in text
    for command in ("uv sync", "app.cli init", "app.serve", "pytest"):
        assert command in text, f"README 缺少 {command}"
    assert "deploy/README.md" in text


def test_dev_notes_records_the_sandbox_quirks() -> None:
    """这些坑不写下来，下次还会再踩一遍。"""
    text = (DOCS / "dev-notes.md").read_text(encoding="utf-8")
    assert "Schannel" in text
    assert "no:cacheprovider" in text
    assert "trace" in text.lower()
