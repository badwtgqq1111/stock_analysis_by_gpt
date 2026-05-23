"""CLI command: all_hk - legacy combined validate+select for full HK market."""

from copy import deepcopy
from pathlib import Path

import pandas as pd

from analyzer_core import StockAnalyzer
from cli.formatters import _format_factor_reason_lines, _safe_close_analyzer
from cli.helpers import (
    _build_factor_scorecard_ridge,
    _build_validation_cache_key,
    _get_validation_cache_dir,
    _is_usable_validation_scorecard,
    _load_validation_weight_cache,
    _merge_recommended_factor_weights,
    _sanitize_validation_scorecard,
    _write_validation_weight_cache,
)
from data.ingest.service import MarketDataService


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
