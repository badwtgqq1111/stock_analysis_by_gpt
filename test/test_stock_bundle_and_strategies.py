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


def test_watchlist_component_groups_and_local_storage(client):
    """侧栏的快速分组改为**由策略池驱动**（2026-10-10），不再硬编码来源子集。

    为什么改成契约测试而不是"组件里出现 cn_arm 字样"：分组来自
    `getStrategyPool()` → `strategyGroups()`，按 `selection_source` 过滤，
    所以**新的策略（含备选/候选）一进池就自动出现在侧栏**；写死字符串的断言
    反而会把这条链路钉回硬编码（而且候选策略 `cn_jq_*` 根本没有对应的旧字符串）。
    """
    text = (ROOT / "frontend/src/components/StockWatchlist.vue").read_text(encoding="utf-8")
    assert "getStrategyPool" in text and "strategyGroups" in text, "分组必须来自策略池"
    assert "key: 'paper'" in text or "'paper'" in text, "侧栏要有模拟盘持仓分组"
    # 2026-10-10：持久化搬进了共享 composable（选股日历 / 行业热度 / 选股结果 也用同一份自选），
    # 侧栏改成消费它。所以"本地自选需要持久化"这条不变量现在落在 useWatchlist.ts 上。
    assert "useWatchlist" in text, "侧栏必须用共享自选 composable"
    composable = (ROOT / "frontend/src/composables/useWatchlist.ts").read_text(encoding="utf-8")
    assert "localStorage" in composable, "本地自选需要持久化"
    assert "kline.watchlist.v1" in composable, "沿用旧 key，避免升级后用户自选丢失"

    # 池里每一个带 selection_source 的策略（生产 + 备选）都要能变成侧栏分组
    pool = client.get("/api/strategies", params={"refresh": "false"}).json()
    sources = {s.get("selection_source") for s in pool["strategies"] if s.get("selection_source")}
    expected = {"cn_ensemble", "cn_arm", "cn_arm_concept",
                "cn_jq_multi_factor", "cn_jq_roe_roa"}
    assert expected <= sources, f"策略池缺少这些来源：{sorted(expected - sources)}"

    helper = (ROOT / "frontend/src/components/watchlistStrategies.ts").read_text(encoding="utf-8")
    assert "selection_source" in helper and "retired" in helper, \
        "分组按选择来源过滤，且 retired 的策略不再出现"


def test_info_panel_tabs_cover_existing_data():
    text = (ROOT / "frontend/src/components/StockInfoPanel.vue").read_text(encoding="utf-8")
    for tab in ("概况", "财务", "资金", "龙虎榜", "选股记录", "公告"):
        assert tab in text, f"资料面板缺少 {tab}"
    assert "外站" in text, "新闻没有本地源，必须标注是外站链接"


# ─── 正期望策略的选股同步到其他前端部分（2026-10-10 第十四轮）────────────────
def test_positive_expectancy_strategies_are_selectable_everywhere(client):
    """用户要求："把正期望的策略选股同步到其他前端部分，包括选股，K线自选等等。"

    一个策略要能在前端各处出现，靠的是这三条链接（缺一条就会"策略池里有、别处看不到"）：
      1. `config/strategies.json` 里 `selection_source` 非空 → 选股结果页的来源 tab、K 线自选侧栏的策略分组
         （侧栏分组由 `watchlistStrategies.strategyGroups(pool.strategies)` 按 selection_source 生成）；
      2. `selection_sources.SOURCES` 里有对应来源且产物存在 → `/api/selection?source=...` 能出名单；
      3. 正期望的策略还要能被"个股资料 → 选股记录"查到（台账 reader 或最新名单）。
    """
    import json as _json
    from backend.services.selection_sources import SOURCES

    registry = _json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    performance_path = ROOT / "output/strategy_pool/performance.json"
    if not performance_path.exists():
        pytest.skip("还没有绩效快照")
    performance = _json.loads(performance_path.read_text(encoding="utf-8"))["strategies"]

    positive = []
    for spec in registry["strategies"]:
        row = performance.get(spec["id"]) or {}
        curve = row.get("research") or row.get("tracking") or {}
        # 与前端同一条口径：样本 ≥30 才判方向（1~2 个观测的"正收益"不算正期望）
        if ((curve.get("metrics") or {}).get("total_return", 0) > 0
                and (curve.get("observations") or 0) >= 30):
            positive.append(spec)
    assert positive, "至少应有一条正期望策略"

    for spec in positive:
        source = spec.get("selection_source")
        assert source, f"正期望策略 {spec['id']} 没有 selection_source（选股/自选侧栏就看不到它）"
        assert source in SOURCES, f"{source} 不在 selection_sources.SOURCES 里"
        body = client.get("/api/selection", params={"source": source}).json()
        rows = body.get("rows") or []
        assert rows, f"/api/selection?source={source} 没有返回名单"
        # 阶段标签要能区分"研究候选"与"最终名单"（不能都落到"候选池（preselect）"）
        stages = {r.get("stage") for r in rows}
        assert stages - {None}, f"{source} 的名单没有 stage 标签（阶段列会显示成空）"

    # K 线自选侧栏：分组就是"带 selection_source 且未下线"的策略
    helper = (ROOT / "frontend/src/components/watchlistStrategies.ts").read_text(encoding="utf-8")
    assert "selection_source" in helper and "retired" in helper
    pool = client.get("/api/strategies", params={"refresh": "false"}).json()
    group_sources = {s.get("selection_source") for s in pool["strategies"]
                     if s.get("selection_source") and s["stage"] != "retired"}
    for spec in positive:
        assert spec["selection_source"] in group_sources, f"{spec['id']} 不会出现在 K 线自选侧栏"


def test_bundle_selection_record_covers_every_registered_strategy(client):
    """「个股资料 → 选股记录」不能只认硬编码的三份名单：要遍历策略登记表（含 box_arm / rot_arm / jq_*）。"""
    text = (ROOT / "backend/api/stock_info.py").read_text(encoding="utf-8")
    assert "config/strategies.json" in text, "选股记录的最新名单要遍历策略登记表"
    assert '"cn_ensemble_selected.csv"' not in text, "不该再硬编码名单文件名"

    # 取一条 box_arm 的持仓票，核对 bundle 里能查到它
    picks = ROOT / "output/results_cn/box_arm_selection.csv"
    if not picks.exists():
        pytest.skip("还没有 box_arm 名单")
    import pandas as pd
    code = str(pd.read_csv(picks).iloc[0]["stock_code"])
    body = client.get(f"/api/stocks/{code}/bundle").json()
    selections = body.get("selections") or {}
    assert selections.get("available"), f"{code} 应能查到选股记录"
    strategies = {x["strategy"] for x in (selections.get("latest") or [])}
    assert any("箱体突破回踩" in s for s in strategies), f"最新名单里应有 box_arm：{strategies}"
    sources = {r["source"] for r in (selections.get("rows") or [])}
    assert "box_arm" in sources, f"台账来源里应有 box_arm：{sources}"


def test_calendar_knows_the_structural_arm(client):
    """选股日历要认得 box_arm（STRATEGY_META + 前端字母 B + 台账 reader）。"""
    from backend.api.calendar import STRATEGY_META
    assert "box_arm" in STRATEGY_META and STRATEGY_META["box_arm"]["group"] == "打板/低吸"
    page = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert page.count("box_arm") >= 4 and "B=box_arm" in page
    ledger = (ROOT / "scripts/build_selection_ledger.py").read_text(encoding="utf-8")
    assert "read_box_arm_history" in ledger and '"box_arm"' in ledger
