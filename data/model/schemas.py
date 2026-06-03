#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""统一市场数据 schema 与标准化函数。"""

from datetime import datetime
import re

import pandas as pd

from .adjustments import normalize_adjust


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

FEATURE_COLUMNS = [
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "adjust",
    "feature_set",
    "feature_version",
    "feature_config_hash",
    "feature_name",
    "feature_value",
    "source",
    "ingest_time",
]

SIGNAL_COLUMNS = [
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "adjust",
    "signal_set",
    "signal_type",
    "signal_strength",
    "score",
    "actionable",
    "batch_id",
    "rank_position",
    "strategy_name",
    "source",
    "ingest_time",
]

TRADE_COLUMNS = [
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "adjust",
    "account_id",
    "strategy_name",
    "order_id",
    "trade_type",
    "price",
    "shares",
    "amount",
    "commission",
    "source",
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
    "pb_ratio",
    "dividend_yield",
    "total_shares",
    "circulating_shares",
    "week_52_high",
    "week_52_low",
    "industry_l1",
    "industry_l2",
    "industry_l3",
    "theme_tags",
    "industry_source",
    "industry_updated_at",
    "instrument_type",
    "is_fund_like",
    "tradable_flag",
    "instrument_source",
    "instrument_updated_at",
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


HK_FUND_LIKE_CODE_PREFIXES = (
    "03",  # ETF / listed funds
    "09",  # USD/RMB counters for ETFs and funds
    "28",  # ETF / listed fund block
    "30",  # ETF block
    "31",  # ETF block
    "34",  # ETF block
    "43",  # depositary / structured products
    "72",  # leveraged and inverse products
    "73",
    "75",
    "77",
)

HK_FUND_LIKE_NAME_KEYWORDS = (
    "ETF",
    "ETP",
    "基金",
    "信托基金",
    "房产信托",
    "房地产信托",
    "REIT",
    "REITS",
    "债券",
    "债",
    "票据",
    "黄金",
    "白银",
    "原油",
    "期货",
    "杠杆",
    "反向",
    "反",
    "两倍",
    "二倍",
    "每日",
    "牛熊",
    "权证",
    "认购",
    "认沽",
    "备兑",
    "比特币",
    "以太币",
    "BITCOIN",
    "ETHER",
    "USD",
    "美元",
    "港元",
)

HK_REIT_NAME_KEYWORDS = (
    "REIT",
    "REITS",
    "房产信托",
    "房地产信托",
    "置富产业信托",
    "领展房产基金",
    "冠君产业信托",
    "阳光房地产基金",
)


def infer_instrument_type(stock_code, market="HK", name=None, asset_type="equity"):
    """Infer security type for universe hygiene; conservative for HK fund blocks."""
    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)
    normalized_asset_type = str(asset_type or "equity").strip().lower() or "equity"
    normalized_name = str(name or "").upper()

    if normalized_asset_type not in {"equity", "stock"}:
        return normalized_asset_type

    if normalized_market == "HK":
        digits = re.sub(r"\D", "", normalized_code)
        if any(keyword.upper() in normalized_name for keyword in HK_REIT_NAME_KEYWORDS):
            return "reit"
        if digits.startswith(HK_FUND_LIKE_CODE_PREFIXES):
            return "fund_like"
        if any(keyword.upper() in normalized_name for keyword in HK_FUND_LIKE_NAME_KEYWORDS):
            return "fund_like"

    return "common_stock"


def is_fund_like_instrument(instrument_type=None, stock_code=None, market="HK", name=None, asset_type="equity"):
    """Return True for funds, ETFs, REITs, structured and leveraged products."""
    inferred = str(
        instrument_type
        or infer_instrument_type(stock_code, market=market, name=name, asset_type=asset_type)
        or ""
    ).strip().lower()
    return inferred in {
        "fund",
        "fund_like",
        "etf",
        "reit",
        "structured_product",
        "warrant",
        "bond",
        "note",
        "trust",
    }


def normalize_bool(value, default=False):
    """Normalize nullable/scalar bool-ish values without treating NaN as True."""
    if value is None:
        return bool(default)
    try:
        if pd.isna(value):
            return bool(default)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "是"}:
            return True
        if normalized in {"0", "false", "no", "n", "否", ""}:
            return False
    return bool(value)


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
    normalized_adjust = normalize_adjust(adjust)

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
    if getattr(working["trade_date"].dt, "tz", None) is not None:
        working["trade_date"] = working["trade_date"].dt.tz_localize(None)
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
    working["adjust"] = normalized_adjust
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


def normalize_feature_frame(
    frame,
    stock_code,
    market="HK",
    exchange=None,
    asset_type="equity",
    frequency="daily",
    adjust="qfq",
    feature_set="default",
    feature_version=None,
    feature_config_hash=None,
    source=None,
    feature_columns=None,
):
    """将宽表或长表特征数据统一为 feature 层长表格式。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)
    normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
    normalized_adjust = normalize_adjust(adjust)

    working = frame.copy()
    rename_mapping = {
        "date": "trade_date",
        "Date": "trade_date",
        "datetime": "trade_date",
        "trade_date": "trade_date",
        "feature_name": "feature_name",
        "factor_name": "feature_name",
        "name": "feature_name",
        "feature_value": "feature_value",
        "factor_value": "feature_value",
        "value": "feature_value",
    }
    working.rename(columns=rename_mapping, inplace=True)

    if "trade_date" not in working.columns:
        if working.index.name in {"date", "trade_date"} or isinstance(working.index, pd.DatetimeIndex):
            working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
        else:
            raise ValueError("输入特征数据缺少 trade_date/date 列")

    metadata_columns = {
        "trade_date",
        "stock_code",
        "market",
        "exchange",
        "asset_type",
        "frequency",
        "adjust",
        "feature_set",
        "feature_version",
        "feature_config_hash",
        "feature_name",
        "feature_value",
        "source",
        "ingest_time",
    }

    if {"feature_name", "feature_value"}.issubset(working.columns):
        long_frame = working[["trade_date", "feature_name", "feature_value"]].copy()
    else:
        target_feature_columns = list(feature_columns or [])
        if not target_feature_columns:
            target_feature_columns = [column for column in working.columns if column not in metadata_columns]
        if not target_feature_columns:
            raise ValueError("未找到可写入 feature 层的特征列")
        long_frame = working.melt(
            id_vars=["trade_date"],
            value_vars=target_feature_columns,
            var_name="feature_name",
            value_name="feature_value",
        )

    long_frame["trade_date"] = pd.to_datetime(long_frame["trade_date"], errors="coerce")
    long_frame.dropna(subset=["trade_date", "feature_name"], inplace=True)
    long_frame["feature_value"] = pd.to_numeric(long_frame["feature_value"], errors="coerce")
    long_frame.dropna(subset=["feature_value"], inplace=True)

    long_frame["stock_code"] = normalized_code
    long_frame["market"] = normalized_market
    long_frame["exchange"] = normalized_exchange
    long_frame["asset_type"] = asset_type
    long_frame["frequency"] = frequency
    long_frame["adjust"] = normalized_adjust
    long_frame["feature_set"] = str(feature_set or "default").strip() or "default"
    long_frame["feature_version"] = str(feature_version or "0.1.0").strip() or "0.1.0"
    long_frame["feature_config_hash"] = str(feature_config_hash or "default").strip() or "default"
    long_frame["source"] = source or "unknown"
    long_frame["ingest_time"] = pd.Timestamp.utcnow()

    long_frame = long_frame[FEATURE_COLUMNS].copy()
    long_frame.sort_values(
        ["trade_date", "feature_set", "feature_version", "feature_config_hash", "feature_name"],
        inplace=True,
    )
    long_frame.drop_duplicates(
        subset=[
            "market",
            "stock_code",
            "trade_date",
            "frequency",
            "adjust",
            "feature_set",
            "feature_version",
            "feature_config_hash",
            "feature_name",
        ],
        keep="last",
        inplace=True,
    )
    long_frame.reset_index(drop=True, inplace=True)
    return long_frame


def normalize_signal_frame(
    frame,
    stock_code,
    market="HK",
    exchange=None,
    asset_type="equity",
    frequency="daily",
    adjust="qfq",
    signal_set="default",
    strategy_name=None,
    source=None,
):
    """将信号数据统一为 signal 层格式。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)

    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)
    normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
    normalized_adjust = normalize_adjust(adjust)

    working = frame.copy()
    working.rename(
        columns={
            "date": "trade_date",
            "Date": "trade_date",
            "datetime": "trade_date",
            "signal_source": "source",
        },
        inplace=True,
    )

    if "trade_date" not in working.columns:
        if working.index.name in {"date", "trade_date"} or isinstance(working.index, pd.DatetimeIndex):
            working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
        else:
            raise ValueError("输入信号数据缺少 trade_date/date 列")

    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    if getattr(working["trade_date"].dt, "tz", None) is not None:
        working["trade_date"] = working["trade_date"].dt.tz_localize(None)
    working.dropna(subset=["trade_date"], inplace=True)

    working["signal_type"] = working.get("signal_type")
    working["signal_strength"] = pd.to_numeric(working.get("signal_strength"), errors="coerce")
    working["score"] = pd.to_numeric(working.get("score"), errors="coerce")
    if "actionable" in working.columns:
        working["actionable"] = working["actionable"].fillna(False).astype(bool)
    else:
        working["actionable"] = False

    working["stock_code"] = normalized_code
    working["market"] = normalized_market
    working["exchange"] = normalized_exchange
    working["asset_type"] = asset_type
    working["frequency"] = frequency
    working["adjust"] = normalized_adjust
    working["signal_set"] = str(signal_set or "default").strip() or "default"
    working["batch_id"] = working.get("batch_id")
    working["rank_position"] = pd.to_numeric(working.get("rank_position"), errors="coerce")
    working["strategy_name"] = strategy_name or working.get("strategy_name")
    working["source"] = source or working.get("source") or "unknown"
    working["ingest_time"] = pd.Timestamp.utcnow()

    working = working[SIGNAL_COLUMNS].copy()
    working.sort_values(["trade_date", "signal_set", "signal_type"], inplace=True)
    working.drop_duplicates(
        subset=["market", "stock_code", "trade_date", "frequency", "adjust", "signal_set", "signal_type"],
        keep="last",
        inplace=True,
    )
    working.reset_index(drop=True, inplace=True)
    return working


def normalize_trade_frame(
    frame,
    stock_code,
    market="HK",
    exchange=None,
    asset_type="equity",
    frequency="daily",
    adjust="qfq",
    account_id="default",
    strategy_name=None,
    source=None,
):
    """将成交/订单数据统一为 trade 层格式。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)
    normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()
    normalized_adjust = normalize_adjust(adjust)

    working = frame.copy()
    working.rename(
        columns={
            "date": "trade_date",
            "Date": "trade_date",
            "datetime": "trade_date",
            "trade_source": "source",
        },
        inplace=True,
    )

    if "trade_date" not in working.columns:
        if working.index.name in {"date", "trade_date"} or isinstance(working.index, pd.DatetimeIndex):
            working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
        else:
            raise ValueError("输入交易数据缺少 trade_date/date 列")

    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working.dropna(subset=["trade_date"], inplace=True)

    working["trade_type"] = working.get("trade_type")
    working["price"] = pd.to_numeric(working.get("price"), errors="coerce")
    working["shares"] = pd.to_numeric(working.get("shares"), errors="coerce")
    working["amount"] = pd.to_numeric(working.get("amount"), errors="coerce")
    working["commission"] = pd.to_numeric(working.get("commission"), errors="coerce").fillna(0.0)
    working["order_id"] = working.get("order_id")

    working["stock_code"] = normalized_code
    working["market"] = normalized_market
    working["exchange"] = normalized_exchange
    working["asset_type"] = asset_type
    working["frequency"] = frequency
    working["adjust"] = normalized_adjust
    working["account_id"] = str(account_id or "default").strip() or "default"
    working["strategy_name"] = strategy_name or working.get("strategy_name")
    working["source"] = source or working.get("source") or "unknown"
    working["ingest_time"] = pd.Timestamp.utcnow()

    working = working[TRADE_COLUMNS].copy()
    working.sort_values(["trade_date", "account_id", "order_id"], inplace=True)
    working.drop_duplicates(
        subset=["market", "stock_code", "trade_date", "account_id", "order_id"],
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
    payload_asset_type = payload.get("asset_type") or asset_type
    payload_name = payload.get("name")
    instrument_type = (
        payload.get("instrument_type")
        or payload.get("security_type")
        or infer_instrument_type(
            normalized_code,
            market=normalized_market,
            name=payload_name,
            asset_type=payload_asset_type,
        )
    )
    is_fund_like = payload.get("is_fund_like")
    if is_fund_like is None:
        is_fund_like = is_fund_like_instrument(
            instrument_type=instrument_type,
            stock_code=normalized_code,
            market=normalized_market,
            name=payload_name,
            asset_type=payload_asset_type,
        )
    theme_tags = payload.get("theme_tags")
    if isinstance(theme_tags, (list, tuple, set)):
        theme_tags = ";".join(str(tag).strip() for tag in theme_tags if str(tag).strip())
    return {
        "stock_code": normalized_code,
        "market": normalized_market,
        "exchange": normalized_exchange,
        "asset_type": payload_asset_type,
        "name": payload_name,
        "current_price": payload.get("current_price"),
        "close_price": payload.get("close_price"),
        "open_price": payload.get("open_price"),
        "high": payload.get("high"),
        "low": payload.get("low"),
        "volume": payload.get("volume"),
        "market_cap": payload.get("market_cap"),
        "pe_ratio": payload.get("pe_ratio"),
        "pb_ratio": payload.get("pb_ratio"),
        "dividend_yield": payload.get("dividend_yield"),
        "total_shares": payload.get("total_shares"),
        "circulating_shares": payload.get("circulating_shares"),
        "week_52_high": payload.get("52_week_high") or payload.get("week_52_high"),
        "week_52_low": payload.get("52_week_low") or payload.get("week_52_low"),
        "industry_l1": payload.get("industry_l1") or payload.get("sector") or payload.get("industry"),
        "industry_l2": payload.get("industry_l2") or payload.get("sub_industry"),
        "industry_l3": payload.get("industry_l3"),
        "theme_tags": theme_tags,
        "industry_source": payload.get("industry_source"),
        "industry_updated_at": payload.get("industry_updated_at"),
        "instrument_type": instrument_type,
        "is_fund_like": normalize_bool(is_fund_like, default=False),
        "tradable_flag": normalize_bool(payload.get("tradable_flag"), default=True),
        "instrument_source": payload.get("instrument_source") or "local_inference",
        "instrument_updated_at": payload.get("instrument_updated_at") or datetime.utcnow().isoformat(),
        "source": source or payload.get("source") or "unknown",
        "ingest_time": datetime.utcnow().isoformat(),
    }
