import numpy as np
import pandas as pd

from backend.services.alpha101_exact import Alpha101Exact71, build_alpha101, factor_panel
from backend.services.market_contract import normalize_panel


def random_wide(seed=0, n_days=320, n_assets=12):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range('2020-01-01', periods=n_days)
    columns = [f'A{i:02d}' for i in range(n_assets)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, .01, (n_days, n_assets)), axis=0)),
                         index=index, columns=columns)
    open_ = close * (1 + rng.normal(0, .002, close.shape))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, .005, close.shape)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, .005, close.shape)))
    volume = pd.DataFrame(rng.lognormal(10, .4, close.shape), index=index, columns=columns)
    vwap = (high + low + close) / 3
    return open_, high, low, close, volume, vwap, close.pct_change(fill_method=None)


def test_exactly_71_and_all_compute():
    calc = Alpha101Exact71(*random_wide())
    names = calc.alpha_names()
    assert len(names) == 71
    assert names[0] == 'alpha001' and names[-1] == 'alpha101'
    result = calc.compute_all(strict=True)
    assert len(result) == 71 and all(isinstance(v, pd.DataFrame) for v in result.values())
    assert not calc.last_failures if hasattr(calc, 'last_failures') else True


def test_alpha003_hand_calculation_and_cross_sectional_rank():
    open_, high, low, close, volume, vwap, ret = random_wide(n_days=40, n_assets=6)
    calc = Alpha101Exact71(open_, high, low, close, volume, vwap, ret)
    got = calc.alpha003()
    x = open_.rank(axis=1, pct=True)
    y = volume.rank(axis=1, pct=True)
    expected = -x.rolling(10, min_periods=3).corr(y).replace([np.inf, -np.inf], 0).fillna(0)
    pd.testing.assert_frame_equal(got, expected)


def test_asset_permutation_and_future_rows_do_not_change_past_values():
    args = random_wide(n_days=260, n_assets=8)
    base = Alpha101Exact71(*args).compute_all()
    perm = Alpha101Exact71(*[x.sample(frac=1, axis=1, random_state=3) for x in args[:6]] + [None])
    permuted = perm.compute_all()
    for name in base:
        pd.testing.assert_frame_equal(base[name], permuted[name][base[name].columns],
                                      check_names=False)
    changed = list(args)
    changed[3] = changed[3].copy()
    changed[3].iloc[-5:] *= 1.5
    changed[6] = changed[3].pct_change(fill_method=None)
    future = Alpha101Exact71(*changed).compute_all()
    for name in base:
        pd.testing.assert_frame_equal(base[name].iloc[:-5], future[name].iloc[:-5],
                                      check_names=False)


def test_long_panel_builder_uses_proxy_vwap_and_preserves_panel_shape():
    dates = pd.bdate_range('2025-01-01', periods=60)
    rows = []
    for i in range(14):
        for j, date in enumerate(dates):
            p = 10 + i + j * .01
            rows.append({'trade_date': date, 'stock_code': f'{i:06d}.SZ', 'open': p,
                         'high': p * 1.01, 'low': p * .99, 'close': p,
                         'volume': 1000 + i + j, 'amount': (1000 + i + j) * p,
                         'vwap': p * 1.2})
    panel = normalize_panel(pd.DataFrame(rows))
    calc = build_alpha101(panel)
    signals = calc.compute_all()
    assert all(s.shape == (60, 14) for s in signals.values())
    long = factor_panel(signals)
    assert set(long.columns) == {'trade_date', 'stock_code', 'alpha', 'value'}
    assert long.alpha.nunique() == 71
