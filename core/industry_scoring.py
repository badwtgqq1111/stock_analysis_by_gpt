#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Industry-standardized quality & valuation scoring.

Replaces the ``cluster_id``-based peer grouping in ``core/quality.py`` and
``core/sector_valuation.py`` with real industry classification from the
stock info registry.

Key design decisions
--------------------
- Peer group = ``industry_l2`` with ``industry_l1`` fallback when l2 has
  fewer than *min_peers* (default 5) members.
- When even l1 is too small, fall back to global (all-stock) ranking.
- Every output includes a ``_data_coverage`` field so the downstream
  pipeline can distinguish "low quality" from "no data".
- Valuation metric is chosen per industry (PE for growth/consumer, PB for
  financials, PS/EV_EBITDA where available).

Usage
-----
::

    from core.industry_scoring import (
        compute_industry_quality_scores,
        compute_industry_valuation_scores,
    )

    quality = compute_industry_quality_scores(
        raw_components, industry_map, min_peers=5,
    )
    valuation = compute_industry_valuation_scores(
        valuation_data, industry_map, min_peers=5,
    )
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rank_within_group(
    values: dict[str, float | None],
    groups: dict[str, str | None],
    *,
    min_peers: int = 5,
    higher_is_better: bool = True,
    fallback_l1: dict[str, str | None] | None = None,
) -> dict[str, float]:
    """Compute within-industry percentile rank for each stock.

    Parameters
    ----------
    values : dict
        ``{stock_code: raw_value}`` where None means missing.
    groups : dict
        ``{stock_code: industry_l2}`` primary peer group.
    min_peers : int
        Minimum peer count before falling back to l1 → global.
    higher_is_better : bool
        If True, percentile 100 = highest value.  If False, 100 = lowest.
    fallback_l1 : dict or None
        ``{stock_code: industry_l1}`` for first fallback.

    Returns
    -------
    dict ``{stock_code: percentile_0_100}`` (50 for missing / insufficient peers).
    """
    result: dict[str, float] = {}

    # Group stocks
    l2_groups: dict[str, list[str]] = {}
    for code, g in groups.items():
        if g:
            l2_groups.setdefault(g, []).append(code)

    l1_groups: dict[str, list[str]] = {}
    if fallback_l1:
        for code, g in fallback_l1.items():
            if g:
                l1_groups.setdefault(g, []).append(code)

    all_codes = list(values.keys())

    for code in all_codes:
        raw = values.get(code)
        if raw is None or (isinstance(raw, float) and np.isnan(raw)):
            result[code] = 50.0
            continue

        # Try l2 first
        group = groups.get(code)
        peers: list[str] | None = None
        if group and group in l2_groups:
            candidates = l2_groups[group]
            peer_vals = [
                v for c in candidates
                if (v := values.get(c)) is not None and not (isinstance(v, float) and np.isnan(v))
            ]
            if len(peer_vals) >= min_peers:
                peers = candidates

        # Fallback to l1
        if peers is None and fallback_l1:
            group_l1 = fallback_l1.get(code)
            if group_l1 and group_l1 in l1_groups:
                candidates = l1_groups[group_l1]
                peer_vals = [
                    v for c in candidates
                    if (v := values.get(c)) is not None and not (isinstance(v, float) and np.isnan(v))
                ]
                if len(peer_vals) >= min_peers:
                    peers = candidates

        # Global fallback
        if peers is None:
            peers = all_codes

        # Filter to stocks with valid values
        valid_peers = {
            c: values[c]
            for c in peers
            if c in values and values[c] is not None and not (isinstance(values[c], float) and np.isnan(values[c]))
        }
        if len(valid_peers) < 2:
            result[code] = 50.0
            continue

        peer_series = pd.Series(valid_peers)
        pct = peer_series.rank(pct=True).get(code, 0.5) * 100.0
        if not higher_is_better:
            pct = 100.0 - pct
        result[code] = float(np.clip(pct, 0.0, 100.0))

    return result


def _coverage_field(values: dict[str, float | None], codes: list[str]) -> float:
    """Fraction of *codes* that have a valid (non-None, non-NaN) value."""
    n = len(codes)
    if n == 0:
        return 0.0
    valid = sum(
        1 for c in codes
        if values.get(c) is not None and not (isinstance(values.get(c), float) and np.isnan(values.get(c)))
    )
    return valid / n


# ---------------------------------------------------------------------------
# Industry quality scoring
# ---------------------------------------------------------------------------

def compute_industry_quality_scores(
    raw_components: dict[str, dict[str, float | None]],
    industry_map: dict[str, str | None],
    industry_l1_map: dict[str, str | None] | None = None,
    *,
    min_peers: int = 5,
) -> pd.DataFrame:
    """Compute industry-standardized quality scores.

    Parameters
    ----------
    raw_components : dict
        ``{stock_code: {component_name: raw_value}}``.
        Expected components (all optional):

        - Profitability: ``roe``, ``roa``, ``gross_margin``, ``net_margin``,
          ``gross_profit_to_assets``, ``ocf_to_assets``
        - Growth: ``revenue_yoy``, ``profit_yoy``, ``roe_change``,
          ``gross_margin_change``
        - Safety: ``debt_ratio``, ``current_ratio``, ``interest_coverage``,
          ``profit_volatility``, ``price_volatility``, ``beta``
        - Payout: ``dividend_yield``, ``payout_ratio``, ``net_equity_issuance``,
          ``net_debt_issuance``

    industry_map : dict
        ``{stock_code: industry_l2}`` primary peer group.
    industry_l1_map : dict or None
        ``{stock_code: industry_l1}`` fallback peer group.
    min_peers : int
        Minimum peers per group before falling back.

    Returns
    -------
    DataFrame with columns:
        - ``stock_code``
        - ``quality_score`` (0–100, industry-standardized)
        - ``quality_data_coverage`` (0–1 fraction of components available)
        - ``quality_peer_group`` (which group was used: l2 / l1 / global)
    """
    stock_codes = list(raw_components.keys())

    # Collect per-component dicts
    component_dicts: dict[str, dict[str, float | None]] = {}
    component_names = set()
    for code, comps in raw_components.items():
        for name in comps:
            component_names.add(name)
        component_dicts[code] = dict(comps)

    # Direction for each component: +1 = higher is better, -1 = lower is better
    higher_better = {
        "roe": True, "roa": True, "gross_margin": True, "net_margin": True,
        "gross_profit_to_assets": True, "ocf_to_assets": True,
        "revenue_yoy": True, "profit_yoy": True, "roe_change": True,
        "gross_margin_change": True,
        "debt_ratio": False, "profit_volatility": False,
        "price_volatility": False, "beta": False,
        "current_ratio": True, "interest_coverage": True,
        "dividend_yield": True, "payout_ratio": True,
        "net_equity_issuance": False, "net_debt_issuance": False,
    }

    # Compute within-industry percentile for each component
    component_scores: dict[str, dict[str, float]] = {}
    for comp_name in sorted(component_names):
        vals = {c: component_dicts.get(c, {}).get(comp_name) for c in stock_codes}
        higher = higher_better.get(comp_name, True)
        component_scores[comp_name] = _rank_within_group(
            vals, industry_map,
            min_peers=min_peers,
            higher_is_better=higher,
            fallback_l1=industry_l1_map,
        )

    # Aggregate: equal-weight all available components per stock
    rows = []
    for code in stock_codes:
        scores = []
        for comp_name in component_names:
            s = component_scores.get(comp_name, {}).get(code, 50.0)
            if s != 50.0:  # Only count if we have real data
                scores.append(s)
        if not scores:
            scores = [50.0]

        quality_score = float(np.mean(scores))
        coverage = _coverage_field(
            {c: component_dicts.get(c, {}).get("roe") for c in stock_codes},
            [code],
        )
        # Full coverage: fraction of components present
        comp_coverage = (
            sum(1 for n in component_names if component_dicts.get(code, {}).get(n) is not None)
            / max(len(component_names), 1)
        )

        # Determine peer group
        group = industry_map.get(code)
        if group is None:
            group = industry_l1_map.get(code) if industry_l1_map else None
        peer_group = group or "global"

        rows.append({
            "stock_code": code,
            "quality_score": float(np.clip(quality_score, 0.0, 100.0)),
            "quality_data_coverage": float(comp_coverage),
            "quality_missing_fields": [
                n for n in sorted(component_names)
                if component_dicts.get(code, {}).get(n) is None
            ],
            "quality_peer_group": peer_group,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Industry valuation scoring
# ---------------------------------------------------------------------------

# Industry → preferred valuation metric
INDUSTRY_VALUATION_METRICS: dict[str, list[str]] = {
    "银行": ["pb_ratio"],
    "保险": ["pb_ratio", "pe_ratio"],
    "多元金融": ["pb_ratio"],
    "房地产": ["pb_ratio", "pe_ratio"],
    "公用事业": ["pb_ratio", "dividend_yield"],
    "能源": ["pb_ratio", "ev_ebitda"],
    "材料": ["pb_ratio", "pe_ratio"],
    "工业": ["pe_ratio", "pb_ratio"],
    "工用运输": ["pe_ratio", "pb_ratio"],
    "消费者服务": ["pe_ratio", "ps_ratio"],
    "媒体": ["pe_ratio", "ps_ratio"],
    "零售": ["pe_ratio", "ps_ratio"],
    "食品": ["pe_ratio"],
    "饮料": ["pe_ratio"],
    "制药": ["ps_ratio", "pe_ratio"],
    "生物科技": ["ps_ratio", "pe_ratio"],
    "医疗保健": ["ps_ratio", "pe_ratio"],
    "软件服务": ["ps_ratio", "pe_ratio"],
    "技术硬件": ["pe_ratio", "pb_ratio"],
    "半导体": ["ps_ratio", "pe_ratio"],
    "电信服务": ["pe_ratio", "dividend_yield"],
    "汽车": ["pe_ratio", "pb_ratio"],
    "家庭与个人用品": ["pe_ratio", "ps_ratio"],
    "商业和专业服务": ["pe_ratio", "pb_ratio"],
    "资本货物": ["pe_ratio", "pb_ratio"],
}

DEFAULT_VALUATION_METRICS = ["pe_ratio", "pb_ratio"]


def compute_industry_valuation_scores(
    valuation_data: dict[str, dict[str, float | None]],
    industry_map: dict[str, str | None],
    industry_l1_map: dict[str, str | None] | None = None,
    *,
    min_peers: int = 5,
) -> pd.DataFrame:
    """Compute industry-standardized valuation scores.

    Parameters
    ----------
    valuation_data : dict
        ``{stock_code: {metric: value}}`` where metric is one of
        ``pe_ratio``, ``pb_ratio``, ``ps_ratio``, ``dividend_yield``,
        ``ev_ebitda``.  Missing metrics are None.
    industry_map : dict
    industry_l1_map : dict or None
    min_peers : int

    Returns
    -------
    DataFrame with columns:
        - ``stock_code``
        - ``valuation_score`` (0–100, lower = cheaper relative to industry)
        - ``valuation_metric_used``
        - ``pe_percentile``, ``pb_percentile``, ``ps_percentile``
        - ``valuation_data_coverage``
        - ``valuation_peer_group``
    """
    stock_codes = list(valuation_data.keys())

    # For each metric, compute within-industry percentile (lower price = higher score)
    metric_scores: dict[str, dict[str, float]] = {}
    for metric in ["pe_ratio", "pb_ratio", "ps_ratio", "dividend_yield", "ev_ebitda"]:
        vals = {
            c: valuation_data.get(c, {}).get(metric) if isinstance(valuation_data.get(c), dict) else None
            for c in stock_codes
        }
        # For dividend_yield, higher is better
        higher = (metric == "dividend_yield")
        metric_scores[metric] = _rank_within_group(
            vals, industry_map,
            min_peers=min_peers,
            higher_is_better=higher,
            fallback_l1=industry_l1_map,
        )

    rows = []
    for code in stock_codes:
        vdata = valuation_data.get(code) or {}
        if isinstance(vdata, tuple):
            # Handle legacy (pe, pb) tuple format
            pe_val, pb_val = vdata
            vdata = {"pe_ratio": pe_val, "pb_ratio": pb_val}

        # Determine which metrics are available
        available = {}
        for m in ["pe_ratio", "pb_ratio", "ps_ratio", "dividend_yield", "ev_ebitda"]:
            v = vdata.get(m) if isinstance(vdata, dict) else None
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                available[m] = v

        # Pick preferred metric for this industry
        ind = industry_map.get(code)
        preferred = INDUSTRY_VALUATION_METRICS.get(ind or "", DEFAULT_VALUATION_METRICS)

        valuation_score: float
        metric_used: str
        if not available:
            valuation_score = 50.0
            metric_used = "none"
        else:
            # Use first available from preferred list
            chosen = None
            for m in preferred:
                if m in available:
                    chosen = m
                    break
            if chosen is None:
                chosen = next(iter(available))
            metric_used = chosen
            valuation_score = float(metric_scores.get(chosen, {}).get(code, 50.0))

        # Data coverage
        coverage = len(available) / 5.0

        # Peer group
        group = industry_map.get(code)
        if group is None and industry_l1_map:
            group = industry_l1_map.get(code)
        peer_group = group or "global"

        rows.append({
            "stock_code": code,
            "valuation_score": float(np.clip(valuation_score, 0.0, 100.0)),
            "valuation_metric_used": metric_used,
            "pe_percentile": float(metric_scores.get("pe_ratio", {}).get(code, 50.0)),
            "pb_percentile": float(metric_scores.get("pb_ratio", {}).get(code, 50.0)),
            "ps_percentile": float(metric_scores.get("ps_ratio", {}).get(code, 50.0)),
            "valuation_data_coverage": coverage,
            "valuation_peer_group": peer_group,
        })

    return pd.DataFrame(rows)
