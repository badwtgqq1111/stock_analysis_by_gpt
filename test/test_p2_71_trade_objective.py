import numpy as np
import pandas as pd
import pytest

from backend.services.factor_trade_objective import trade_labels


def test_trade_label_matches_stop_exit_and_costs():
    dates = pd.bdate_range("2024-01-01", periods=8)
    bars = pd.DataFrame({"stock_code": "A", "trade_date": dates,
                         "open": [10, 10, 8, 9, 10, 11, 12, 13],
                         "close": [10, 10, 8, 9, 10, 11, 12, 13]})
    frame = pd.DataFrame({"stock_code": ["A"], "trade_date": [dates[0]], "rl": [10.0]})
    out = trade_labels(frame, bars, dates, start=dates[0], end=dates[0], boundary=dates[-1], horizon=3, stop_pct=.08, cost_one_way=.00155)
    assert out.iloc[0].exit_reason == "stop"
    expected = (9 / 10) * ((1-.00155)/(1+.00155)) - 1
    assert out.iloc[0].label == pytest.approx(expected)
    assert out.iloc[0].label_start == dates[1]
    assert out.iloc[0].label_end == dates[3]


def test_trade_label_purges_exit_crossing_boundary_and_does_not_fill_missing():
    dates = pd.bdate_range("2024-01-01", periods=8)
    bars = pd.DataFrame({"stock_code": "A", "trade_date": dates,
                         "open": [10, 10, 10, 10, 10, 10, 10, 10],
                         "close": [10, 10, 10, 10, 10, 10, 10, 10]})
    bars.loc[3, "open"] = np.nan
    frame = pd.DataFrame({"stock_code": ["A"], "trade_date": [dates[0]], "rl": [10.0]})
    out = trade_labels(frame, bars, dates, start=dates[0], end=dates[0], boundary=dates[4], horizon=3)
    assert out.empty
