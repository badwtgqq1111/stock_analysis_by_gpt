"""Web 层的缓存与构建戳契约（"我加了功能，前端看不到"这类问题的守门人）。

事故复盘（2026-10-09）：概念/板块手臂在后端、命令行、`/api/selection` 全部正常，
但**用户在页面上怎么刷新都看不到**。原因是 `/api/*` 响应**没有任何缓存头**，
浏览器对无缓存头的 GET 会启发式缓存 —— 与 2026-10-08 那次"index.html 被缓存导致白屏"
是同一类问题（那次只修了静态资源）。本文件把三条契约钉死：
  1. `/api/*` 必须 `Cache-Control: no-store`；
  2. 静态入口 `index.html` 必须 `no-cache, must-revalidate`（可以 304，但不能强缓存）；
  3. `/health` 必须带**构建戳**（built_at + entry bundle），前端把它显示在导航栏，
     用户一眼就能确认"浏览器里跑的是不是最新构建"。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_api_responses_are_not_cached(client):
    for url in ("/api/selection", "/api/selection?source=cn_arm_concept",
                "/api/pipeline/selection", "/api/stocks"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        cache = resp.headers.get("cache-control", "")
        assert "no-store" in cache, f"{url} 缺少 no-store（会被浏览器启发式缓存）：{cache!r}"


def test_health_exposes_build_stamp(client):
    payload = client.get("/health").json()
    frontend = payload.get("frontend") or {}
    if not frontend.get("dist"):
        pytest.skip("未挂载前端 dist")
    assert frontend.get("built_at"), "缺少 built_at：前端无法自证构建版本"
    assert (frontend.get("entry") or "").startswith("/assets/"), frontend.get("entry")


def test_index_html_is_not_strongly_cached(client):
    resp = client.get("/")
    if resp.status_code != 200:
        pytest.skip("未挂载前端 dist")
    cache = resp.headers.get("cache-control", "")
    assert "no-cache" in cache and "max-age=31536000" not in cache, cache


def test_nav_shows_build_stamp():
    """导航栏必须显示构建戳（App.vue），否则用户无法自查缓存问题。"""
    text = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    assert "build-stamp" in text and "buildStamp" in text
    health_ts = (ROOT / "frontend/src/api/health.ts").read_text(encoding="utf-8")
    assert "built_at" in health_ts and "entry" in health_ts


def test_selection_page_supports_source_deeplink():
    """`/#/?source=cn_arm_concept` 要能直接落到该来源（否则别处没法做入口链接）。"""
    text = (ROOT / "frontend/src/pages/StockSelection.vue").read_text(encoding="utf-8")
    assert "route.query.source" in text
    assert "router.replace" in text, "切换来源后要同步到 URL，刷新/分享仍停在同一来源"
    industry = (ROOT / "frontend/src/pages/IndustryHeat.vue").read_text(encoding="utf-8")
    assert "#/?source=cn_arm_concept" in industry, "行业热度页应给出概念手臂名单的入口"


def test_health_build_stamp_recomputes_after_rebuild(client):
    """构建戳必须**每次请求重算**，不能是进程启动期的快照。

    事故（2026-10-15）：前端 `npm run build` 之后页面确实加载了新 bundle，但 `/health` 还
    报着启动那一刻的 `built_at` 与**磁盘上已经不存在的 entry**（实测 `index-B4_GoZm1.js`
    vs index.html 里引用的 `index-cNKXPK90.js`）—— 用户正是拿这个戳判断"浏览器里跑的是不是
    最新构建"，读到旧快照比没有戳更坏。这里用"改 index.html 的 mtime"当一次重建的替身：
    mtime 变了而 `built_at` 没跟着变，就说明读的是快照。
    """
    payload = client.get("/health").json()
    frontend = payload.get("frontend") or {}
    if not frontend.get("dist"):
        pytest.skip("未挂载前端 dist")
    index = Path(frontend["dist"]) / "index.html"
    original = index.stat().st_mtime
    touch_time = original + 7.0
    try:
        os.utime(index, (touch_time, touch_time))
        second = client.get("/health").json().get("frontend") or {}
    finally:
        os.utime(index, (original, original))         # 别把 dist 的戳改坏（其它用例要用）
    stamped = datetime.fromtimestamp(touch_time).strftime("%Y-%m-%d %H:%M:%S")
    assert second.get("built_at") == stamped, (
        f"/health 的构建戳没跟着 index.html 变：{second.get('built_at')} != {stamped}"
        "（说明返回的是进程启动期快照）")
    assert (second.get("entry") or "").startswith("/assets/"), second.get("entry")
