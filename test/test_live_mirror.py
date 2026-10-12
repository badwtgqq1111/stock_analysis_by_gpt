"""实盘镜像账户（真实持仓）—— `factor_engine/portfolio/live_mirror.py` + `scripts/build_live_mirror.py`。

守住五件事（都是差点踩的坑）：
  1. 账户算术：累计收益的分母必须是**推算初始本金**（成本 + 现金 + |已实现亏损|）——
     忘了把已实现亏损加回去，分母就变小、亏损率被说轻；
  2. 快照校验：缺字段/股数非正要直接报错，不许变成 nan 静默流下去；
  3. 盯市：快照日之前**不回填**（真实买入日期未知），缺 bar 用最后可得价；
  4. 建议规则 R0~R5 全部有判定，「卖出」只在 R3 出现；
  5. 接线：策略池有 live 阶段、选股源有 cn_live_holdings、每日自动化的 live-mirror 必须排在
     ai_picks **之前**（AI 逐股分析读的就是它写的名单归档）、预检清单带上本测试。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

from factor_engine.portfolio import live_mirror as lm  # noqa: E402


def _load_script(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_script("build_live_mirror_under_test", "scripts/build_live_mirror.py")


def _snapshot(**over):
    base = {
        "broker": "测试券商",
        "account_masked": "**0001",
        "asof": "2026-10-12",
        "captured_at": "2026-10-12 13:23",
        "cash": 40.55,
        "reported": {"total_assets": 39174.05, "market_value": 39133.00, "float_pnl": -6059.51},
        "positions": [
            {"stock_code": "300229.SZ", "name": "拓尔思", "shares": 1100,
             "cost_price": 13.145, "last_price": 13.120},
            {"stock_code": "002966.SZ", "name": "苏州银行", "shares": 400,
             "cost_price": 9.513, "last_price": 9.510},
        ],
        "cleared_positions": [{"stock_code": "600460.SH", "name": "士兰微", "realized_pnl": -1589.94}],
    }
    base.update(over)
    return base


def test_account_state_arithmetic():
    state = lm.account_state(_snapshot())
    assert state["cost_basis"] == pytest.approx(18264.70)
    assert state["market_value"] == pytest.approx(1100 * 13.12 + 400 * 9.51)
    # 推算初始本金必须把**已实现亏损加回去**，否则亏损率被说轻
    assert state["initial_capital_implied"] == pytest.approx(state["cost_basis"] + 40.55 + 1589.94)
    assert state["realized_pnl"] == pytest.approx(-1589.94)
    assert state["cumulative_return"] == pytest.approx(
        state["total_assets"] / state["initial_capital_implied"] - 1)
    assert state["positions"]["weight"].sum() == pytest.approx(1.0)
    assert state["top1_weight"] == pytest.approx(state["positions"]["weight"].iloc[0])
    # 与券商 App 的对账字段必须在（差多少都要明写，不许悄悄抹平）
    assert "delta_total_assets" in state["reconciliation"]


def test_validate_snapshot_rejects_bad_input():
    with pytest.raises(ValueError):
        lm.account_state({"asof": "2026-10-12", "cash": 0.0})
    with pytest.raises(ValueError):
        lm.account_state(_snapshot(positions=[{"stock_code": "300229.SZ", "shares": 100}]))
    bad = _snapshot()
    bad["positions"][0]["shares"] = 0
    with pytest.raises(ValueError):
        lm.account_state(bad)
    cleared = _snapshot()
    del cleared["cleared_positions"][0]["realized_pnl"]
    with pytest.raises(ValueError):
        lm.account_state(cleared)


def test_mark_to_market_never_backfills():
    snapshot = _snapshot()
    bars = pd.DataFrame([
        # 快照日之前：**不该**出现在曲线里（真实买入日期未知，回填等于编数据）
        {"trade_date": "2026-10-08", "stock_code": "300229.SZ", "close": 12.0},
        {"trade_date": "2026-10-08", "stock_code": "002966.SZ", "close": 9.0},
        {"trade_date": "2026-10-12", "stock_code": "300229.SZ", "close": 13.0},
        {"trade_date": "2026-10-12", "stock_code": "002966.SZ", "close": 9.5},
        # 10-13 苏州银行停牌（无 bar）→ 用最后可得价
        {"trade_date": "2026-10-13", "stock_code": "300229.SZ", "close": 13.5},
    ])
    state = lm.account_state(snapshot)
    nav = lm.mark_to_market(snapshot, bars)
    assert list(nav["asof_date"]) == ["2026-10-12", "2026-10-13"]
    assert nav["nav"].iloc[0] == pytest.approx(state["cash"] + 1100 * 13.0 + 400 * 9.5)
    assert nav["cum_return"].iloc[0] == pytest.approx(0.0)
    # 停牌那天的 9.5 是**顺延**来的：两个仓位都有价（priced=2），不是缺价
    assert nav["priced"].iloc[-1] == 2 and nav["unpriced"].iloc[-1] == 0
    assert nav["nav"].iloc[-1] == pytest.approx(state["cash"] + 1100 * 13.5 + 400 * 9.5)
    assert (nav["drawdown"] <= 0).all()
    # 从头到尾没有行情的票：不许外推、计进 unpriced，且净值里就当它没有价
    only_one = bars[bars["stock_code"] == "300229.SZ"]
    nav_one = lm.mark_to_market(snapshot, only_one)
    assert nav_one["unpriced"].iloc[-1] == 1
    assert nav_one["nav"].iloc[-1] == pytest.approx(state["cash"] + 1100 * 13.5)


def test_advice_rules_cover_all_branches():
    state = lm.account_state(_snapshot())
    positions = state["positions"]
    codes = list(positions["stock_code"])
    mom = {codes[0]: -0.05, codes[1]: 0.05}

    got = lm.advice_rows(positions, preselected={codes[0]: 9}, momentum_20d=mom).set_index("stock_code")
    assert got.loc[codes[0], "rule"] == "R2"
    got = lm.advice_rows(positions, selected={codes[0]: 1}, momentum_20d=mom).set_index("stock_code")
    assert (got.loc[codes[0], "action"], got.loc[codes[0], "rule"]) == ("买入", "R0")
    got = lm.advice_rows(positions, selected={codes[0]: 8}, momentum_20d=mom).set_index("stock_code")
    assert got.loc[codes[0], "rule"] == "R1"
    got = lm.advice_rows(positions, momentum_20d=mom).set_index("stock_code")
    assert (got.loc[codes[0], "action"], got.loc[codes[0], "rule"]) == ("卖出", "R3")
    assert got.loc[codes[1], "rule"] == "R4"
    got = lm.advice_rows(positions, momentum_20d={})
    assert set(got["rule"]) == {"R5"} and set(got["action"]) == {"持有"}
    assert set(lm.ADVICE_RULES) == {"R0", "R1", "R2", "R3", "R4", "R5"}
    assert set(lm.advice_rows(positions, selected={codes[0]: 1}, momentum_20d=mom)["action"]) <= {"买入", "持有", "卖出"}


def test_merge_live_prices_keeps_snapshot_on_miss():
    snapshot = _snapshot()
    # 腾讯批量快照的 key 是 6 位纯数字（`tx_symbol` 之后取后 6 位）
    merged = lm.merge_live_prices(snapshot, {"300229": {"price": 13.33, "time": "20261012140000"}})
    by_code = {p["stock_code"]: p for p in merged["positions"]}
    assert by_code["300229.SZ"]["last_price"] == pytest.approx(13.33)
    assert by_code["300229.SZ"]["price_source"].startswith("live")
    # 没拿到价的票保留快照价（绝不填 0、不外推）
    assert by_code["002966.SZ"]["last_price"] == pytest.approx(9.510)
    assert by_code["002966.SZ"]["price_source"] == "snapshot"
    assert snapshot["positions"][0]["last_price"] == pytest.approx(13.120)


def test_build_writes_four_artifacts(tmp_path, builder):
    snapshot_path = tmp_path / "snap.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "live_mirror"
    selection_csv = tmp_path / "results" / "cn_live_holdings.csv"
    history = tmp_path / "history"
    builder.build(snapshot_path=snapshot_path, out_dir=out, use_live=False,
                  selection_csv=selection_csv, history_dir=history)
    for name in ("snapshot.json", "positions.csv", "nav.csv", "summary.json"):
        assert (out / name).exists(), f"缺产物 {name}"
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["asof"] == "2026-10-12"
    assert summary["price_source"] == "snapshot"
    assert summary["sample_status"] == "short_sample"
    assert summary["notes"] and any("不回填" in n for n in summary["notes"])
    positions = pd.read_csv(out / "positions.csv")
    for col in ("action", "rule", "reason", "weight", "pnl_amount"):
        assert col in positions.columns
    rows = pd.read_csv(selection_csv)
    assert (history / "2026-10-12.csv").exists()
    assert "rank_kind" in rows.columns
    assert set(rows["stage"]) == {"live"}


def test_wiring_strategy_pool_source_and_automation():
    config = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    entry = next((s for s in config["strategies"] if s["id"] == "live_holdings"), None)
    assert entry, "策略池登记表里要有 live_holdings"
    assert entry["stage"] == "live"
    assert entry["selection_source"] == "cn_live_holdings"
    assert entry["date_column"] == "trade_date"
    assert entry["curve_source"]["nav_csv"] == "output/paper_trading/live_mirror/nav.csv"

    pool_script = (ROOT / "scripts/sync_strategy_pool.py").read_text(encoding="utf-8")
    assert '"live"' in pool_script.split("STAGES = (")[1].split(")")[0], "策略池阶段词汇要含 live"
    assert '"live":' in pool_script, "live 阶段要有中文标签"

    from backend.services import selection_sources as src
    assert "cn_live_holdings" in src.SOURCES
    assert src.SOURCES["cn_live_holdings"]["files"][0][1].name == "cn_live_holdings.csv"
    for col in ("action", "rule", "cost_price", "pnl_pct", "note", "price_source"):
        assert col in src._COLS, f"表头白名单缺 {col}，页面上会是空的"
        assert col in src.SOURCES["cn_live_holdings"]["columns"]

    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "DAILY_LIVE_MIRROR" in daily
    assert daily.index("run_data_step live_mirror") < daily.index("run_data_step ai_picks")
    assert "test_live_mirror.py" in daily, "预检清单要带上本测试"

    freshness = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "实盘镜像" in freshness and "live_mirror/nav.csv" in freshness


def test_api_and_frontend_expose_live_mirror():
    api = (ROOT / "backend/api/paper.py").read_text(encoding="utf-8")
    assert "_live_mirror" in api and '"live": _live_mirror()' in api
    page = (ROOT / "frontend/src/pages/PaperTrading.vue").read_text(encoding="utf-8")
    assert "实盘镜像" in page and "data.value?.live" in page
    types = (ROOT / "frontend/src/types/api.ts").read_text(encoding="utf-8")
    assert "export interface LiveMirror" in types and "live?: LiveMirror" in types


def test_ai_picks_feeds_position_context():
    script = (ROOT / "scripts/analyze_strategy_picks_ai.py").read_text(encoding="utf-8")
    for field in ("cost_price", "pnl_pct", "action", "rule", "rank_kind"):
        assert f'"{field}"' in script, f"AI 逐股要拿到 {field}，否则会把已持仓的票当新候选评"


# ─── 收益对比（日/周/月 vs 中证500）与收益日历（2026-10-12 第二轮：用户在页面上要的三样）───
def _bars_for_curve():
    """两周的合成行情：组合一路持平、基准先跌后涨 —— 用来手算核对每个统计量。"""
    days = pd.bdate_range("2026-03-02", periods=10)
    rows = []
    for i, day in enumerate(days):
        rows.append({"trade_date": day, "stock_code": "300229.SZ", "close": 10.0})          # 不动
        rows.append({"trade_date": day, "stock_code": "002966.SZ", "close": 20.0 * (1 + 0.01 * i)})
    return pd.DataFrame(rows), days


def test_buy_and_hold_curve_is_weight_fixed_and_normalized():
    bars, days = _bars_for_curve()
    curve = lm.buy_and_hold_curve({"300229.SZ": 0.5, "002966.SZ": 0.5}, bars, start=days[0])
    assert len(curve) == len(days)
    assert curve["port_nav"].iloc[0] == pytest.approx(1.0)
    # 0.5*1 + 0.5*(1+1%*t) ⇒ 第 9 天 = 1 + 0.5*0.09
    assert curve["port_nav"].iloc[-1] == pytest.approx(1.0 + 0.5 * 0.09)
    assert curve["coverage_weight"].iloc[0] == pytest.approx(1.0)
    # 权重自动归一：只给 0.3/0.3 也应该是 50/50
    same = lm.buy_and_hold_curve({"300229.SZ": 0.3, "002966.SZ": 0.3}, bars, start=days[0])
    assert same["port_nav"].iloc[-1] == pytest.approx(curve["port_nav"].iloc[-1])


def test_resample_uses_last_real_trading_day_not_period_label():
    bars, days = _bars_for_curve()
    curve = lm.buy_and_hold_curve({"300229.SZ": 0.5, "002966.SZ": 0.5}, bars, start=days[0])
    curve["bench_nav"] = 1.0
    curve["bench_ret"] = 0.0
    curve["excess_ret"] = curve["port_ret"]
    monthly = lm.resample_curve(curve, "M")
    # 标签必须是**最后一根真实交易日**（2026-03-13），不是 3 月月末
    assert monthly["period_end"].iloc[-1] == str(days[-1].date())
    assert "period_label" in monthly.columns
    weekly = lm.resample_curve(curve, "W")
    assert len(weekly) >= 2
    for row in weekly.itertuples():
        assert row.period_end <= "2026-03-13"


def test_curve_stats_and_calendar_cells():
    bars, days = _bars_for_curve()
    curve = lm.buy_and_hold_curve({"300229.SZ": 0.5, "002966.SZ": 0.5}, bars, start=days[0])
    curve["bench_nav"] = 1.0
    curve["bench_ret"] = 0.0
    curve["excess_ret"] = curve["port_ret"]
    stats = lm.curve_stats(curve, freq="D")
    assert stats["periods"] == len(days) - 1
    assert stats["total_return"] == pytest.approx(0.045)
    assert stats["benchmark_total_return"] == pytest.approx(0.0)
    assert stats["excess_return"] == pytest.approx(0.045)
    assert stats["max_drawdown"] == pytest.approx(0.0)         # 单调上行
    assert stats["win_rate"] == pytest.approx(1.0)             # 每期都跑赢 0
    # 周/月频也要能算（列名不同：period_end）
    assert lm.curve_stats(lm.resample_curve(curve, "W"), freq="W")["periods"] >= 1
    cells = lm.calendar_cells(curve, "D")
    assert len(cells) == len(days)
    assert cells[0]["excess"] == pytest.approx(cells[0]["port_ret"] - cells[0]["bench_ret"])
    assert lm.calendar_cells(curve, "M")[0]["period"] == str(days[-1].date())


def test_performance_payload_written_and_served(tmp_path, builder):
    snapshot_path = tmp_path / "snap.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "live_mirror"
    result = builder.build(snapshot_path=snapshot_path, out_dir=out, use_live=False,
                           selection_csv=tmp_path / "cn_live_holdings.csv",
                           history_dir=tmp_path / "hist", years=1)
    assert (out / "performance.json").exists(), "日/周/月 对比与收益日历要有独立产物"
    perf = json.loads((out / "performance.json").read_text(encoding="utf-8"))
    if not perf.get("available"):           # 合成票在本地库里可能没有日线：允许 unavailable，但字段要规范
        assert "reason" in perf
        return
    for key in ("daily", "weekly", "monthly"):
        block = perf[key]
        assert len(block["dates"]) == len(block["port_nav"]) == len(block["bench_nav"])
        assert len(block["calendar"]) == len(block["dates"])
        assert block["stats"]["periods"] >= 0
    assert perf["benchmark"]["code"] == "000905.SH"
    assert perf["weights"] and abs(sum(perf["weights"].values()) - 1.0) < 1e-6
    assert any("不是" in n and "账户业绩" in n for n in perf["notes"]), "口径说明必须写清楚这不是账户业绩"


def test_api_and_page_wire_the_new_module():
    api = (ROOT / "backend/api/paper.py").read_text(encoding="utf-8")
    assert '@router.get("/live/performance")' in api and "ttl_cache" in api
    page = (ROOT / "frontend/src/pages/LiveAccount.vue").read_text(encoding="utf-8")
    for marker in ("实时分时", "日K", "中证500", "收益日历", "KlineChart", "getLivePerformance"):
        assert marker in page, f"实盘页缺 {marker}"
    router = (ROOT / "frontend/src/router/index.ts").read_text(encoding="utf-8")
    assert "LiveAccount.vue" in router and "'/live'" in router
    app = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    assert "{ to: '/live', label: '实盘镜像' }" in app, "导航要能点到实盘页"
    client = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "getLivePerformance" in client


def test_granularity_totals_must_agree(tmp_path, builder):
    """**同一段行情，日/周/月三种切法的累计收益必须完全相等**（链式相乘 = 总收益）。

    这条不变式抓到过一个真 bug：周/月频的第一行是「期末」净值，`nav末/nav首 − 1` 会把第一期的
    涨跌漏掉 ⇒ 日频 −19.46% 被算成周频 −17.90%（差 1.56pp，而且页面三档数字互相打架）。
    """
    perf_path = ROOT / "output/paper_trading/live_mirror/performance.json"
    if not perf_path.exists():
        pytest.skip("还没有 performance.json（跑 scripts/build_live_mirror.py）")
    perf = json.loads(perf_path.read_text(encoding="utf-8"))
    if not perf.get("available"):
        pytest.skip("回填曲线不可用")
    totals = {k: perf[k]["stats"]["total_return"] for k in ("daily", "weekly", "monthly")}
    bench = {k: perf[k]["stats"]["benchmark_total_return"] for k in ("daily", "weekly", "monthly")}
    assert max(totals.values()) - min(totals.values()) < 1e-9, f"三个粒度的组合累计不一致：{totals}"
    assert max(bench.values()) - min(bench.values()) < 1e-9, f"三个粒度的基准累计不一致：{bench}"
    # 日历复利连乘 == 曲线累计（同一份数据的两种展示，不许各算各的）
    for key in ("daily", "weekly", "monthly"):
        acc = 1.0
        for cell in perf[key]["calendar"]:
            acc *= (1 + cell["port_ret"])
        assert abs((acc - 1) - perf[key]["stats"]["total_return"]) < 1e-6, f"{key} 日历与曲线对不上"


# ─── 盘中实时视图 + 每天更新（2026-10-12 第三轮：用户"实盘的要求每天数据都是实时更新的"）───
def test_realtime_view_recomputes_account_with_live_prices():
    positions = pd.DataFrame([
        {"stock_code": "300229.SZ", "name": "拓尔思", "shares": 1000, "cost_price": 10.0,
         "last_price": 10.0, "action": "持有", "rule": "R4", "momentum_20d": 0.01},
        {"stock_code": "002966.SZ", "name": "苏州银行", "shares": 500, "cost_price": 20.0,
         "last_price": 20.0, "action": "卖出", "rule": "R3", "momentum_20d": -0.02},
    ])
    quotes = {
        "300229": {"price": 11.0, "time": "20261012140000", "prev_close": 10.5, "change_pct": 0.0476},
        "002966": {"price": 19.0, "time": "20261012140000", "prev_close": 20.5, "change_pct": -0.0732},
    }
    view = lm.realtime_view(positions, quotes, cash=100.0, cleared_pnl=-200.0, asof="2026-10-12")
    # 市值 = 1000*11 + 500*19 = 20,500；总资产 = 20,600
    assert view["market_value"] == pytest.approx(20_500.0)
    assert view["total_assets"] == pytest.approx(20_600.0)
    assert view["unrealized_pnl"] == pytest.approx(1000 * 1.0 + 500 * -1.0)
    # 推算初始本金 = 成本(1000*10+500*20=20000) + 现金 100 + |已实现 200|
    assert view["initial_capital_implied"] == pytest.approx(20_300.0)
    assert view["cumulative_return"] == pytest.approx(20_600 / 20_300 - 1)
    # 当日参考盈亏 = 1000*(11-10.5) + 500*(19-20.5)
    assert view["day_pnl"] == pytest.approx(1000 * 0.5 + 500 * -1.5)
    assert view["day_pnl_coverage"] == pytest.approx(1.0)
    assert view["priced"] == 2 and view["unpriced"] == 0
    by_code = {p["stock_code"]: p for p in view["positions"]}
    assert by_code["300229.SZ"]["last_price"] == pytest.approx(11.0)
    assert by_code["300229.SZ"]["action"] == "持有"          # 建议等"一天一变"的字段从盘后产物带过来
    assert by_code["002966.SZ"]["rule"] == "R3"
    assert by_code["300229.SZ"]["weight"] == pytest.approx(11000 / 20500)
    # 行情侧字段必须带出来（实测踩过：这些字段只存在临时 rows 里，页面四行全显示"盘后"）
    assert by_code["300229.SZ"]["live_time"] == "20261012140000"
    assert str(by_code["300229.SZ"]["price_source"]).startswith("live:")
    assert by_code["300229.SZ"]["prev_close"] == pytest.approx(10.5)


def test_realtime_view_falls_back_to_disk_price():
    positions = pd.DataFrame([{"stock_code": "300229.SZ", "name": "拓尔思", "shares": 100,
                               "cost_price": 10.0, "last_price": 10.0}])
    view = lm.realtime_view(positions, {}, cash=0.0, cleared_pnl=0.0, asof="2026-10-12")
    p = view["positions"][0]
    assert p["last_price"] == pytest.approx(10.0) and p["price_source"] == "disk"
    assert view["priced"] == 0 and view["unpriced"] == 1
    assert view["day_pnl"] is None, "没有昨收就不该编一个当日盈亏"


def test_realtime_endpoint_and_page_polling():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as client:
        resp = client.get("/api/paper/live/realtime")
        body = resp.json()
        assert resp.status_code == 200 and body.get("available") is True
        for field in ("updated_at", "session", "price_source", "priced", "unpriced", "positions"):
            assert field in body, f"实时接口缺 {field}"
        assert body["positions"] and all("price_source" in p for p in body["positions"])
        assert body["session"].get("phase") in ("am", "pm", "lunch", "closed", "auction")
        # 同一个进程里 2 秒 TTL：紧接着再请求应当命中缓存（不把实时源打穿）
        assert client.get("/api/paper/live/realtime").json()["updated_at"] == body["updated_at"]
    api = (ROOT / "backend/api/paper.py").read_text(encoding="utf-8")
    assert '@router.get("/live/realtime")' in api and "ttl_cache(2.0" in api
    page = (ROOT / "frontend/src/pages/LiveAccount.vue").read_text(encoding="utf-8")
    for marker in ("getLiveRealtime", "loadRealtime", "day_pnl", "15 秒轮询中", "rtInterval"):
        assert marker in page, f"实盘页缺 {marker}"
    client_ts = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "getLiveRealtime" in client_ts and "/api/paper/live/realtime" in client_ts


def test_nav_prefers_official_close_and_is_refreshed_daily(tmp_path, builder):
    """盯市那行：**官方收盘价入库后就不能再用盘中价**（否则 nav.csv 永远留着一个盘中价）。"""
    snapshot_path = tmp_path / "snap.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "m"
    # 造一根"快照日当天"的官方日线：300229 用收盘 13.9（不是快照价 13.12）
    import pyarrow as pa
    import pyarrow.parquet as pq
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    frame = pd.DataFrame([
        {"trade_date": pd.Timestamp("2026-10-09"), "stock_code": "300229.SZ", "close": 13.0},
        {"trade_date": pd.Timestamp("2026-10-12"), "stock_code": "300229.SZ", "close": 13.9},
    ])
    pq.write_table(pa.Table.from_pandas(frame), bars_dir / "part.parquet")
    original = builder.load_bars
    builder.load_bars = lambda codes, start="2026-01-01": pd.read_parquet(bars_dir / "part.parquet")
    try:
        result = builder.build(snapshot_path=snapshot_path, out_dir=out, use_live=False,
                               selection_csv=tmp_path / "sel.csv", history_dir=tmp_path / "h", years=1)
    finally:
        builder.load_bars = original
    nav = pd.read_csv(out / "nav.csv")
    row = nav[nav["asof_date"] == "2026-10-12"].iloc[0]
    # 有官方收盘时：300229 用 13.9、苏州银行缺当日 bar ⇒ 用快照价 9.51（顺延）
    expected = 40.55 + 1100 * 13.9 + 400 * 9.51
    assert row["nav"] == pytest.approx(expected), "官方收盘价入库后不该继续用盘中价"
    assert row["price_source"] == "close"
    assert result["summary"]["nav_last"] == "2026-10-12"


def test_daily_automation_refreshes_everything():
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "run_data_step live_mirror uv run python scripts/build_live_mirror.py" in daily
    assert daily.index("run_data_step live_mirror") < daily.index("run_data_step ai_picks")
    freshness = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "实盘镜像收益对比" in freshness and "_live_mirror_perf_end" in freshness
    config = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    entry = next(s for s in config["strategies"] if s["id"] == "live_holdings")
    for rel in ("output/paper_trading/live_mirror/performance.json", "output/results_cn/cn_live_holdings.csv"):
        assert rel in entry["artifacts"], f"策略池产物清单缺 {rel}"
