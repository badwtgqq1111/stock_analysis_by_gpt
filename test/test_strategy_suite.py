import importlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core import StockAnalyzer
from portfolio_strategy import STRATEGY_SUITE
from core.reporting import build_strategy_comparison_tables, generate_strategy_comparison_report


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


def test_strategy_registry_contains_expected_suite():
    codes = [item['code'] for item in STRATEGY_SUITE]
    assert codes == ['model_driven']


def test_legacy_strategy_package_is_removed():
    sys.modules.pop("strategy", None)
    try:
        importlib.import_module("strategy")
    except ModuleNotFoundError:
        removed = True
    else:
        removed = False
    assert removed is True

    sys.modules.pop("strategy_signals", None)
    try:
        importlib.import_module("strategy_signals")
    except ModuleNotFoundError:
        removed = True
    else:
        removed = False
    assert removed is True


def test_analyzer_signal_methods_are_available():
    analyzer = StockAnalyzer(db_dir='./assets')
    data = make_sample_data()
    result = analyzer.identify_buy_signals(data)
    assert result is None  # no model score columns in sample data

    sell_result = analyzer.identify_sell_signals(data)
    assert sell_result is None or {'date', 'close', 'signal_strength', 'signal_type'}.issubset(sell_result.columns)

    merged = analyzer.merge_buy_signal_zones(result)
    assert merged is None


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

    def stub_backtest_portfolio(self, stock_codes, days=365, top_n=3, initial_capital=100000, **kwargs):
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
    run_test('strategy registry contains expected suite', test_strategy_registry_contains_expected_suite)
    run_test('analyzer signal methods are available', test_analyzer_signal_methods_are_available)
    run_test('comparison report shapes matrix', test_generate_strategy_comparison_report_shapes_matrix)
    run_test('comparison tables shape dataframes', test_build_strategy_comparison_tables_shapes_dataframes)
    run_test('compare strategy suite with stub analyzer', test_compare_strategy_suite_with_stub_analyzer)
    print('[DONE] strategy suite checks passed')
