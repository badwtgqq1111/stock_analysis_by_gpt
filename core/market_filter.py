"""Market data filter: market cap, daily turnover, and IPO cooling period.

Fetches market cap + turnover via concurrent Tencent API and counts
trading days from the warehouse OHLCV data.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd
import requests


@dataclass
class MarketInfo:
    stock_code: str
    name: str = ""
    market_cap: float = 0.0  # billion HKD
    daily_turnover: float = 0.0  # HKD (today)
    avg_turnover_20d: float = 0.0  # HKD (20-day average)
    pe_ratio: float = 0.0
    pb_ratio: float = 0.0
    trading_days: int = 0


@dataclass
class FilterResult:
    passed: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    total: int = 0


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fetch_market_data_batch(
    stock_codes: list[str],
    max_workers: int = 20,
    progress_callback=None,
) -> dict[str, MarketInfo]:
    """Fetch market cap and daily turnover for HK stocks via Tencent API.

    Returns dict mapping stock_code -> MarketInfo.
    """
    result: dict[str, MarketInfo] = {}
    lock = threading.Lock()
    completed = 0
    total = len(stock_codes)

    def _fetch_one(code: str) -> None:
        nonlocal completed
        try:
            info = _fetch_single(code)
            with lock:
                result[code] = info
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)
        except Exception:
            with lock:
                result[code] = MarketInfo(stock_code=code)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in stock_codes}
        for _ in as_completed(futures):
            pass

    return result


def _fetch_single(code: str) -> MarketInfo:
    url = f"https://qt.gtimg.cn/q=r_hk{code}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    r.encoding = "gbk"
    m = re.search(r'"(.+)"', r.text)
    if not m:
        return MarketInfo(stock_code=code)

    fields = m.group(1).split("~")
    if len(fields) < 50:
        return MarketInfo(stock_code=code)

    return MarketInfo(
        stock_code=code,
        name=fields[1],
        market_cap=float(fields[44]) if fields[44] else 0.0,  # 亿港元
        daily_turnover=float(fields[37]) if fields[37] else 0.0,  # 港元
        pe_ratio=float(fields[39]) if len(fields) > 39 and fields[39] else 0.0,
        pb_ratio=float(fields[43]) if len(fields) > 43 and fields[43] else 0.0,
    )


def compute_trading_days_batch(
    stock_codes: list[str],
    warehouse,  # MarketDataWarehouse
    max_workers: int = 8,
) -> dict[str, int]:
    """Count trading days per stock from warehouse OHLCV data."""
    result: dict[str, int] = {}

    def _count_one(code: str) -> tuple[str, int]:
        try:
            df = warehouse.read_ohlcv(
                stock_code=code,
                market="HK",
                asset_type="equity",
                frequency="daily",
                adjust="qfq",
            )
            if df is not None and not df.empty:
                return code, len(df)
        except Exception:
            pass
        return code, 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_count_one, code): code for code in stock_codes}
        for future in as_completed(futures):
            code, days = future.result()
            result[code] = days

    return result


def apply_filters(
    stock_codes: list[str],
    market_data: dict[str, MarketInfo],
    trading_days: dict[str, int] | None = None,
    min_market_cap: float | None = None,     # billion HKD
    min_daily_turnover: float | None = None,  # HKD
    min_ipo_days: int | None = None,          # trading days
    min_avg_turnover_20d: float | None = None,  # HKD, 20-day avg
) -> FilterResult:
    """Apply quality filters to stock list.

    Args:
        stock_codes: Full stock list.
        market_data: MarketInfo per stock from Tencent API.
        trading_days: Trading day count per stock from warehouse.
        min_market_cap: Minimum market cap in 亿港元.
        min_daily_turnover: Minimum daily turnover in 港元.
        min_ipo_days: Minimum number of trading days (IPO cooling).
        min_avg_turnover_20d: Minimum 20-day avg turnover in 港元.

    Returns:
        FilterResult with passed and excluded lists.
    """
    result = FilterResult(total=len(stock_codes))
    filters_active = any([
        min_market_cap, min_daily_turnover, min_ipo_days, min_avg_turnover_20d
    ])
    if not filters_active:
        result.passed = list(stock_codes)
        return result

    for code in stock_codes:
        info = market_data.get(code)
        reasons = []

        if min_market_cap is not None:
            mc = info.market_cap if info else 0.0
            if mc < min_market_cap:
                reasons.append(f"市值{mc:.1f}亿<{min_market_cap}亿")

        if min_daily_turnover is not None:
            dt = info.daily_turnover if info else 0.0
            if dt < min_daily_turnover:
                reasons.append(f"成交额{dt/1e4:.0f}万<{min_daily_turnover/1e4:.0f}万")

        if min_ipo_days is not None and trading_days is not None:
            td = trading_days.get(code, 0)
            if td < min_ipo_days:
                reasons.append(f"上市天数{td}<{min_ipo_days}")

        if reasons:
            result.excluded.append({
                "stock_code": code,
                "name": info.name if info else "",
                "market_cap": info.market_cap if info else 0.0,
                "daily_turnover": info.daily_turnover if info else 0.0,
                "trading_days": trading_days.get(code, 0) if trading_days else 0,
                "reasons": reasons,
            })
        else:
            result.passed.append(code)

    return result


def print_filter_report(result: FilterResult, min_market_cap=None, min_daily_turnover=None, min_ipo_days=None):
    """Print a filter summary to stdout."""
    n_excluded = len(result.excluded)
    n_passed = len(result.passed)
    print(f"[FILTER] 股票过滤: {result.total} -> {n_passed} 通过 (排除 {n_excluded})")

    if min_market_cap is not None:
        mc_excluded = sum(
            1 for e in result.excluded
            if any(f"市值" in r for r in e["reasons"])
        )
        print(f"  市值 >= {min_market_cap}亿: 排除 {mc_excluded} 只")

    if min_daily_turnover is not None:
        dt_excluded = sum(
            1 for e in result.excluded
            if any(f"成交额" in r for r in e["reasons"])
        )
        print(f"  日均成交额 >= {min_daily_turnover/1e4:.0f}万: 排除 {dt_excluded} 只")

    if min_ipo_days is not None:
        ipo_excluded = sum(
            1 for e in result.excluded
            if any(f"上市天数" in r for r in e["reasons"])
        )
        print(f"  上市天数 >= {min_ipo_days}: 排除 {ipo_excluded} 只")

    if n_passed == 0:
        print("[FILTER] 警告: 过滤后无股票通过，请放宽过滤条件")
