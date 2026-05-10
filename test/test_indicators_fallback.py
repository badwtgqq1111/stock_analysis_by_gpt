#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""无 talib 环境下的 indicators 回归测试。"""

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from indicators import calculate_technical_indicators


def _make_price_frame(rows=160):
    index = pd.date_range("2025-01-01", periods=rows, freq="D")
    close = pd.Series(range(rows), dtype=float) * 0.08 + 10.0
    return pd.DataFrame(
        {
            "Open": close - 0.05,
            "High": close + 0.20,
            "Low": close - 0.20,
            "Close": close,
            "Volume": 1000 + pd.Series(range(rows), dtype=float) * 10,
        },
        index=index,
    )


def test_calculate_indicators_without_talib_import_error():
    frame = _make_price_frame()
    result = calculate_technical_indicators(frame)

    assert result is not None
    for column in [
        "MA20",
        "EMA12",
        "MACD",
        "RSI",
        "BB_Upper",
        "Volume_MA20",
        "StochRSI_K",
        "ATR",
        "Williams_R",
        "Alpha101_Range_Long_Composite",
        "expected_3m_score",
    ]:
        assert column in result.columns


if __name__ == "__main__":
    test_calculate_indicators_without_talib_import_error()
    print("indicators fallback tests passed")
