"""Fundamental quality scoring for HK stocks.

Fetches key financial indicators via Eastmoney GMAININDICATOR and
computes a composite quality score (0-100) per stock.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _eastmoney_datacenter(report_name, filter_str="", page_size=4, sort_columns="REPORT_DATE", sort_types="-1"):
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = requests.get(DATACENTER_URL, params=params, headers={"User-Agent": UA}, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


def fetch_quality_scores(
    stock_codes: list[str],
    max_workers: int = 8,
    progress_callback=None,
) -> dict[str, float]:
    """Fetch fundamental data and compute quality scores for HK stocks.

    Returns dict mapping stock_code -> quality_score (0-100, higher is better).
    NaN quality_score means no data available.
    """
    result: dict[str, float] = {}
    lock = threading.Lock()
    completed = 0
    total = len(stock_codes)

    def _fetch_one(code: str) -> tuple[str, float]:
        try:
            data = _eastmoney_datacenter(
                report_name="RPT_HKF10_FN_GMAININDICATOR",
                filter_str=f'(SECUCODE="{code}.HK")',
                page_size=4,
                sort_columns="REPORT_DATE",
                sort_types="-1",
            )
            score = _compute_quality_score(data) if data else np.nan
            return code, score
        except Exception:
            return code, np.nan

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in stock_codes}
        for future in as_completed(futures):
            code, score = future.result()
            with lock:
                result[code] = score
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)

    return result


def _compute_quality_score(records: list[dict]) -> float:
    """Compute composite quality score from GMAININDICATOR records.

    Uses the latest 4 reports. Returns 0-100 score.
    Components:
      - ROE (weight 0.25)
      - Gross margin (weight 0.20)
      - Net margin (weight 0.15)
      - Revenue growth YoY (weight 0.20)
      - Debt ratio: lower is better (weight 0.10)
      - Current ratio (weight 0.10)
    """
    if not records:
        return np.nan

    df = pd.DataFrame(records)
    numeric_cols = [
        "ROE_AVG", "GROSS_PROFIT_RATIO", "NET_PROFIT_RATIO",
        "OPERATE_INCOME_YOY", "DEBT_ASSET_RATIO", "CURRENT_RATIO",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Use the latest 4 reports, take mean for stability
    recent = df.head(4)

    def _mean(col_name):
        if col_name in recent.columns:
            vals = recent[col_name].dropna()
            return vals.mean() if len(vals) > 0 else np.nan
        return np.nan

    roe = _mean("ROE_AVG") or 0
    gross_margin = _mean("GROSS_PROFIT_RATIO") or 0
    net_margin = _mean("NET_PROFIT_RATIO") or 0
    rev_growth = _mean("OPERATE_INCOME_YOY") or 0
    debt_ratio = _mean("DEBT_ASSET_RATIO") or 50
    current_ratio = _mean("CURRENT_RATIO") or 1.0

    # Score each component (0-100 scale)
    roe_score = _clip_score(roe / 20 * 100)       # ROE 20% = 100
    gm_score = _clip_score(gross_margin)           # Gross margin % as is
    nm_score = _clip_score(net_margin)             # Net margin % as is
    growth_score = _clip_score(rev_growth / 2 + 50)  # 0% growth = 50, 100% = 100
    debt_score = _clip_score(100 - debt_ratio)     # Lower debt = better
    cur_score = _clip_score(min(current_ratio * 50, 100))  # CR 2.0 = 100

    quality = (
        roe_score * 0.25 +
        gm_score * 0.20 +
        nm_score * 0.15 +
        growth_score * 0.20 +
        debt_score * 0.10 +
        cur_score * 0.10
    )

    return round(quality, 2)


def _clip_score(val: float, lower: float = 0, upper: float = 100) -> float:
    return max(lower, min(upper, val))


def enrich_with_quality(
    stock_codes: list[str],
    quality_scores: dict[str, float],
    show_progress: bool = False,
) -> dict[str, float]:
    """Fill missing quality scores and print summary."""
    enriched = {}
    valid = 0
    missing = 0
    for code in stock_codes:
        score = quality_scores.get(code, np.nan)
        if pd.notna(score):
            enriched[code] = score
            valid += 1
        else:
            enriched[code] = 50.0  # neutral default
            missing += 1

    if show_progress:
        print(f"[QUALITY] 质量评分: {valid} 只有效数据, {missing} 只用默认值(50)")

    return enriched
