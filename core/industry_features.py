#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Industry benchmark engine — real-industry-based features.

Replaces / complements ``core/sector_features.py`` (which uses correlation
clustering) with features grounded in external industry classification
(industry_l1 / industry_l2 from stock_info_registry).

All computations are **point-in-time safe**: they only use price data up to
and including the target date.  Industry membership is taken from the
registry as-of the computation date.

Features produced (per stock, per date)
---------------------------------------
Industry-level:
  industry_l1, industry_l2
  industry_member_count           – number of stocks in this industry
  industry_ret_5d / 20d / 60d     – equal-weighted industry return
  industry_rps_20d / 60d          – cross-sectional industry rank (0–100)
  industry_breadth_5d / 20d       – fraction of members with positive return
  industry_vol_20d / 60d          – annualised industry volatility

Stock-vs-industry:
  stock_vs_industry_ret_5d / 20d  – excess return over industry
  stock_vs_industry_rank          – within-industry percentile rank (0–100)

Composite signals (analogous to sector_features.py):
  dip_buy_signal_industry         – long-term strength + short-term weakness
  industry_leader                 – strong industry × strong stock
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_price_matrix(price_data: dict) -> pd.DataFrame | None:
    """Convert {stock_code: DataFrame} → (dates × stocks) close-price matrix."""
    closes: dict[str, pd.Series] = {}
    for code, df in price_data.items():
        if df is None or (hasattr(df, "empty") and df.empty) or len(df) < 10:
            continue
        col = df.get("close")
        if col is None:
            col = df.get("Close")
        if col is None:
            continue
        s = pd.to_numeric(col, errors="coerce").dropna()
        if len(s) < 10:
            continue
        closes[code] = s
    if not closes:
        return None
    matrix = pd.DataFrame(closes).sort_index()
    return matrix


def _build_industry_map(
    stock_codes: list[str],
    stock_info_map: dict[str, dict],
    level: str = "l2",
) -> dict[str, str | None]:
    """Build {stock_code: industry_label} from stock_info_registry data.

    *level* may be ``"l1"`` or ``"l2"``.  Falls back from l2 → l1 → None.
    """
    mapping: dict[str, str | None] = {}
    for code in stock_codes:
        info = stock_info_map.get(code) or {}
        if level == "l2":
            label = info.get("industry_l2") or info.get("industry_l1")
        else:
            label = info.get("industry_l1")
        mapping[code] = str(label).strip() if label else None
    return mapping


def _equal_weight_industry_returns(
    ret_matrix: pd.DataFrame,
    industry_map: dict[str, str | None],
) -> pd.DataFrame:
    """Compute daily equal-weighted return for each industry.

    Returns
    -------
    DataFrame  dates × industries  of daily returns
    """
    industries = {}
    for code, ind in industry_map.items():
        if ind is None or code not in ret_matrix.columns:
            continue
        industries.setdefault(ind, []).append(code)

    ind_rets: dict[str, pd.Series] = {}
    for ind, members in industries.items():
        member_rets = ret_matrix[members]
        ind_rets[ind] = member_rets.mean(axis=1, skipna=True)
    if not ind_rets:
        return pd.DataFrame()
    return pd.DataFrame(ind_rets).sort_index()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_industry_features(
    price_data: dict,
    stock_info_map: dict[str, dict],
    date: str | pd.Timestamp | None = None,
    *,
    level: str = "l2",
) -> pd.DataFrame:
    """Compute industry-grounded features for every stock.

    Parameters
    ----------
    price_data : dict
        ``{stock_code: DataFrame}`` where each DataFrame has at least a
        ``close`` (or ``Close``) column with a DatetimeIndex.
    stock_info_map : dict
        ``{stock_code: dict}`` as returned by ``MarketDataWarehouse.
        read_stock_info()``.  Must contain ``industry_l1`` / ``industry_l2``.
    date : str or Timestamp, optional
        Target date for point-in-time safety.  Only data ≤ this date is used.
        When *None* the last date in *price_data* is used.
    level : str
        Industry granularity: ``"l1"`` or ``"l2"`` (default).

    Returns
    -------
    DataFrame
        One row per stock with columns:

        - ``stock_code``
        - ``industry_l1``, ``industry_l2``
        - ``industry_member_count``
        - ``industry_ret_5d``, ``industry_ret_20d``, ``industry_ret_60d``
        - ``industry_rps_20d``, ``industry_rps_60d``
        - ``industry_breadth_5d``, ``industry_breadth_20d``
        - ``industry_vol_20d``, ``industry_vol_60d``
        - ``stock_vs_industry_ret_5d``, ``stock_vs_industry_ret_20d``
        - ``stock_vs_industry_rank``
        - ``dip_buy_signal_industry``
        - ``industry_leader``
    """
    normalized_price_data = {}
    for code, data in (price_data or {}).items():
        if isinstance(data, pd.Series):
            normalized_price_data[code] = pd.DataFrame({"close": data})
        elif data is not None:
            normalized_price_data[code] = pd.DataFrame(data).copy()

    price_matrix = _build_price_matrix(normalized_price_data)
    if price_matrix is None or price_matrix.shape[1] < 3:
        return pd.DataFrame()

    # Point-in-time truncation
    if date is not None:
        target_ts = pd.Timestamp(date)
        price_matrix = price_matrix[price_matrix.index <= target_ts]
    if len(price_matrix) < 10:
        return pd.DataFrame()

    stock_codes = list(price_matrix.columns)
    industry_map = _build_industry_map(stock_codes, stock_info_map, level=level)

    # Also build l1 map for output
    industry_l1_map = _build_industry_map(stock_codes, stock_info_map, level="l1")

    # Daily return matrix
    ret_matrix = price_matrix.pct_change().dropna(how="all")
    if ret_matrix.empty:
        return pd.DataFrame()

    # Industry daily returns (equal-weighted)
    ind_ret_df = _equal_weight_industry_returns(ret_matrix, industry_map)
    if ind_ret_df.empty or ind_ret_df.shape[1] < 1:
        return pd.DataFrame()

    # ---- Industry-level metrics ----
    n_dates = len(ind_ret_df)

    # Rolling industry returns
    ind_ret5 = ind_ret_df.rolling(5, min_periods=3).apply(
        lambda x: (1 + x).prod() - 1, raw=False
    ).iloc[-1] if n_dates >= 5 else pd.Series(0.0, index=ind_ret_df.columns)
    ind_ret20 = ind_ret_df.rolling(20, min_periods=10).apply(
        lambda x: (1 + x).prod() - 1, raw=False
    ).iloc[-1] if n_dates >= 20 else pd.Series(0.0, index=ind_ret_df.columns)
    ind_ret60 = ind_ret_df.rolling(60, min_periods=30).apply(
        lambda x: (1 + x).prod() - 1, raw=False
    ).iloc[-1] if n_dates >= 60 else pd.Series(0.0, index=ind_ret_df.columns)

    # Industry RPS (cross-sectional rank of industry returns)
    def _compute_rps(series: pd.Series) -> pd.Series:
        if len(series.dropna()) < 2:
            return pd.Series(50.0, index=series.index)
        ranked = series.rank(pct=True) * 100.0
        return ranked.fillna(50.0)

    ind_rps20 = _compute_rps(ind_ret20)
    ind_rps60 = _compute_rps(ind_ret60)

    # Industry breadth
    def _industry_breadth(window: int) -> pd.Series:
        """Fraction of members with positive return over *window* days."""
        result: dict[str, float] = {}
        price_tail = price_matrix.iloc[-window:] if len(price_matrix) >= window else price_matrix
        for ind, members in _group_by_industry(industry_map, price_matrix.columns):
            member_rets = price_tail[members].pct_change().dropna(how="all")
            if member_rets.empty:
                result[ind] = 0.5
            else:
                total_ret = (1 + member_rets).prod() - 1
                result[ind] = float((total_ret > 0).mean()) if len(total_ret) > 0 else 0.5
        return pd.Series(result)

    def _group_by_industry(
        imap: dict[str, str | None], codes: list[str]
    ) -> list[tuple[str, list[str]]]:
        groups: dict[str, list[str]] = {}
        for c in codes:
            ind = imap.get(c)
            if ind:
                groups.setdefault(ind, []).append(c)
        return list(groups.items())

    breadth5 = _industry_breadth(5)
    breadth20 = _industry_breadth(20)

    # Industry volatility (annualised)
    def _industry_vol(window: int) -> pd.Series:
        if n_dates < window:
            return pd.Series(0.0, index=ind_ret_df.columns)
        vol = ind_ret_df.iloc[-window:].std() * np.sqrt(252)
        return vol.fillna(0.0)

    ind_vol20 = _industry_vol(20)
    ind_vol60 = _industry_vol(60)

    # Member counts
    member_counts = pd.Series({
        ind: len(members)
        for ind, members in _group_by_industry(industry_map, stock_codes)
    })

    # ---- Per-stock metrics ----
    stock_ret5 = price_matrix.pct_change(5).iloc[-1] if n_dates > 5 else pd.Series(0.0, index=stock_codes)
    stock_ret20 = price_matrix.pct_change(20).iloc[-1] if n_dates > 20 else pd.Series(0.0, index=stock_codes)

    rows = []
    for code in stock_codes:
        ind = industry_map.get(code)
        ind_l1 = industry_l1_map.get(code)

        if ind is None:
            # No industry — fill with neutral values
            rows.append({
                "stock_code": code,
                "industry_l1": ind_l1 or "",
                "industry_l2": "",
                "industry_member_count": 0,
                "industry_ret_5d": 0.0,
                "industry_ret_20d": 0.0,
                "industry_ret_60d": 0.0,
                "industry_rps_20d": 50.0,
                "industry_rps_60d": 50.0,
                "industry_breadth_5d": 0.5,
                "industry_breadth_20d": 0.5,
                "industry_vol_20d": 0.0,
                "industry_vol_60d": 0.0,
                "stock_vs_industry_ret_5d": 0.0,
                "stock_vs_industry_ret_20d": 0.0,
                "stock_vs_industry_rank": 50.0,
                "dip_buy_signal_industry": 0.0,
                "industry_leader": 0.0,
            })
            continue

        s_ret5 = float(stock_ret5.get(code, 0.0)) if code in stock_ret5.index else 0.0
        s_ret20 = float(stock_ret20.get(code, 0.0)) if code in stock_ret20.index else 0.0
        i_ret5 = float(ind_ret5.get(ind, 0.0)) if ind in ind_ret5.index else 0.0
        i_ret20 = float(ind_ret20.get(ind, 0.0)) if ind in ind_ret20.index else 0.0
        i_ret60 = float(ind_ret60.get(ind, 0.0)) if ind in ind_ret60.index else 0.0

        vs_ind_5 = s_ret5 - i_ret5
        vs_ind_20 = s_ret20 - i_ret20

        # dip_buy_signal: long-term industry out-performance + short-term weakness
        dip = vs_ind_20 - abs(vs_ind_5) * 2.0
        dip = float(np.clip(dip, -1.0, 1.0))

        # industry_leader: strong industry × strong stock within it
        i_rps20 = float(ind_rps20.get(ind, 50.0)) if ind in ind_rps20.index else 50.0
        leader = (i_rps20 / 100.0) * (vs_ind_20 + 0.5)
        leader = float(np.clip(leader, 0.0, 1.0))

        rows.append({
            "stock_code": code,
            "industry_l1": ind_l1 or "",
            "industry_l2": ind,
            "industry_member_count": int(member_counts.get(ind, 0)),
            "industry_ret_5d": i_ret5,
            "industry_ret_20d": i_ret20,
            "industry_ret_60d": i_ret60,
            "industry_rps_20d": i_rps20,
            "industry_rps_60d": float(ind_rps60.get(ind, 50.0)) if ind in ind_rps60.index else 50.0,
            "industry_breadth_5d": float(breadth5.get(ind, 0.5)) if ind in breadth5.index else 0.5,
            "industry_breadth_20d": float(breadth20.get(ind, 0.5)) if ind in breadth20.index else 0.5,
            "industry_vol_20d": float(ind_vol20.get(ind, 0.0)) if ind in ind_vol20.index else 0.0,
            "industry_vol_60d": float(ind_vol60.get(ind, 0.0)) if ind in ind_vol60.index else 0.0,
            "stock_vs_industry_ret_5d": vs_ind_5,
            "stock_vs_industry_ret_20d": vs_ind_20,
            "stock_vs_industry_rank": 50.0,  # filled below
            "dip_buy_signal_industry": dip,
            "industry_leader": leader,
        })

    result = pd.DataFrame(rows)

    # ---- Within-industry rank ----
    for ind in result["industry_l2"].unique():
        if not ind:
            continue
        mask = result["industry_l2"] == ind
        n = mask.sum()
        if n >= 3:
            result.loc[mask, "stock_vs_industry_rank"] = (
                result.loc[mask, "stock_vs_industry_ret_20d"]
                .rank(pct=True) * 100.0
            ).fillna(50.0)
        else:
            result.loc[mask, "stock_vs_industry_rank"] = 50.0

    # Fill NaN
    numeric_cols = result.select_dtypes(include=[np.number]).columns
    result[numeric_cols] = result[numeric_cols].fillna(0.0)

    return result


# ---------------------------------------------------------------------------
# Batch computation for model training panel
# ---------------------------------------------------------------------------

def compute_industry_feature_panel(
    price_data: dict,
    stock_info_map: dict[str, dict],
    *,
    level: str = "l2",
) -> pd.DataFrame:
    """Compute industry features for every date in the price history.

    This is the batch version used during model training.  It produces a
    panel DataFrame with (trade_date, stock_code) MultiIndex so that every
    date has point-in-time-safe industry features.

    Parameters
    ----------
    price_data : dict
    stock_info_map : dict
    level : str

    Returns
    -------
    DataFrame with MultiIndex (trade_date, stock_code) and all industry
    feature columns.
    """
    price_matrix = _build_price_matrix(price_data)
    if price_matrix is None:
        return pd.DataFrame()

    stock_codes = list(price_matrix.columns)
    industry_map = _build_industry_map(stock_codes, stock_info_map, level=level)
    industry_l1_map = _build_industry_map(stock_codes, stock_info_map, level="l1")

    ret_matrix = price_matrix.pct_change().dropna(how="all")
    if ret_matrix.empty:
        return pd.DataFrame()

    ind_ret_df = _equal_weight_industry_returns(ret_matrix, industry_map)
    if ind_ret_df.empty:
        return pd.DataFrame()

    all_dates = ind_ret_df.index
    min_date = all_dates[60] if len(all_dates) > 60 else all_dates[0]

    panels = []
    for i, date in enumerate(all_dates):
        if date < min_date:
            continue
        # Slice data up to this date
        truncated = {c: price_matrix[c].loc[:date] for c in stock_codes if c in price_matrix.columns}
        date_features = compute_industry_features(truncated, stock_info_map, date=date, level=level)
        if date_features.empty:
            continue
        date_features["trade_date"] = date
        panels.append(date_features)

    if not panels:
        return pd.DataFrame()

    panel = pd.concat(panels, ignore_index=True)
    panel.set_index(["trade_date", "stock_code"], inplace=True)
    return panel
