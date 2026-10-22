"""R-Breaker level math, causal stop simulation and strategy-pool wiring."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _core():
    spec = importlib.util.spec_from_file_location("rbreaker_core", ROOT / "scripts/rbreaker_core.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["rbreaker_core"] = module
    spec.loader.exec_module(module)
    return module


def _bars(n: int = 21) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=n)
    frame = pd.DataFrame({
        "trade_date": dates,
        "stock_code": "000001.SZ",
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.0,
        "volume": 1_000_000.0,
        "amount": 30_000_000.0,
        "turnover": 0.01,
        "name": "平安银行",
        "industry": "银行",
        "is_st": False,
        "is_delist_risk": False,
    })
    frame.loc[frame.index[-2], "volume"] = 2_000_000.0
    return frame


def test_rbreaker_levels_use_previous_hlc_and_long_only_signal():
    core = _core()
    params = core.RBreakerParams(max_breakout_distance=0.35)
    marked = core.add_signals(_bars(), params)
    row = marked.iloc[-1]
    assert row["rb_pivot"] == 10.0
    assert row["rb_r2"] == 12.0
    assert row["rb_r3"] == 13.0
    assert row["rb_s2"] == 8.0
    assert row["rb_s3"] == 7.0
    assert bool(row["stop_candidate"]) is True
    assert row["stop_score"] > 1.0


def test_simulator_respects_t_plus_one(monkeypatch):
    core = _core()
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    bars = pd.DataFrame([
        # signal day: buy-stop at 10 is touched
        {"trade_date": dates[0], "stock_code": "000001.SZ", "name": "平安银行",
         "open": 9.8, "high": 10.5, "low": 9.7, "close": 10.1, "prev_close": 9.9,
         "rb_r2": 9.5, "rb_r3": 10.0, "stop_candidate": True, "stop_score": 2.0,
         "is_st": False, "is_delist_risk": False, "industry": "银行"},
        # first sellable session: intraday stop at R2
        {"trade_date": dates[1], "stock_code": "000001.SZ", "name": "平安银行",
         "open": 10.1, "high": 10.2, "low": 9.4, "close": 9.8, "prev_close": 10.1,
         "rb_r2": 9.9, "rb_r3": 11.0, "stop_candidate": False, "stop_score": 0.0,
         "is_st": False, "is_delist_risk": False, "industry": "银行"},
        {"trade_date": dates[2], "stock_code": "000001.SZ", "name": "平安银行",
         "open": 9.8, "high": 9.9, "low": 9.7, "close": 9.8, "prev_close": 9.8,
         "rb_r2": 9.0, "rb_r3": 11.0, "stop_candidate": False, "stop_score": 0.0,
         "is_st": False, "is_delist_risk": False, "industry": "银行"},
    ])
    params = core.RBreakerParams(slots=1, initial_capital=45000)
    nav, trades, _ = core.simulate(bars, params, "2026-01-05", "2026-01-07")
    assert not nav.empty and not trades.empty
    buys = trades[trades["side"] == "buy"]
    sells = trades[trades["side"] == "sell"]
    assert len(buys) == 1 and buys.iloc[0]["date"] == dates[0]
    assert len(sells) == 1 and sells.iloc[0]["date"] == dates[1]
    assert sells.iloc[0]["reason"] == "r2_reversal"


def test_strategy_pool_registration_and_artifacts():
    from backend.services import strategy_registry

    spec = strategy_registry.spec("rbreaker_arm")
    assert spec["selection_source"] == "cn_rbreaker"
    assert spec["ledger_sources"] == ["rbreaker_arm"]
    assert spec["curve"]["replay"]["nav_csv"].endswith("output/research/rbreaker/curve/nav.csv")
    assert not strategy_registry.validate(), strategy_registry.validate()
    assert (ROOT / "output/research/rbreaker/REPORT.md").exists()
    assert (ROOT / "output/results_cn/rbreaker_latest_selection.csv").exists()
