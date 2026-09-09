#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Valuation, liquidity and financial factor sets."""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.catalog import FactorManifestEntry
from factor_engine.expressions.operators import safe_divide, ts_mean
from factor_engine.expressions.qlib_alpha import _prepare_qlib_frame
from factor_engine.registry import register_factor_set


VALUATION_HK_FEATURE_NAMES = [
    "valuation_pe",
    "valuation_pb",
    "valuation_ps",
    "valuation_ev_ebitda",
    "valuation_dividend_yield",
    "valuation_fcf_yield",
    "valuation_market_cap_log",
    "valuation_market_cap_age_td",
    "valuation_pe_age_td",
    "valuation_pb_age_td",
    "valuation_market_cap_is_stale",
    "valuation_pe_is_stale",
    "valuation_pb_is_stale",
    "valuation_observation_coverage",
    "liquidity_amount_ma20",
    "liquidity_amount_ma60",
    "liquidity_turnover_rate",
    "liquidity_amihud_illiq_20d",
    "liquidity_capacity_score",
]

FINANCIAL_QUALITY_HK_FEATURE_NAMES = [
    "financial_roe",
    "financial_roa",
    "financial_gross_margin",
    "financial_net_margin",
    "financial_operating_margin",
    "financial_ocf_to_net_income",
    "financial_debt_to_assets",
    "financial_current_ratio",
    "financial_interest_coverage",
    "financial_revenue_yoy",
    "financial_net_profit_yoy",
    "financial_eps_yoy",
    "financial_fcf_yield",
    "financial_quality_score",
    "financial_growth_score",
    "financial_safety_score",
    "financial_coverage_score",
]

FINANCIAL_CROSS_SECTION_HK_FEATURE_NAMES = [
    "pe_ind_pct",
    "pb_ind_pct",
    "ps_ind_pct",
    "ev_ebitda_ind_pct",
    "dividend_yield_ind_pct",
    "roe_ind_pct",
    "gross_margin_ind_pct",
    "debt_ratio_ind_pct",
    "revenue_yoy_ind_pct",
    "quality_value_score",
    "growth_quality_score",
]


def _latest_context_value(context, key):
    if context is None:
        return np.nan
    extra = getattr(context, "extra", None) or {}
    value = extra.get(key)
    return np.nan if value is None else value


def _constant(index, value):
    return pd.Series(pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0], index=index)


def _asof_context_series(index, context, key):
    """Return the point-in-time value from sparse valuation observations."""
    return _asof_valuation_alignment(index, context, key)[0]


def _asof_valuation_alignment(index, context, key):
    """Align a field and expose the observation's age in trading sessions.

    The sparse Baidu charts are observations, not daily values.  Values are
    carried only forward and their age is retained so models can distinguish
    a current valuation from an old one.  A latest stock-info snapshot is not
    used as a historical fallback because it would leak future information.
    """
    extra = getattr(context, "extra", None) or {} if context is not None else {}
    history = extra.get("valuation_history")
    if not history:
        return _constant(index, np.nan), _constant(index, np.nan)
    records = pd.DataFrame(history)
    if "trade_date" not in records or key not in records:
        return _constant(index, np.nan), _constant(index, np.nan)
    records["trade_date"] = pd.to_datetime(records["trade_date"], errors="coerce")
    records[key] = pd.to_numeric(records[key], errors="coerce")
    records = records.dropna(subset=["trade_date", key]).sort_values("trade_date")
    records = records.drop_duplicates("trade_date", keep="last")
    if records.empty:
        return _constant(index, np.nan), _constant(index, np.nan)
    target = pd.DataFrame({"_sample_date": pd.to_datetime(index, errors="coerce")})
    target["_row_order"] = range(len(target))
    ordered = target.sort_values("_sample_date")
    aligned = pd.merge_asof(
        ordered,
        records[["trade_date", key]].rename(columns={"trade_date": "_valuation_date"}),
        left_on="_sample_date",
        right_on="_valuation_date",
        direction="backward",
    )
    sample_dates = aligned["_sample_date"].to_numpy(dtype="datetime64[ns]")
    observation_dates = aligned["_valuation_date"].to_numpy(dtype="datetime64[ns]")
    age = np.full(len(aligned), np.nan, dtype=float)
    observed = ~pd.isna(observation_dates)
    # searchsorted gives the first available sample at/after the source date.
    # The difference is therefore the number of completed trading sessions.
    positions = np.arange(len(aligned), dtype=float)
    observed_positions = np.searchsorted(sample_dates, observation_dates[observed], side="left")
    age[observed] = np.maximum(0.0, positions[observed] - observed_positions)
    aligned["_valuation_age_td"] = age
    aligned = aligned.sort_values("_row_order")
    return (
        pd.Series(aligned[key].to_numpy(), index=index),
        pd.Series(aligned["_valuation_age_td"].to_numpy(), index=index),
    )


def _stale_flag(age, threshold):
    age = pd.to_numeric(age, errors="coerce")
    return pd.Series(np.where(age.notna(), (age > threshold).astype(float), np.nan), index=age.index)


def _score_good(series):
    return series.clip(lower=-5, upper=5)


@register_factor_set("valuation_hk")
class ValuationHKFactorSet(BaseFactorSet):
    """Valuation and liquidity features from persisted snapshots."""

    name = "valuation_hk"
    description = "HK valuation and liquidity factors from local snapshots"
    version = "0.1.0"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame(columns=VALUATION_HK_FEATURE_NAMES)
        close = qlib_frame["close"]
        volume = qlib_frame["volume"].replace(0, np.nan)
        amount = qlib_frame.get("amount", volume * qlib_frame["vwap"])
        amount = pd.to_numeric(amount, errors="coerce")
        ret_abs = (safe_divide(close, close.shift(1)) - 1.0).abs()
        market_cap, market_cap_age = _asof_valuation_alignment(close.index, context, "market_cap")
        pe_ratio, pe_age = _asof_valuation_alignment(close.index, context, "pe_ratio")
        pb_ratio, pb_age = _asof_valuation_alignment(close.index, context, "pb_ratio")
        stale_after_td = max(1, int(self.config.get("stale_after_trading_days", 20)))
        circulating_shares = _constant(close.index, _latest_context_value(context, "circulating_shares"))
        turnover_rate = qlib_frame.get("turnover")
        if turnover_rate is None or pd.to_numeric(turnover_rate, errors="coerce").notna().sum() == 0:
            turnover_rate = safe_divide(volume, circulating_shares) * 100.0
        else:
            turnover_rate = pd.to_numeric(turnover_rate, errors="coerce")

        columns = {
            "valuation_pe": pe_ratio,
            "valuation_pb": pb_ratio,
            "valuation_ps": _constant(close.index, _latest_context_value(context, "ps_ratio")),
            "valuation_ev_ebitda": _constant(close.index, _latest_context_value(context, "ev_ebitda")),
            "valuation_dividend_yield": _constant(close.index, _latest_context_value(context, "dividend_yield")),
            "valuation_fcf_yield": _constant(close.index, _latest_context_value(context, "fcf_yield")),
            "valuation_market_cap_log": np.log1p(market_cap.where(market_cap > 0)),
            "valuation_market_cap_age_td": market_cap_age,
            "valuation_pe_age_td": pe_age,
            "valuation_pb_age_td": pb_age,
            "valuation_market_cap_is_stale": _stale_flag(market_cap_age, stale_after_td),
            "valuation_pe_is_stale": _stale_flag(pe_age, stale_after_td),
            "valuation_pb_is_stale": _stale_flag(pb_age, stale_after_td),
            "valuation_observation_coverage": (
                market_cap.notna().astype(float) + pe_ratio.notna().astype(float) + pb_ratio.notna().astype(float)
            ) / 3.0,
            "liquidity_amount_ma20": ts_mean(amount, 20),
            "liquidity_amount_ma60": ts_mean(amount, 60),
            "liquidity_turnover_rate": turnover_rate,
            "liquidity_amihud_illiq_20d": ts_mean(safe_divide(ret_abs, amount), 20),
            "liquidity_capacity_score": np.log(ts_mean(amount, 60) + 1.0),
        }
        return pd.DataFrame(columns, index=close.index).reindex(columns=VALUATION_HK_FEATURE_NAMES)

    def metadata(self):
        manifest = [
            FactorManifestEntry(
                factor_id=name,
                factor_set=self.name,
                family="valuation_liquidity",
                source="valuation_snapshot_or_stock_info_registry",
                status="implemented",
                exactness="snapshot_latest_until_pit_panel_available",
                input_fields=("close", "volume", "vwap"),
                requires_pit=name.startswith("valuation_"),
                notes="Sparse valuation fields use backward as-of alignment; age and stale flags retain observation freshness.",
            ).to_dict()
            for name in VALUATION_HK_FEATURE_NAMES
        ]
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=(
                "Sparse valuation observations are aligned only backward in time.",
                "A valuation older than 20 trading sessions is marked stale; missingness remains explicit.",
            ),
            extra={
                "feature_count": len(VALUATION_HK_FEATURE_NAMES),
                "feature_names": VALUATION_HK_FEATURE_NAMES,
                "exactness": "snapshot_proxy",
                "manifest": manifest,
            },
        )


@register_factor_set("financial_quality_hk")
class FinancialQualityHKFactorSet(BaseFactorSet):
    """Financial statement quality, growth and safety factors."""

    name = "financial_quality_hk"
    description = "HK financial statement quality/growth/safety factors"
    version = "0.1.0"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame(columns=FINANCIAL_QUALITY_HK_FEATURE_NAMES)
        index = qlib_frame.index
        raw = {name: _constant(index, _latest_context_value(context, name.replace("financial_", ""))) for name in FINANCIAL_QUALITY_HK_FEATURE_NAMES}
        roe = raw["financial_roe"]
        roa = raw["financial_roa"]
        gross_margin = raw["financial_gross_margin"]
        net_margin = raw["financial_net_margin"]
        ocf_to_net_income = raw["financial_ocf_to_net_income"]
        debt_to_assets = raw["financial_debt_to_assets"]
        current_ratio = raw["financial_current_ratio"]
        interest_coverage = raw["financial_interest_coverage"]
        revenue_yoy = raw["financial_revenue_yoy"]
        net_profit_yoy = raw["financial_net_profit_yoy"]
        eps_yoy = raw["financial_eps_yoy"]

        coverage_inputs = [
            roe, roa, gross_margin, net_margin, ocf_to_net_income, debt_to_assets,
            current_ratio, interest_coverage, revenue_yoy, net_profit_yoy, eps_yoy,
        ]
        coverage = sum(series.notna().astype(float) for series in coverage_inputs) / float(len(coverage_inputs))
        quality_score = (
            _score_good(roe) + _score_good(roa) + _score_good(gross_margin)
            + _score_good(net_margin) + _score_good(ocf_to_net_income)
        ) / 5.0
        growth_score = (_score_good(revenue_yoy) + _score_good(net_profit_yoy) + _score_good(eps_yoy)) / 3.0
        safety_score = (_score_good(current_ratio) + _score_good(interest_coverage) - _score_good(debt_to_assets)) / 3.0

        columns = dict(raw)
        columns.update(
            {
                "financial_quality_score": quality_score,
                "financial_growth_score": growth_score,
                "financial_safety_score": safety_score,
                "financial_coverage_score": coverage,
            }
        )
        return pd.DataFrame(columns, index=index).reindex(columns=FINANCIAL_QUALITY_HK_FEATURE_NAMES)

    def metadata(self):
        manifest = [
            FactorManifestEntry(
                factor_id=name,
                factor_set=self.name,
                family="financial_statement",
                source="financial_statement_metrics",
                status="implemented",
                exactness="pit_required",
                requires_pit=True,
                notes="Requires persisted financial_statement_metrics context; no live download.",
            ).to_dict()
            for name in FINANCIAL_QUALITY_HK_FEATURE_NAMES
        ]
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=("Financial metrics must be filtered by available_at <= trade_date in production backtests.",),
            extra={
                "feature_count": len(FINANCIAL_QUALITY_HK_FEATURE_NAMES),
                "feature_names": FINANCIAL_QUALITY_HK_FEATURE_NAMES,
                "exactness": "pit_financial",
                "manifest": manifest,
            },
        )


@register_factor_set("financial_cross_section_hk")
class FinancialCrossSectionHKFactorSet(BaseFactorSet):
    """Industry-relative valuation and financial quality factors."""

    name = "financial_cross_section_hk"
    description = "HK industry-relative valuation/quality composite factors"
    version = "0.1.0"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame(columns=FINANCIAL_CROSS_SECTION_HK_FEATURE_NAMES)
        index = qlib_frame.index
        columns = {
            name: _constant(index, _latest_context_value(context, name))
            for name in FINANCIAL_CROSS_SECTION_HK_FEATURE_NAMES
        }
        return pd.DataFrame(columns, index=index).reindex(columns=FINANCIAL_CROSS_SECTION_HK_FEATURE_NAMES)

    def metadata(self):
        manifest = [
            FactorManifestEntry(
                factor_id=name,
                factor_set=self.name,
                family="financial_cross_section",
                source="local_industry_scoring",
                status="implemented",
                exactness="industry_relative_snapshot",
                requires_panel=True,
                requires_pit=True,
                notes="Computed from local valuation_snapshot/financial_statement_metrics cross-section; no live download.",
            ).to_dict()
            for name in FINANCIAL_CROSS_SECTION_HK_FEATURE_NAMES
        ]
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=(
                "Industry-relative percentiles are generated from the local stock universe available at feature generation time.",
                "Higher values mean more attractive valuation/quality unless noted by factor name.",
            ),
            extra={
                "feature_count": len(FINANCIAL_CROSS_SECTION_HK_FEATURE_NAMES),
                "feature_names": FINANCIAL_CROSS_SECTION_HK_FEATURE_NAMES,
                "exactness": "industry_relative_snapshot",
                "manifest": manifest,
            },
        )
