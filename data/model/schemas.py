#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""统一市场数据 schema 与标准化函数。"""

from datetime import datetime
import re

import pandas as pd


CLEAN_OHLCV_COLUMNS = [
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "adjust",
    "currency",
    "ingest_time",
]

STOCK_INFO_FIELDS = [
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "name",
    "current_price",
    "close_price",
    "open_price",
    "high",
    "low",
    "volume",
    "market_cap",
    "pe_ratio",
    "week_52_high",
    "week_52_low",
    "source",
    "ingest_time",
]

DEFAULT_EXCHANGE_BY_MARKET = {
    "HK": "HKEX",
    "US": "NASDAQ",
}

DEFAULT_CURRENCY_BY_MARKET = {
    "HK": "HKD",
    "CN": "CNY",
    "US": "USD",
}


def infer_exchange(stock_code, market="HK"):
    """根据市场和代码推断交易所。"""
    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)

    if normalized_market == "HK":
        return "HKEX"

    if normalized_market == "CN":
        digits = re.sub(r"\D", "", normalized_code)
        if normalized_code.endswith(".SH") or digits.startswith(("5", "6", "9")):
            return "SSE"
        if normalized_code.endswith(".SZ") or digits.startswith(("0", "2", "3")):
            return "SZSE"
        if normalized_code.endswith(".BJ") or digits.startswith(("4", "8")):
            return "BSE"

    return DEFAULT_EXCHANGE_BY_MARKET.get(normalized_market, normalized_market)


def normalize_stock_code(stock_code, market="HK"):
    """标准化证券代码。"""
    normalized_market = (market or "HK").upper()
    code = str(stock_code or "").strip().upper()

    if normalized_market == "HK":
        code = code.replace("HK", "")
        digits = re.sub(r"\D", "", code)
        return digits.zfill(5)

    if normalized_market == "CN":
        code = code.replace("SH", ".SH").replace("SZ", ".SZ").replace("BJ", ".BJ")
        code = code.replace("..", ".")
        digits = re.sub(r"\D", "", code)
        if "." in code:
            left, right = code.split(".", 1)
            left_digits = re.sub(r"\D", "", left)
            suffix = re.sub(r"\W", "", right).upper()
            if left_digits and suffix:
                return f"{left_digits.zfill(6)}.{suffix}"
        if len(digits) == 6:
            if digits.startswith(("5", "6", "9")):
                exchange = "SSE"
            elif digits.startswith(("0", "2", "3")):
                exchange = "SZSE"
            elif digits.startswith(("4", "8")):
                exchange = "BSE"
            else:
                exchange = None
            suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange)
            return f"{digits}.{suffix}" if suffix else digits
        return digits or code

    return code


def normalize_ohlcv_frame(
    frame,
    stock_code,
    market="HK",
    exchange=None,
    asset_type="equity",
    frequency="daily",
    source=None,
    adjust="qfq",
    currency=None,
):
    """将不同来源 OHLCV 数据统一为 clean 层格式。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=CLEAN_OHLCV_COLUMNS)

    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)
    normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
    normalized_currency = (currency or DEFAULT_CURRENCY_BY_MARKET.get(normalized_market, normalized_market)).upper()

    working = frame.copy()
    rename_mapping = {
        "date": "trade_date",
        "Date": "trade_date",
        "datetime": "trade_date",
        "trade_date": "trade_date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    working.rename(columns=rename_mapping, inplace=True)

    if "trade_date" not in working.columns:
        if working.index.name in {"date", "trade_date"} or isinstance(working.index, pd.DatetimeIndex):
            working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
        else:
            raise ValueError("输入数据缺少 trade_date/date 列")

    required = ["trade_date", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in working.columns]
    if missing:
        raise ValueError(f"OHLCV 数据缺少必要列: {', '.join(missing)}")

    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working.dropna(subset=["trade_date"], inplace=True)

    for column in ["open", "high", "low", "close", "volume"]:
        working[column] = pd.to_numeric(working[column], errors="coerce")

    working.dropna(subset=["open", "high", "low", "close"], inplace=True)
    working["volume"] = working["volume"].fillna(0)
    working["stock_code"] = normalized_code
    working["market"] = normalized_market
    working["exchange"] = normalized_exchange
    working["asset_type"] = asset_type
    working["frequency"] = frequency
    working["source"] = source or "unknown"
    working["adjust"] = adjust
    working["currency"] = normalized_currency
    working["ingest_time"] = pd.Timestamp.utcnow()

    working = working[CLEAN_OHLCV_COLUMNS].copy()
    working.sort_values("trade_date", inplace=True)
    working.drop_duplicates(
        subset=["market", "stock_code", "trade_date", "frequency", "adjust"],
        keep="last",
        inplace=True,
    )
    working.reset_index(drop=True, inplace=True)
    return working


def normalize_stock_info(
    stock_info,
    stock_code,
    market="HK",
    exchange=None,
    asset_type="equity",
    source=None,
):
    """将股票基础信息统一为 registry 结构。"""
    payload = dict(stock_info or {})
    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)
    normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
    return {
        "stock_code": normalized_code,
        "market": normalized_market,
        "exchange": normalized_exchange,
        "asset_type": asset_type,
        "name": payload.get("name"),
        "current_price": payload.get("current_price"),
        "close_price": payload.get("close_price"),
        "open_price": payload.get("open_price"),
        "high": payload.get("high"),
        "low": payload.get("low"),
        "volume": payload.get("volume"),
        "market_cap": payload.get("market_cap"),
        "pe_ratio": payload.get("pe_ratio"),
        "week_52_high": payload.get("52_week_high") or payload.get("week_52_high"),
        "week_52_low": payload.get("52_week_low") or payload.get("week_52_low"),
        "source": source or payload.get("source") or "unknown",
        "ingest_time": datetime.utcnow().isoformat(),
    }
