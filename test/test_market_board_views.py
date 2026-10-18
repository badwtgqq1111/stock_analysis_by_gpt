"""行情榜单 / 连板梯队（2026-10-09 新增页面的后端）。

覆盖三类容易悄悄错的东西：
  1. **涨停幅度的板块判定**：主板 10% / 创业板·科创板 20% / 北交所 30%（含 920xxx 新代码段）。
  2. **按价格浮动的容差**：交易所涨停价四舍五入到分，3 元的票涨停涨幅只有 9.90%，
     67 元的票 19.975% 却差一分钱没封板 —— 固定容差两头都会错。
  3. **窗口长度**：连板数要在窗口内累计，窗口只覆盖到目标日会把长连板截断
     （实测踩过：8 连板被算成 2 板），所以这里断言"梯队统计"与"明细"两个入口
     在同一交易日必须给出同一组家数。
另外附一组 API 冒烟（全部走本地数据，`live=0`，不碰网络）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import market_board_service as mbs  # noqa: E402


# ─── 1. 涨停幅度 ─────────────────────────────────────────────────────────────
def test_limit_ratio_by_board_and_bj_new_code_segment():
    assert mbs.limit_ratio("600519.SH") == 0.10
    assert mbs.limit_ratio("000001.SZ") == 0.10
    assert mbs.limit_ratio("300750.SZ") == 0.20
    assert mbs.limit_ratio("688981.SH") == 0.20
    assert mbs.limit_ratio("430047.BJ") == 0.30
    assert mbs.limit_ratio("920627.BJ", market="北交所") == 0.30       # 920xxx 是北交所新代码段
    # ST 用 PIT 标记展示，但不改变板块上限（数据标定结论见 service 头部注释）
    assert mbs.limit_ratio("002547.SZ", name="*ST春兴") == 0.10


def _frame(rows):
    """(code, day, close) → 带基础列的最小日线。"""
    frame = pd.DataFrame(rows, columns=["stock_code", "trade_date", "close"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["open"] = frame["high"] = frame["low"] = frame["close"]
    frame["volume"] = 1_000_000.0
    frame["amount"] = frame["close"] * frame["volume"]
    return frame


# ─── 2. 涨停判定与连板累计 ───────────────────────────────────────────────────
def test_limit_up_price_rounding_tolerance():
    """0.5% 固定容差会漏掉便宜的涨停、也会误判贵的未封板。"""
    frame = _frame([
        ("600408.SH", "2026-10-07", 3.13),
        ("600408.SH", "2026-10-08", 3.44),        # 3.13×1.1=3.443 → 涨停价 3.44，涨幅只有 9.9042%
        ("002547.SZ", "2026-10-07", 2.58),
        ("002547.SZ", "2026-10-08", 2.73),        # +5.81%，10% 板根本没封
        ("301513.SZ", "2026-10-07", 56.52),
        ("301513.SZ", "2026-10-08", 67.81),       # +19.975%，比 20% 涨停价低一分钱
    ])
    flagged = mbs.with_limit_flags(frame).set_index(["stock_code", "trade_date"])
    day = pd.Timestamp("2026-10-08")
    assert bool(flagged.loc[("600408.SH", day), "is_limit_up"]) is True
    assert bool(flagged.loc[("002547.SZ", day), "is_limit_up"]) is False
    assert bool(flagged.loc[("301513.SZ", day), "is_limit_up"]) is False


def test_boards_count_consecutive_limit_ups():
    # 第一天是基准日（窗口内没有更早的收盘价 → 算不出涨跌幅，不计入连板）
    rows = [("600000.SH", "2026-09-19", 10.0)]
    price = 10.0
    for i in range(6):
        price = round(price * 1.1, 2)
        rows.append(("600000.SH", f"2026-09-{20 + i:02d}", price))
    rows.append(("600000.SH", "2026-09-28", round(price * 1.02, 2)))      # 断板
    flagged = mbs.with_limit_flags(_frame(rows))
    assert flagged["boards"].tolist() == [0, 1, 2, 3, 4, 5, 6, 0]
    assert mbs.board_bucket(1) == "b1"
    assert mbs.board_bucket(4) == "b4p"
    assert mbs.board_bucket(9) == "b4p"


# ─── 3. 窗口长度：梯队 vs 明细必须一致（回归：8 连板被截断成 2 板）──────────────
@pytest.fixture(scope="module")
def latest_day():
    day = mbs.latest_trade_date()
    if day is None:
        pytest.skip("本地日线为空")
    return day


def test_ladder_and_detail_agree_on_latest_day(latest_day):
    ladder = mbs.ladder(5)
    target = latest_day.strftime("%Y-%m-%d")
    day_stats = next((s for s in ladder["series"] if s["date"] == target), None)
    assert day_stats is not None, f"梯队里没有 {target}"

    rows = mbs.limit_up_detail(latest_day, limit=2000)
    counts = {"b1": 0, "b2": 0, "b3": 0, "b4p": 0}
    for row in rows:
        counts[mbs.board_bucket(row["boards"])] += 1
    assert counts == day_stats["counts"]
    assert len(rows) == day_stats["total"]
    assert max((r["boards"] for r in rows), default=0) == day_stats["max_boards"]
    assert day_stats["max_boards"] >= 1


def test_promotion_rate_is_consistent(latest_day):
    ladder = mbs.ladder(10)
    for day in ladder["series"]:
        for key, promo in day["promotion"].items():
            assert promo["next"] <= promo["prev"], (day["date"], key)
            if promo["prev"] == 0:
                assert promo["rate"] is None
            else:
                assert promo["rate"] == pytest.approx(promo["next"] / promo["prev"], abs=1e-4)


# ─── 4. 资金流：口径隔离、排序、单位 ─────────────────────────────────────────
def test_moneyflow_rank_isolates_source_and_sorts(latest_day):
    day = mbs.moneyflow_dates(5)
    if not day:
        pytest.skip("资金流面板为空")
    target = day[-1]
    rows, stats = mbs.moneyflow_rank(target, period="daily", limit=30, direction="in",
                                      source="moneyflow")
    assert rows, "资金流榜为空"
    assert stats["source"] == "moneyflow"
    codes = [r["stock_code"] for r in rows]
    assert len(codes) == len(set(codes)), "同一只票重复出现（source 没过滤干净？）"
    values = [r["net_period"] for r in rows]
    assert values == sorted(values, reverse=True)

    out_rows, _ = mbs.moneyflow_rank(target, period="daily", limit=10, direction="out",
                                     source="moneyflow_dc")
    assert [r["net_period"] for r in out_rows] == sorted([r["net_period"] for r in out_rows])
    assert mbs.yi(10_000) == 1.0                     # 万元 → 亿元


def test_moneyflow_weekly_column_is_five_day_cumulative(latest_day):
    days = mbs.moneyflow_dates(3)
    if not days:
        pytest.skip("资金流面板为空")
    target = days[-1]
    daily, _ = mbs.moneyflow_rank(target, period="daily", limit=5, direction="in")
    weekly, stats = mbs.moneyflow_rank(target, period="weekly", limit=5, direction="in")
    assert stats["column"] == "net_5d"
    assert weekly and daily
    assert weekly[0]["net_period"] == weekly[0]["net_5d"]


# ─── 5. 涨跌榜：周口径 = 5 个交易日的累计 ────────────────────────────────────
def test_change_rank_periods(latest_day):
    daily, stats_d = mbs.change_rank(period="daily", limit=5, direction="up")
    weekly, stats_w = mbs.change_rank(period="weekly", limit=5, direction="up")
    assert stats_d["span"] == 1 and stats_w["span"] == 5
    assert stats_d["date"] == latest_day.strftime("%Y-%m-%d")
    assert stats_d["n_up"] + stats_d["n_down"] + stats_d["n_flat"] == stats_d["n_total"]
    assert [r["period_ret"] for r in daily] == sorted([r["period_ret"] for r in daily], reverse=True)

    # 手工核对榜首的周涨幅：直接读日线里的两个收盘价
    daily_bars = mbs.load_daily(10)
    days = sorted(daily_bars["trade_date"].unique())
    top = weekly[0]
    bars = daily_bars[daily_bars["stock_code"] == top["stock_code"]].sort_values("trade_date")
    expected = float(bars["close"].iloc[-1]) / float(bars["close"].iloc[-1 - 5]) - 1
    assert top["period_ret"] == pytest.approx(expected, abs=1e-4)


def test_change_rank_filters_apply_before_limit(latest_day):
    rows, _ = mbs.change_rank(period="daily", limit=50, direction="up", board="主板")
    assert rows and all(r["market"] == "主板" for r in rows)
    rows_st, _ = mbs.change_rank(period="daily", limit=50, direction="up", exclude_st=True)
    assert all(not r["is_st"] for r in rows_st)


# ─── 6. 分时：本地归档只返回一天 ─────────────────────────────────────────────
def test_intraday_store_returns_single_day():
    frame = mbs.load_intraday_store("000001.SZ")
    if frame.empty:
        pytest.skip("本地 5 分钟分时缺失")
    assert frame["date"].nunique() == 1
    assert len(frame) <= 60                                    # 一个交易日最多 48 根 5 分钟线
    clock = pd.to_datetime(frame["clock"])
    assert clock.dt.strftime("%H:%M").between("09:30", "15:00").all()


# ─── 7. API 冒烟（全部本地，不碰网络）───────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_api_rankings_smoke(client):
    assert client.get("/api/rankings/overview").status_code == 200
    change = client.get("/api/rankings/change?period=weekly&direction=down&limit=5").json()
    assert change["rows"] and change["rows"][0]["rank"] == 1
    flow = client.get("/api/rankings/moneyflow?period=daily&limit=5&live=0").json()
    assert flow["rows"] and flow["rows"][0]["unit"] == "亿元"


def test_api_limitup_smoke_local_only(client):
    ladder = client.get("/api/limitup/ladder?lookback=10").json()
    assert len(ladder["dates"]) == len(ladder["series"]) > 0
    detail = client.get("/api/limitup/detail?live=0&limit=5").json()
    assert detail["available"] and len(detail["rows"]) <= 5
    assert sum(detail["counts"].values()) <= detail["total"]


def test_limitup_rolling_window_stats(client):
    """3天2板 / 5天3板 / 10天5板：窗口内涨停次数，不要求连续。"""
    detail = client.get("/api/limitup/detail?live=0&limit=80").json()
    assert set(detail["window_counts"]) == {"d3_2b", "d5_3b", "d10_5b"}
    for row in detail["rows"]:
        assert 0 <= row["lu_3d"] <= 3
        assert 0 <= row["lu_5d"] <= 5
        assert 0 <= row["lu_10d"] <= 10
        assert row["is_3d2b"] == (row["lu_3d"] >= 2)
        assert row["is_5d3b"] == (row["lu_5d"] >= 3)
        assert row["is_10d5b"] == (row["lu_10d"] >= 5)
    ladder = client.get("/api/limitup/ladder?lookback=5").json()
    for day in ladder["series"]:
        assert set(day["window_counts"]) == {"d3_2b", "d5_3b", "d10_5b"}


def test_api_intraday_store_only(client):
    response = client.get("/api/intraday/000001.SZ?mode=store&period=5")
    if response.status_code == 404:
        pytest.skip("本地分时未同步")
    body = response.json()
    assert body["source"] == "store"
    assert len(body["points"]) > 10
    assert body["points"][0]["time"] <= body["points"][-1]["time"]
    assert body["latest"]["price"] == body["points"][-1]["price"]


def test_intraday_store_is_aligned_to_daily_bars(client):
    """本地 5 分钟归档与日线面板的复权基准不同，必须按日对齐 —— 否则点开 K 线看分时，
    分时价格会整段偏离那根蜡烛（实测约 2%）。

    判据：同一交易日的分时 开/高/低/收 与日线 OHLC 落在 0.5% 内，涨跌幅与日线一致。
    """
    from backend.services.data_service import load_cn_stock_ohlcv

    history = load_cn_stock_ohlcv("000001.SZ", days=400)
    if history is None or history.empty:
        pytest.skip("日线不可用")

    coverage = client.get("/api/intraday/coverage/000001.SZ").json()
    if not coverage["store"]["available"]:
        pytest.skip("本地分时未同步")
    dates = coverage["store_dates"][-3:]

    for day in dates:
        body = client.get(f"/api/intraday/000001.SZ?date={day}").json()
        assert body["source"] == "store"
        points = body["points"]
        bar = history.loc[pd.Timestamp(day)]
        assert points[0]["open"] == pytest.approx(bar["Open"], rel=0.005)
        assert max(p["high"] for p in points) == pytest.approx(bar["High"], rel=0.005)
        assert min(p["low"] for p in points) == pytest.approx(bar["Low"], rel=0.005)
        assert points[-1]["price"] == pytest.approx(bar["Close"], rel=0.005)

        position = list(history.index).index(pd.Timestamp(day))
        prev_close = float(history["Close"].iloc[position - 1])
        assert body["prev_close"] == pytest.approx(prev_close, rel=0.001)
        assert body["latest"]["change_pct"] == pytest.approx(bar["Close"] / prev_close - 1, abs=2e-3)

        # 均价线也必须落在当日区间内：baostock 的 amount/volume 是**不复权**金额与股数，
        # 不折算的话均价会高出当日最高价 5%（2025-10-27 实测 11.52 vs 最高 11.24）
        low = min(p["low"] for p in points)
        high = max(p["high"] for p in points)
        for point in points:
            if point["avg"] is not None:
                assert low - 1e-6 <= point["avg"] <= high + 1e-6


def test_intraday_coverage_reports_ranges(client):
    body = client.get("/api/intraday/coverage/000001.SZ").json()
    assert body["code"] == "000001.SZ"
    assert body["today"]
    store = body["store"]
    if store["available"]:
        assert store["n_dates"] == len(body["store_dates"]) > 0
        assert store["first_date"] <= store["last_date"]
        assert body["store_dates"] == sorted(body["store_dates"])
