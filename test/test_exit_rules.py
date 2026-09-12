#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""持仓卖出规则测试。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.portfolio.exits import ExitRules, evaluate_exit_plan, render_exit_plan_markdown


def _holdings(*rows):
    return pd.DataFrame(list(rows), columns=["stock_code", "shares", "cost_price"])


def _state(**kwargs):
    base = {
        "stock_code": "600460.SH", "name": "士兰微", "close": 30.07,
        "ma20": 32.75, "ma60": 36.65, "high60": 56.96,
        "return_20d": -0.082, "drawdown_60d": -0.472, "donchian_pos": 0.04,
        "median_amount_20": 1.7e9, "model_percentile": 82.0, "sessions_since_last_bar": 0,
    }
    base.update(kwargs)
    return pd.DataFrame([base])


def _plan(holdings, state, **rules):
    # A single-name book is 100% weighted; the concentration cap is only under
    # test where a test asks for it, so default it out of the way here.
    rules.setdefault("max_weight", 1.0)
    return evaluate_exit_plan(holdings, state, rules=ExitRules(**rules))


def test_a_deep_drawdown_position_is_not_stopped_out() -> None:
    plan = _plan(_holdings(("600460.SH", 400, 34.80)), _state())

    row = plan.iloc[0]
    assert row["action"] == "HOLD"
    assert row["suggested_shares_to_sell"] == 0
    assert "深度回撤" in row["reasons"]


def test_st_name_is_exited() -> None:
    plan = _plan(_holdings(("600735.SH", 400, 6.5)), _state(stock_code="600735.SH", name="ST新华锦", close=6.5))

    assert plan.iloc[0]["action"] == "EXIT"
    assert "ST" in plan.iloc[0]["reasons"]


def test_illiquid_position_is_exited() -> None:
    plan = _plan(_holdings(("600460.SH", 400, 34.80)), _state(median_amount_20=1.7e7))

    assert plan.iloc[0]["action"] == "EXIT"
    assert "流动性枯竭" in plan.iloc[0]["reasons"]


def test_model_rank_decay_exits() -> None:
    plan = _plan(_holdings(("600460.SH", 400, 34.80)), _state(model_percentile=18.0))

    assert plan.iloc[0]["action"] == "EXIT"
    assert "模型排名跌出下限" in plan.iloc[0]["reasons"]


def test_suspended_position_is_exited() -> None:
    plan = _plan(_holdings(("600460.SH", 400, 34.80)), _state(sessions_since_last_bar=12))

    assert plan.iloc[0]["action"] == "EXIT"
    assert "停牌" in plan.iloc[0]["reasons"]


def test_concentration_above_the_cap_is_trimmed_to_the_cap() -> None:
    holdings = _holdings(("300503.SZ", 400, 66.59), ("600460.SH", 400, 34.80))
    state = pd.concat([
        _state(stock_code="300503.SZ", name="昊志机电", close=62.99, return_20d=-0.137, donchian_pos=-0.04, model_percentile=97.0),
        _state(stock_code="600460.SH", name="士兰微", close=30.07, model_percentile=82.0),
    ], ignore_index=True)
    plan = _plan(holdings, state, max_weight=0.35)
    row = plan.set_index("stock_code").loc["300503.SZ"]

    assert row["action"] == "REDUCE"
    # invested-only cap: 35% of 37,224 = 13,028 -> 206.8 shares -> one lot-compliant 200
    assert row["suggested_shares_to_sell"] == 200
    assert row["suggested_shares_to_sell"] % 100 == 0
    assert float(row["weight"]) > 0.35
    assert plan.set_index("stock_code").loc["600460.SH", "action"] == "HOLD"


def test_position_near_the_channel_top_is_trimmed_not_exited() -> None:
    plan = _plan(_holdings(("600460.SH", 400, 20.0)), _state(close=54.0, donchian_pos=0.92, return_20d=0.22, model_percentile=88.0))

    row = plan.iloc[0]
    assert row["action"] == "REDUCE"
    assert 0 < row["suggested_shares_to_sell"] < 400
    assert "通道上沿" in row["reasons"]


def test_stop_loss_is_available_but_off_by_default() -> None:
    state = _state(close=28.0, return_20d=-0.15, drawdown_60d=-0.30)
    holdings = _holdings(("600460.SH", 400, 34.80))

    assert _plan(holdings, state).iloc[0]["action"] == "HOLD"
    armed = _plan(holdings, state, stop_loss_pct=0.08)
    assert armed.iloc[0]["action"] == "EXIT"
    assert "触发止损" in armed.iloc[0]["reasons"]


def test_markdown_report_renders_actions_and_rules() -> None:
    plan = _plan(_holdings(("600460.SH", 400, 34.80)), _state())
    markdown = render_exit_plan_markdown(plan, as_of="2026-09-11")

    assert "| 600460.SH |" in markdown
    assert "HOLD" in markdown
    assert "收益型止损" in markdown


def test_weights_use_total_equity_when_cash_is_provided() -> None:
    holdings = _holdings(("300503.SZ", 400, 66.633), ("600460.SH", 400, 34.813))
    state = pd.concat([
        _state(stock_code="300503.SZ", name="昊志机电", close=62.99, model_percentile=97.0),
        _state(stock_code="600460.SH", name="士兰微", close=30.07, model_percentile=82.0),
    ], ignore_index=True)

    plan = evaluate_exit_plan(holdings, state, rules=ExitRules(max_weight=0.35), cash=5913.82)
    row = plan.set_index("stock_code").loc["300503.SZ"]

    # 25,196 / (37,224 + 5,913.82) = 58.4% of total equity
    assert row["weight"] == pytest.approx(0.5841, abs=0.001)
    assert row["invested_weight"] == pytest.approx(0.6769, abs=0.001)
    assert row["action"] == "REDUCE"


def test_trim_quantity_is_a_whole_lot() -> None:
    holdings = _holdings(("300503.SZ", 400, 66.633), ("600460.SH", 400, 34.813))
    state = pd.concat([
        _state(stock_code="300503.SZ", name="昊志机电", close=62.99, model_percentile=97.0),
        _state(stock_code="600460.SH", name="士兰微", close=30.07, model_percentile=82.0),
    ], ignore_index=True)

    plan = evaluate_exit_plan(holdings, state, rules=ExitRules(max_weight=0.35), cash=5913.82)
    row = plan.set_index("stock_code").loc["300503.SZ"]

    # 35% of 43,137.82 = 15,098 -> 239.7 shares -> one lot-compliant holding of 200
    assert row["suggested_shares_to_sell"] == 200
    assert row["suggested_shares_to_sell"] % 100 == 0
    assert row["suggested_lots_to_sell"] == 2


def test_lot_order_plan_rounds_targets_down_to_lots() -> None:
    from factor_engine.portfolio.exits import plan_lot_orders, render_lot_order_markdown

    plan = plan_lot_orders(
        {"CHEAP.SH": 0.20, "EXPENSIVE.SH": 0.20},
        {"CHEAP.SH": 12.80, "EXPENSIVE.SH": 390.30},
        43_137.82,
        lot_size=100,
    )
    by_code = plan.set_index("stock_code")

    assert by_code.loc["CHEAP.SH", "shares"] == 600      # 8,627 / 12.80 -> 674 -> 6 lots
    assert by_code.loc["EXPENSIVE.SH", "shares"] == 0    # 1 lot costs 39,030 > target
    assert plan.attrs["cash_left"] > 0
    assert "按整手" in render_lot_order_markdown(plan)
