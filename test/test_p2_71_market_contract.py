import numpy as np
import pandas as pd
import pytest

from backend.services.market_contract import (
    attach_labels, forward_return, normalize_panel, purged_split, validate_panel,
)


def frame(n_dates=80, n_assets=14):
    dates = pd.bdate_range('2025-01-01', periods=n_dates)
    rows = []
    for i in range(n_assets):
        px = 10 + i + np.arange(n_dates) * 0.01
        for d, p in zip(dates, px):
            rows.append({'trade_date': d, 'stock_code': f'{i:06d}.SZ', 'open': p,
                         'high': p * 1.01, 'low': p * .99, 'close': p,
                         'volume': 1000.0 + i, 'amount': (1000.0 + i) * p,
                         'turnover': 1.0, 'vwap': p * 1.1})
    return pd.DataFrame(rows)


def test_panel_validation_and_labels_are_hand_calculable():
    panel = normalize_panel(frame())
    info = validate_panel(panel)
    assert info['assets'] == 14
    label = forward_return(panel, 3, kind='paper')
    first = panel[panel.stock_code == '000000.SZ'].sort_values('trade_date')
    assert label.loc[(first.trade_date.iloc[0], '000000.SZ')] == pytest.approx(
        first.close.iloc[3] / first.close.iloc[0] - 1)
    executable = forward_return(panel, 3, kind='executable')
    assert executable.loc[(first.trade_date.iloc[0], '000000.SZ')] == pytest.approx(
        first.open.iloc[4] / first.open.iloc[1] - 1)
    joined = attach_labels(panel, 3)
    assert joined.label_paper.notna().sum() > 0
    assert joined.label_executable.notna().sum() > 0


def test_purge_uses_label_horizon_not_calendar_days():
    panel = normalize_panel(frame(100, 3))
    split = {'train': ('2025-01-01', '2025-03-01'),
             'validation': ('2025-03-02', '2025-04-01'),
             'test': ('2025-04-02', '2025-12-31')}
    out = purged_split(panel, split, horizon=20)
    dates = pd.DatetimeIndex(sorted(out.trade_date.unique()))
    train_dates = dates[(dates <= pd.Timestamp('2025-03-01'))]
    assert out[out.trade_date.isin(train_dates[-20:])].segment.unique().tolist() == ['purged']
    assert set(out.segment.dropna().unique()) <= {'train', 'validation', 'test', 'purged'}


def test_duplicate_and_bad_prices_rejected():
    panel = normalize_panel(frame(30, 3))
    with pytest.raises(ValueError):
        validate_panel(pd.concat([panel, panel.iloc[:1]]))
    bad = panel.copy()
    bad.loc[0, 'high'] = bad.loc[0, 'low'] * .5
    with pytest.raises(ValueError):
        validate_panel(bad)


def test_native_vwap_is_flagged_and_proxy_is_qfq_scale():
    panel = normalize_panel(frame(30, 3))
    assert np.allclose(panel.vwap_proxy, (panel.high + panel.low + panel.close) / 3)
    assert (panel.vwap > panel.close).all()
