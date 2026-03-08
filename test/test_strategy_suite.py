import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from analyzer_core import StockAnalyzer
from indicators import calculate_technical_indicators
from reporting import build_strategy_comparison_tables, generate_strategy_comparison_report
from strategy import (
    STRATEGY_SUITE,
    BollingerUpperStochOrUnifiedSellStrategy,
    BottomVolumeReversalBuyStrategy,
    BullishImpulseCYCBuyStrategy,
    BullishTrend2560BuyStrategy,
    StochOverboughtOrUnifiedSellStrategy,
    TrendPullbackReboundBuyStrategy,
    TrendRangeBandBuyStrategy,
    UnifiedHighVolumeBearishSellStrategy,
)


def make_sample_data(rows=140):
    index = pd.date_range('2025-01-01', periods=rows, freq='D')
    close = np.linspace(10, 18, rows)
    close[80:90] -= np.linspace(0.4, 1.1, 10)
    close[90:100] += np.linspace(0.2, 0.9, 10)
    close[105:115] -= np.linspace(0.3, 0.8, 10)
    close[115:125] += np.linspace(0.2, 0.7, 10)
    open_price = close - 0.08
    high = close + 0.25
    low = close - 0.25
    volume = np.linspace(1000, 2200, rows)
    volume[70:75] *= 2.2
    volume[110:113] *= 1.9
    return pd.DataFrame({
        'Open': open_price,
        'High': high,
        'Low': low,
        'Close': close,
        'Volume': volume,
    }, index=index)


def run_test(name, fn):
    fn()
    print(f"[OK] {name}")


def test_indicators_add_new_columns():
    df = calculate_technical_indicators(make_sample_data())
    assert df is not None
    assert 'Volume_MA60' in df.columns
    assert 'Volume_Cross_5_60' in df.columns
    assert 'MACD_Bullish_Divergence' in df.columns
    assert 'Alpha101_Range_Reversion' in df.columns
    assert 'Alpha101_Volatility_Compression' in df.columns
    assert 'Alpha101_VWAP_Stretch' in df.columns
    assert 'Alpha101_Range_Stability' in df.columns
    assert 'Alpha101_Range_Long_Composite' in df.columns


def test_alpha101_range_factor_scores_are_bounded():
    df = calculate_technical_indicators(make_sample_data())
    columns = [
        'Alpha101_Range_Reversion',
        'Alpha101_Volatility_Compression',
        'Alpha101_VWAP_Stretch',
        'Alpha101_Range_Stability',
        'Alpha101_Range_Long_Composite',
    ]
    for column in columns:
        series = df[column].dropna()
        assert not series.empty
        assert series.between(0, 100).all()


def test_strategy_registry_contains_expected_suite():
    codes = [item['code'] for item in STRATEGY_SUITE]
    assert codes == [
        'current_strategy',
        'bullish_trend_2560',
        'bullish_impulse_cyc',
        'trend_pullback_rebound',
        'trend_range_band',
        'bottom_volume_reversal',
    ]


def test_buy_strategies_return_dataframe_or_none():
    data = calculate_technical_indicators(make_sample_data())
    strategies = [
        BullishTrend2560BuyStrategy(),
        BullishImpulseCYCBuyStrategy(),
        TrendPullbackReboundBuyStrategy(),
        TrendRangeBandBuyStrategy(),
        BottomVolumeReversalBuyStrategy(),
    ]
    for strategy in strategies:
        result = strategy.identify_buy_signals(data)
        assert result is None or {'date', 'close', 'signal_strength', 'entry_type'}.issubset(result.columns)


def test_bottom_volume_reversal_merges_nearby_signals():
    strategy = BottomVolumeReversalBuyStrategy()
    buy_signals = pd.DataFrame([
        {'date': pd.Timestamp('2025-12-23'), 'signal_strength': 8, 'reasons': ['a']},
        {'date': pd.Timestamp('2025-12-24'), 'signal_strength': 9, 'reasons': ['b']},
        {'date': pd.Timestamp('2025-12-31'), 'signal_strength': 7, 'reasons': ['c']},
    ])
    merged = strategy.merge_buy_signal_zones(buy_signals)
    assert len(merged) == 2
    assert merged.iloc[0]['date'] == pd.Timestamp('2025-12-24')
    assert merged.iloc[0]['merged_signal_count'] == 2
    assert merged.iloc[0]['zone_start_date'] == pd.Timestamp('2025-12-23')
    assert merged.iloc[0]['zone_end_date'] == pd.Timestamp('2025-12-24')
    assert merged.iloc[0]['zone_type'] == 'bottom_volume_reversal'


def test_sell_strategies_return_dataframe_or_none():
    data = calculate_technical_indicators(make_sample_data())
    strategies = [
        UnifiedHighVolumeBearishSellStrategy(),
        StochOverboughtOrUnifiedSellStrategy(),
        BollingerUpperStochOrUnifiedSellStrategy(),
    ]
    for strategy in strategies:
        result = strategy.identify_sell_signals(data)
        assert result is None or {'date', 'close', 'signal_strength', 'signal_type'}.issubset(result.columns)


def test_generate_strategy_comparison_report_shapes_matrix():
    report = generate_strategy_comparison_report([
        {
            'strategy_code': 'demo',
            'strategy_name': 'Demo Strategy',
            'portfolio_result': {'ranking': []},
            'per_stock_returns': {'03633': 12.3, '02706': -2.5},
            'summary': {
                'estimated_portfolio_return': 8.5,
                'estimated_portfolio_win_rate': 60.0,
                'estimated_trade_count': 4,
                'selected_count': 2,
            }
        }
    ], ['03633', '02706'])
    assert report is not None
    assert len(report['strategy_summaries']) == 1
    assert report['return_matrix'][0]['03633'] == 12.3
    assert report['return_matrix'][0]['02706'] == -2.5



def test_build_strategy_comparison_tables_shapes_dataframes():
    report = generate_strategy_comparison_report([
        {
            'strategy_code': 'demo',
            'strategy_name': 'Demo Strategy',
            'portfolio_result': {
                'ranking': [
                    {
                        'stock_code': '03633',
                        'ranking_score': 81.2,
                        'entry_type': 'demo_entry',
                        'signal_tier': 'strong',
                        'backtest_return': 12.3,
                    }
                ],
                'selected': [],
            },
            'per_stock_returns': {'03633': 12.3, '02706': -2.5},
            'summary': {
                'estimated_portfolio_return': 8.5,
                'estimated_portfolio_win_rate': 60.0,
                'estimated_trade_count': 4,
                'selected_count': 2,
            }
        }
    ], ['03633', '02706'])
    tables = build_strategy_comparison_tables(report, ['03633', '02706'])
    assert tables is not None
    assert list(tables['summary'].columns) == ['策略', '组合收益率(%)', '胜率(%)', '交易次数', '入选数量']
    assert list(tables['returns'].columns) == ['策略', '03633', '02706']
    assert list(tables['rankings'].columns) == ['策略', '排名', '股票代码', '排名分', '入场类型', '信号层级', '单股收益率(%)']
    assert tables['returns'].iloc[0]['03633'] == 12.3
    assert tables['rankings'].iloc[0]['股票代码'] == '03633'


def test_compare_strategy_suite_with_stub_analyzer():
    original_backtest_portfolio = StockAnalyzer.backtest_portfolio

    def stub_backtest_portfolio(self, stock_codes, days=365, top_n=3, initial_capital=100000):
        analysis_results = []
        ranking = []
        for idx, stock_code in enumerate(stock_codes[:2]):
            analysis_results.append({
                'stock_code': stock_code,
                'backtest': {'total_return': 5.0 + idx, 'win_rate': 50.0, 'total_trades': 1},
                'latest_expected_3m_score': 60.0 + idx,
                'latest_matrix_score': 55.0 + idx,
                'latest_regime_score': 58.0 + idx,
                'latest_entry_type': 'demo_entry',
                'latest_signal_tier': 'strong',
                'latest_signal_date': pd.Timestamp('2025-05-01'),
                'current_signal_active': True,
                'current_signal_actionable': True,
                'current_signal_score': 62.0 + idx,
                'avg_forward_return_60_signal': 6.0,
                'avg_forward_return_60_watch': 0.0,
                'buy_signals': None,
            })
            ranking.append({
                'stock_code': stock_code,
                'ranking_score': 80.0 - idx,
                'entry_type': 'demo_entry',
                'signal_tier': 'strong',
                'backtest_return': 5.0 + idx,
                'current_signal_active': True,
                'current_signal_actionable': True,
            })
        return {
            'stock_pool': stock_codes,
            'top_n': top_n,
            'ranking': ranking,
            'selected': ranking[:top_n],
            'watchlist': [],
            'estimated_portfolio_return': 9.5,
            'estimated_portfolio_win_rate': 66.0,
            'estimated_trade_count': 3,
            'cross_sectional_picks': [],
            'daily_candidate_counts': {},
            'contributions': [],
            'analysis_results': analysis_results,
        }

    StockAnalyzer.backtest_portfolio = stub_backtest_portfolio
    try:
        comparison = StockAnalyzer.compare_strategy_suite(['03633', '02706'], days=120, top_n=2)
    finally:
        StockAnalyzer.backtest_portfolio = original_backtest_portfolio

    assert comparison is not None
    assert len(comparison['strategies']) == len(STRATEGY_SUITE)
    assert comparison['report'] is not None
    assert len(comparison['report']['return_matrix']) == len(STRATEGY_SUITE)


if __name__ == '__main__':
    run_test('indicators add new columns', test_indicators_add_new_columns)
    run_test('alpha101 range factors are bounded', test_alpha101_range_factor_scores_are_bounded)
    run_test('strategy registry contains expected suite', test_strategy_registry_contains_expected_suite)
    run_test('buy strategies return dataframe or none', test_buy_strategies_return_dataframe_or_none)
    run_test('bottom volume reversal merges nearby signals', test_bottom_volume_reversal_merges_nearby_signals)
    run_test('sell strategies return dataframe or none', test_sell_strategies_return_dataframe_or_none)
    run_test('comparison report shapes matrix', test_generate_strategy_comparison_report_shapes_matrix)
    run_test('comparison tables shape dataframes', test_build_strategy_comparison_tables_shapes_dataframes)
    run_test('compare strategy suite with stub analyzer', test_compare_strategy_suite_with_stub_analyzer)
    print('[DONE] strategy suite checks passed')
