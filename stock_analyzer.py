#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
股票技术分析模块 - 基于价格和成交量数据分析买卖点策略
"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
import warnings

from analyzer_core import StockAnalyzer
from data.ingest.service import MarketDataService
from reporting import (
    analyze_buy_points,
    analyze_target_date_alignment,
    build_strategy_comparison_tables,
    create_visualization_charts,
    format_table_for_console,
)
from strategy import BuyStrategy, CurrentStrategy, SellStrategy

warnings.filterwarnings("ignore")

TARGET_STOCKS = ['03633', '02706', '02015', '01860', '02432', '02590', '09866', '00020']

__all__ = [
    "StockAnalyzer",
    "BuyStrategy",
    "SellStrategy",
    "CurrentStrategy",
    "main",
    "main_all_hk",
    "main_factor_report",
    "main_review_batch",
    "main_strategy_suite",
    "analyze_single_stock_with_visualization",
    "create_visualization_charts",
    "run_cli",
]


def _safe_close_analyzer(analyzer):
    close_method = getattr(analyzer, "close", None)
    if callable(close_method):
        close_method()


def _format_factor_reason_lines(item):
    explanation = (item or {}).get("factor_explanation") or {}
    component_scores = explanation.get("component_scores") or {}
    component_weights = explanation.get("component_weights") or {}
    top_positive = explanation.get("top_positive_factors") or []
    if not explanation:
        return []

    lines = []
    lines.append(
        "  因子总分: "
        f"composite={component_scores.get('composite_score', float('nan')):.1f}, "
        f"trend={component_scores.get('trend_score', float('nan')):.1f}, "
        f"quality={component_scores.get('quality_score', float('nan')):.1f}, "
        f"risk={component_scores.get('risk_score', float('nan')):.1f}"
    )
    lines.append(
        "  组件权重: "
        f"trend={component_weights.get('trend_score', 0):.2f}, "
        f"quality={component_weights.get('quality_score', 0):.2f}, "
        f"risk={component_weights.get('risk_score', 0):.2f}"
    )
    if top_positive:
        factor_parts = []
        for factor in top_positive[:3]:
            factor_parts.append(
                f"{factor.get('factor')}("
                f"w={factor.get('weight', 0):.2f}, "
                f"score={factor.get('score', float('nan')):.1f}, "
                f"contrib={factor.get('weighted_contribution', float('nan')):.2f})"
            )
        lines.append("  主要因子: " + ", ".join(factor_parts))
    return lines


def _parse_horizons(raw_value):
    if raw_value is None:
        return (1, 5, 10, 20)
    if isinstance(raw_value, (tuple, list)):
        return tuple(int(item) for item in raw_value)
    values = []
    for chunk in str(raw_value).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        values.append(int(chunk))
    return tuple(values or [1, 5, 10, 20])


def _parse_signal_recipes(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (tuple, list)):
        return tuple(str(item).strip() for item in raw_value if str(item).strip())
    values = []
    for chunk in str(raw_value).split(","):
        chunk = chunk.strip()
        if chunk:
            values.append(chunk)
    return tuple(values) if values else None


def _build_current_factor_weight_table(score_config):
    rows = []
    config = score_config or {}
    component_weights = config.get("weights", {})
    for component_name in ("trend", "quality", "risk"):
        component_weight = float(component_weights.get(f"{component_name}_score", 0.0))
        for factor_name, rule in (config.get(component_name, {}) or {}).items():
            rows.append(
                {
                    "component": component_name,
                    "factor": factor_name,
                    "configured_factor_weight": float(rule.get("weight", 0.0)),
                    "configured_component_weight": component_weight,
                    "direction": "higher_is_better" if bool(rule.get("higher_is_better", True)) else "lower_is_better",
                }
            )
    return pd.DataFrame(rows)


def _merge_recommended_factor_weights(score_config, factor_scorecard):
    base_config = deepcopy(score_config or {})
    scorecard = factor_scorecard if isinstance(factor_scorecard, pd.DataFrame) else pd.DataFrame()
    if scorecard.empty or "component" not in scorecard.columns:
        return base_config
    factor_name_column = "factor" if "factor" in scorecard.columns else ("feature_name" if "feature_name" in scorecard.columns else None)
    if factor_name_column is None:
        return base_config

    # Track existing factor → component mapping from config
    existing_factor_component = {}  # factor_name → component_name
    for comp_name in base_config:
        if comp_name == "weights":
            continue
        for factor_name in (base_config.get(comp_name) or {}):
            existing_factor_component[factor_name] = comp_name

    component_names = sorted(set(scorecard["component"].dropna()))
    for component_name in component_names:
        component_rows = scorecard[scorecard["component"].fillna("") == component_name].copy()
        if component_rows.empty or "recommended_factor_weight" not in component_rows.columns:
            continue
        recommended = pd.to_numeric(component_rows["recommended_factor_weight"], errors="coerce")
        if recommended.notna().any() and recommended.fillna(0).sum() > 0:
            for _, row in component_rows.iterrows():
                factor_name = row.get(factor_name_column)
                if not factor_name:
                    continue
                weight_value = row.get("recommended_factor_weight")
                if pd.isna(weight_value):
                    continue
                higher_is_better = bool(row.get("higher_is_better", True))

                if factor_name in existing_factor_component:
                    # Factor already exists in config — update it in its ORIGINAL component
                    orig_component = existing_factor_component[factor_name]
                    base_config.setdefault(orig_component, {})
                    base_config[orig_component][factor_name]["weight"] = float(weight_value)
                else:
                    # Add new factor that Ridge identified as predictive
                    base_config.setdefault(component_name, {})
                    base_config[component_name][factor_name] = {
                        "weight": float(weight_value),
                        "higher_is_better": higher_is_better,
                    }
                    existing_factor_component[factor_name] = component_name

    # Ensure validated component has a weight entry
    if "validated" in base_config and base_config["validated"]:
        base_config.setdefault("weights", {})
        base_config["weights"].setdefault("validated_score", 0.15)

    return base_config


def _build_validation_cache_key(
    factor_set,
    validation_days,
    validation_horizons,
    validation_quantiles,
    validation_min_observations,
    validation_stock_codes,
    validation_factor_scope="all",
    validated_feature_names=None,
):
    stock_codes = list(validation_stock_codes or [])
    stock_code_hash = hashlib.sha1("\n".join(stock_codes).encode("utf-8")).hexdigest() if stock_codes else "none"
    validated_feature_names = [str(item) for item in (validated_feature_names or []) if str(item).strip()]
    identity = {
        "factor_set": factor_set,
        "validation_days": int(validation_days),
        "validation_horizons": [int(item) for item in validation_horizons],
        "validation_quantiles": int(validation_quantiles),
        "validation_min_observations": int(validation_min_observations),
        "stock_count": len(stock_codes),
        "stock_code_hash": stock_code_hash,
        "validation_factor_scope": str(validation_factor_scope),
        "validated_feature_names": validated_feature_names,
    }
    cache_key = hashlib.sha1(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return cache_key, identity


def _get_validation_cache_dir(analyzer):
    data_layout = getattr(analyzer, "data_layout", None)
    layer_path = getattr(data_layout, "layer_path", None)
    if callable(layer_path):
        return Path(layer_path("meta")) / "factor_weight_cache"
    return None


def _load_validation_weight_cache(cache_dir, cache_key):
    if cache_dir is None:
        return None
    cache_path = Path(cache_dir) / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["_cache_path"] = str(cache_path)
    return payload


def _write_validation_weight_cache(cache_dir, cache_key, payload):
    if cache_dir is None:
        return None
    cache_path = Path(cache_dir) / f"{cache_key}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return cache_path


def _fallback_classify_factor_name(name):
    text = str(name or "").strip().upper()
    if not text:
        return "validated"
    trend_prefixes = ("MA", "ROC", "MAX", "MIN", "RSV", "IMAX", "IMIN", "IMXD", "QTLU", "QTLD", "OPEN", "HIGH", "LOW", "CLOSE", "VWAP")
    quality_prefixes = ("VMA", "WVMA", "VSUMP", "VSUMN", "VSUMD", "CORR", "CORD", "CNTP", "CNTN", "CNTD", "SUMP", "SUMN", "SUMD", "RSQR", "RESI", "VOLUME", "KMID", "KLEN", "KUP", "KLOW", "KSFT")
    risk_prefixes = ("STD", "VSTD")
    if text.startswith(risk_prefixes):
        return "risk"
    if text.startswith(quality_prefixes):
        return "quality"
    if text.startswith(trend_prefixes):
        return "trend"
    return "validated"


def _sanitize_validation_scorecard(scorecard):
    working = scorecard.copy() if isinstance(scorecard, pd.DataFrame) else pd.DataFrame()
    if working.empty:
        return working

    for column in ("validation_score", "recommended_factor_weight", "configured_factor_weight", "ridge_coef", "abs_ridge_coef"):
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")
            working[column] = working[column].replace([np.inf, -np.inf], np.nan)

    if "component" in working.columns:
        try:
            from analyzer_core import classify_factor
        except ImportError:
            classify_factor = _fallback_classify_factor_name
        missing_mask = working["component"].isna() | (working["component"].astype(str).str.strip() == "")
        if missing_mask.any() and "feature_name" in working.columns:
            working.loc[missing_mask, "component"] = working.loc[missing_mask, "feature_name"].apply(classify_factor)
    return working


def _is_usable_validation_scorecard(scorecard):
    working = _sanitize_validation_scorecard(scorecard)
    if working.empty or "validation_score" not in working.columns:
        return False
    validation_score = pd.to_numeric(working["validation_score"], errors="coerce")
    finite = validation_score[np.isfinite(validation_score)]
    if finite.empty:
        return False
    return bool((finite > 0).any())


def _build_factor_scorecard(report):
    ic_summary = report.get("ic_summary")
    long_short_summary = report.get("long_short_summary")
    turnover_summary = report.get("turnover_summary")
    decay_summary = report.get("decay_summary")
    ic_summary = ic_summary if isinstance(ic_summary, pd.DataFrame) else pd.DataFrame()
    long_short_summary = long_short_summary if isinstance(long_short_summary, pd.DataFrame) else pd.DataFrame()
    turnover_summary = turnover_summary if isinstance(turnover_summary, pd.DataFrame) else pd.DataFrame()
    decay_summary = decay_summary if isinstance(decay_summary, pd.DataFrame) else pd.DataFrame()
    configured_weights = _build_current_factor_weight_table(
        (report.get("metadata") or {}).get("factor_score_config") or {}
    )

    base = pd.DataFrame(columns=["feature_name"])
    if not ic_summary.empty:
        base = (
            ic_summary.groupby(["feature_name"], dropna=False)
            .agg(
                mean_ic=("mean_ic", "mean"),
                mean_rank_ic=("mean_rank_ic", "mean"),
                ic_positive_rate=("ic_positive_rate", "mean"),
                rank_ic_positive_rate=("rank_ic_positive_rate", "mean"),
                ic_ir=("ic_ir", "mean"),
                rank_ic_ir=("rank_ic_ir", "mean"),
                horizons=("horizon", "nunique"),
            )
            .reset_index()
        )
    if not long_short_summary.empty:
        spread = (
            long_short_summary.groupby(["feature_name"], dropna=False)
            .agg(
                mean_spread=("mean_spread", "mean"),
                spread_ir=("spread_ir", "mean"),
                spread_positive_rate=("positive_rate", "mean"),
            )
            .reset_index()
        )
        base = spread if base.empty else base.merge(spread, on="feature_name", how="outer")
    if not turnover_summary.empty:
        turnover = (
            turnover_summary.groupby(["feature_name"], dropna=False)
            .agg(
                mean_turnover=("mean_turnover", "mean"),
                max_turnover=("max_turnover", "max"),
            )
            .reset_index()
        )
        base = turnover if base.empty else base.merge(turnover, on="feature_name", how="outer")
    if not decay_summary.empty:
        decay = (
            decay_summary.groupby(["feature_name"], dropna=False)
            .agg(
                ic_decay_ratio=("ic_decay_ratio", "mean"),
                rank_ic_decay_ratio=("rank_ic_decay_ratio", "mean"),
                spread_decay_ratio=("spread_decay_ratio", "mean"),
            )
            .reset_index()
        )
        base = decay if base.empty else base.merge(decay, on="feature_name", how="outer")

    if base.empty:
        base = pd.DataFrame(columns=["feature_name"])
    if not configured_weights.empty:
        base = base.merge(configured_weights, left_on="feature_name", right_on="factor", how="left")
        if "factor" in base.columns:
            base.drop(columns=["factor"], inplace=True)

    for column in [
        "mean_ic",
        "mean_rank_ic",
        "ic_positive_rate",
        "rank_ic_positive_rate",
        "ic_ir",
        "rank_ic_ir",
        "mean_spread",
        "spread_ir",
        "spread_positive_rate",
        "mean_turnover",
        "ic_decay_ratio",
        "rank_ic_decay_ratio",
        "spread_decay_ratio",
    ]:
        if column not in base.columns:
            base[column] = pd.NA

    def _safe_value(value):
        return float(value) if pd.notna(value) else 0.0

    def _validation_score(row):
        metric_columns = [
            "mean_rank_ic",
            "mean_ic",
            "mean_spread",
            "rank_ic_positive_rate",
            "ic_positive_rate",
            "mean_turnover",
        ]
        if all(pd.isna(row.get(column)) for column in metric_columns):
            return 0.0
        turnover_bonus = (
            max(0.0, 1.0 - float(row["mean_turnover"])) * 5.0
            if pd.notna(row.get("mean_turnover"))
            else 0.0
        )
        return (
            abs(_safe_value(row["mean_rank_ic"])) * 35.0
            + abs(_safe_value(row["mean_ic"])) * 20.0
            + max(_safe_value(row["mean_spread"]), 0.0) * 100.0 * 20.0
            + _safe_value(row["rank_ic_positive_rate"]) * 15.0
            + _safe_value(row["ic_positive_rate"]) * 5.0
            + turnover_bonus
        )

    base["validation_score"] = base.apply(_validation_score, axis=1)

    # Auto-classify factors if component missing
    if "component" not in base.columns or base["component"].isna().all():
        from analyzer_core import classify_factor
        base["component"] = base["feature_name"].apply(classify_factor)

    base.sort_values(["validation_score", "mean_rank_ic", "mean_spread"], ascending=False, inplace=True)
    base.reset_index(drop=True, inplace=True)

    base["recommended_factor_weight"] = pd.NA
    component_names = sorted(set(base["component"].dropna())) if "component" in base.columns else []
    if not component_names:
        component_names = ["trend", "quality", "risk"]
    for component_name in component_names:
        mask = base["component"].fillna("") == component_name
        if mask.any():
            total = pd.to_numeric(base.loc[mask, "validation_score"], errors="coerce").clip(lower=0).sum()
            if total > 0:
                base.loc[mask, "recommended_factor_weight"] = (
                    pd.to_numeric(base.loc[mask, "validation_score"], errors="coerce").clip(lower=0) / total
                )
            else:
                base.loc[mask, "recommended_factor_weight"] = pd.NA

    return base


def _build_factor_scorecard_ridge(report, ridge_alpha=1.0, target_horizon=5):
    """用 Ridge 回归估计因子权重，替代硬编码线性打分。

    小因子集 (p <= n/2): 逐日做横截面回归，系数时序均值作为 validation_score。
    大因子集 (p > n/2): 堆叠面板回归，一次性拟合，速度更快且 p>n 时更稳定。
    validation_frame 不可用时回退到原公式。
    """
    try:
        from sklearn.linear_model import Ridge
    except ImportError:
        return _build_factor_scorecard(report)

    validation_frame = report.get("validation_frame")
    if validation_frame is None or validation_frame.empty:
        return _build_factor_scorecard(report)

    target_col = f"forward_return_{int(target_horizon)}"
    if target_col not in validation_frame.columns:
        return _build_factor_scorecard(report)

    pivot = validation_frame.pivot_table(
        index=["trade_date", "stock_code"],
        columns="feature_name",
        values="feature_value",
        aggfunc="last",
    )
    returns = validation_frame.groupby(["trade_date", "stock_code"])[target_col].last()
    common_idx = pivot.index.intersection(returns.index)
    if len(common_idx) == 0:
        return _build_factor_scorecard(report)
    pivot = pivot.loc[common_idx]
    returns = returns.loc[common_idx]

    feature_names = list(pivot.columns)
    K = len(feature_names)
    if K < 2:
        return _build_factor_scorecard(report)

    dates = list(pivot.index.get_level_values("trade_date").unique())
    avg_stocks = len(common_idx) // max(len(dates), 1)

    # --- 堆叠面板回归：逐日去均值 + 全量一次性 Ridge，适合 p > n/2 ---
    stacked_rows = []
    for trade_date in dates:
        X = pivot.xs(trade_date, level="trade_date").dropna()
        y = returns.xs(trade_date, level="trade_date").loc[X.index]
        common = X.index.intersection(y.dropna().index)
        if len(common) < max(5, 2):
            continue
        X = X.loc[common]
        y = y.loc[common].astype(float)
        X_mean = X.mean()
        X_std = X.std().replace(0, 1)
        y_mean = y.mean()
        X_scaled = (X - X_mean) / X_std
        y_centered = y - y_mean
        stacked_rows.append((X_scaled, y_centered))

    if not stacked_rows:
        return _build_factor_scorecard(report)

    X_all = pd.concat([r[0] for r in stacked_rows], axis=0)
    y_all = pd.concat([r[1] for r in stacked_rows], axis=0)

    model = Ridge(alpha=ridge_alpha, fit_intercept=False)
    model.fit(X_all.values.astype(np.float64), y_all.values.astype(np.float64))
    coefs = model.coef_

    rows = []
    for name, coef in zip(feature_names, coefs):
        rows.append({
            "feature_name": name,
            "ridge_coef": float(coef),
            "ridge_panel": True,
        })

    scorecard = pd.DataFrame(rows)
    scorecard["abs_ridge_coef"] = scorecard["ridge_coef"].abs()
    scorecard["higher_is_better"] = scorecard["ridge_coef"].fillna(0) > 0
    scorecard.sort_values("abs_ridge_coef", ascending=False, inplace=True)
    scorecard.reset_index(drop=True, inplace=True)

    # Auto-classify all factors so every factor gets a component assignment
    from analyzer_core import classify_factor
    scorecard["component"] = scorecard["feature_name"].apply(classify_factor)

    configured_weights = _build_current_factor_weight_table(
        (report.get("metadata") or {}).get("factor_score_config") or {}
    )
    if not configured_weights.empty:
        # Preserve original component from config if it exists (override auto-classify)
        original_component_map = {}
        for _, cw_row in configured_weights.iterrows():
            fn = cw_row.get("factor")
            comp = cw_row.get("component")
            if fn and comp:
                original_component_map[fn] = comp
        if original_component_map:
            scorecard["component"] = scorecard.apply(
                lambda r: original_component_map.get(r["feature_name"], r["component"]), axis=1
            )

        # Drop overlapping columns from configured_weights to avoid _x/_y suffix
        overlap = [c for c in configured_weights.columns if c in scorecard.columns and c not in ("factor", "feature_name")]
        if overlap:
            configured_weights = configured_weights.drop(columns=overlap)
        scorecard = scorecard.merge(configured_weights, left_on="feature_name", right_on="factor", how="left")
        if "factor" in scorecard.columns:
            scorecard.drop(columns=["factor"], inplace=True)

    # --- Multi-dimensional validation_score ---
    # Default: abs Ridge coefficient
    scorecard["validation_score"] = scorecard["abs_ridge_coef"]
    scorecard["validation_score_components"] = "ridge_only"

    # Try to incorporate IC, Fama-MacBeth, monotonicity if available
    ic_summary = report.get("ic_summary")
    fm_result = report.get("fm_result")
    monotonicity = report.get("monotonicity")

    if ic_summary is not None and not ic_summary.empty:
        # Merge |mean_rank_ic| from ic_summary (shortest horizon per factor)
        ic_rank = ic_summary.copy()
        if "horizon" in ic_rank.columns:
            ic_rank = ic_rank.sort_values("horizon").groupby("feature_name").first().reset_index()
        if "mean_rank_ic" in ic_rank.columns:
            scorecard = scorecard.merge(
                ic_rank[["feature_name", "mean_rank_ic"]].rename(columns={"mean_rank_ic": "_mean_rank_ic"}),
                on="feature_name", how="left",
            )

    if fm_result is not None and not fm_result.empty and "fm_tstat" in fm_result.columns:
        scorecard = scorecard.merge(
            fm_result[["feature_name", "fm_tstat", "fm_pvalue"]].rename(
                columns={"fm_tstat": "_fm_tstat", "fm_pvalue": "_fm_pvalue"}
            ),
            on="feature_name", how="left",
        )

    if monotonicity is not None and not monotonicity.empty and "monotonicity_score" in monotonicity.columns:
        scorecard = scorecard.merge(
            monotonicity[["feature_name", "monotonicity_score"]].rename(
                columns={"monotonicity_score": "_monotonicity_score"}
            ),
            on="feature_name", how="left",
        )

    # Build composite if auxiliary metrics are available
    aux_cols_present = [c for c in ["_mean_rank_ic", "_fm_tstat", "_monotonicity_score"] if c in scorecard.columns]
    if len(aux_cols_present) >= 2:
        def _minmax_norm(s):
            clean = pd.to_numeric(s, errors="coerce")
            mn, mx = clean.min(), clean.max()
            if pd.isna(mn) or pd.isna(mx) or mx == mn:
                return pd.Series(0.5, index=s.index)
            return (clean - mn) / (mx - mn)

        score_components = {"_ridge_norm": _minmax_norm(scorecard["abs_ridge_coef"]) * 0.40}

        if "_mean_rank_ic" in scorecard.columns:
            score_components["_ic_norm"] = _minmax_norm(scorecard["_mean_rank_ic"].abs()) * 0.15
        else:
            score_components["_ridge_norm"] += 0.15

        if "_fm_tstat" in scorecard.columns:
            score_components["_fm_norm"] = _minmax_norm(scorecard["_fm_tstat"].abs().clip(upper=5)) * 0.20
        else:
            score_components["_ridge_norm"] += 0.20

        if "_monotonicity_score" in scorecard.columns:
            score_components["_mono_norm"] = _minmax_norm(scorecard["_monotonicity_score"]) * 0.15
        else:
            score_components["_ridge_norm"] += 0.15

        # rank_autocorr placeholder (0.10 redistributed to Ridge if not available)
        score_components["_ridge_norm"] += 0.10

        composite = sum(score_components.values())
        scorecard["validation_score"] = composite.clip(lower=0)
        scorecard["validation_score_components"] = "+".join(score_components.keys())

    scorecard["recommended_factor_weight"] = pd.NA
    component_names = sorted(set(scorecard["component"].dropna()))
    for component_name in component_names:
        mask = scorecard["component"].fillna("") == component_name
        if mask.any():
            total = pd.to_numeric(scorecard.loc[mask, "validation_score"], errors="coerce").clip(lower=0).sum()
            if total > 0:
                scorecard.loc[mask, "recommended_factor_weight"] = (
                    pd.to_numeric(scorecard.loc[mask, "validation_score"], errors="coerce").clip(lower=0) / total
                )

    return scorecard


def main():
    """主函数 - 固定8股票池的3个月收益导向策略分析"""
    print("=" * 80)
    print("港股技术分析系统 - 8股票池三个月收益优化")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        print(f"[INFO] 固定分析股票池: {', '.join(TARGET_STOCKS)}")

        portfolio_result = analyzer.backtest_portfolio(TARGET_STOCKS, days=365, top_n=3)
        if portfolio_result is None:
            print("[ERROR] 组合分析失败")
            return

        analysis_results = portfolio_result['analysis_results']
        strategy = analyzer.generate_trading_strategy(analysis_results)

        print(f"\n[INFO] 成功分析 {len(analysis_results)} 只股票")
        print(f"[INFO] 组合预计持有 Top {portfolio_result['top_n']} 只股票")
        print(f"[INFO] 组合估算收益率: {portfolio_result['estimated_portfolio_return']:.1f}%")
        print(f"[INFO] 组合估算胜率: {portfolio_result['estimated_portfolio_win_rate']:.1f}%")
        print(f"[INFO] 组合估算交易次数: {portfolio_result['estimated_trade_count']}")

        if strategy:
            print("\n" + "=" * 80)
            print("8股票池三个月收益策略报告")
            print("=" * 80)

            print("\n当前股票排名:")
            for i, stock in enumerate(strategy['ranked_stocks'], 1):
                signal_flag = '强买点' if stock.get('current_signal_active') and stock.get('current_signal_actionable') else ('观察名单' if stock.get('current_signal_active') else '无新信号')
                signal_score = stock.get('current_signal_score')
                signal_score_text = f"{signal_score:.1f}" if pd.notna(signal_score) else 'None'
                print(
                    f"{i:2d}. {stock['stock_code']} - 排名分: {stock['ranking_score']:.1f}, "
                    f"当前信号: {signal_flag}, 信号评分: {signal_score_text}, "
                    f"最新预期3月评分: {stock['expected_3m_score']:.1f}, "
                    f"矩阵评分: {stock['matrix_score']:.1f}, 趋势评分: {stock['regime_score']:.1f}, "
                    f"回测收益: {stock['total_return']:.1f}%, 入场类型: {stock['entry_type']}, 信号层级: {stock.get('signal_tier')}"
                )

            print("\n当前建议持有:")
            for item in portfolio_result['selected']:
                signal_flag = '强买点' if item.get('current_signal_active') and item.get('current_signal_actionable') else ('观察名单' if item.get('current_signal_active') else '评分候选')
                signal_score = item.get('current_signal_score')
                signal_score_text = f"{signal_score:.1f}" if pd.notna(signal_score) else 'None'
                print(
                    f"- {item['stock_code']} - {signal_flag}, 排名分 {item['ranking_score']:.1f}, "
                    f"信号评分 {signal_score_text}, 建议买点 {item['entry_type']}, 信号层级 {item.get('signal_tier')}, "
                    f"单股回测收益 {item['backtest_return']:.1f}%"
                )

            if portfolio_result.get('watchlist'):
                print("\n观察名单:")
                for item in portfolio_result['watchlist']:
                    print(
                        f"- {item['stock_code']} - 入场类型 {item['entry_type']}, 信号层级 {item.get('signal_tier')}, "
                        f"预期3月评分 {item.get('expected_3m_score', 0):.1f}, 趋势评分 {item.get('regime_score', 0):.1f}"
                    )

            print("\n风险管理:")
            risk = strategy['recommended_strategy']['risk_management']
            print(f"- 仓位: {risk['max_position_size']}")
            print(f"- 止损: {risk['stop_loss']}")
            print(f"- 止盈: {risk['take_profit']}")
            print(f"- 最大日交易数: {risk['max_daily_trades']}")
            print(f"- 默认持有周期: {risk['holding_horizon']} 个交易日")

        print("\n" + "=" * 80)
        print("分析完成！")
        print("=" * 80)
    finally:
        _safe_close_analyzer(analyzer)


def main_validate_factors(
    days=365,
    factor_set="qlib_alpha158",
    max_workers=1,
    show_progress=False,
    validation_horizons=(1, 5, 10, 20),
    validation_quantiles=5,
    validation_min_observations=5,
    validation_stock_limit=None,
    validation_factor_scope="scoring_only",
    refresh_recommended_factor_weights=False,
    export_csv=None,
):
    """独立因子验证：只跑验证流水线，产出权重缓存和因子记分卡，不选股。"""
    print("=" * 80)
    print("港股技术分析系统 - 因子验证（独立模式）")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        validation_stock_codes = analyzer.get_all_stocks()
        if validation_stock_limit is not None:
            validation_stock_codes = validation_stock_codes[: max(int(validation_stock_limit), 0)]
        validated_feature_names = None
        effective_scope = validation_factor_scope or "scoring_only"
        if effective_scope == "scoring_only":
            validated_feature_names = analyzer.get_score_factor_names()

        cache_key, cache_identity = _build_validation_cache_key(
            factor_set=factor_set,
            validation_days=days,
            validation_horizons=validation_horizons,
            validation_quantiles=validation_quantiles,
            validation_min_observations=validation_min_observations,
            validation_stock_codes=validation_stock_codes,
            validation_factor_scope=effective_scope,
            validated_feature_names=validated_feature_names,
        )
        cache_dir = _get_validation_cache_dir(analyzer)
        cached_payload = None
        if not refresh_recommended_factor_weights:
            cached_payload = _load_validation_weight_cache(cache_dir, cache_key)

        if cached_payload is not None:
            candidate_scorecard = _sanitize_validation_scorecard(
                pd.DataFrame(cached_payload.get("factor_scorecard") or [])
            )
            if _is_usable_validation_scorecard(candidate_scorecard):
                validation_scorecard = candidate_scorecard
                print(
                    f"[INFO] 已命中验证权重缓存: key={cache_key}, "
                    f"path={cached_payload.get('_cache_path')}"
                )
            else:
                print(
                    f"[WARN] 验证权重缓存已失效，自动重算: key={cache_key}"
                )
                cached_payload = None

        if cached_payload is None:
            if show_progress:
                print(
                    f"[PROGRESS] validation phase=features "
                    f"stocks={len(validation_stock_codes)} workers={max_workers} factor_set={factor_set} "
                    f"scope={effective_scope}"
                )
            validation_report = analyzer.build_factor_validation_report(
                stock_codes=validation_stock_codes,
                days=days,
                factor_set=factor_set,
                horizons=validation_horizons,
                quantiles=validation_quantiles,
                min_observations=validation_min_observations,
                max_workers=max_workers,
                show_progress=show_progress,
                validation_factor_scope=effective_scope,
                validated_feature_names=validated_feature_names,
            )
            if validation_report is None:
                print("[ERROR] 因子验证失败")
                return None
            validation_scorecard = _build_factor_scorecard_ridge(validation_report)
            factor_score_config = _merge_recommended_factor_weights(
                (validation_report.get("metadata") or {}).get("factor_score_config"),
                validation_scorecard,
            )
            cache_payload = {
                "cache_key": cache_key,
                "identity": cache_identity,
                "factor_score_config": factor_score_config,
                "factor_scorecard": validation_scorecard.to_dict(orient="records"),
                "created_at": pd.Timestamp.utcnow().isoformat(),
            }
            cache_path = _write_validation_weight_cache(cache_dir, cache_key, cache_payload)
            if cache_path is not None:
                print(f"[OK] 已写入验证权重缓存: {cache_path}")

        if not validation_scorecard.empty:
            validation_scorecard = _sanitize_validation_scorecard(validation_scorecard)
            preview_columns = [
                "feature_name",
                "component",
                "configured_factor_weight",
                "recommended_factor_weight",
                "validation_score",
            ]
            preview_columns = [c for c in preview_columns if c in validation_scorecard.columns]
            print(validation_scorecard[preview_columns].head(10).to_string(index=False))

        if export_csv:
            export_path = Path(export_csv)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            scorecard_path = export_path.with_name(f"{export_path.stem}_scorecard.csv")
            validation_scorecard.to_csv(scorecard_path, index=False, encoding="utf-8-sig")
            print(f"[OK] 已导出因子记分卡: {scorecard_path}")
    finally:
        _safe_close_analyzer(analyzer)

    print("\n" + "=" * 80)
    print("因子验证完成！")
    print("=" * 80)
    return {"cache_key": cache_key, "scorecard": validation_scorecard}


def main_select_stocks(
    days=365,
    top_n=10,
    initial_capital=100000,
    export_csv=None,
    persist_signals=False,
    batch_id=None,
    max_workers=1,
    analysis_mode="factor",
    factor_set="qlib_alpha158",
    show_progress=False,
    fast_mode=False,
    validation_days=None,
    validation_horizons=(1, 5, 10, 20),
    validation_quantiles=5,
    validation_min_observations=5,
    validation_stock_limit=None,
    validation_factor_scope="scoring_only",
    signal_recipes=None,
):
    """从验证权重缓存读取推荐权重，执行全港股 TopN 选股+回测，不跑因子验证。"""
    print("=" * 80)
    print(f"港股技术分析系统 - 全港股 Top {top_n} 组合筛选（基于验证权重）")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        effective_scope = validation_factor_scope or "scoring_only"
        validation_stock_codes = analyzer.get_all_stocks()
        if validation_stock_limit is not None:
            validation_stock_codes = validation_stock_codes[: max(int(validation_stock_limit), 0)]
        validated_feature_names = None
        if effective_scope == "scoring_only":
            validated_feature_names = analyzer.get_score_factor_names()
        effective_validation_days = validation_days or days

        cache_key, _ = _build_validation_cache_key(
            factor_set=factor_set,
            validation_days=effective_validation_days,
            validation_horizons=validation_horizons,
            validation_quantiles=validation_quantiles,
            validation_min_observations=validation_min_observations,
            validation_stock_codes=validation_stock_codes,
            validation_factor_scope=effective_scope,
            validated_feature_names=validated_feature_names,
        )
        cache_dir = _get_validation_cache_dir(analyzer)
        cached_payload = _load_validation_weight_cache(cache_dir, cache_key)

        if cached_payload is None:
            print(
                f"[ERROR] 未找到验证权重缓存: key={cache_key}\n"
                f"  请先运行 validate_factors 生成权重缓存，再运行 select_stocks。"
            )
            return None

        candidate_scorecard = _sanitize_validation_scorecard(
            pd.DataFrame(cached_payload.get("factor_scorecard") or [])
        )
        if not _is_usable_validation_scorecard(candidate_scorecard):
            print(f"[ERROR] 验证权重缓存已失效: key={cache_key}\n  请重新运行 validate_factors。")
            return None

        factor_score_config = deepcopy(cached_payload.get("factor_score_config") or {})
        if not factor_score_config:
            print(f"[ERROR] 缓存中缺少有效权重配置: key={cache_key}")
            return None

        print(f"[INFO] 已读取验证权重缓存: key={cache_key}, path={cached_payload.get('_cache_path')}")

        ridge_factors = None
        if effective_scope == "all" and not candidate_scorecard.empty:
            ridge_factors = StockAnalyzer._select_top_ridge_factors(candidate_scorecard, top_k=30)
            if show_progress and ridge_factors is not None and not ridge_factors.empty:
                print(
                    f"[PROGRESS] ridge_factors selected top_k={len(ridge_factors)} "
                    f"components={ridge_factors['component'].value_counts().to_dict()}"
                )

        backtest_kwargs = {
            "days": days,
            "top_n": top_n,
            "initial_capital": initial_capital,
            "max_workers": max_workers,
            "analysis_mode": analysis_mode,
            "factor_set": factor_set,
            "factor_score_config": factor_score_config,
            "show_progress": show_progress,
            "enable_portfolio_replay": not fast_mode,
        }
        if signal_recipes is not None:
            backtest_kwargs["signal_recipes"] = signal_recipes
        if ridge_factors is not None:
            backtest_kwargs["ridge_factors"] = ridge_factors
        portfolio_result = analyzer.backtest_hk_market(**backtest_kwargs)
    finally:
        _safe_close_analyzer(analyzer)

    if portfolio_result is None:
        print("[ERROR] 全港股组合分析失败")
        return None

    analysis_results = portfolio_result.get("analysis_results", [])
    print(f"\n[INFO] 成功分析 {len(analysis_results)} 只股票")
    print(f"[INFO] 组合预计持有 Top {portfolio_result['top_n']} 只股票")
    print(f"[INFO] 组合估算收益率: {portfolio_result['estimated_portfolio_return']:.1f}%")
    print(f"[INFO] 组合估算胜率: {portfolio_result['estimated_portfolio_win_rate']:.1f}%")
    print(f"[INFO] 组合估算交易次数: {portfolio_result['estimated_trade_count']}")

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        ranking_path = export_path.with_name(f"{export_path.stem}_ranking.csv")
        selected_path = export_path.with_name(f"{export_path.stem}_selected.csv")
        watchlist_path = export_path.with_name(f"{export_path.stem}_watchlist.csv")

        pd.DataFrame(portfolio_result.get("ranking", [])).to_csv(ranking_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(portfolio_result.get("selected", [])).to_csv(selected_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(portfolio_result.get("watchlist", [])).to_csv(watchlist_path, index=False, encoding="utf-8-sig")

        print(f"[OK] 已导出全市场排名: {ranking_path}")
        print(f"[OK] 已导出当前持有: {selected_path}")
        print(f"[OK] 已导出观察名单: {watchlist_path}")

    if persist_signals:
        service = MarketDataService()
        try:
            persist_result = service.persist_portfolio_result(
                portfolio_result=portfolio_result,
                market="HK",
                signal_set="all_hk_topn",
                strategy_name="all_hk_topn",
                batch_id=batch_id,
                source="stock_analyzer_cli",
            )
        finally:
            service.close()
        print(f"[OK] 已写入 signal 层: batch_id={persist_result['batch_id']}, rows={persist_result['signal_rows']}")

    print("\n当前建议持有:")
    for item in portfolio_result.get("selected", []):
        print(f"- {item['stock_code']}")
        for line in _format_factor_reason_lines(item):
            print(line)

    print("\n" + "=" * 80)
    print("全港股 TopN 分析完成！")
    print("=" * 80)
    return portfolio_result


def main_all_hk(
    days=365,
    top_n=10,
    initial_capital=100000,
    export_csv=None,
    persist_signals=False,
    batch_id=None,
    max_workers=1,
    analysis_mode="factor",
    factor_set="qlib_alpha158",
    show_progress=False,
    fast_mode=False,
    validation_days=None,
    validation_horizons=(1, 5, 10, 20),
    validation_quantiles=5,
    validation_min_observations=5,
    validation_stock_limit=None,
    use_recommended_factor_weights=False,
    refresh_recommended_factor_weights=False,
    validation_factor_scope="scoring_only",
    signal_recipes=None,
):
    """对本地已同步的全部港股执行 TopN 组合分析（兼容旧接口：验证+选股一次完成）。"""
    print("=" * 80)
    print(f"港股技术分析系统 - 全港股 Top {top_n} 组合筛选")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        factor_score_config = None
        if analysis_mode == "factor" and use_recommended_factor_weights:
            effective_validation_factor_scope = validation_factor_scope or "scoring_only"
            validation_stock_codes = analyzer.get_all_stocks()
            if validation_stock_limit is not None:
                validation_stock_codes = validation_stock_codes[: max(int(validation_stock_limit), 0)]
            effective_validation_days = validation_days or days
            validated_feature_names = None
            if effective_validation_factor_scope == "scoring_only":
                validated_feature_names = analyzer.get_score_factor_names()
            cache_key, cache_identity = _build_validation_cache_key(
                factor_set=factor_set,
                validation_days=effective_validation_days,
                validation_horizons=validation_horizons,
                validation_quantiles=validation_quantiles,
                validation_min_observations=validation_min_observations,
                validation_stock_codes=validation_stock_codes,
                validation_factor_scope=effective_validation_factor_scope,
                validated_feature_names=validated_feature_names,
            )
            cache_dir = _get_validation_cache_dir(analyzer)
            cached_payload = None
            if not refresh_recommended_factor_weights:
                cached_payload = _load_validation_weight_cache(cache_dir, cache_key)

            if cached_payload is not None:
                candidate_scorecard = _sanitize_validation_scorecard(
                    pd.DataFrame(cached_payload.get("factor_scorecard") or [])
                )
                if _is_usable_validation_scorecard(candidate_scorecard):
                    factor_score_config = deepcopy(cached_payload.get("factor_score_config") or {})
                    validation_scorecard = candidate_scorecard
                    print(
                        f"[INFO] 已命中验证权重缓存: key={cache_key}, "
                        f"path={cached_payload.get('_cache_path')}"
                    )
                else:
                    print(
                        f"[WARN] 验证权重缓存已失效，自动重算: key={cache_key}, "
                        f"path={cached_payload.get('_cache_path')}"
                    )
                    cached_payload = None

            if cached_payload is None:
                if show_progress:
                    print(
                        f"[PROGRESS] validation phase=features "
                        f"stocks={len(validation_stock_codes)} workers={max_workers} factor_set={factor_set} "
                        f"scope={effective_validation_factor_scope}"
                    )
                validation_report = analyzer.build_factor_validation_report(
                    stock_codes=validation_stock_codes,
                    days=effective_validation_days,
                    factor_set=factor_set,
                    horizons=validation_horizons,
                    quantiles=validation_quantiles,
                    min_observations=validation_min_observations,
                    max_workers=max_workers,
                    show_progress=show_progress,
                    validation_factor_scope=effective_validation_factor_scope,
                    validated_feature_names=validated_feature_names,
                )
                if validation_report is None:
                    print("[ERROR] 验证驱动权重生成失败")
                    return None
                validation_scorecard = _build_factor_scorecard_ridge(validation_report)
                factor_score_config = _merge_recommended_factor_weights(
                    (validation_report.get("metadata") or {}).get("factor_score_config"),
                    validation_scorecard,
                )
                cache_payload = {
                    "cache_key": cache_key,
                    "identity": cache_identity,
                    "factor_score_config": factor_score_config,
                    "factor_scorecard": validation_scorecard.to_dict(orient="records"),
                    "created_at": pd.Timestamp.utcnow().isoformat(),
                }
                cache_path = _write_validation_weight_cache(cache_dir, cache_key, cache_payload)
                if cache_path is not None:
                    print(f"[OK] 已写入验证权重缓存: {cache_path}")

            print("[INFO] 已启用验证驱动权重模式")
            if not validation_scorecard.empty:
                validation_scorecard = _sanitize_validation_scorecard(validation_scorecard)
                preview_columns = [
                    "feature_name",
                    "component",
                    "configured_factor_weight",
                    "recommended_factor_weight",
                    "validation_score",
                ]
                preview_columns = [column for column in preview_columns if column in validation_scorecard.columns]
                print(validation_scorecard[preview_columns].head(10).to_string(index=False))

            # Build ridge_factors for cross-sectional scoring when scope is "all"
            ridge_factors = None
            if effective_validation_factor_scope == "all" and not validation_scorecard.empty:
                ridge_factors = StockAnalyzer._select_top_ridge_factors(validation_scorecard, top_k=30)
                if show_progress and ridge_factors is not None and not ridge_factors.empty:
                    print(
                        f"[PROGRESS] ridge_factors selected top_k={len(ridge_factors)} "
                        f"components={ridge_factors['component'].value_counts().to_dict()}"
                    )
        else:
            ridge_factors = None

        backtest_kwargs = {
            "days": days,
            "top_n": top_n,
            "initial_capital": initial_capital,
            "max_workers": max_workers,
            "analysis_mode": analysis_mode,
            "factor_set": factor_set,
            "factor_score_config": factor_score_config,
            "show_progress": show_progress,
            "enable_portfolio_replay": not fast_mode,
        }
        if signal_recipes is not None:
            backtest_kwargs["signal_recipes"] = signal_recipes
        if ridge_factors is not None:
            backtest_kwargs["ridge_factors"] = ridge_factors
        portfolio_result = analyzer.backtest_hk_market(**backtest_kwargs)
    finally:
        _safe_close_analyzer(analyzer)
    if portfolio_result is None:
        print("[ERROR] 全港股组合分析失败")
        return None

    analysis_results = portfolio_result.get("analysis_results", [])
    print(f"\n[INFO] 成功分析 {len(analysis_results)} 只股票")
    print(f"[INFO] 组合预计持有 Top {portfolio_result['top_n']} 只股票")
    print(f"[INFO] 组合估算收益率: {portfolio_result['estimated_portfolio_return']:.1f}%")
    print(f"[INFO] 组合估算胜率: {portfolio_result['estimated_portfolio_win_rate']:.1f}%")
    print(f"[INFO] 组合估算交易次数: {portfolio_result['estimated_trade_count']}")

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        ranking_path = export_path.with_name(f"{export_path.stem}_ranking.csv")
        selected_path = export_path.with_name(f"{export_path.stem}_selected.csv")
        watchlist_path = export_path.with_name(f"{export_path.stem}_watchlist.csv")

        pd.DataFrame(portfolio_result.get("ranking", [])).to_csv(ranking_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(portfolio_result.get("selected", [])).to_csv(selected_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(portfolio_result.get("watchlist", [])).to_csv(watchlist_path, index=False, encoding="utf-8-sig")

        print(f"[OK] 已导出全市场排名: {ranking_path}")
        print(f"[OK] 已导出当前持有: {selected_path}")
        print(f"[OK] 已导出观察名单: {watchlist_path}")

    if persist_signals:
        service = MarketDataService()
        try:
            persist_result = service.persist_portfolio_result(
                portfolio_result=portfolio_result,
                market="HK",
                signal_set="all_hk_topn",
                strategy_name="all_hk_topn",
                batch_id=batch_id,
                source="stock_analyzer_cli",
            )
        finally:
            service.close()
        print(f"[OK] 已写入 signal 层: batch_id={persist_result['batch_id']}, rows={persist_result['signal_rows']}")

    print("\n当前建议持有:")
    for item in portfolio_result.get("selected", []):
        print(f"- {item['stock_code']}")
        for line in _format_factor_reason_lines(item):
            print(line)

    print("\n" + "=" * 80)
    print("全港股 TopN 分析完成！")
    print("=" * 80)
    return portfolio_result


def main_review_batch(batch_id, export_csv=None):
    """按 batch_id 回看某次全港股扫描结果。"""
    print("=" * 80)
    print(f"港股技术分析系统 - 扫描批次复盘 {batch_id}")
    print("=" * 80)

    service = MarketDataService()
    try:
        frame = service.get_signal_frame(
            market="HK",
            signal_set="all_hk_topn",
            batch_id=batch_id,
        )
    finally:
        service.close()

    if frame is None or frame.empty:
        print(f"[ERROR] 未找到批次 {batch_id} 的扫描结果")
        return None

    ranking_df = frame[frame["signal_type"] == "ranking"].copy()
    selected_df = frame[frame["signal_type"] == "selected"].copy()
    watchlist_df = frame[frame["signal_type"] == "watchlist"].copy()
    ranking_avg_score = float(ranking_df["score"].mean()) if not ranking_df.empty and "score" in ranking_df.columns else 0.0
    selected_avg_score = float(selected_df["score"].mean()) if not selected_df.empty and "score" in selected_df.columns else 0.0
    watchlist_avg_score = float(watchlist_df["score"].mean()) if not watchlist_df.empty and "score" in watchlist_df.columns else 0.0
    summary_df = pd.DataFrame(
        [
            {
                "batch_id": batch_id,
                "ranking_count": len(ranking_df),
                "selected_count": len(selected_df),
                "watchlist_count": len(watchlist_df),
                "ranking_avg_score": ranking_avg_score,
                "selected_avg_score": selected_avg_score,
                "watchlist_avg_score": watchlist_avg_score,
            }
        ]
    )

    print(f"\n[INFO] 批次号: {batch_id}")
    print(f"[INFO] ranking 数量: {len(ranking_df)}")
    print(f"[INFO] selected 数量: {len(selected_df)}")
    print(f"[INFO] watchlist 数量: {len(watchlist_df)}")
    print(f"[INFO] 平均评分: ranking={ranking_avg_score:.1f}, selected={selected_avg_score:.1f}, watchlist={watchlist_avg_score:.1f}")

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path = export_path.with_name(f"{export_path.stem}_summary.csv")
        ranking_path = export_path.with_name(f"{export_path.stem}_ranking.csv")
        selected_path = export_path.with_name(f"{export_path.stem}_selected.csv")
        watchlist_path = export_path.with_name(f"{export_path.stem}_watchlist.csv")

        summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
        ranking_df.to_csv(ranking_path, index=False, encoding="utf-8-sig")
        selected_df.to_csv(selected_path, index=False, encoding="utf-8-sig")
        watchlist_df.to_csv(watchlist_path, index=False, encoding="utf-8-sig")

        print(f"[OK] 已导出批次 summary: {summary_path}")
        print(f"[OK] 已导出批次 ranking: {ranking_path}")
        print(f"[OK] 已导出批次 selected: {selected_path}")
        print(f"[OK] 已导出批次 watchlist: {watchlist_path}")

    print("\n当前持有建议:")
    for _, row in selected_df.sort_values(["rank_position", "stock_code"]).iterrows():
        print(f"- {row['stock_code']}")

    print("\n" + "=" * 80)
    print("批次复盘完成！")
    print("=" * 80)
    return {
        "batch_id": batch_id,
        "summary": summary_df,
        "ranking": ranking_df,
        "selected": selected_df,
        "watchlist": watchlist_df,
    }


def main_factor_report(
    days=365,
    factor_set="qlib_alpha158",
    export_csv=None,
    max_workers=1,
    show_progress=False,
    horizons=(1, 5, 10, 20),
    quantiles=5,
    min_observations=5,
    stock_limit=None,
    validation_factor_scope=None,
):
    """输出全市场因子验证报告。"""
    print("=" * 80)
    print(f"港股技术分析系统 - 因子验证报告 {factor_set}")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        stock_codes = analyzer.get_all_stocks()
        if stock_limit is not None:
            stock_codes = stock_codes[: max(int(stock_limit), 0)]
        effective_validation_factor_scope = validation_factor_scope or "all"
        report = analyzer.build_factor_validation_report(
            stock_codes=stock_codes,
            days=days,
            factor_set=factor_set,
            horizons=horizons,
            quantiles=quantiles,
            min_observations=min_observations,
            max_workers=max_workers,
            show_progress=show_progress,
            validation_factor_scope=effective_validation_factor_scope,
            validated_feature_names=(
                analyzer.get_score_factor_names() if effective_validation_factor_scope == "scoring_only" else None
            ),
        )
    finally:
        _safe_close_analyzer(analyzer)

    if report is None:
        print("[ERROR] 因子验证报告生成失败")
        return None
    metadata = report.get("metadata", {})
    factor_scorecard = _build_factor_scorecard_ridge(report)
    factor_score_config = _merge_recommended_factor_weights(
        metadata.get("factor_score_config"),
        factor_scorecard,
    )
    if isinstance(report.get("metadata"), dict):
        report["metadata"]["factor_score_config"] = factor_score_config
    metadata = report.get("metadata", {})
    stock_summary = report.get("stock_summary", pd.DataFrame())

    print(f"\n[INFO] 因子集: {metadata.get('factor_set')}")
    print(f"[INFO] 样本股票数: {metadata.get('success_count', 0)} / {metadata.get('stock_count', 0)}")
    print(f"[INFO] horizons: {metadata.get('horizons')}")
    print(f"[INFO] quantiles: {metadata.get('quantiles')}, min_observations: {metadata.get('min_observations')}")
    print(
        f"[INFO] validation_factor_scope: {metadata.get('validation_factor_scope', 'all')}, "
        f"validated_feature_count: {len(metadata.get('validated_feature_names') or []) if metadata.get('validated_feature_names') else 'all'}"
    )

    if not stock_summary.empty:
        print(
            "[INFO] 样本内均值: "
            f"mean_ic={stock_summary['mean_ic'].mean():.4f}, "
            f"mean_rank_ic={stock_summary['mean_rank_ic'].mean():.4f}, "
            f"mean_spread={stock_summary['mean_spread'].mean():.4f}, "
            f"mean_turnover={stock_summary['mean_turnover'].mean():.4f}"
        )

    if not factor_scorecard.empty:
        print("\nTop 因子质量:")
        preview_columns = [
            "feature_name",
            "component",
            "configured_factor_weight",
            "recommended_factor_weight",
            "mean_rank_ic",
            "mean_spread",
            "mean_turnover",
            "validation_score",
        ]
        preview_columns = [column for column in preview_columns if column in factor_scorecard.columns]
        print(factor_scorecard[preview_columns].head(15).to_string(index=False))

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        outputs = {
            "stock_summary": report.get("stock_summary", pd.DataFrame()),
            "factor_coverage": report.get("factor_coverage", pd.DataFrame()),
            "factor_scorecard": factor_scorecard,
            "ic_summary": report.get("ic_summary", pd.DataFrame()),
            "quantile_summary": report.get("quantile_summary", pd.DataFrame()),
            "long_short_summary": report.get("long_short_summary", pd.DataFrame()),
            "turnover_summary": report.get("turnover_summary", pd.DataFrame()),
            "decay_summary": report.get("decay_summary", pd.DataFrame()),
        }
        for name, frame in outputs.items():
            output_file = export_path.with_name(f"{export_path.stem}_{name}.csv")
            frame.to_csv(output_file, index=False, encoding="utf-8-sig")
            print(f"[OK] 已导出 {name}: {output_file}")

        metadata_file = export_path.with_name(f"{export_path.stem}_metadata.json")
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"[OK] 已导出 metadata: {metadata_file}")

    print("\n" + "=" * 80)
    print("因子验证报告完成！")
    print("=" * 80)
    return {
        **report,
        "factor_scorecard": factor_scorecard,
    }


def main_signal_report(
    days=365,
    export_csv=None,
    max_workers=1,
    show_progress=False,
    horizons=(20, 40, 60),
    stock_limit=None,
    signal_recipes=None,
    signal_cooldown_days=20,
    signal_event_policy="first",
):
    """输出全市场信号 recipe 验证报告。"""
    print("=" * 80)
    print("港股技术分析系统 - 信号配方验证报告")
    print("=" * 80)

    analyzer = StockAnalyzer(signal_recipes=signal_recipes)
    try:
        stock_codes = analyzer.get_all_stocks()
        if stock_limit is not None:
            stock_codes = stock_codes[: max(int(stock_limit), 0)]
        report = analyzer.build_signal_recipe_report(
            stock_codes=stock_codes,
            days=days,
            signal_recipes=signal_recipes,
            horizons=horizons,
            max_workers=max_workers,
            show_progress=show_progress,
            signal_cooldown_days=signal_cooldown_days,
            signal_event_policy=signal_event_policy,
        )
    finally:
        _safe_close_analyzer(analyzer)

    if report is None:
        print("[ERROR] 信号配方验证报告生成失败")
        return None

    metadata = report.get("metadata", {})
    summary = report.get("summary", pd.DataFrame())
    events = report.get("events", pd.DataFrame())
    events_raw = report.get("events_raw", pd.DataFrame())

    print(f"\n[INFO] 样本股票数: {metadata.get('stock_count', 0)}")
    print(f"[INFO] 原始触发事件数: {metadata.get('raw_event_count', metadata.get('event_count', 0))}")
    print(f"[INFO] 合并后事件数: {metadata.get('event_count', 0)}")
    print(f"[INFO] signal_recipes: {metadata.get('signal_recipes')}")
    print(f"[INFO] horizons: {metadata.get('horizons')}")
    print(f"[INFO] signal_cooldown_days: {metadata.get('signal_cooldown_days')}, signal_event_policy: {metadata.get('signal_event_policy')}")

    if not summary.empty:
        print("\n信号表现摘要:")
        print(summary.head(20).to_string(index=False))
    else:
        print("[WARN] 未发现有效信号事件")

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path = export_path.with_name(f"{export_path.stem}_signal_summary.csv")
        events_path = export_path.with_name(f"{export_path.stem}_signal_events.csv")
        raw_events_path = export_path.with_name(f"{export_path.stem}_signal_events_raw.csv")
        metadata_path = export_path.with_name(f"{export_path.stem}_metadata.json")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        events.to_csv(events_path, index=False, encoding="utf-8-sig")
        events_raw.to_csv(raw_events_path, index=False, encoding="utf-8-sig")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"[OK] 已导出信号摘要: {summary_path}")
        print(f"[OK] 已导出合并信号事件: {events_path}")
        print(f"[OK] 已导出原始信号事件: {raw_events_path}")
        print(f"[OK] 已导出元数据: {metadata_path}")

    print("\n" + "=" * 80)
    print("信号配方验证报告完成！")
    print("=" * 80)
    return report


def main_strategy_suite(days=365, top_n=3, initial_capital=100000, export_csv=None):
    """运行多策略对固定股票池的一年收益率比较。"""
    print("=" * 80)
    print("港股技术分析系统 - 多策略收益率对比")
    print("=" * 80)
    print(f"[INFO] 固定分析股票池: {', '.join(TARGET_STOCKS)}")

    comparison = StockAnalyzer.compare_strategy_suite(
        TARGET_STOCKS,
        days=days,
        top_n=top_n,
        initial_capital=initial_capital,
    )
    report = comparison.get('report') if comparison else None
    if not comparison or report is None:
        print("[ERROR] 多策略比较失败")
        return None

    tables = build_strategy_comparison_tables(report, TARGET_STOCKS)

    print(f"\n[INFO] 成功完成 {len(comparison['strategies'])} 套策略对比")
    print("\n策略总览表:")
    print(format_table_for_console(tables['summary']))

    print("\n八只股票近一年收益率矩阵表:")
    print(format_table_for_console(tables['returns']))

    print("\n各策略当前 Top 候选表:")
    print(format_table_for_console(tables['rankings']))

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path = export_path.with_name(f"{export_path.stem}_summary.csv")
        returns_path = export_path.with_name(f"{export_path.stem}_returns.csv")
        rankings_path = export_path.with_name(f"{export_path.stem}_rankings.csv")
        tables['summary'].to_csv(summary_path, index=False, encoding='utf-8-sig')
        tables['returns'].to_csv(returns_path, index=False, encoding='utf-8-sig')
        tables['rankings'].to_csv(rankings_path, index=False, encoding='utf-8-sig')
        print(f"\n[OK] 已导出策略总览表: {summary_path}")
        print(f"[OK] 已导出收益率矩阵表: {returns_path}")
        print(f"[OK] 已导出候选明细表: {rankings_path}")

    print("\n" + "=" * 80)
    print("多策略对比完成！")
    print("=" * 80)
    return comparison


def analyze_single_stock_with_visualization(stock_code="03633", days=365):
    """
    专门分析单只股票并生成可视化图表

    Args:
        stock_code (str): 股票代码
        days (int): 分析天数
    """
    print(f"\n{'='*80}")
    print(f"{stock_code}股票深度分析与可视化")
    print(f"{'='*80}")

    analyzer = StockAnalyzer()
    try:
        print(f"\n[INFO] 加载 {stock_code} 股票数据...")
        warmup_days = max(days + 120, days)
        full_data = analyzer.load_stock_data(stock_code, days=warmup_days)

        if full_data is None or full_data.empty:
            print(f"[ERROR] 无法加载 {stock_code} 数据")
            return None

        print(f"[OK] 成功加载 {len(full_data)} 条数据记录")

        print(f"\n[INFO] 使用TA-Lib计算技术指标...")
        data_with_indicators = analyzer.calculate_technical_indicators(full_data)

        if data_with_indicators is None:
            print(f"[ERROR] 技术指标计算失败")
            return None

        analysis_start_idx = max(len(data_with_indicators) - days, 0)
        analysis_data = data_with_indicators.iloc[analysis_start_idx:].copy()
        analysis_start_date = analysis_data.index[0]

        print(f"[INFO] 识别买卖信号...")
        buy_signals_full = analyzer.identify_buy_signals(data_with_indicators, stock_code=stock_code)
        sell_signals_full = analyzer.identify_sell_signals(data_with_indicators)

        buy_signals = None
        if buy_signals_full is not None and not buy_signals_full.empty:
            buy_signals = buy_signals_full[buy_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            buy_signals = analyzer.merge_buy_signal_zones(buy_signals, stock_code=stock_code)
            if buy_signals is not None and buy_signals.empty:
                buy_signals = None

        sell_signals = None
        if sell_signals_full is not None and not sell_signals_full.empty:
            sell_signals = sell_signals_full[sell_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            if sell_signals.empty:
                sell_signals = None

        print(f"[INFO] 执行策略回测...")
        backtest_result = analyzer.backtest_strategy(analysis_data, buy_signals, sell_signals)

        print(f"[INFO] 生成可视化图表...")
        create_visualization_charts(analysis_data, buy_signals, sell_signals, stock_code)

        buy_point_analysis = analyze_buy_points(analysis_data, buy_signals)

        target_alignment = analyze_target_date_alignment(
            analysis_data,
            buy_signals,
            ['2026-01-13', '2026-02-13', '2026-03-02']
        )
    finally:
        _safe_close_analyzer(analyzer)

    # 输出详细分析报告
    print(f"\n{'='*80}")
    print(f"{stock_code} 详细分析报告")
    print(f"{'='*80}")

    print(f"\n数据概览:")
    print(f"- 数据周期: {analysis_data.index.min().strftime('%Y-%m-%d')} 至 {analysis_data.index.max().strftime('%Y-%m-%d')}")
    print(f"- 总交易日: {len(analysis_data)}")
    print(f"- 价格区间: {analysis_data['Close'].min():.2f} - {analysis_data['Close'].max():.2f}")
    print(f"- 平均成交量: {analysis_data['Volume'].mean():,.0f}")

    if backtest_result:
        print(f"\n回测结果:")
        print(f"- 胜率: {backtest_result['win_rate']:.1f}%")
        print(f"- 总收益率: {backtest_result['total_return']:.1f}%")
        print(f"- 完成交易次数: {backtest_result['total_trades']}")
        print(f"- 盈利交易: {backtest_result['winning_trades']}")
        print(f"- 亏损交易: {backtest_result['losing_trades']}")
        if backtest_result.get('open_position'):
            open_position = backtest_result['open_position']
            print(f"- 未平仓头寸: {open_position['shares']}股，开仓价 {open_position['entry_price']:.2f}")
        if backtest_result.get('round_trips'):
            holding_days = [trade['holding_days'] for trade in backtest_result['round_trips']]
            if holding_days:
                avg_holding = sum(holding_days) / len(holding_days)
                print(f"- 平均持仓时间: {avg_holding:.1f} 天")

    if buy_signals is not None and not buy_signals.empty:
        print(f"\n买入信号统计:")
        print(f"- 总买入信号: {len(buy_signals)}")
        print(f"- 平均信号强度: {buy_signals['signal_strength'].mean():.1f}")
        print(f"- 最强信号: {buy_signals['signal_strength'].max()}")

        # 显示最近的买入信号
        recent_signals = buy_signals.tail(5)
        print(f"\n最近5个买入信号:")
        for _, signal in recent_signals.iterrows():
            print(f"- {signal['date'].strftime('%Y-%m-%d')}: 强度{signal['signal_strength']}, 价格{signal['close']:.2f}")

    if buy_point_analysis:
        print(f"\n买点评分分析:")
        print(f"- 优质买点数量: {buy_point_analysis['high_quality_signals']}")
        print(f"- 平均买点评分: {buy_point_analysis['avg_score']:.1f}")
        print(f"- 最佳买点评分: {buy_point_analysis['best_score']:.1f}")

        # 显示评分最高的买点
        if buy_point_analysis['top_signals']:
            print(f"\n评分最高的买点:")
            for i, signal in enumerate(buy_point_analysis['top_signals'][:3], 1):
                print(f"{i}. {signal['date'].strftime('%Y-%m-%d')}: 评分{signal['score']:.1f}, 价格{signal['close']:.2f}")

    print(f"\n目标日期匹配检查:")
    for item in target_alignment:
        if item['hit']:
            print(f"- {item['target_date']}: 当天命中")
        elif item['nearby_hit']:
            print(f"- {item['target_date']}: 附近命中 {item['matched_date']}")
        else:
            print(f"- {item['target_date']}: 未命中，原因 {item['blocking_reason']}")

    print(f"\n投资建议:")
    if buy_point_analysis and buy_point_analysis['high_quality_signals'] > 0:
        print("- 当前有优质买点，建议关注")
        print("- 重点关注StochRSI超卖且多重风险过滤确认的信号")
    else:
        print("- 当前无明显优质买点，建议观望")
        print("- 等待StochRSI超卖信号出现")

    print(f"\n图表已保存至 output/{stock_code}_analysis.png")
    return {
        'data': analysis_data,
        'buy_signals': buy_signals,
        'sell_signals': sell_signals,
        'backtest': backtest_result,
        'buy_analysis': buy_point_analysis
    }


def run_cli(argv=None):
    """CLI 入口，便于脚本调用与测试。"""
    parser = argparse.ArgumentParser(
        description="港股技术分析系统 - 支持单股回测、批量分析与多策略比较"
    )
    parser.add_argument('mode', nargs='?', default=None,
                        help='运行模式：single / suite / all_hk / validate_factors / select_stocks / factor_report / review_batch / 直接股票代码')
    parser.add_argument('value', nargs='?', default=None,
                        help='兼容旧模式：single 时为股票代码')
    parser.add_argument('--days', dest='days', type=int, default=365,
                        help='分析天数，默认 365')
    parser.add_argument('--top-n', dest='top_n', type=int, default=3,
                        help='组合持有数量，默认 3')
    parser.add_argument('--initial-capital', dest='initial_capital', type=float, default=100000,
                        help='初始资金，默认 100000')
    parser.add_argument('--export-csv', dest='export_csv', default=None,
                        help='在 suite 模式下导出表格 CSV 的基础路径，例如 output/strategy_suite')
    parser.add_argument('--persist-signals', dest='persist_signals', action='store_true',
                        help='在 all_hk 模式下将 ranking/selected/watchlist 写入 signal 层')
    parser.add_argument('--batch-id', dest='batch_id', default=None,
                        help='在 persist-signals 时指定批次号')
    parser.add_argument('--max-workers', dest='max_workers', type=int, default=0,
                        help='批量分析并发线程数，默认 0（自动根据系统资源决定）')
    parser.add_argument('--analysis-mode', dest='analysis_mode', default='factor',
                        choices=['factor', 'strategy'],
                        help='全市场分析模式：factor 或 strategy，默认 factor')
    parser.add_argument('--factor-set', dest='factor_set', default='qlib_alpha158',
                        help='因子模式下使用的因子集，默认 qlib_alpha158')
    parser.add_argument('--signal-recipes', dest='signal_recipes', default=None,
                        help='信号 recipe 名称，逗号分隔；默认 low_price_setup')
    parser.add_argument('--signal-cooldown-days', dest='signal_cooldown_days', type=int, default=20,
                        help='signal_report 中同股票同 recipe/setup 的信号合并窗口，默认 20 个自然日')
    parser.add_argument('--signal-event-policy', dest='signal_event_policy',
                        choices=['first', 'latest', 'best_score'], default='first',
                        help='signal_report 合并窗口内选择事件的方式，默认 first')
    parser.add_argument('--show-progress', dest='show_progress', action='store_true',
                        help='显示全市场分析进度')
    parser.add_argument('--fast-mode', dest='fast_mode', action='store_true',
                        help='快速模式：跳过组合真实 replay，仅保留研究型结果')
    parser.add_argument('--horizons', dest='horizons', default='1,5,10,20',
                        help='因子验证 horizons，逗号分隔，默认 1,5,10,20')
    parser.add_argument('--quantiles', dest='quantiles', type=int, default=5,
                        help='因子验证分组数，默认 5')
    parser.add_argument('--min-observations', dest='min_observations', type=int, default=5,
                        help='因子验证最小样本数，默认 5')
    parser.add_argument('--stock-limit', dest='stock_limit', type=int, default=None,
                        help='限制参与因子验证/扫描的股票数量，默认不限制')
    parser.add_argument('--use-recommended-factor-weights', dest='use_recommended_factor_weights', action='store_true',
                        help='在 all_hk factor 模式下先跑因子验证，再使用 recommended_factor_weight 回填打分权重')
    parser.add_argument('--validation-days', dest='validation_days', type=int, default=None,
                        help='验证驱动权重模式下使用的验证窗口天数，默认跟 --days 一致')
    parser.add_argument('--validation-horizons', dest='validation_horizons', default='1,5,10,20',
                        help='验证驱动权重模式下的 horizons，逗号分隔，默认 1,5,10,20')
    parser.add_argument('--validation-quantiles', dest='validation_quantiles', type=int, default=5,
                        help='验证驱动权重模式下的分组数，默认 5')
    parser.add_argument('--validation-min-observations', dest='validation_min_observations', type=int, default=5,
                        help='验证驱动权重模式下的最小样本数，默认 5')
    parser.add_argument('--validation-stock-limit', dest='validation_stock_limit', type=int, default=None,
                        help='验证驱动权重模式下限制参与验证的股票数量，默认不限制')
    parser.add_argument('--refresh-recommended-factor-weights', dest='refresh_recommended_factor_weights', action='store_true',
                        help='强制重算 recommended_factor_weight，不使用本地缓存')
    parser.add_argument('--validation-factor-scope', dest='validation_factor_scope',
                        choices=['scoring_only', 'all'], default=None,
                        help='因子验证范围：all_hk 推荐权重模式默认 scoring_only，factor_report 默认 all')
    args = parser.parse_args(argv)
    horizons = _parse_horizons(args.horizons)
    validation_horizons = _parse_horizons(args.validation_horizons)
    signal_recipes = _parse_signal_recipes(args.signal_recipes)

    if args.mode == "single":
        return analyze_single_stock_with_visualization(args.value or "03633", days=args.days)
    elif args.mode == "suite":
        return main_strategy_suite(
            days=args.days,
            top_n=args.top_n,
            initial_capital=args.initial_capital,
            export_csv=args.export_csv,
        )
    elif args.mode == "validate_factors":
        return main_validate_factors(
            days=args.days,
            factor_set=args.factor_set,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
            validation_horizons=validation_horizons,
            validation_quantiles=args.quantiles,
            validation_min_observations=args.min_observations,
            validation_stock_limit=args.stock_limit,
            validation_factor_scope=args.validation_factor_scope,
            refresh_recommended_factor_weights=args.refresh_recommended_factor_weights,
            export_csv=args.export_csv,
        )
    elif args.mode == "select_stocks":
        return main_select_stocks(
            days=args.days,
            top_n=args.top_n,
            initial_capital=args.initial_capital,
            export_csv=args.export_csv,
            persist_signals=args.persist_signals,
            batch_id=args.batch_id,
            max_workers=args.max_workers,
            analysis_mode=args.analysis_mode,
            factor_set=args.factor_set,
            show_progress=args.show_progress,
            fast_mode=args.fast_mode,
            validation_days=args.validation_days,
            validation_horizons=validation_horizons,
            validation_quantiles=args.quantiles,
            validation_min_observations=args.min_observations,
            validation_stock_limit=args.stock_limit,
            validation_factor_scope=args.validation_factor_scope,
            signal_recipes=signal_recipes,
        )
    elif args.mode == "all_hk":
        return main_all_hk(
            days=args.days,
            top_n=args.top_n,
            initial_capital=args.initial_capital,
            export_csv=args.export_csv,
            persist_signals=args.persist_signals,
            batch_id=args.batch_id,
            max_workers=args.max_workers,
            analysis_mode=args.analysis_mode,
            factor_set=args.factor_set,
            show_progress=args.show_progress,
            fast_mode=args.fast_mode,
            validation_days=args.validation_days,
            validation_horizons=validation_horizons,
            validation_quantiles=args.validation_quantiles,
            validation_min_observations=args.validation_min_observations,
            validation_stock_limit=args.validation_stock_limit,
            use_recommended_factor_weights=args.use_recommended_factor_weights,
            refresh_recommended_factor_weights=args.refresh_recommended_factor_weights,
            validation_factor_scope=args.validation_factor_scope,
            signal_recipes=signal_recipes,
        )
    elif args.mode == "factor_report":
        return main_factor_report(
            days=args.days,
            factor_set=args.factor_set,
            export_csv=args.export_csv,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
            horizons=horizons,
            quantiles=args.quantiles,
            min_observations=args.min_observations,
            stock_limit=args.stock_limit,
            validation_factor_scope=args.validation_factor_scope,
        )
    elif args.mode == "signal_report":
        return main_signal_report(
            days=args.days,
            export_csv=args.export_csv,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
            horizons=horizons,
            stock_limit=args.stock_limit,
            signal_recipes=signal_recipes,
            signal_cooldown_days=args.signal_cooldown_days,
            signal_event_policy=args.signal_event_policy,
        )
    elif args.mode == "review_batch":
        return main_review_batch(
            batch_id=args.value,
            export_csv=args.export_csv,
        )
    elif args.mode:
        return analyze_single_stock_with_visualization(args.mode, days=args.days)
    else:
        return main()


if __name__ == "__main__":
    run_cli()
