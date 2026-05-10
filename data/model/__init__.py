#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""数据模型与标准化工具。"""

from .schemas import (
    CLEAN_OHLCV_COLUMNS,
    FEATURE_COLUMNS,
    SIGNAL_COLUMNS,
    STOCK_INFO_FIELDS,
    TRADE_COLUMNS,
    infer_exchange,
    normalize_feature_frame,
    normalize_ohlcv_frame,
    normalize_signal_frame,
    normalize_stock_code,
    normalize_stock_info,
    normalize_trade_frame,
)
from .adjustments import (
    CORPORATE_ACTION_FIELDS,
    ADJUSTMENT_PROFILES,
    AdjustmentProfile,
    get_adjustment_profile,
    normalize_adjust,
    normalize_corporate_actions_frame,
)
from .calendar import (
    CN_MARKET_CALENDAR,
    HK_MARKET_CALENDAR,
    MarketCalendar,
    get_market_calendar,
)
from .quality import validate_ohlcv_frame

__all__ = [
    "ADJUSTMENT_PROFILES",
    "CLEAN_OHLCV_COLUMNS",
    "CN_MARKET_CALENDAR",
    "CORPORATE_ACTION_FIELDS",
    "FEATURE_COLUMNS",
    "HK_MARKET_CALENDAR",
    "SIGNAL_COLUMNS",
    "AdjustmentProfile",
    "MarketCalendar",
    "STOCK_INFO_FIELDS",
    "TRADE_COLUMNS",
    "get_adjustment_profile",
    "get_market_calendar",
    "infer_exchange",
    "normalize_adjust",
    "normalize_corporate_actions_frame",
    "normalize_feature_frame",
    "normalize_ohlcv_frame",
    "normalize_signal_frame",
    "normalize_stock_code",
    "normalize_stock_info",
    "normalize_trade_frame",
    "validate_ohlcv_frame",
]
