"""Backtest operations mixin for StockAnalyzer."""

import sys
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from core.constants import DEFAULT_FACTOR_SET


class BacktestMixin:
    """Methods for portfolio backtesting and strategy comparison."""

    def backtest_portfolio(
        self,
        stock_codes=None,
        days=365,
        top_n=3,
        initial_capital=100000,
        weighting_mode="equal_weight",
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
        max_workers=1,
        analysis_mode="lightgbm",
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        persist_features=False,
        show_progress=False,
        enable_portfolio_replay=True,
        ridge_factors=None,
        signal_recipes=None,
        max_features=0,
        model_type="lightgbm",
        backtest_date=None,
        min_market_cap=None,
        min_daily_turnover=None,
        min_ipo_days=None,
        industry_selection_mode="core_overlay",
        industry_overlay_strength=0.0,
        max_industry_weight=0.35,
        hot_industry_weight_multiplier=1.3,
        enable_theme_features=True,
        theme_feature_set="theme_opportunity",
        theme_overlay_strength=0.0,
        model_objective="regression_csrank",
        neutralization_mode="industry_size",
    ):
        """固定股票池组合回测：按日期横向比较评分，只持有当日最优的 Top N 信号。"""
        from backtest_engine import TopNPortfolioBuilder
        from factor_engine.signals import SignalRecipeRunner

        if stock_codes is None:
            stock_codes = self.get_all_stocks()
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return None

        # --- Market quality filters ---
        if min_market_cap or min_daily_turnover or min_ipo_days:
            from core.market_filter import (
                apply_filters,
                build_market_info_from_warehouse,
                compute_trading_days_batch,
                fetch_market_data_batch,
                print_filter_report,
            )

            # CLI passes turnover in 万港元, convert to 港元 for filter
            turnover_hkd = min_daily_turnover * 1e4 if min_daily_turnover else None

            print("[FILTER] 正在从本地仓库读取市场数据以进行质量过滤...")
            t0 = time.time()
            market_data = build_market_info_from_warehouse(
                stock_codes,
                self.market_warehouse,
            )
            allow_live_market_filter_fetch = str(
                os.environ.get("ALLOW_LIVE_MARKET_FILTER_FETCH", "")
            ).strip().lower() in {"1", "true", "yes", "on"}
            missing_market_data_codes = [
                code for code in stock_codes
                if code not in market_data
                or (
                    market_data[code].market_cap <= 0
                    and market_data[code].daily_turnover <= 0
                    and market_data[code].pe_ratio <= 0
                    and market_data[code].pb_ratio <= 0
                )
            ]
            if missing_market_data_codes and allow_live_market_filter_fetch:
                print(f"[FILTER] 本地仓库缺少 {len(missing_market_data_codes)} 只股票的市场数据，回退实时抓取...")
                live_market_data = fetch_market_data_batch(
                    missing_market_data_codes,
                    max_workers=min(max_workers or 20, 30),
                    progress_callback=(
                        lambda done, total: print(
                            f"\r[FILTER] 实时市场数据获取 {done}/{total} ({done/total*100:.0f}%)",
                            end="", file=sys.stderr,
                        )
                        if show_progress else None
                    ),
                )
                market_data.update(live_market_data)
                if show_progress:
                    print(file=sys.stderr)
            elif missing_market_data_codes:
                print(f"[FILTER] 本地仓库缺少 {len(missing_market_data_codes)} 只股票的市场数据，未启用实时抓取兜底")

            trading_days = None
            if min_ipo_days is not None:
                print("[FILTER] 正在统计各股票上市天数...")
                trading_days = compute_trading_days_batch(
                    stock_codes, self.market_warehouse, max_workers=8,
                )

            filter_result = apply_filters(
                stock_codes,
                market_data,
                trading_days=trading_days,
                min_market_cap=min_market_cap,
                min_daily_turnover=turnover_hkd,
                min_ipo_days=min_ipo_days,
            )
            elapsed = time.time() - t0
            print(f"[FILTER] 过滤完成，耗时 {elapsed:.1f}s")
            print_filter_report(
                filter_result,
                min_market_cap=min_market_cap,
                min_daily_turnover=turnover_hkd,
                min_ipo_days=min_ipo_days,
            )

            if not filter_result.passed:
                print("[ERROR] 过滤后无股票剩余，请放宽过滤条件")
                return None

            stock_codes = filter_result.passed

        pool_results = []
        requested_workers = int(max_workers or 0)
        max_workers = self._resolve_safe_analysis_workers(requested_workers, analysis_mode=analysis_mode)
        normalized_mode = str(analysis_mode or "lightgbm").strip().lower()
        if normalized_mode not in {"factor", "lightgbm"}:
            raise ValueError(f"unsupported analysis_mode: {analysis_mode}")
        if show_progress and requested_workers > 0 and max_workers != requested_workers:
            print(
                f"[INFO] analysis workers auto-clamped from {requested_workers} to {max_workers} "
                f"for mode={normalized_mode}"
            )

        if normalized_mode == "lightgbm":
            pool_results = self._analyze_lightgbm_market(
                stock_codes,
                days=days,
                factor_set=factor_set,
                signal_recipes=signal_recipes,
                persist_features=persist_features,
                show_progress=show_progress,
                max_features=max_features,
                model_type=model_type,
                backtest_date=backtest_date,
                enable_theme_features=enable_theme_features,
                theme_feature_set=theme_feature_set,
                model_objective=model_objective,
                neutralization_mode=neutralization_mode,
            )
            if not pool_results:
                return None
            builder = TopNPortfolioBuilder(
                top_n=top_n,
                initial_capital=initial_capital,
                weighting_mode=weighting_mode,
                buy_commission_rate=buy_commission_rate,
                sell_commission_rate=sell_commission_rate,
                slippage_rate=slippage_rate,
                min_commission=min_commission,
                enable_portfolio_replay=enable_portfolio_replay,
                industry_selection_mode=industry_selection_mode,
                industry_overlay_strength=industry_overlay_strength,
                max_industry_weight=max_industry_weight,
                hot_industry_weight_multiplier=hot_industry_weight_multiplier,
                theme_overlay_strength=theme_overlay_strength,
            )
            return builder.build(stock_codes=stock_codes, analysis_results=pool_results)

        def run_analysis(stock_code):
            factor_kwargs = {
                "days": days,
                "factor_set": factor_set,
                "factor_score_config": factor_score_config,
                "persist_features": persist_features,
            }
            if ridge_factors is not None:
                factor_kwargs["ridge_factors"] = ridge_factors
            if signal_recipes is not None:
                factor_kwargs["signal_recipes"] = signal_recipes
            return self.analyze_stock_factors(stock_code, **factor_kwargs)

        # Import StockAnalyzer lazily to avoid circular import
        from core.analyzer import StockAnalyzer as _StockAnalyzer
        default_analyze_stock_factors = getattr(_StockAnalyzer, "_default_analyze_stock_factors", None)
        supports_batch_factor_analysis = (
            default_analyze_stock_factors is not None
            and getattr(type(self), "analyze_stock_factors", None) is default_analyze_stock_factors
        )

        if normalized_mode == "factor" and supports_batch_factor_analysis and max_workers > 1 and len(stock_codes) > 1:
            batch_size = self._resolve_factor_analysis_batch_size(
                total_stocks=len(stock_codes),
                max_workers=max_workers,
                analysis_mode=normalized_mode,
            )
            stock_batches = [
                stock_codes[index:index + batch_size]
                for index in range(0, len(stock_codes), batch_size)
            ]
            worker_count = min(max_workers, len(stock_batches))
            available_bytes = type(self)._available_memory_bytes()
            memory_text = (
                f"{(available_bytes / (1024 ** 3)):.1f}"
                if available_bytes is not None
                else "unknown"
            )
            if show_progress:
                print(
                    f"[PROGRESS] analysis phase=batch_factor stocks={len(stock_codes)} "
                    f"batches={len(stock_batches)} batch_size={batch_size} workers={worker_count} "
                    f"memory_available_gb={memory_text}"
                )
            started_at = time.time()
            total = len(stock_codes)
            completed = 0
            success_count = 0
            completed_batches = 0
            active_batches = set()
            batch_progress_counts = {
                batch_index + 1: 0
                for batch_index in range(len(stock_batches))
            }
            stock_done_lock = None

            def emit_batch_progress():
                self._emit_progress_line(
                    prefix="[PROGRESS] analysis phase=batch_factor",
                    completed=completed,
                    total=total,
                    success_count=success_count,
                    started_at=started_at,
                    extra_fields=[
                        ("batches_done", f"{completed_batches}/{len(stock_batches)}"),
                        ("active_batches", len(active_batches)),
                    ],
                )

            if show_progress:
                import threading

                stock_done_lock = threading.Lock()

            def make_progress_callback(batch_no):
                if not show_progress:
                    return None

                def _progress_callback(_stock_code):
                    nonlocal completed
                    with stock_done_lock:
                        completed += 1
                        batch_progress_counts[batch_no] = batch_progress_counts.get(batch_no, 0) + 1
                        emit_batch_progress()

                return _progress_callback

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {}
                for batch_index, batch in enumerate(stock_batches):
                    batch_kwargs = {
                        "days": days,
                        "factor_set": factor_set,
                        "factor_score_config": factor_score_config,
                        "persist_features": persist_features,
                        "ridge_factors": ridge_factors,
                        "progress_callback": make_progress_callback(batch_index + 1),
                        "batch_index": batch_index + 1,
                        "total_batches": len(stock_batches),
                    }
                    if signal_recipes is not None:
                        batch_kwargs["signal_recipes"] = signal_recipes
                    future_map[
                        executor.submit(
                            self._analyze_factor_batch,
                            batch,
                            **batch_kwargs,
                        )
                    ] = (batch_index + 1, batch)
                if show_progress:
                    active_batches = {batch_index for batch_index, _batch in future_map.values()}

                for future in as_completed(future_map):
                    batch_index, batch = future_map[future]
                    try:
                        result_batch = future.result()
                    except Exception as exc:
                        if show_progress:
                            with stock_done_lock:
                                missing_count = max(len(batch) - batch_progress_counts.get(batch_index, 0), 0)
                                completed += missing_count
                                batch_progress_counts[batch_index] = len(batch)
                                active_batches.discard(batch_index)
                                completed_batches += 1
                                emit_batch_progress()
                        print(f"\n[ERROR] 批量因子分析失败 batch={batch_index}/{len(stock_batches)} batch_size={len(batch)}: {exc}")
                        continue
                    if show_progress:
                        with stock_done_lock:
                            missing_count = max(len(batch) - batch_progress_counts.get(batch_index, 0), 0)
                            completed += missing_count
                            batch_progress_counts[batch_index] = len(batch)
                            active_batches.discard(batch_index)
                            completed_batches += 1
                    if result_batch:
                        pool_results.extend(result_batch)
                        success_count += len(result_batch)
                    if show_progress:
                        emit_batch_progress()
            if show_progress:
                print(file=sys.stderr)
            if not pool_results:
                return None
            builder = TopNPortfolioBuilder(
                top_n=top_n,
                initial_capital=initial_capital,
                weighting_mode=weighting_mode,
                buy_commission_rate=buy_commission_rate,
                sell_commission_rate=sell_commission_rate,
                slippage_rate=slippage_rate,
                min_commission=min_commission,
                enable_portfolio_replay=enable_portfolio_replay,
                industry_selection_mode=industry_selection_mode,
                industry_overlay_strength=industry_overlay_strength,
                max_industry_weight=max_industry_weight,
                hot_industry_weight_multiplier=hot_industry_weight_multiplier,
                theme_overlay_strength=theme_overlay_strength,
            )
            return builder.build(stock_codes=stock_codes, analysis_results=pool_results)

        if max_workers == 1 or len(stock_codes) <= 1:
            started_at = time.time()
            total = len(stock_codes)
            completed = 0
            success_count = 0
            for stock_code in stock_codes:
                result = run_analysis(stock_code)
                completed += 1
                if result is not None:
                    pool_results.append(result)
                    success_count += 1
                if show_progress:
                    elapsed = max(time.time() - started_at, 1e-9)
                    rate = completed / elapsed
                    remaining = total - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    print(
                        f"\r[PROGRESS] {completed}/{total} "
                        f"({completed / total:.1%}) success={success_count} "
                        f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                    , end="", flush=True, file=sys.stderr)
            if show_progress:
                print(file=sys.stderr)
        else:
            started_at = time.time()
            total = len(stock_codes)
            completed = 0
            success_count = 0
            with ThreadPoolExecutor(max_workers=min(max_workers, len(stock_codes))) as executor:
                future_map = {
                    executor.submit(run_analysis, stock_code): stock_code
                    for stock_code in stock_codes
                }
                for future in as_completed(future_map):
                    stock_code = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        print(f"\n[ERROR] 并行分析股票 {stock_code} 失败: {exc}")
                        completed += 1
                        if show_progress:
                            elapsed = max(time.time() - started_at, 1e-9)
                            rate = completed / elapsed
                            remaining = total - completed
                            eta = remaining / rate if rate > 0 else 0.0
                            print(
                                f"\r[PROGRESS] {completed}/{total} "
                                f"({completed / total:.1%}) success={success_count} "
                                f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                            , end="", flush=True, file=sys.stderr)
                        continue
                    completed += 1
                    if result is not None:
                        pool_results.append(result)
                        success_count += 1
                    if show_progress:
                        elapsed = max(time.time() - started_at, 1e-9)
                        rate = completed / elapsed
                        remaining = total - completed
                        eta = remaining / rate if rate > 0 else 0.0
                        print(
                            f"\r[PROGRESS] {completed}/{total} "
                            f"({completed / total:.1%}) success={success_count} "
                            f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                        , end="", flush=True, file=sys.stderr)
            if show_progress:
                print(file=sys.stderr)

        if not pool_results:
            return None
        builder = TopNPortfolioBuilder(
            top_n=top_n,
            initial_capital=initial_capital,
            weighting_mode=weighting_mode,
            buy_commission_rate=buy_commission_rate,
            sell_commission_rate=sell_commission_rate,
            slippage_rate=slippage_rate,
            min_commission=min_commission,
            enable_portfolio_replay=enable_portfolio_replay,
            industry_selection_mode=industry_selection_mode,
            industry_overlay_strength=industry_overlay_strength,
            max_industry_weight=max_industry_weight,
            hot_industry_weight_multiplier=hot_industry_weight_multiplier,
            theme_overlay_strength=theme_overlay_strength,
        )
        return builder.build(stock_codes=stock_codes, analysis_results=pool_results)

    def backtest_hk_market(
        self,
        days=365,
        top_n=3,
        initial_capital=100000,
        weighting_mode="equal_weight",
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
        max_workers=1,
        analysis_mode="lightgbm",
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        persist_features=False,
        stock_codes=None,
        show_progress=False,
        enable_portfolio_replay=True,
        ridge_factors=None,
        signal_recipes=None,
        max_features=0,
        model_type="lightgbm",
        backtest_date=None,
        min_market_cap=None,
        min_daily_turnover=None,
        min_ipo_days=None,
        industry_selection_mode="core_overlay",
        industry_overlay_strength=0.0,
        max_industry_weight=0.35,
        hot_industry_weight_multiplier=1.3,
        enable_theme_features=True,
        theme_feature_set="theme_opportunity",
        theme_overlay_strength=0.0,
        model_objective="regression_csrank",
        neutralization_mode="industry_size",
    ):
        """对本地已同步的全部港股执行 TopN 组合回测。"""
        return self.backtest_portfolio(
            stock_codes=stock_codes,
            days=days,
            top_n=top_n,
            initial_capital=initial_capital,
            weighting_mode=weighting_mode,
            buy_commission_rate=buy_commission_rate,
            sell_commission_rate=sell_commission_rate,
            slippage_rate=slippage_rate,
            min_commission=min_commission,
            max_workers=max_workers,
            analysis_mode=analysis_mode,
            factor_set=factor_set,
            factor_score_config=factor_score_config,
            persist_features=persist_features,
            show_progress=show_progress,
            min_market_cap=min_market_cap,
            min_daily_turnover=min_daily_turnover,
            min_ipo_days=min_ipo_days,
            enable_portfolio_replay=enable_portfolio_replay,
            ridge_factors=ridge_factors,
            signal_recipes=signal_recipes,
            max_features=max_features,
            model_type=model_type,
            backtest_date=backtest_date,
            industry_selection_mode=industry_selection_mode,
            industry_overlay_strength=industry_overlay_strength,
            max_industry_weight=max_industry_weight,
            hot_industry_weight_multiplier=hot_industry_weight_multiplier,
            enable_theme_features=enable_theme_features,
            theme_feature_set=theme_feature_set,
            theme_overlay_strength=theme_overlay_strength,
            model_objective=model_objective,
            neutralization_mode=neutralization_mode,
        )

    def backtest_strategy(self, data, buy_signals, sell_signals, initial_capital=100000, default_holding_days=60):
        from backtest_engine import backtest_strategy
        return backtest_strategy(
            data,
            buy_signals,
            sell_signals,
            initial_capital=initial_capital,
            default_holding_days=default_holding_days
        )

    @staticmethod
    def compare_strategy_suite(
        stock_codes,
        days=365,
        top_n=3,
        initial_capital=100000,
        db_dir="./assets",
        weighting_mode="equal_weight",
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
    ):
        from core.reporting import generate_strategy_comparison_report
        from portfolio_strategy import STRATEGY_SUITE
        # Import lazily to avoid circular import at module level
        from core.analyzer import StockAnalyzer

        suite_results = []
        for strategy_config in STRATEGY_SUITE:
            analyzer = StockAnalyzer(db_dir=db_dir)
            portfolio_result = analyzer.backtest_portfolio(
                stock_codes,
                days=days,
                top_n=top_n,
                initial_capital=initial_capital,
                weighting_mode=weighting_mode,
                buy_commission_rate=buy_commission_rate,
                sell_commission_rate=sell_commission_rate,
                slippage_rate=slippage_rate,
                min_commission=min_commission,
            )
            if portfolio_result is None:
                continue

            per_stock_returns = {
                item['stock_code']: item.get('backtest', {}).get('total_return', 0)
                for item in portfolio_result.get('analysis_results', [])
            }
            suite_results.append({
                'strategy_code': strategy_config['code'],
                'strategy_name': strategy_config['name'],
                'buy_strategy': 'model_driven',
                'sell_strategy': 'model_driven',
                'portfolio_result': portfolio_result,
                'analysis_results': portfolio_result.get('analysis_results', []),
                'per_stock_returns': per_stock_returns,
                'summary': {
                    'estimated_portfolio_return': portfolio_result.get('estimated_portfolio_return', 0),
                    'estimated_portfolio_win_rate': portfolio_result.get('estimated_portfolio_win_rate', 0),
                    'estimated_trade_count': portfolio_result.get('estimated_trade_count', 0),
                    'selected_count': len(portfolio_result.get('selected', [])),
                }
            })

        return {
            'stock_pool': stock_codes,
            'days': days,
            'top_n': top_n,
            'initial_capital': initial_capital,
            'strategies': suite_results,
            'report': generate_strategy_comparison_report(suite_results, stock_codes)
        }
