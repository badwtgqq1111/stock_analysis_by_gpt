#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""统一市场交易日历与盘中会话工具。"""

from dataclasses import dataclass
from datetime import time

import pandas as pd


OHLCV_RENAME_TO_TITLE = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}


@dataclass(frozen=True)
class SessionWindow:
    """盘中连续交易时段。"""

    start: time
    end: time


class MarketCalendar:
    """工作日级交易日历与分钟线会话处理。"""

    def __init__(self, market, sessions=None, closing_auction_end=None):
        self.market = (market or "").upper()
        self.sessions = tuple(sessions or ())
        self.closing_auction_end = closing_auction_end

    @staticmethod
    def _coerce_timestamp(value):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_localize(None)
        return timestamp

    def is_trading_day(self, value):
        """当前版本先按工作日判断交易日。"""
        return self._coerce_timestamp(value).weekday() < 5

    def get_next_trading_day(self, value):
        """获取下一个交易日。"""
        current = self._coerce_timestamp(value).normalize() + pd.Timedelta(days=1)
        while not self.is_trading_day(current):
            current += pd.Timedelta(days=1)
        return current

    def get_last_trading_day(self, value):
        """获取不晚于指定日期的最近交易日。"""
        current = self._coerce_timestamp(value).normalize()
        while not self.is_trading_day(current):
            current -= pd.Timedelta(days=1)
        return current

    def intraday_sessions(self, include_closing_auction=False):
        """返回当前市场的有效盘中时段。"""
        active_sessions = list(self.sessions)
        if include_closing_auction and active_sessions and self.closing_auction_end is not None:
            last_session = active_sessions[-1]
            active_sessions[-1] = SessionWindow(start=last_session.start, end=self.closing_auction_end)
        return tuple(active_sessions)

    @staticmethod
    def _ensure_datetime_index(frame):
        if frame is None or frame.empty:
            return frame

        working = frame.copy()
        if isinstance(working.index, pd.DatetimeIndex):
            working.index = pd.to_datetime(working.index, errors="coerce")
        elif "date" in working.columns:
            working["date"] = pd.to_datetime(working["date"], errors="coerce")
            working.set_index("date", inplace=True)
        elif "trade_date" in working.columns:
            working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
            working.set_index("trade_date", inplace=True)
        else:
            raise ValueError("盘中数据需要 DatetimeIndex 或 date/trade_date 列")

        working = working.loc[~working.index.isna()].copy()
        if working.index.tz is not None:
            working.index = working.index.tz_localize(None)
        working.index.name = "date"
        return working

    @staticmethod
    def _drop_synthetic_flat_bars(frame):
        if frame is None or frame.empty:
            return frame

        required_columns = {"Open", "High", "Low", "Close", "Volume"}
        if not required_columns.issubset(frame.columns):
            return frame

        return frame.loc[
            ~(
                (frame["Volume"] <= 0)
                & (frame["Open"] == frame["High"])
                & (frame["High"] == frame["Low"])
                & (frame["Low"] == frame["Close"])
            )
        ].copy()

    def filter_intraday_session(self, frame, include_closing_auction=False):
        """过滤到市场有效交易时段，并清理明显伪造 bar。"""
        if frame is None or frame.empty:
            return frame

        working = self._ensure_datetime_index(frame)
        active_sessions = self.intraday_sessions(include_closing_auction=include_closing_auction)
        if not active_sessions:
            return working

        mask = working.index.map(
            lambda ts: any(session.start <= ts.time() <= session.end for session in active_sessions)
        )
        filtered = working.loc[mask].copy()
        return self._drop_synthetic_flat_bars(filtered)

    def resample_intraday_frame(self, frame, period, include_closing_auction=False):
        """按市场会话规则重采样分钟线数据。"""
        period_rule_map = {
            "5min": "5min",
            "15min": "15min",
            "30min": "30min",
            "60min": "60min",
        }
        rule = period_rule_map.get(period)
        if frame is None or frame.empty or rule is None:
            return frame

        working = self.filter_intraday_session(
            frame,
            include_closing_auction=include_closing_auction,
        )
        if working is None or working.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        working = working.rename(columns=OHLCV_RENAME_TO_TITLE)
        required_columns = ["Open", "High", "Low", "Close", "Volume"]
        missing_columns = [column for column in required_columns if column not in working.columns]
        if missing_columns:
            raise ValueError(f"重采样缺少必要列: {', '.join(missing_columns)}")

        groups = []
        for _, day_frame in working.groupby(working.index.date):
            if day_frame.empty:
                continue

            day_resampled = day_frame.resample(rule, label="right", closed="right").agg(
                {
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                }
            )
            day_resampled.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
            day_resampled = self._drop_synthetic_flat_bars(day_resampled)
            if not day_resampled.empty:
                groups.append(day_resampled)

        if not groups:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        result = pd.concat(groups).sort_index()
        result.index.name = "date"
        return result


HK_MARKET_CALENDAR = MarketCalendar(
    market="HK",
    sessions=(
        SessionWindow(start=time(9, 30), end=time(12, 0)),
        SessionWindow(start=time(13, 0), end=time(16, 0)),
    ),
    closing_auction_end=time(16, 8),
)

CN_MARKET_CALENDAR = MarketCalendar(
    market="CN",
    sessions=(
        SessionWindow(start=time(9, 30), end=time(11, 30)),
        SessionWindow(start=time(13, 0), end=time(15, 0)),
    ),
)

MARKET_CALENDARS = {
    "HK": HK_MARKET_CALENDAR,
    "CN": CN_MARKET_CALENDAR,
}


def get_market_calendar(market):
    """按市场获取统一交易日历实例。"""
    normalized_market = (market or "HK").upper()
    return MARKET_CALENDARS.get(normalized_market, MarketCalendar(market=normalized_market))
