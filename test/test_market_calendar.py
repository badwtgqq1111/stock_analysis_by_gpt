#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""统一市场日历的本地单元测试。"""

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import get_market_calendar


def _build_intraday_frame(rows):
    frame = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close", "Volume"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame.set_index("date", inplace=True)
    return frame


def test_hk_trading_day_navigation():
    calendar = get_market_calendar("HK")

    saturday = pd.Timestamp("2026-04-25")
    assert calendar.is_trading_day("2026-04-24") is True
    assert calendar.is_trading_day(saturday) is False
    assert calendar.get_last_trading_day(saturday) == pd.Timestamp("2026-04-24")
    assert calendar.get_next_trading_day(pd.Timestamp("2026-04-24")) == pd.Timestamp("2026-04-27")


def test_hk_session_filter_and_cleanup():
    calendar = get_market_calendar("HK")
    frame = _build_intraday_frame(
        [
            ("2026-04-24 09:29:00", 100, 100, 100, 100, 10),
            ("2026-04-24 09:30:00", 101, 101, 101, 101, 11),
            ("2026-04-24 12:30:00", 102, 102, 102, 102, 12),
            ("2026-04-24 13:30:00", 103, 103, 103, 103, 0),
            ("2026-04-24 16:05:00", 104, 104, 104, 104, 13),
            ("2026-04-24 16:08:00", 105, 105, 105, 105, 14),
            ("2026-04-24 16:09:00", 106, 106, 106, 106, 15),
        ]
    )

    regular = calendar.filter_intraday_session(frame, include_closing_auction=False)
    assert list(regular.index.strftime("%H:%M:%S")) == ["09:30:00"]

    with_closing = calendar.filter_intraday_session(frame, include_closing_auction=True)
    assert list(with_closing.index.strftime("%H:%M:%S")) == ["09:30:00", "16:05:00", "16:08:00"]


def test_hk_resample_intraday_frame():
    calendar = get_market_calendar("HK")
    frame = _build_intraday_frame(
        [
            ("2026-04-24 09:30:00", 10.0, 10.0, 10.0, 10.0, 100),
            ("2026-04-24 09:31:00", 10.1, 10.2, 10.1, 10.2, 200),
            ("2026-04-24 09:34:00", 10.2, 10.3, 10.1, 10.1, 300),
            ("2026-04-24 12:30:00", 10.5, 10.5, 10.5, 10.5, 400),
            ("2026-04-24 13:00:00", 10.3, 10.4, 10.3, 10.4, 500),
            ("2026-04-24 13:01:00", 10.4, 10.5, 10.4, 10.5, 600),
        ]
    )

    result = calendar.resample_intraday_frame(frame, "5min")
    assert list(result.index.strftime("%H:%M:%S")) == ["09:30:00", "09:35:00", "13:00:00", "13:05:00"]
    assert result.loc[pd.Timestamp("2026-04-24 09:35:00"), "Volume"] == 500
    assert result.loc[pd.Timestamp("2026-04-24 13:05:00"), "Close"] == 10.5


if __name__ == "__main__":
    test_hk_trading_day_navigation()
    test_hk_session_filter_and_cleanup()
    test_hk_resample_intraday_frame()
    print("market calendar tests passed")
