"""Opt-in causal accounting prevents future open from changing t-close NAV."""

import pandas as pd

from factor_engine.portfolio.paper_account import run_paper_account


def _run(opens, closes=None, selection_weights=(1.,), selection_dates=(0,), **kwargs):
    dates = pd.bdate_range("2026-01-05", periods=len(opens))
    bars = pd.DataFrame({"trade_date": dates, "stock_code": ["600000.SH"] * len(dates),
                         "open": opens, "close": closes or opens})
    signals = pd.DataFrame({"stock_code": ["600000.SH"] * len(selection_dates),
                            "trade_date": [dates[i] for i in selection_dates], "target_weight": selection_weights})
    return run_paper_account(signals, bars, initial_capital=10000, commission_bps=0, slippage_bps=0,
                             enforce_frictions=False, **kwargs)


def test_causal_execution_does_not_book_future_fill_at_decision_close():
    result = _run([10., 11.], causal_execution=True)
    assert result["nav"].nav.tolist() == [10000., 10000.]
    assert result["positions"].asof_date.min() == pd.Timestamp("2026-01-06")
    assert result["fills"].iloc[0].decision_time == pd.Timestamp("2026-01-05")
    assert result["fills"].iloc[0].fill_time == pd.Timestamp("2026-01-06")


def test_causal_nav_prefix_invariant_to_future_open_and_no_future_positions():
    a = _run([10., 10., 11.], causal_execution=True)
    b = _run([10., 12., 15.], causal_execution=True)
    assert a["nav"].iloc[0].nav == b["nav"].iloc[0].nav == 10000
    assert a["nav"].nav.tolist() == [10000., 10000., 11000.]


def test_causal_sell_and_pending_latest_signal():
    r = _run([10., 10., 9., 8.], selection_weights=(1., 0., .5), selection_dates=(0, 1, 3), causal_execution=True)
    assert r["nav"].iloc[0].nav == 10000
    assert r["nav"].iloc[1].nav == 10000
    assert r["fills"].iloc[-1].fill_time == pd.Timestamp("2026-01-07")
    assert r["orders"].iloc[-1].status == "pending"
    assert pd.isna(r["orders"].iloc[-1].executable_from)
    assert r["positions"].asof_date.max() == pd.Timestamp("2026-01-06")


def test_legacy_default_retains_existing_behavior():
    result = _run([10., 11.])
    assert result["nav"].nav.tolist() == [9100., 10000.]


def test_causal_limits_and_deferred_sell_are_applied_on_actual_execution_day():
    dates = pd.bdate_range("2026-01-05", periods=4)
    bars = pd.DataFrame({"stock_code": ["600000.SH"] * 4, "trade_date": dates,
                         "open": [10., 10., 9., 9.5], "close": [10., 10., 9., 9.5]})
    selection = pd.DataFrame({"stock_code": ["600000.SH"] * 2, "trade_date": dates[:2], "target_weight": [1., 0.]})
    result = run_paper_account(selection, bars, initial_capital=10000, causal_execution=True)
    assert result["fills"].side.tolist() == ["buy", "sell"]
    assert result["fills"].fill_time.tolist() == [dates[1], dates[3]]
    assert result["nav"].iloc[0].nav == 10000
    assert result["orders"].iloc[-1].status == "deferred_limit_down"


def test_causal_nav_at_close_is_independent_of_next_close_when_rebalancing():
    a = _run([10., 10., 11.], selection_weights=(.5, 1.), selection_dates=(0, 1), causal_execution=True)
    b = _run([10., 10., 11.], closes=[10., 10., 99.], selection_weights=(.5, 1.), selection_dates=(0, 1), causal_execution=True)
    assert a["nav"].nav.iloc[:2].tolist() == b["nav"].nav.iloc[:2].tolist() == [10000, 10000]
    assert a["fills"].quantity.tolist() == b["fills"].quantity.tolist()
