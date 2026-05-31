"""Fundamental quality scoring for HK stocks — QMJ methodology.

Reference: Asness, Frazzini & Pedersen (2014/2019) "Quality Minus Junk"
  - Each raw metric: cross-sectional rank within cluster → z-score
  - Group into QMJ dimensions (Profitability, Growth, Safety)
  - Equal weight within dimension, equal weight across dimensions
  - Final re-standardization: z(Profitability + Growth + Safety)

Fetches financial indicators via Eastmoney GMAININDICATOR.
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

# ---- QMJ Dimension Definitions ----
# Each dimension is an equal-weighted average of its component z-scores.
QMJ_DIMENSIONS = {
    "profitability": ["ROE_AVG", "GROSS_PROFIT_RATIO", "NET_PROFIT_RATIO"],
    "growth":       ["OPERATE_INCOME_YOY"],
    "safety":       ["DEBT_ASSET_RATIO", "CURRENT_RATIO"],
}
# Direction: +1 = higher is better, -1 = lower is better
COMPONENT_DIRECTION = {
    "ROE_AVG":              1,
    "GROSS_PROFIT_RATIO":   1,
    "NET_PROFIT_RATIO":     1,
    "OPERATE_INCOME_YOY":   1,
    "DEBT_ASSET_RATIO":    -1,  # lower debt = safer
    "CURRENT_RATIO":        1,
}


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


def _extract_raw_components(records: list[dict]) -> dict[str, float | None]:
    """Extract raw financial metrics from Eastmoney GMAININDICATOR records.

    Uses the latest 4 reports, takes mean for stability.
    Returns dict of component_name -> raw_value (or None if missing).
    """
    if not records:
        return {}

    df = pd.DataFrame(records)
    all_components = [c for dim in QMJ_DIMENSIONS.values() for c in dim]
    for col in all_components:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    recent = df.head(4)
    result = {}
    for comp in all_components:
        if comp in recent.columns:
            vals = recent[comp].dropna()
            result[comp] = float(vals.mean()) if len(vals) > 0 else None
        else:
            result[comp] = None
    return result


def fetch_quality_scores(
    stock_codes: list[str],
    max_workers: int = 8,
    progress_callback=None,
) -> dict[str, float]:
    """Fetch fundamental data and compute QMJ-style quality scores for HK stocks.

    Returns dict mapping stock_code -> quality_score (0-100, higher is better).
    NaN quality_score means no data available.
    Cross-sectional normalization is done in a second pass via
    compute_qmj_quality_scores().
    """
    # Phase 1: fetch raw components per stock
    raw_components: dict[str, dict[str, float | None]] = {}
    lock = threading.Lock()
    completed = 0
    total = len(stock_codes)

    def _fetch_one(code: str) -> tuple[str, dict[str, float | None]]:
        try:
            data = _eastmoney_datacenter(
                report_name="RPT_HKF10_FN_GMAININDICATOR",
                filter_str=f'(SECUCODE="{code}.HK")',
                page_size=4,
                sort_columns="REPORT_DATE",
                sort_types="-1",
            )
            comps = _extract_raw_components(data) if data else {}
            return code, comps
        except Exception:
            return code, {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in stock_codes}
        for future in as_completed(futures):
            code, comps = future.result()
            with lock:
                raw_components[code] = comps
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)

    # Phase 2: cross-sectional normalization & QMJ scoring
    # Without cluster info, do global normalization.
    return _compute_qmj_scores(raw_components, cluster_map=None)


def compute_qmj_quality_scores(
    raw_components: dict[str, dict[str, float | None]],
    cluster_map: dict[str, int] | None = None,
) -> dict[str, float]:
    """Compute QMJ-style quality scores with within-cluster normalization.

    Args:
        raw_components: stock_code -> {component_name: raw_value_or_None}
        cluster_map: stock_code -> cluster_id (optional, for within-cluster z-score)

    Returns:
        stock_code -> quality_score (0-100)
    """
    return _compute_qmj_scores(raw_components, cluster_map)


def _compute_qmj_scores(
    raw_components: dict[str, dict[str, float | None]],
    cluster_map: dict[str, int] | None,
) -> dict[str, float]:
    """Core QMJ scoring logic."""
    if not raw_components:
        return {}

    all_components = [c for dim in QMJ_DIMENSIONS.values() for c in dim]
    codes = list(raw_components.keys())

    # Build component DataFrame: rows=stocks, cols=components
    comp_data = {}
    for code in codes:
        comps = raw_components.get(code, {})
        comp_data[code] = {c: comps.get(c) for c in all_components}
    comp_df = pd.DataFrame.from_dict(comp_data, orient="index")
    comp_df.index.name = "stock_code"

    # ---- Step 1: Within-cluster rank → z-score for each component ----
    if cluster_map is not None:
        comp_df["_cluster"] = comp_df.index.map(lambda c: cluster_map.get(c, -1))
    else:
        comp_df["_cluster"] = -1

    z_scored = pd.DataFrame(index=comp_df.index)
    for comp in all_components:
        direction = COMPONENT_DIRECTION.get(comp, 1)
        z_col = _component_to_zscore(comp_df, comp, direction)
        z_scored[comp] = z_col

    # ---- Step 2: Aggregate into QMJ dimensions (equal weight within dim) ----
    dim_scores = {}
    for dim_name, components in QMJ_DIMENSIONS.items():
        valid_comps = [c for c in components if c in z_scored.columns]
        if not valid_comps:
            continue
        dim_scores[dim_name] = z_scored[valid_comps].mean(axis=1)

    if not dim_scores:
        return {code: 50.0 for code in codes}

    dim_df = pd.DataFrame(dim_scores)

    # ---- Step 3: Equal weight across dimensions → raw composite ----
    composite_raw = dim_df.mean(axis=1)

    # ---- Step 4: Re-standardize final score (z-score) ----
    c_mean = composite_raw.mean()
    c_std = composite_raw.std(ddof=1)
    if c_std > 0.001:
        final_z = (composite_raw - c_mean) / c_std
    else:
        final_z = composite_raw * 0.0

    # ---- Step 5: Map z-score to 0-100 scale ----
    # z=-2.5 → 0, z=0 → 50, z=+2.5 → 100
    scores = 50.0 + final_z * 20.0
    scores = scores.clip(0.0, 100.0)

    return scores.to_dict()


def _component_to_zscore(df: pd.DataFrame, comp: str, direction: int) -> pd.Series:
    """Convert a single component to within-cluster z-scores.

    1. Within each cluster: rank the raw values
    2. Convert rank to z-score: z = (rank - mean_rank) / std_rank
    3. Apply direction (flip sign if lower is better)
    4. For singleton clusters: use global rank → z-score
    """
    result = pd.Series(np.nan, index=df.index, dtype=float)

    for cluster_id, group in df.groupby("_cluster"):
        vals = group[comp]
        valid_mask = vals.notna()

        if valid_mask.sum() < 3:
            # Small cluster: use global rank as fallback
            global_vals = df[comp]
            global_valid = global_vals.notna()
            if global_valid.sum() < 3:
                result.loc[group.index] = 0.0
                continue
            # Rank within global
            ranks = global_vals.rank(method="average")
            rank_mean = ranks.mean()
            rank_std = ranks.std(ddof=1)
            if rank_std < 0.01:
                result.loc[group.index] = 0.0
                continue
            for idx in group.index:
                if pd.notna(vals.loc[idx]):
                    result.loc[idx] = (ranks.loc[idx] - rank_mean) / rank_std * direction
                else:
                    result.loc[idx] = 0.0
            continue

        # Within-cluster ranking
        cluster_vals = vals[valid_mask]
        ranks = cluster_vals.rank(method="average")
        rank_mean = ranks.mean()
        rank_std = ranks.std(ddof=1)

        if rank_std < 0.01:
            result.loc[group.index] = 0.0
            continue

        for idx in group.index:
            if valid_mask.loc[idx]:
                result.loc[idx] = (ranks.loc[idx] - rank_mean) / rank_std * direction
            else:
                result.loc[idx] = 0.0

    return result


def enrich_with_quality(
    stock_codes: list[str],
    quality_scores: dict[str, float],
    show_progress: bool = False,
) -> dict[str, float]:
    """Fill missing quality scores with neutral default (50.0)."""
    enriched = {}
    valid = 0
    missing = 0
    for code in stock_codes:
        score = quality_scores.get(code, np.nan)
        if pd.notna(score):
            enriched[code] = float(score)
            valid += 1
        else:
            enriched[code] = 50.0
            missing += 1

    if show_progress:
        print(f"[QUALITY] 质量评分 (QMJ): {valid} 只有效数据, {missing} 只用默认值(50)")

    return enriched
