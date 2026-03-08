#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
股票技术分析模块 - 基于价格和成交量数据分析买卖点策略
"""

import argparse
import pandas as pd
import warnings

from analyzer_core import StockAnalyzer
from reporting import analyze_buy_points, analyze_target_date_alignment, create_visualization_charts
from strategy import BuyStrategy, CurrentStrategy, SellStrategy

warnings.filterwarnings("ignore")

__all__ = [
    "StockAnalyzer",
    "BuyStrategy",
    "SellStrategy",
    "CurrentStrategy",
    "main",
    "analyze_single_stock_with_visualization",
    "create_visualization_charts",
]


def main():
    """主函数 - 固定8股票池的3个月收益导向策略分析"""
    print("=" * 80)
    print("港股技术分析系统 - 8股票池三个月收益优化")
    print("=" * 80)

    analyzer = StockAnalyzer()
    target_stocks = ['03633', '02706', '02015', '01860', '02432', '02590', '09866', '00020']
    print(f"[INFO] 固定分析股票池: {', '.join(target_stocks)}")

    portfolio_result = analyzer.backtest_portfolio(target_stocks, days=365, top_n=3)
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
        print("📊 8股票池三个月收益策略报告")
        print("=" * 80)

        print("\n🏆 当前股票排名:")
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

        print("\n🎯 当前建议持有:")
        for item in portfolio_result['selected']:
            signal_flag = '强买点' if item.get('current_signal_active') and item.get('current_signal_actionable') else ('观察名单' if item.get('current_signal_active') else '评分候选')
            signal_score = item.get('current_signal_score')
            signal_score_text = f"{signal_score:.1f}" if pd.notna(signal_score) else 'None'
            print(
                f"• {item['stock_code']} - {signal_flag}, 排名分 {item['ranking_score']:.1f}, "
                f"信号评分 {signal_score_text}, 建议买点 {item['entry_type']}, 信号层级 {item.get('signal_tier')}, "
                f"单股回测收益 {item['backtest_return']:.1f}%"
            )

        if portfolio_result.get('watchlist'):
            print("\n👀 观察名单:")
            for item in portfolio_result['watchlist']:
                print(
                    f"• {item['stock_code']} - 入场类型 {item['entry_type']}, 信号层级 {item.get('signal_tier')}, "
                    f"预期3月评分 {item.get('expected_3m_score', 0):.1f}, 趋势评分 {item.get('regime_score', 0):.1f}"
                )

        print("\n⚠️ 风险管理:")
        risk = strategy['recommended_strategy']['risk_management']
        print(f"• 仓位: {risk['max_position_size']}")
        print(f"• 止损: {risk['stop_loss']}")
        print(f"• 止盈: {risk['take_profit']}")
        print(f"• 最大日交易数: {risk['max_daily_trades']}")
        print(f"• 默认持有周期: {risk['holding_horizon']} 个交易日")

    print("\n" + "=" * 80)
    print("分析完成！")
    print("=" * 80)


def analyze_single_stock_with_visualization(stock_code="03633", days=365):
    """
    专门分析单只股票并生成可视化图表

    Args:
        stock_code (str): 股票代码
        days (int): 分析天数
    """
    print(f"\n{'='*80}")
    print(f"🎯 03633股票深度分析与可视化")
    print(f"{'='*80}")

    analyzer = StockAnalyzer()

    # 加载数据
    print(f"\n[INFO] 加载 {stock_code} 股票数据...")
    warmup_days = max(days + 120, days)
    full_data = analyzer.load_stock_data(stock_code, days=warmup_days)

    if full_data is None or full_data.empty:
        print(f"[ERROR] 无法加载 {stock_code} 数据")
        return None

    print(f"[OK] 成功加载 {len(full_data)} 条数据记录")

    # 计算技术指标
    print(f"\n[INFO] 使用TA-Lib计算技术指标...")
    data_with_indicators = analyzer.calculate_technical_indicators(full_data)

    if data_with_indicators is None:
        print(f"[ERROR] 技术指标计算失败")
        return None

    analysis_start_idx = max(len(data_with_indicators) - days, 0)
    analysis_data = data_with_indicators.iloc[analysis_start_idx:].copy()
    analysis_start_date = analysis_data.index[0]

    # 识别买卖信号
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

    # 执行回测
    print(f"[INFO] 执行策略回测...")
    backtest_result = analyzer.backtest_strategy(analysis_data, buy_signals, sell_signals)

    # 生成可视化图表
    print(f"[INFO] 生成可视化图表...")
    create_visualization_charts(analysis_data, buy_signals, sell_signals, stock_code)

    # 分析买点评分
    buy_point_analysis = analyze_buy_points(analysis_data, buy_signals)

    target_alignment = analyze_target_date_alignment(
        analysis_data,
        buy_signals,
        ['2026-01-13', '2026-02-13', '2026-03-02']
    )

    # 输出详细分析报告
    print(f"\n{'='*80}")
    print(f"📊 {stock_code} 详细分析报告")
    print(f"{'='*80}")

    print(f"\n📈 数据概览:")
    print(f"• 数据周期: {analysis_data.index.min().strftime('%Y-%m-%d')} 至 {analysis_data.index.max().strftime('%Y-%m-%d')}")
    print(f"• 总交易日: {len(analysis_data)}")
    print(f"• 价格区间: {analysis_data['Close'].min():.2f} - {analysis_data['Close'].max():.2f}")
    print(f"• 平均成交量: {analysis_data['Volume'].mean():,.0f}")

    if backtest_result:
        print(f"\n💰 回测结果:")
        print(f"• 胜率: {backtest_result['win_rate']:.1f}%")
        print(f"• 总收益率: {backtest_result['total_return']:.1f}%")
        print(f"• 完成交易次数: {backtest_result['total_trades']}")
        print(f"• 盈利交易: {backtest_result['winning_trades']}")
        print(f"• 亏损交易: {backtest_result['losing_trades']}")
        if backtest_result.get('open_position'):
            open_position = backtest_result['open_position']
            print(f"• 未平仓头寸: {open_position['shares']}股，开仓价 {open_position['entry_price']:.2f}")
        if backtest_result.get('round_trips'):
            holding_days = [trade['holding_days'] for trade in backtest_result['round_trips']]
            if holding_days:
                avg_holding = sum(holding_days) / len(holding_days)
                print(f"• 平均持仓时间: {avg_holding:.1f} 天")

    if buy_signals is not None and not buy_signals.empty:
        print(f"\n🎯 买入信号统计:")
        print(f"• 总买入信号: {len(buy_signals)}")
        print(f"• 平均信号强度: {buy_signals['signal_strength'].mean():.1f}")
        print(f"• 最强信号: {buy_signals['signal_strength'].max()}")

        # 显示最近的买入信号
        recent_signals = buy_signals.tail(5)
        print(f"\n📅 最近5个买入信号:")
        for _, signal in recent_signals.iterrows():
            print(f"• {signal['date'].strftime('%Y-%m-%d')}: 强度{signal['signal_strength']}, 价格{signal['close']:.2f}")

    if buy_point_analysis:
        print(f"\n⭐ 买点评分分析:")
        print(f"• 优质买点数量: {buy_point_analysis['high_quality_signals']}")
        print(f"• 平均买点评分: {buy_point_analysis['avg_score']:.1f}")
        print(f"• 最佳买点评分: {buy_point_analysis['best_score']:.1f}")

        # 显示评分最高的买点
        if buy_point_analysis['top_signals']:
            print(f"\n🏆 评分最高的买点:")
            for i, signal in enumerate(buy_point_analysis['top_signals'][:3], 1):
                print(f"{i}. {signal['date'].strftime('%Y-%m-%d')}: 评分{signal['score']:.1f}, 价格{signal['close']:.2f}")

    print(f"\n🎯 目标日期匹配检查:")
    for item in target_alignment:
        if item['hit']:
            print(f"• {item['target_date']}: 当天命中")
        elif item['nearby_hit']:
            print(f"• {item['target_date']}: 附近命中 {item['matched_date']}")
        else:
            print(f"• {item['target_date']}: 未命中，原因 {item['blocking_reason']}")

    print(f"\n💡 投资建议:")
    if buy_point_analysis and buy_point_analysis['high_quality_signals'] > 0:
        print("• 当前有优质买点，建议关注")
        print("• 重点关注StochRSI超卖且多重风险过滤确认的信号")
    else:
        print("• 当前无明显优质买点，建议观望")
        print("• 等待StochRSI超卖信号出现")

    print(f"\n✅ 图表已保存至 output/{stock_code}_analysis.png")
    return {
        'data': analysis_data,
        'buy_signals': buy_signals,
        'sell_signals': sell_signals,
        'backtest': backtest_result,
        'buy_analysis': buy_point_analysis
    }


if __name__ == "__main__":
    # 选择分析模式
    parser = argparse.ArgumentParser(
        description="港股技术分析系统 - 支持单股回测与批量分析"
    )
    parser.add_argument('stock_code', nargs='?', default=None,
                        help='股票代码；提供后执行单股深度分析')
    parser.add_argument('legacy_mode', nargs='?', default=None,
                        help='兼容旧模式：single')

    args = parser.parse_args()

    if args.stock_code == "single":
        analyze_single_stock_with_visualization(args.legacy_mode or "03633")
    elif args.stock_code:
        analyze_single_stock_with_visualization(args.stock_code)
    else:
        main()
