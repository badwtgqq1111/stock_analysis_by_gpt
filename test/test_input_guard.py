"""打分前输入体检（R2 覆盖率 + R11 单位合理性）的回归测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from factor_engine.ml.input_guard import (
    evaluate_input_guard,
    feature_coverage_report,
    price_unit_anomalies,
)


def _panel():
    return pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-09-23"] * 3),
        "stock_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
        "a_clean": [1.0, 2.0, 3.0],
        "b_clean": [np.nan, np.nan, np.nan],
        "c_clean": [1.0, np.nan, 3.0],
    })


def _ohlcv(volume_last=1000.0):
    dates = pd.date_range("2026-09-01", periods=6)
    x = pd.DataFrame({"stock_code": "X", "trade_date": dates, "close": 10.0,
                      "volume": 1000.0, "amount": 10000.0})
    y = pd.DataFrame({"stock_code": "Y", "trade_date": dates, "close": 20.0,
                      "volume": [1000.0] * 5 + [volume_last], "amount": 20000.0})
    return pd.concat([x, y], ignore_index=True)


def test_coverage_flags_all_missing_model_column():
    report = feature_coverage_report(_panel(), ["a_clean", "b_clean", "c_clean"],
                                     gain=pd.Series({"a_clean": 10.0, "b_clean": 1.0, "c_clean": 5.0}))
    assert report["columns_all_missing"] == 1
    assert report["used_columns_all_missing"] == 1
    # gain 加权：a=100%, b=0%, c=66.7% -> (10*1 + 1*0 + 5*0.667)/16
    assert report["gain_weighted_coverage"] == pytest.approx((10 + 0 + 5 * 2 / 3) / 16, rel=1e-3)
    assert report["ok"] is False


def test_coverage_passes_on_complete_input():
    sparse = feature_coverage_report(_panel(), ["a_clean", "c_clean"],
                                     gain=pd.Series({"a_clean": 1.0, "c_clean": 1.0}), min_gain_coverage=0.9)
    assert sparse["ok"] is False  # gain 加权 0.833 < 0.9 -> 不过
    full = _panel()
    full["c_clean"] = [1.0, 2.0, 3.0]
    report = feature_coverage_report(full, ["a_clean", "c_clean"],
                                     gain=pd.Series({"a_clean": 1.0, "c_clean": 1.0}))
    assert report["ok"] is True
    assert report["gain_weighted_coverage"] == 1.0


def test_price_unit_anomalies_detects_100x_volume_error():
    anomalies = price_unit_anomalies(_ohlcv(volume_last=100000.0), sessions=30)
    assert set(anomalies["stock_code"]) == {"Y"}
    assert anomalies["reason"].eq("row_ratio").all()


def test_price_unit_anomalies_detects_day_over_day_jump():
    frame = _ohlcv()
    frame.loc[frame.stock_code.eq("Y") & frame.trade_date.eq(frame.trade_date.max()), "volume"] = 50000.0
    anomalies = price_unit_anomalies(frame, sessions=30)
    assert "Y" in set(anomalies["stock_code"])


def test_price_unit_anomalies_clean_frame():
    assert price_unit_anomalies(_ohlcv(), sessions=30).empty


def test_evaluate_input_guard_combines_both_checks():
    report = evaluate_input_guard(_panel(), ["a_clean", "b_clean", "c_clean"],
                                  pd.Series({"a_clean": 1.0, "b_clean": 1.0, "c_clean": 1.0}),
                                  _ohlcv(volume_last=100000.0))
    assert report["coverage_ok"] is False
    assert report["units_ok"] is False
    assert report["excluded_codes"] == ["Y"]


def test_price_unit_anomalies_respects_code_allowlist():
    """指数/基金等非股票代码（close 与 volume/amount 不同口径）必须被 codes 过滤掉。"""
    frame = _ohlcv()
    index_rows = pd.DataFrame({
        "stock_code": "000001.SH", "trade_date": pd.date_range("2026-09-01", periods=3),
        "close": 3800.0, "volume": 1.0e10, "amount": 5.0e9,
    })
    polluted = pd.concat([frame, index_rows], ignore_index=True)
    assert "000001.SH" in set(price_unit_anomalies(polluted, sessions=30)["stock_code"])
    assert "000001.SH" not in set(price_unit_anomalies(polluted, sessions=30, codes=["X", "Y"])["stock_code"])


def test_valuation_freshness_detects_stale_observations():
    """估值观测整体变旧（如退回残缺镜像）必须被单独识别，覆盖率指标看不出来。"""
    from factor_engine.ml.input_guard import valuation_freshness_report

    fresh = pd.DataFrame({"valuation_market_cap_age_td_clean": [0.0, 1.0, 2.0, 3.0]})
    stale = pd.DataFrame({"valuation_market_cap_age_td_clean": [32.0, 35.0, 40.0, 44.0]})
    assert valuation_freshness_report(fresh)["ok"] is True
    report = valuation_freshness_report(stale)
    assert report["ok"] is False
    assert report["median_age_sessions"] == pytest.approx(37.5)
    # 没有任何估值列时不做判断（避免误伤其它市场/数据集）
    assert valuation_freshness_report(pd.DataFrame({"a": [1]}))["checked"] is False


def test_evaluate_input_guard_reports_valuation_state():
    report = evaluate_input_guard(
        _panel().assign(valuation_market_cap_age_td_clean=[0.0, 1.0, 2.0]),
        ["a_clean", "c_clean"], None, _ohlcv(),
    )
    assert report["valuation_ok"] is True
    assert report["valuation"]["median_age_sessions"] == 1.0
