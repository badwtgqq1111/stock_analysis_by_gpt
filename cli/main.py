"""Main CLI entry point: argparse definition and run_cli() dispatcher."""

import argparse

import pandas as pd

from analyzer_core import StockAnalyzer
from cli.formatters import _safe_close_analyzer
from cli.helpers import _parse_horizons, _parse_signal_recipes
from cli.select_stocks import main_select_stocks
from cli.validate_factors import main_validate_factors
from cli.all_hk import main_all_hk
from cli.review_batch import main_review_batch
from cli.factor_report import main_factor_report, main_signal_report
from cli.strategy_suite import main_strategy_suite, analyze_single_stock_with_visualization

TARGET_STOCKS = ['03633', '02706', '02015', '01860', '02432', '02590', '09866', '00020']


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
                        choices=['factor', 'strategy', 'lightgbm'],
                        help='全市场分析模式：factor / strategy / lightgbm，默认 factor')
    parser.add_argument('--factor-set', dest='factor_set', default='qlib_alpha158',
                        help='因子模式下使用的因子集，默认 qlib_alpha158')
    parser.add_argument('--max-features', dest='max_features', type=int, default=0,
                        help='LightGBM 模式下的最大因子数（先训练→按 importance 筛到 TopN→重训），0 表示使用全部因子')
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
            max_features=args.max_features,
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
