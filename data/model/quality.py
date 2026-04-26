#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""OHLCV 数据质量巡检。"""

import pandas as pd


QUALITY_REQUIRED_COLUMNS = ["trade_date", "open", "high", "low", "close", "volume"]


def _prepare_quality_frame(frame):
    if frame is None:
        return pd.DataFrame(columns=QUALITY_REQUIRED_COLUMNS)

    working = frame.copy()
    working.rename(
        columns={
            "date": "trade_date",
            "Date": "trade_date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        },
        inplace=True,
    )

    if "trade_date" not in working.columns:
        if isinstance(working.index, pd.DatetimeIndex) or working.index.name in {"date", "trade_date"}:
            working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
        else:
            return pd.DataFrame(columns=QUALITY_REQUIRED_COLUMNS)

    for column in QUALITY_REQUIRED_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA

    working = working[QUALITY_REQUIRED_COLUMNS].copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume"]:
        working[column] = pd.to_numeric(working[column], errors="coerce")
    return working


def validate_ohlcv_frame(frame, market=None, frequency=None):
    """对标准化或原始 OHLCV 数据做轻量质量巡检。"""
    working = _prepare_quality_frame(frame)
    issue_counts = {}
    issues = []

    def add_issue(issue_type, severity, count, message):
        normalized_count = int(count)
        if normalized_count <= 0:
            return
        issues.append(
            {
                "type": issue_type,
                "severity": severity,
                "count": normalized_count,
                "message": message,
            }
        )
        issue_counts[issue_type] = normalized_count

    if working.empty:
        add_issue("empty_frame", "warning", 1, "数据为空，无法做有效巡检")
    else:
        missing_required_rows = working[QUALITY_REQUIRED_COLUMNS].isna().any(axis=1).sum()
        add_issue("missing_required_values", "error", missing_required_rows, "存在关键字段缺失")

        duplicate_trade_dates = working["trade_date"].duplicated().sum()
        add_issue("duplicate_trade_date", "error", duplicate_trade_dates, "存在重复 trade_date")

        non_monotonic_trade_date = int(not working["trade_date"].is_monotonic_increasing)
        add_issue("non_monotonic_trade_date", "error", non_monotonic_trade_date, "trade_date 未按升序排列")

        price_columns = ["open", "high", "low", "close"]
        non_positive_price = (working[price_columns] <= 0).any(axis=1).sum()
        add_issue("non_positive_price", "error", non_positive_price, "存在非正价格")

        negative_volume = (working["volume"] < 0).sum()
        add_issue("negative_volume", "error", negative_volume, "存在负成交量")

        invalid_ohlc = (
            (working["high"] < working[["open", "close", "low"]].max(axis=1))
            | (working["low"] > working[["open", "close", "high"]].min(axis=1))
            | (working["low"] > working["high"])
        ).sum()
        add_issue("invalid_ohlc_relationship", "error", invalid_ohlc, "存在不满足 OHLC 关系的数据")

        flat_bar = (
            (working["open"] == working["high"])
            & (working["high"] == working["low"])
            & (working["low"] == working["close"])
        )
        zero_volume_flat = ((working["volume"] <= 0) & flat_bar).sum()
        add_issue("zero_volume_flat_bar", "warning", zero_volume_flat, "存在零成交量平盘 bar")

        zero_volume_nonflat = ((working["volume"] <= 0) & (~flat_bar)).sum()
        add_issue("zero_volume_nonflat_bar", "warning", zero_volume_nonflat, "存在零成交量但价格变动的 bar")

    error_count = sum(item["count"] for item in issues if item["severity"] == "error")
    warning_count = sum(item["count"] for item in issues if item["severity"] == "warning")
    return {
        "market": (market or "").upper() if market else None,
        "frequency": frequency,
        "rows": int(len(working)),
        "passed": error_count == 0,
        "error_count": int(error_count),
        "warning_count": int(warning_count),
        "issue_counts": issue_counts,
        "issues": issues,
    }
