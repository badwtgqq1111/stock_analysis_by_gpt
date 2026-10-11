"""行业热度指数：分行业排名 + 行业内涨跌排名（每日/每周/每月）。

守住三件事：
  1. 热度指数的加权公式与 `HEAT_WEIGHTS` 一致（改权重忘了改文档 / 忘了同步会增加这类漂移）；
  2. 分行业榜按热度降序、热度落在 0~100、成分数过滤生效；
  3. 行业内 drill-down 返回的票确实属于该行业，且按区间涨跌幅排序。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import industry_heat_service as ihs  # noqa: E402
from backend.services import market_board_service as mbs  # noqa: E402


def test_heat_z_matches_documented_weights():
    built = ihs.build_frame("weekly")
    if not built.get("available"):
        pytest.skip("本地日线/资金流不可用")
    table = built["indices"]
    from scipy import stats  # noqa: F401  # 仅确认依赖存在（不做额外检验）

    weight_sum = sum(ihs.HEAT_WEIGHTS.values())
    assert abs(weight_sum - 1.0) < 1e-9, "权重必须归一到 1"
    composites = []
    for key in ihs.HEAT_WEIGHTS:
        column = table[key].astype(float)
        std = column.std(ddof=0)
        composites.append(((column - column.mean()) / std if std > 0 else 0.0) * ihs.HEAT_WEIGHTS[key])
    expected = sum(composites)
    assert np.allclose(expected.to_numpy(), table["heat_z"].to_numpy(), atol=1e-9)


@pytest.mark.parametrize("period,span", [("daily", 1), ("weekly", 5), ("monthly", 20)])
def test_periods_have_expected_window(period, span):
    result = ihs.industry_heat(period=period, limit=200)
    if not result.get("available"):
        pytest.skip("本地日线不可用")
    market = result["market"]
    assert market["span"] == span
    assert market["date"] >= market["base_date"]
    assert result["n_total"] > 50
    heats = [r["heat"] for r in result["rows"]]
    assert heats == sorted(heats, reverse=True)
    assert all(0 <= h <= 100 for h in heats)
    assert all(0.0 <= r["up_ratio"] <= 1.0 for r in result["rows"])
    assert all(r["rank"] == i for i, r in enumerate(result["rows"], 1))


def test_min_n_filter_and_flow_intensity_scale():
    result = ihs.industry_heat(period="weekly", limit=200, min_n=20)
    if not result.get("available"):
        pytest.skip("本地日线不可用")
    assert result["rows"], "min_n=20 之后不该是空榜"
    assert all(r["n"] >= 20 for r in result["rows"])
    # 资金流强度不能全是 0（万元/元 单位没换算时会被四舍五入成 0 —— 回归过）
    assert any(abs(r["flow_intensity"]) > 1e-6 for r in result["rows"])


def test_industry_stocks_belong_to_industry_and_sorted():
    heat = ihs.industry_heat(period="weekly", limit=5)
    if not heat.get("available"):
        pytest.skip("本地日线不可用")
    industry = heat["rows"][0]["industry"]
    result = ihs.industry_stocks(industry=industry, period="weekly", direction="up", limit=30)
    assert result["available"] and result["rows"]
    basics = mbs.basic_frame()
    for row in result["rows"]:
        assert str(basics.loc[row["stock_code"], "industry"]) == industry
    rets = [r["period_ret"] for r in result["rows"]]
    assert rets == sorted(rets, reverse=True)
    down = ihs.industry_stocks(industry=industry, period="weekly", direction="down", limit=30)
    down_rets = [r["period_ret"] for r in down["rows"]]
    assert down_rets == sorted(down_rets)


def test_unknown_industry_returns_empty():
    result = ihs.industry_stocks(industry="不存在的行业", period="daily")
    assert result["available"] is False and not result["rows"]


# ─── API 冒烟 ───
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_api_industry_smoke(client):
    overview = client.get("/api/industry/overview").json()
    assert overview["n_industries"] > 50
    assert len(overview["weights"]) == 5

    for period, span in (("daily", 1), ("weekly", 5), ("monthly", 20)):
        body = client.get(f"/api/industry/heat?period={period}&limit=10").json()
        assert body["available"] and body["market"]["span"] == span
        assert len(body["rows"]) == 10

    industry = client.get("/api/industry/heat?limit=1").json()["rows"][0]["industry"]
    stocks = client.get(f"/api/industry/{industry}/stocks?period=weekly&limit=5").json()
    assert stocks["industry"] == industry and len(stocks["rows"]) <= 5
    assert client.get("/api/industry/查无此业/stocks").status_code == 404


# ─── 热力图（2026-10-10 用户要求：a-share-heatmap 风格的股票热力图）──────────
def test_heatmap_two_levels_and_json_safe():
    """两级结构（行业 → 成分股）+ JSON 里不能出现 NaN（浏览器 JSON.parse 会直接失败）。"""
    import json as _json

    result = ihs.heatmap("daily")
    if not result.get("available"):
        pytest.skip("本地日线/资金流不可用")
    assert result["industries"], "至少要有一个行业"
    totals = result["totals"]
    assert totals["n_stocks"] > 3000 and totals["n_industries"] > 50
    assert totals["up"] + totals["down"] + totals["flat"] == totals["n_stocks"]
    # 面积用的市值口径必须有值（否则 treemap 会退化成等分，看不出大小）
    assert totals["mktcap_yi"] and totals["mktcap_yi"] > 1e5
    for industry in result["industries"][:5]:
        assert industry["stocks"], f"{industry['industry']} 没有成分股"
    # 行业按市值降序：treemap 从大到小铺，前端才有稳定的视觉顺序
    caps = [x["mktcap_yi"] or 0 for x in result["industries"]]
    assert caps == sorted(caps, reverse=True)
    # NaN/inf 不允许出现在 JSON 里
    text = _json.dumps(result, allow_nan=False)
    assert "NaN" not in text


def test_heatmap_market_filter_and_api(client):
    body = client.get("/api/industry/heatmap?period=weekly").json()
    if not body.get("available"):
        pytest.skip("本地日线不可用")
    assert body["period_label"] == "每周（近 5 个交易日）"
    assert body["span"] == 5
    filtered = client.get("/api/industry/heatmap?period=daily&market=创业板").json()
    assert filtered["available"]
    assert filtered["totals"]["n_stocks"] < body["totals"]["n_stocks"]
    assert body["mktcap_date"], "市值快照日期必须回传（页面上要写明是哪天的市值）"


# ─── 行业龙头 / 次龙头标签（2026-10-10 用户提问："应该选行业龙头或者次龙头么？"）───
def test_industry_stocks_marks_cap_leaders():
    """龙一/龙二 = 行业内**流通市值**前二，且标签必须真的落在市值最大的两只上。

    踩坑回归：第一版把 `caps.sort_values(...).index`（行标签 0/1/2…）当成股票代码，
    cap_rank 的键全错，页面上一个"龙一"都标不出来。
    """
    result = ihs.industry_stocks(industry="银行", period="daily", direction="up", limit=200)
    if not result.get("available"):
        pytest.skip("本地日线不可用")
    rows = result["rows"]
    tagged = {row["leader_rank"]: row for row in rows if row["leader_rank"] in (1, 2)}
    assert set(tagged) == {1, 2}, "银行行业必须标出龙一与龙二"
    caps = {r["mktcap_yi"] for r in rows if r["mktcap_yi"] is not None}
    assert tagged[1]["mktcap_yi"] == max(caps), "龙一必须是行业内流通市值最大"
    assert tagged[2]["mktcap_yi"] == sorted(caps)[-2], "龙二必须是行业内流通市值第二"
    assert all(r["mktcap_yi"] is not None for r in rows), "每行都要能给出流通市值"
    # 标签不改变排序：涨跌榜仍按区间涨跌幅降序
    rets = [r["period_ret"] for r in rows]
    assert rets == sorted(rets, reverse=True)


def test_industry_stocks_leader_label_survives_json(client):
    body = client.get("/api/industry/银行/stocks?period=daily&limit=200").json()
    if not body.get("available"):
        pytest.skip("本地日线不可用")
    ranks = [r.get("leader_rank") for r in body["rows"]]
    assert any(r == 1 for r in ranks) and any(r == 2 for r in ranks)


def test_daily_bars_have_no_duplicate_symbol_days():
    """日线窗口里不能有重复的 (trade_date, stock_code)。

    2026-10-10 实测：clean/ohlcv 分区里同一代码同一天有重复行（最近 120 天 3,870 行、值完全相同），
    下游 `set_index("stock_code")` 后再 `.map()/.reindex()` 在 pandas 3 会直接抛
    `InvalidIndexError: Reindexing only valid with uniquely valued Index objects` ——
    行业热度与概念手臂的一批测试同时变红，根因是数据重复而不是业务代码。
    """
    bars = mbs.enriched_daily(120)
    if bars.empty:
        pytest.skip("本地日线不可用")
    duplicated = int(bars.duplicated(subset=["trade_date", "stock_code"]).sum())
    assert duplicated == 0, f"日线窗口里出现了 {duplicated} 行重复的 (trade_date, stock_code)"
