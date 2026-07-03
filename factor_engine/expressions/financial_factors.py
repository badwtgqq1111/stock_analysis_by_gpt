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
        amount = volume * qlib_frame["vwap"]
        ret_abs = (safe_divide(close, close.shift(1)) - 1.0).abs()
        market_cap = _constant(close.index, _latest_context_value(context, "market_cap"))
        circulating_shares = _constant(close.index, _latest_context_value(context, "circulating_shares"))
        turnover_rate = safe_divide(volume, circulating_shares) * 100.0

        columns = {
            "valuation_pe": _constant(close.index, _latest_context_value(context, "pe_ratio")),
            "valuation_pb": _constant(close.index, _latest_context_value(context, "pb_ratio")),
            "valuation_ps": _constant(close.index, _latest_context_value(context, "ps_ratio")),
            "valuation_ev_ebitda": _constant(close.index, _latest_context_value(context, "ev_ebitda")),
            "valuation_dividend_yield": _constant(close.index, _latest_context_value(context, "dividend_yield")),
            "valuation_fcf_yield": _constant(close.index, _latest_context_value(context, "fcf_yield")),
            "valuation_market_cap_log": np.log(market_cap + 1.0),
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
                notes="Uses local context values supplied by feature generation; no live download.",
            ).to_dict()
            for name in VALUATION_HK_FEATURE_NAMES
        ]
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=("Snapshot context should come from local warehouse, not live fetch during selection.",),
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
