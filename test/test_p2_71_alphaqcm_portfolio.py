import numpy as np
import pandas as pd

from backend.services.alphaqcm_portfolio import PortfolioConfig, backtest_signal, composite_signal
from backend.services.alphaqcm_data import prepare_vendor_path

prepare_vendor_path()


def panel():
    dates = pd.bdate_range('2024-01-01', periods=40)
    rows = []
    for i in range(4):
        for j, d in enumerate(dates):
            px = 10 + i + j * .02
            rows.append({'trade_date': d, 'stock_code': f'A{i}', 'open': px,
                         'high': px * 1.01, 'low': px * .99, 'close': px,
                         'volume': 1000 + i, 'vwap': px, 'roe': 10 + i})
    return pd.DataFrame(rows)


def test_composite_signal_and_long_only_curve():
    p = panel()
    factors = [{'name': 'f0', 'expression': 'Mean($close,3)', 'weight': 1.0},
               {'name': 'f1', 'expression': 'Mean($roe,3)', 'weight': .5}]
    signal = composite_signal(factors, p)
    assert signal.notna().any()
    dates = pd.DatetimeIndex(sorted(p.trade_date.unique()))
    bench = pd.Series(np.linspace(100, 110, len(dates)), index=dates)
    result = backtest_signal(p, signal, bench, PortfolioConfig(top_n=2, rebalance_every=5))
    nav = result['nav']
    assert nav.portfolio_nav.notna().all()
    assert (nav.portfolio_nav > 0).all()
    assert nav.benchmark_nav.iloc[-1] > 1
    assert result['observations'] == len(dates) - 1
    assert 'annual_return_252' in result['portfolio_metrics']
