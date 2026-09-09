#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Data quality rules and non-destructive audit helpers."""

from dataclasses import asdict, dataclass
from pathlib import Path
import csv
import json

import numpy as np
import pandas as pd


QUALITY_REQUIRED_COLUMNS = ["trade_date", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class DataQualityRule:
    """Versioned description of one audit rule."""

    rule_id: str
    dataset: str
    severity: str
    description: str
    version: str = "v1"


@dataclass(frozen=True)
class DataQualityIssue:
    """A compact, serializable quality finding."""

    rule_id: str
    severity: str
    count: int
    dataset: str
    market: str | None = None
    frequency: str | None = None
    stock_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


QUALITY_RULES = (
    DataQualityRule("ohlcv.required_values", "ohlcv", "error", "关键 OHLCV 字段不得缺失"),
    DataQualityRule("ohlcv.unique_timestamp", "ohlcv", "error", "同一标的时间戳不得重复"),
    DataQualityRule("ohlcv.price_relationship", "ohlcv", "error", "OHLC 关系必须有效"),
    DataQualityRule("ohlcv.non_negative_volume", "ohlcv", "error", "成交量不得为负"),
    DataQualityRule("ohlcv.session_bounds", "ohlcv", "warning", "分时 bar 应落在交易时段内"),
    DataQualityRule("pit.available_at", "pit", "error", "数据可得时间不得晚于样本时间"),
    DataQualityRule("pit.required_columns", "pit", "error", "PIT 数据必须包含样本时间和可得时间"),
    DataQualityRule("pit.duplicate_key", "pit", "error", "PIT 记录主键不得重复"),
)


def _prepare_quality_frame(frame):
    if frame is None:
        return pd.DataFrame(columns=QUALITY_REQUIRED_COLUMNS)

    working = frame.copy()
    working.rename(
        columns={
            "date": "trade_date",
            "Date": "trade_date",
            "timestamp": "trade_date",
            "Timestamp": "trade_date",
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


def validate_ohlcv_frame(
    frame,
    market=None,
    frequency=None,
    stock_code=None,
    *,
    check_unique_trade_date=True,
):
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

        if check_unique_trade_date:
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
        "stock_code": stock_code,
        "rows": int(len(working)),
        "passed": error_count == 0,
        "error_count": int(error_count),
        "warning_count": int(warning_count),
        "issue_counts": issue_counts,
        "issues": issues,
    }


def _issue_rows(report: dict, *, dataset: str, market=None, frequency=None, stock_code=None) -> list[dict]:
    """Convert the legacy report shape to the common issue schema."""
    rows = []
    for issue in report.get("issues", []):
        rows.append(
            DataQualityIssue(
                rule_id=f"{dataset}.{issue['type']}",
                severity=issue["severity"],
                count=int(issue["count"]),
                dataset=dataset,
                market=(market or "").upper() or None,
                frequency=frequency,
                stock_code=stock_code,
                message=issue.get("message", ""),
            ).to_dict()
        )
    return rows


def validate_intraday_frame(
    frame,
    *,
    market="CN",
    frequency=None,
    stock_code=None,
    session_windows=None,
):
    """Audit intraday bars, including duplicate timestamps and session bounds.

    The function only returns findings. It never sorts, drops, fills, or writes
    the input frame.
    """
    report = validate_ohlcv_frame(
        frame, market=market, frequency=frequency, check_unique_trade_date=False
    )
    working = _prepare_quality_frame(frame)
    issues = _issue_rows(report, dataset="ohlcv", market=market, frequency=frequency, stock_code=stock_code)
    if working.empty:
        return _finalize_quality_report(report, issues, dataset="intraday", market=market, frequency=frequency, stock_code=stock_code)

    timestamps = pd.to_datetime(working["trade_date"], errors="coerce")
    duplicate_timestamps = int(timestamps.duplicated().sum())
    if duplicate_timestamps:
        issues.append(DataQualityIssue(
            "ohlcv.unique_timestamp", "error", duplicate_timestamps, "intraday",
            (market or "").upper() or None, frequency, stock_code, "存在重复分时 timestamp",
        ).to_dict())

    windows = session_windows or (("09:30", "11:30"), ("13:00", "15:00"))
    valid_mask = pd.Series(False, index=timestamps.index)
    for start, end in windows:
        start_time = pd.to_datetime(start).time()
        end_time = pd.to_datetime(end).time()
        valid_mask |= timestamps.dt.time.between(start_time, end_time)
    outside_session = int((timestamps.notna() & ~valid_mask).sum())
    if outside_session:
        issues.append(DataQualityIssue(
            "ohlcv.session_bounds", "warning", outside_session, "intraday",
            (market or "").upper() or None, frequency, stock_code, "分时 timestamp 位于交易时段外",
        ).to_dict())
    return _finalize_quality_report(report, issues, dataset="intraday", market=market, frequency=frequency, stock_code=stock_code)


def validate_pit_frame(
    frame,
    *,
    dataset="fundamental",
    market=None,
    key_columns=None,
    sample_time_column="trade_date",
    available_at_column="available_at",
):
    """Audit point-in-time availability without changing the input frame."""
    working = frame.copy() if frame is not None else pd.DataFrame()
    issues: list[dict] = []
    if working.empty:
        issues.append(DataQualityIssue(
            "pit.empty", "warning", 1, dataset, (market or "").upper() or None,
            None, None, "PIT 数据为空",
        ).to_dict())
        return _finalize_quality_report({}, issues, dataset=dataset, market=market)

    missing_columns = [
        column for column in (sample_time_column, available_at_column)
        if column not in working.columns
    ]
    if missing_columns:
        issues.append(DataQualityIssue(
            "pit.required_columns", "error", len(missing_columns), dataset,
            (market or "").upper() or None, None, None,
            f"缺少 PIT 字段: {','.join(missing_columns)}",
        ).to_dict())
    if not missing_columns:
        available_at = pd.to_datetime(working[available_at_column], errors="coerce")
        sample_time = pd.to_datetime(working[sample_time_column], errors="coerce")
        future_rows = int((available_at.notna() & sample_time.notna() & (available_at > sample_time)).sum())
        if future_rows:
            issues.append(DataQualityIssue(
                "pit.available_at", "error", future_rows, dataset,
                (market or "").upper() or None, None, None, "available_at 晚于样本时间",
            ).to_dict())

    keys = [column for column in (key_columns or []) if column in working.columns]
    if keys:
        duplicates = int(working.duplicated(subset=keys).sum())
        if duplicates:
            issues.append(DataQualityIssue(
                "pit.duplicate_key", "error", duplicates, dataset,
                (market or "").upper() or None, None, None, f"PIT 主键重复: {','.join(keys)}",
            ).to_dict())
    return _finalize_quality_report({}, issues, dataset=dataset, market=market)


def _finalize_quality_report(base_report, issues, *, dataset, market=None, frequency=None, stock_code=None):
    issue_counts = {}
    error_count = 0
    warning_count = 0
    for issue in issues:
        issue_counts[issue["rule_id"]] = issue_counts.get(issue["rule_id"], 0) + int(issue["count"])
        if issue["severity"] == "error":
            error_count += int(issue["count"])
        else:
            warning_count += int(issue["count"])
    return {
        "dataset": dataset,
        "market": (market or "").upper() or None,
        "frequency": frequency,
        "stock_code": stock_code,
        "rows": int(base_report.get("rows", 0)),
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "issue_counts": issue_counts,
        "issues": issues,
    }


def aggregate_quality_reports(reports) -> dict:
    """Aggregate per-stock/per-frequency audit reports deterministically."""
    reports = list(reports or [])
    issue_counts = {}
    error_count = 0
    warning_count = 0
    rows = 0
    issue_stocks = set()
    for report in reports:
        rows += int(report.get("rows", 0))
        error_count += int(report.get("error_count", 0))
        warning_count += int(report.get("warning_count", 0))
        if report.get("error_count", 0) or report.get("warning_count", 0):
            if report.get("stock_code"):
                issue_stocks.add(str(report["stock_code"]))
        for key, count in (report.get("issue_counts") or {}).items():
            issue_counts[key] = issue_counts.get(key, 0) + int(count)
    return {
        "report_count": len(reports),
        "rows": rows,
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "issue_stock_count": len(issue_stocks),
        "issue_counts": dict(sorted(issue_counts.items())),
    }


def write_quality_report(report: dict, report_dir, *, prefix="clean_feature_panel") -> dict:
    """Write deterministic JSON, CSV and Markdown quality artifacts."""
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    market = str(report.get("market") or "ALL").lower()
    stem = f"{prefix}_{market}"
    json_path = directory / f"{stem}.json"
    csv_path = directory / f"{stem}.csv"
    md_path = directory / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    rows = []
    for item in report.get("details", report.get("issues", [])) or []:
        if isinstance(item, dict):
            rows.append(item)
    fieldnames = sorted({key for row in rows for key in row}) or ["status"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        f"# Data Quality Report ({str(report.get('market') or 'ALL').upper()})",
        "",
        f"- Passed: **{report.get('passed', False)}**",
        f"- Rows: **{report.get('rows', 0)}**",
        f"- Errors: **{report.get('error_count', 0)}**",
        f"- Warnings: **{report.get('warning_count', 0)}**",
        f"- Issue stocks: **{report.get('issue_stock_count', 0)}**",
        "",
        f"JSON: `{json_path}`",
        f"CSV: `{csv_path}`",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}
