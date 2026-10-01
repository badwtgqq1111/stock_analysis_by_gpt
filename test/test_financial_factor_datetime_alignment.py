"""Regression: pandas 3 mixed datetime resolutions broke as-of valuation alignment.

Before the fix, ``_asof_valuation_alignment`` passed a ``datetime64[us]`` sample
index and ``datetime64[s]`` valuation dates into ``pd.merge_asof``, which raised
``pandas.errors.MergeError`` for every stock.  The factor stage counted those as
per-stock errors and wrote zero feature rows for the newest trading dates, so the
clean panel and every model score for the latest date were computed from empty
features.
"""

import numpy as np
import pandas as pd

from factor_engine.expressions.financial_factors import (
    _asof_valuation_alignment,
    _coerce_datetime_resolution,
)


class _Context:
    def __init__(self, history):
        self.extra = {"valuation_history": history}


def test_mixed_second_and_microsecond_resolution_aligns():
    index = pd.DatetimeIndex(pd.to_datetime(["2026-09-22", "2026-09-23"])).as_unit("us")
    history = [
        {"trade_date": pd.Timestamp("2026-09-19").as_unit("s"), "market_cap": 1.0e9},
        {"trade_date": pd.Timestamp("2026-09-22").as_unit("s"), "market_cap": 2.0e9},
    ]
    assert np.datetime_data(index.dtype)[0] == "us"
    assert np.datetime_data(pd.Series([h["trade_date"] for h in history]).dtype)[0] == "s"

    values, age = _asof_valuation_alignment(index, _Context(history), "market_cap")

    assert values.tolist() == [2.0e9, 2.0e9]
    assert age.tolist() == [0.0, 1.0]
    assert values.index.equals(index)


def test_helper_unifies_resolution_and_drops_timezone():
    micro = pd.DatetimeIndex(pd.to_datetime(["2026-09-23"])).as_unit("us")
    seconds = pd.Series(pd.to_datetime(["2026-09-22"]).as_unit("s"))
    aware = pd.Series(pd.to_datetime(["2026-09-23T00:00:00+08:00"]))

    assert _coerce_datetime_resolution(micro).dtype == "datetime64[ns]"
    assert _coerce_datetime_resolution(seconds).dtype == "datetime64[ns]"
    assert _coerce_datetime_resolution(aware).dtype == "datetime64[ns]"
    assert _coerce_datetime_resolution(aware).iloc[0] == pd.Timestamp("2026-09-22 16:00:00")


def test_missing_history_still_returns_nans():
    index = pd.DatetimeIndex(pd.to_datetime(["2026-09-23"])).as_unit("us")
    values, age = _asof_valuation_alignment(index, _Context([]), "market_cap")
    assert values.isna().all() and age.isna().all()
