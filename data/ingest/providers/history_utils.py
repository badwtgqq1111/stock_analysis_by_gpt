#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""历史行情抓取公共工具。"""

import time

import pandas as pd
import requests


PERIOD_ALIASES = {
    None: "daily",
    "d": "daily",
    "day": "daily",
    "daily": "daily",
    "1m": "1min",
    "1min": "1min",
    "1minute": "1min",
    "minute": "1min",
    "5m": "5min",
    "5min": "5min",
    "5minute": "5min",
    "15m": "15min",
    "15min": "15min",
    "30m": "30min",
    "30min": "30min",
    "60m": "60min",
    "60min": "60min",
    "1h": "60min",
    "hour": "60min",
    "hourly": "60min",
}

INTRADAY_PERIOD_MAP = {
    "1min": "1",
    "5min": "5",
    "15min": "15",
    "30min": "30",
    "60min": "60",
}


def normalize_period(period):
    """标准化周期。"""
    if period is None:
        return "daily"
    normalized = str(period).strip().lower()
    if normalized not in PERIOD_ALIASES:
        raise ValueError(f"不支持的周期: {period}")
    return PERIOD_ALIASES[normalized]


def is_intraday_period(period):
    """判断是否为分钟级周期。"""
    return normalize_period(period) in INTRADAY_PERIOD_MAP


def to_frequency_label(period):
    """将抓取周期转换为统一 frequency 标签。"""
    return normalize_period(period)


def to_akshare_intraday_period(period):
    """转换为 akshare 东方财富分钟接口的 period 参数。"""
    normalized = normalize_period(period)
    if normalized not in INTRADAY_PERIOD_MAP:
        raise ValueError(f"{period} 不是分钟级周期")
    return INTRADAY_PERIOD_MAP[normalized]


def normalize_history_dataframe(df, column_mapping):
    """将各数据源行情统一为 date index + OHLCV 列。"""
    if df is None or df.empty:
        return pd.DataFrame()

    working_df = df.copy()
    working_df.rename(columns=column_mapping, inplace=True)

    if "date" not in working_df.columns:
        raise ValueError("缺少 date/日期/时间 字段")

    working_df["date"] = pd.to_datetime(working_df["date"], errors="coerce")
    working_df.dropna(subset=["date"], inplace=True)

    required_columns = ["date", "open", "close", "high", "low", "volume"]
    missing_columns = [column for column in required_columns if column not in working_df.columns]
    if missing_columns:
        raise ValueError(f"缺少必要字段: {', '.join(missing_columns)}")

    for column in required_columns[1:]:
        working_df[column] = pd.to_numeric(working_df[column], errors="coerce")

    working_df = working_df[required_columns].copy()
    working_df.dropna(subset=["open", "close", "high", "low"], inplace=True)
    working_df["volume"] = working_df["volume"].fillna(0)
    working_df.sort_values("date", inplace=True)
    working_df.drop_duplicates(subset=["date"], keep="last", inplace=True)
    working_df.set_index("date", inplace=True)
    working_df.rename(
        columns={
            "open": "Open",
            "close": "Close",
            "high": "High",
            "low": "Low",
            "volume": "Volume",
        },
        inplace=True,
    )
    working_df.index.name = "date"
    return working_df


def apply_date_filters(df, start_date=None, end_date=None, num_records=None):
    """统一处理日期区间和返回条数。"""
    if df is None or df.empty:
        return df

    filtered_df = df.sort_index()
    if start_date:
        filtered_df = filtered_df.loc[filtered_df.index >= pd.to_datetime(start_date)]
    if end_date:
        filtered_df = filtered_df.loc[filtered_df.index <= pd.to_datetime(end_date)]
    if num_records:
        filtered_df = filtered_df.tail(int(num_records))
    return filtered_df


def call_with_retries(func, attempts=3, sleep_seconds=1.0):
    """对不稳定的数据源调用做简单重试。"""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # pragma: no cover
            last_error = exc
            if attempt == attempts:
                raise
            time.sleep(sleep_seconds)
    raise last_error


def fetch_eastmoney_intraday_ohlcv(secid, period, adjust="", start_date=None, end_date=None):
    """直接调用东方财富分时接口，返回标准化前的原始 DataFrame。"""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    adjust_map = {"": "0", "qfq": "1", "hfq": "2"}
    normalized_period = normalize_period(period)

    if normalized_period == "1min":
        url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "iscr": "0",
            "ndays": "5",
            "secid": secid,
        }
        data_json = call_with_retries(
            lambda: requests.get(url, params=params, headers=headers, timeout=15).json(),
            attempts=3,
            sleep_seconds=1.0,
        )
        trends = ((data_json or {}).get("data") or {}).get("trends") or []
        if not trends:
            return pd.DataFrame()
        raw_df = pd.DataFrame([item.split(",") for item in trends])
        raw_df.columns = ["date", "open", "close", "high", "low", "volume", "amount", "extra"]
    else:
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": to_akshare_intraday_period(normalized_period),
            "fqt": adjust_map[adjust],
            "secid": secid,
            "beg": "0",
            "end": "20500000",
        }
        data_json = call_with_retries(
            lambda: requests.get(url, params=params, headers=headers, timeout=15).json(),
            attempts=3,
            sleep_seconds=1.0,
        )
        klines = ((data_json or {}).get("data") or {}).get("klines") or []
        if not klines:
            return pd.DataFrame()
        raw_df = pd.DataFrame([item.split(",") for item in klines])
        raw_df.columns = [
            "date",
            "open",
            "close",
            "high",
            "low",
            "volume",
            "amount",
            "amplitude",
            "pct_chg",
            "chg",
            "turnover",
        ]

    normalized_df = normalize_history_dataframe(
        raw_df,
        {"date": "date", "open": "open", "close": "close", "high": "high", "low": "low", "volume": "volume"},
    )
    return apply_date_filters(normalized_df, start_date, end_date, None)
