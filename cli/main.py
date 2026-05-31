"""Main CLI entry point: argparse definition and run_cli() dispatcher."""

import argparse

from cli.generate_factors import main_generate_factors
from cli.helpers import _parse_horizons, _parse_signal_recipes
from cli.select_stocks import main_select_stocks
from cli.validate_factors import main_validate_factors
from cli.all_hk import main_all_hk
from cli.review_batch import main_review_batch
from cli.factor_report import main_factor_report, main_signal_report
from cli.fetch_alt_data import main_fetch_alt_data
from cli.strategy_suite import main_strategy_suite, analyze_single_stock_with_visualization

def run_cli(argv=None):
    """CLI 入口，便于脚本调用与测试。"""
    parser = argparse.ArgumentParser(
        description="港股技术分析系统 - 支持单股回测、批量分析与多策略比较"
    )
    parser.add_argument('mode', nargs='?', default=None,
                        help='运行模式：single / suite / all_hk / validate_factors / generate_factors / select_stocks / fetch_alt_data / factor_report / review_batch / 直接股票代码')
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
                        choices=['factor', 'strategy', 'lightgbm'],
                        help='全市场分析模式：factor / strategy / lightgbm，默认 factor')
    parser.add_argument('--factor-set', dest='factor_set', default='qlib_alpha158',
                        help='因子模式下使用的因子集，默认 qlib_alpha158')
    parser.add_argument('--max-features', dest='max_features', type=int, default=0,
                        help='LightGBM 模式下的最大因子数（先训练→按 importance 筛到 TopN→重训），0 表示使用全部因子')
    parser.add_argument('--model-type', dest='model_type', default='lightgbm',
                        choices=['lightgbm', 'xgboost', 'catboost'],
                        help='Ranker 模型类型: lightgbm (默认), xgboost, catboost')
    parser.add_argument('--signal-recipes', dest='signal_recipes', default=None,
                        help='信号 recipe 名称，逗号分隔；默认 low_price_setup')
    parser.add_argument('--signal-cooldown-days', dest='signal_cooldown_days', type=int, default=20,
                        help='signal_report 中同股票同 recipe/setup 的信号合并窗口，默认 20 个自然日')
    parser.add_argument('--signal-event-policy', dest='signal_event_policy',
                        choices=['first', 'latest', 'best_score'], default='first',
                        help='signal_report 合并窗口内选择事件的方式，默认 first')
    parser.add_argument('--llm-report', dest='llm_report', action='store_true',
                        help='选股完成后自动生成 LLM 分析报告并归档到 docs/report/')
    parser.add_argument('--llm-model', dest='llm_model', default='deepseek-v4-pro',
                        help='LLM 模型名称 (default: deepseek-v4-pro)')
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
    parser.add_argument('--backtest-date', dest='backtest_date', default=None,
                        help='回测日期: 仅使用指定日期之前的数据选股，格式 YYYY-MM-DD')
    parser.add_argument('--min-market-cap', dest='min_market_cap', type=float, default=None,
                        help='最低市值过滤（亿港元），默认不过滤')
    parser.add_argument('--min-daily-turnover', dest='min_daily_turnover', type=float, default=100,
                        help='最低日成交额过滤（万港元），默认100万港元')
    parser.add_argument('--min-ipo-days', dest='min_ipo_days', type=int, default=None,
                        help='最低上市天数过滤（交易日），默认不过滤；建议 >= 250')
    parser.add_argument('--refresh-recommended-factor-weights', dest='refresh_recommended_factor_weights', action='store_true',
                        help='强制重算 recommended_factor_weight，不使用本地缓存')
    parser.add_argument('--validation-factor-scope', dest='validation_factor_scope',
                        choices=['scoring_only', 'all'], default=None,
                        help='因子验证范围，默认 all（全部因子），可选 scoring_only（仅评分配置中的因子）')
    args = parser.parse_args(argv)

    # Normalize mode aliases (unified CLI uses shorter names with dashes)
    _MODE_ALIASES = {
        "select": "select_stocks",
        "all": "all_hk",
        "review": "review_batch",
        "fetch-alt": "fetch_alt_data",
        "generate-factors": "generate_factors",
        "validate-factors": "validate_factors",
        "signal-report": "signal_report",
        "factor-report": "factor_report",
    }
    if args.mode in _MODE_ALIASES:
        args.mode = _MODE_ALIASES[args.mode]

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
    elif args.mode == "generate_factors":
        return main_generate_factors(
            days=args.days,
            factor_set=args.factor_set,
            stock_limit=args.stock_limit,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
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
            backtest_date=args.backtest_date,
            validation_days=args.validation_days,
            validation_horizons=validation_horizons,
            validation_quantiles=args.quantiles,
            validation_min_observations=args.min_observations,
            validation_stock_limit=args.stock_limit,
            validation_factor_scope=args.validation_factor_scope,
            signal_recipes=signal_recipes,
            max_features=args.max_features,
            model_type=args.model_type,
            min_market_cap=args.min_market_cap,
            min_daily_turnover=args.min_daily_turnover,
            min_ipo_days=args.min_ipo_days,
            llm_report=args.llm_report,
            llm_model=args.llm_model,
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
            min_market_cap=args.min_market_cap,
            min_daily_turnover=args.min_daily_turnover,
            min_ipo_days=args.min_ipo_days,
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
    elif args.mode == "fetch_alt_data":
        return main_fetch_alt_data(
            stock_limit=args.stock_limit,
            max_workers=args.max_workers or 20,
            show_progress=args.show_progress,
            persist=args.persist_signals,
        )
    elif args.mode == "review_batch":
        return main_review_batch(
            batch_id=args.value,
            export_csv=args.export_csv,
        )
    elif args.mode:
        return analyze_single_stock_with_visualization(args.mode, days=args.days)
    else:
        parser.print_help()
