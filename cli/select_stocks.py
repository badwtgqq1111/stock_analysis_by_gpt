"""CLI command: select_stocks - full HK market TopN stock selection."""

from copy import deepcopy
from pathlib import Path

import pandas as pd

from analyzer_core import StockAnalyzer
from cli.formatters import _format_factor_reason_lines, _safe_close_analyzer
from cli.helpers import (
    _build_validation_cache_key,
    _get_validation_cache_dir,
    _is_usable_validation_scorecard,
    _load_validation_weight_cache,
    _sanitize_validation_scorecard,
    _write_run_manifest,
)
from core.llm.report import run_auto_report
from data.ingest.service import MarketDataService


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
    validation_factor_scope="all",
    signal_recipes=None,
    max_features=0,
    model_type="lightgbm",
    backtest_date=None,
    min_market_cap=None,
    min_daily_turnover=None,
    min_ipo_days=None,
    llm_report=False,
    llm_model="deepseek-v4-pro",
):
    """执行全港股 TopN 选股+回测。factor 模式读取验证权重缓存，lightgbm 模式直接训练排序模型。"""
    print("=" * 80)
    if str(analysis_mode).strip().lower() == "lightgbm":
        print(f"港股技术分析系统 - 全港股 Top {top_n} 组合筛选（LightGBM Ranker）")
    else:
        print(f"港股技术分析系统 - 全港股 Top {top_n} 组合筛选（基于验证权重）")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        cached_payload = None
        normalized_mode = str(analysis_mode or "factor").strip().lower()
        factor_score_config = None
        ridge_factors = None
        if normalized_mode == "factor":
            effective_scope = validation_factor_scope or "all"
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

            if effective_scope == "all" and not candidate_scorecard.empty:
                ridge_factors = StockAnalyzer._select_top_ridge_factors(candidate_scorecard, top_k=30)
                if show_progress and ridge_factors is not None and not ridge_factors.empty:
                    print(
                        f"[PROGRESS] ridge_factors selected top_k={len(ridge_factors)} "
                        f"components={ridge_factors['component'].value_counts().to_dict()}"
                    )
        elif normalized_mode == "lightgbm":
            extra = f", max_features={max_features}" if max_features else ""
            if model_type not in ("", "lightgbm", None):
                extra += f", model={model_type}"
            print(f"[INFO] LightGBM 模式启用: factor_set={factor_set}, train_window={days}d{extra}")

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
            "max_features": max_features,
            "model_type": model_type,
            "min_market_cap": min_market_cap,
            "min_daily_turnover": min_daily_turnover,
            "min_ipo_days": min_ipo_days,
        }
        if backtest_date is not None:
            backtest_kwargs["backtest_date"] = str(backtest_date)
            print(f"[INFO] 回测模式: 仅使用 {backtest_date} 之前的数据")
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

    ranking_path = None
    selected_path = None
    watchlist_path = None
    signals_dir = None
    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = f"_{factor_set}" if factor_set else ""
        ranking_path = export_path.with_name(f"{export_path.stem}{suffix}_ranking.csv")
        selected_path = export_path.with_name(f"{export_path.stem}{suffix}_selected.csv")
        watchlist_path = export_path.with_name(f"{export_path.stem}{suffix}_watchlist.csv")

        pd.DataFrame(portfolio_result.get("ranking", [])).to_csv(ranking_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(portfolio_result.get("selected", [])).to_csv(selected_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(portfolio_result.get("watchlist", [])).to_csv(watchlist_path, index=False, encoding="utf-8-sig")

        # 导出入选股票的每日信号时序 (供 K 线图标注买卖点)
        signals_dir = export_path.parent / "signals"
        signals_dir.mkdir(parents=True, exist_ok=True)
        selected_codes = set(
            str(r.get("stock_code", "")).zfill(5)
            for r in portfolio_result.get("selected", [])
        )
        for item in analysis_results:
            code = str(item.get("stock_code", "")).zfill(5)
            if code not in selected_codes:
                continue
            buy_signals_df = item.get("buy_signals")
            if buy_signals_df is not None and not buy_signals_df.empty:
                sig_path = signals_dir / f"{code}_signals.csv"
                buy_signals_df.to_csv(sig_path, index=False, encoding="utf-8-sig")
        print(f"[OK] 已导出信号时序: {signals_dir}/")

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
    else:
        persist_result = None

    manifest_path, _ = _write_run_manifest(
        run_type="select_stocks",
        analyzer=analyzer,
        fallback_base=export_csv,
        params={
            "days": int(days),
            "top_n": int(top_n),
            "initial_capital": float(initial_capital),
            "analysis_mode": normalized_mode,
            "factor_set": factor_set,
            "max_workers": int(max_workers),
            "validation_days": int(validation_days or days),
            "validation_horizons": [int(item) for item in validation_horizons],
            "validation_quantiles": int(validation_quantiles),
            "validation_min_observations": int(validation_min_observations),
            "validation_stock_limit": None if validation_stock_limit is None else int(validation_stock_limit),
            "validation_factor_scope": validation_factor_scope or "all",
            "signal_recipes": list(signal_recipes or []),
            "max_features": int(max_features or 0),
            "persist_signals": bool(persist_signals),
        },
        artifacts={
            "ranking_csv_path": str(ranking_path) if ranking_path is not None else None,
            "selected_csv_path": str(selected_path) if selected_path is not None else None,
            "watchlist_csv_path": str(watchlist_path) if watchlist_path is not None else None,
            "signals_dir": str(signals_dir) if signals_dir is not None else None,
            "persist_batch_id": persist_result.get("batch_id") if persist_result is not None else None,
        },
        factor_materialization=(cached_payload or {}).get("feature_materialization") or {},
        upstream={
            "validation_cache_key": cache_key if normalized_mode == "factor" else None,
            "validation_cache_path": (cached_payload or {}).get("_cache_path") if normalized_mode == "factor" else None,
        },
        status="ok",
    )
    print(f"[OK] 已写入 run manifest: {manifest_path}")

    if llm_report and portfolio_result:
        print("\n[LLM] 正在生成 AI 分析报告...")
        run_auto_report(
            portfolio_result,
            model=llm_model,
            formula_version="v11",
        )
        print("[LLM] 报告生成完成")

    print("\n当前建议持有:")
    for item in portfolio_result.get("selected", []):
        print(f"- {item['stock_code']}")
        for line in _format_factor_reason_lines(item):
            print(line)

    print("\n" + "=" * 80)
    print("全港股 TopN 分析完成！")
    print("=" * 80)
    portfolio_result["manifest_path"] = str(manifest_path)
    return portfolio_result
