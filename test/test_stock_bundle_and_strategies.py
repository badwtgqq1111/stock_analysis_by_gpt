"""个股资料包 + 日历策略标签 + 日 K 自选股/资料面板（三个用户诉求的契约）。

用户 2026-10-09 提了三件事：
  1. 日历的"多策略对比"要能看出**每只票是哪种策略选出来的**；
  2. 任何出现股票代码/名称的地方都应能跳日 K 并看到该股的其他信息（财务/资金/龙虎榜/公告…）；
  3. 日 K 页要有自选股侧栏。
本文件把这三件事的**数据契约**钉住（UI 交互由浏览器验证，见 VERIFICATION 记录）。
"""
from __future__ import annotations

import sys
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


# ─── 1. 日历：策略标签 ──────────────────────────────────────────────────────
def test_calendar_day_carries_strategy_labels(client):
    body = client.get("/api/calendar/day", params={"date": "2026-10-08"}).json()
    if not body.get("rows"):
        pytest.skip("台账没有该决策日")
    assert body.get("strategy_meta"), "缺少 source→策略 映射"
    summary = body.get("strategy_summary") or []
    groups = {s["group"] for s in summary}
    assert {"模型策略", "小市值手臂", "行业轮动"} <= groups, groups
    for row in body["rows"][:5]:
        assert isinstance(row.get("strategy_groups"), list) and row["strategy_groups"], row
    for row in (body.get("compare") or [])[:5]:
        assert row.get("strategy_groups"), row
        assert row.get("strategy_labels")


def test_strategy_meta_covers_known_sources(client):
    meta = client.get("/api/calendar/day", params={"date": "2026-10-08"}).json().get("strategy_meta", {})
    for key in ("notify", "preselected", "arm", "arm_concept"):
        assert key in meta, f"{key} 没映射到策略（日历里会显示英文 key）"
    assert meta["arm"]["group"] == "小市值手臂"
    assert meta["arm_concept"]["group"] == "行业轮动"


# ─── 2. 个股资料包 ─────────────────────────────────────────────────────────
def test_stock_bundle_sections(client):
    body = client.get("/api/stocks/000001.SZ/bundle").json()
    for key in ("profile", "valuation", "financials", "flow", "lhb", "limits",
                "selections", "announcements", "links"):
        assert key in body, key
    assert body["profile"]["name"], "档案必须有名称"
    assert body["links"]["kline"].startswith("/#/kline?code=")
    assert any(k in body["links"] for k in ("eastmoney", "ths", "xueqiu", "cninfo")), "要有外站链接兜底新闻"
    # 财务/资金是核心维度，不能整体缺失（数据在仓库里）
    assert body["financials"]["available"], body["financials"].get("reason")
    assert body["flow"]["available"], body["flow"].get("reason")


def test_stock_bundle_rejects_unknown_code(client):
    assert client.get("/api/stocks/999999.SZ/bundle").status_code == 404


def test_stock_bundle_reports_selections(client):
    """"哪些策略选过它"必须能从台账读出来（否则日 K 页的"选股记录"tab 是空的）。"""
    code = client.get("/api/selection", params={"source": "cn_arm"}).json()["rows"][0]["stock_code"]
    body = client.get(f"/api/stocks/{code}/bundle").json()
    assert body["selections"]["latest"], f"{code} 应能查到研究手臂的入选记录"


# ─── 3. 前端契约（自选股侧栏 + 资料面板 + 可点击）──────────────────────────
def test_kline_page_has_watchlist_and_info_panel():
    page = (ROOT / "frontend/src/pages/KlinePage.vue").read_text(encoding="utf-8")
    assert "StockWatchlist" in page and "StockInfoPanel" in page
    assert 'class="col-side"' in page and 'class="col-main"' in page


def test_watchlist_component_groups_and_local_storage():
    text = (ROOT / "frontend/src/components/StockWatchlist.vue").read_text(encoding="utf-8")
    for group in ("cn_arm_concept", "cn_arm", "cn_ensemble", "paper"):
        assert group in text, f"自选股侧栏缺少快速分组 {group}"
    # 2026-10-10：持久化搬进了共享 composable（选股日历 / 行业热度 / 选股结果 也用同一份自选），
    # 侧栏改成消费它。所以"本地自选需要持久化"这条不变量现在落在 useWatchlist.ts 上。
    assert "useWatchlist" in text, "侧栏必须用共享自选 composable"
    composable = (ROOT / "frontend/src/composables/useWatchlist.ts").read_text(encoding="utf-8")
    assert "localStorage" in composable, "本地自选需要持久化"
    assert "kline.watchlist.v1" in composable, "沿用旧 key，避免升级后用户自选丢失"


def test_info_panel_tabs_cover_existing_data():
    text = (ROOT / "frontend/src/components/StockInfoPanel.vue").read_text(encoding="utf-8")
    for tab in ("概况", "财务", "资金", "龙虎榜", "选股记录", "公告"):
        assert tab in text, f"资料面板缺少 {tab}"
    assert "外站" in text, "新闻没有本地源，必须标注是外站链接"
