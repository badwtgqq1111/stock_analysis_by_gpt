"""Independent factor vertical slice: persistence, DSL causality, pinned rule parity."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy
import sys

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.factor_pool import FactorStore, Conflict, families, annotate_strategies
from backend.services.factor_dsl import validate, evaluate, panel
from backend.services.factor_moneyflow import build_signals
from backend.api.factor_pool import router


@pytest.fixture
def store(tmp_path):
    return FactorStore(tmp_path / 'pool.sqlite3')


@pytest.fixture
def frames():
    dates = pd.bdate_range('2025-01-01', periods=55)
    bars, flow = [], []
    for i, code in enumerate(['000001.SZ', '000002.SZ', '600000.SH', '600001.SH']):
        for t, date in enumerate(dates):
            close = 10 + .02 * t + .1 * np.sin(t + i)
            bars.append(dict(stock_code=code, trade_date=date, close=close, open=close-.01, amount=3e7 if i < 3 else 1e7))
            flow.append(dict(stock_code=code, trade_date=date, moneyflow_net_z_5d=1.0 if t % 3 else 1.5,
                             moneyflow_positive_ratio_10d=.6 if i != 2 else .59))
    return pd.DataFrame(bars), pd.DataFrame(flow)


def legacy_signals(bars, flow, monkeypatch):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import moneyflow_study as legacy
    monkeypatch.setattr(legacy.ws, '_universe', lambda: set(bars.stock_code))
    return legacy.build_signals(bars, flow)


def comparable(df):
    columns = ['stock_code', 'trade_date', 'close', 'rh', 'rl', 'cause_width', 'moneyflow_net_z_5d', 'moneyflow_positive_ratio_10d', 'ret20', 'amount_ma']
    return df[columns].sort_values(['stock_code', 'trade_date']).reset_index(drop=True)


def test_seed_explains_every_variable(store):
    for f in store.list():
        assert f['sources'] and f['change_note']
        assert f['evaluation_status'] == 'unmeasured'
        for v in f['semantics']['variables']:
            assert all(v.get(k) for k in ('name', 'label', 'unit', 'window', 'definition', 'availability'))
        assert f['sha256'] == store.get(f['factor_id'], 1)['sha256']


def test_save_reload_restore_and_pinned_versions(store):
    first = store.get('mf_strength')
    edited = store.save('mf_strength', {**first, 'formula': 'rank(moneyflow_net_z_5d)', 'change_note': 'test optimize', 'action': 'optimize'}, 1)
    assert edited['version'] == 2 and edited['sha256'] != first['sha256']
    again = FactorStore(store.path)
    assert again.get('mf_strength')['formula'].startswith('rank(')
    assert again.get('mf_strength', 1) == first
    assert edited['parent_factor_id'] == 'mf_strength' and edited['parent_version'] == 1
    restored = again.save('mf_strength', {**first, 'change_note': 'restore first formula', 'action': 'restore'}, 2)
    assert restored['version'] == 3 and restored['formula'] == first['formula']
    assert len(again.history('mf_strength')) == 3


def test_optimistic_lock_and_concurrent_writes(store):
    data = store.get('mf_strength')
    def save():
        try:
            return store.save('mf_strength', data, 1)['version']
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(lambda _: save(), range(2)))) == ['2', 'conflict']
    assert len(store.history('mf_strength')) == 2


def test_fork_has_verified_lineage(store):
    original = store.get('mf_strength')
    fork = store.save('new_strength', {**original, 'action': 'create'}, 0, {'factor_id': 'mf_strength', 'version': 1})
    assert fork['parent_factor_id'] == 'mf_strength'
    with pytest.raises(ValueError, match='父因子'):
        store.save('bad_parent', original, 0, {'factor_id': 'absent', 'version': 1})
    assert store.get('mf_strength') == original


@pytest.mark.parametrize('formula', [
    '__import__("os")', 'close.__class__', 'close[0]', 'delay(close,-1)', 'delay(close,0)', 'mean(close,253)',
    'mean(close, 1.5)', 'mean(close,True)', 'close ** 2', '[close]', 'unknown + 1', 'rank(close,1)',
    '1 + 2', 'mean(close, n=2)', 'close and amount', '(close > 1) + amount', 'close > 1e999',
])
def test_invalid_or_future_expression_rejected(formula):
    with pytest.raises(ValueError):
        validate(formula)


def test_ast_rolling_rank_missing_and_panel_order(frames):
    bars, _ = frames
    df = panel(bars)
    values = evaluate('close / delay(close, 20) - 1', bars.sample(frac=1, random_state=2))
    expected = df.close / df.groupby('stock_code').close.shift(20) - 1
    pd.testing.assert_series_equal(values, expected, check_names=False)
    pd.testing.assert_series_equal(evaluate('mean(close,20)', bars), df.groupby('stock_code').close.transform(lambda x: x.rolling(20).mean()), check_names=False)
    pd.testing.assert_series_equal(evaluate('rank(close)', bars), df.groupby('trade_date').close.rank(pct=True), check_names=False)
    assert evaluate('close / 0', bars).isna().all()
    assert not evaluate('close / 0 != 1', bars).any()
    assert evaluate('close >= 10 and amount > 0', bars).equals((df.close >= 10) & (df.amount > 0))
    with pytest.raises(ValueError, match='重复'):
        evaluate('close', pd.concat([bars, bars.iloc[:1]]))


def test_causal_and_asset_permutation_invariance(frames):
    bars, _ = frames
    changed = bars.copy()
    cutoff = bars.trade_date.unique()[30]
    changed.loc[changed.trade_date > cutoff, 'close'] *= 10
    old, new = evaluate('mean(close,20)/delay(close,1)', bars), evaluate('mean(close,20)/delay(close,1)', changed)
    mask = panel(bars).trade_date <= cutoff
    pd.testing.assert_series_equal(old[mask], new[mask])
    pd.testing.assert_series_equal(old, evaluate('mean(close,20)/delay(close,1)', bars.sample(frac=1, random_state=8)))


def test_moneyflow_matches_original_all_fields_and_rank(store, frames, monkeypatch):
    bars, flow = frames
    expected = legacy_signals(bars, flow, monkeypatch)
    actual = build_signals(bars, flow, store, set(bars.stock_code))
    assert len(actual) > 0
    pd.testing.assert_frame_equal(comparable(expected), comparable(actual), check_dtype=False)
    order = expected.sort_values(['trade_date', 'cause_width', 'stock_code'])[['trade_date', 'stock_code']].reset_index(drop=True)
    pd.testing.assert_frame_equal(order, actual[['trade_date', 'stock_code']])
    # Editing latest never changes fixed revision 1 used by the strategy.
    seed = store.get('mf_strength')
    store.save('mf_strength', {**seed, 'formula': 'moneyflow_net_z_5d * 100'}, 1)
    pd.testing.assert_frame_equal(actual, build_signals(bars, flow, store, set(bars.stock_code)))


def test_moneyflow_boundaries_missing_and_empty_universe(store, frames, monkeypatch):
    bars, flow = frames
    day = bars.trade_date.max()
    flow.loc[(flow.trade_date == day) & (flow.stock_code == '000001.SZ'), 'moneyflow_net_z_5d'] = np.nan
    flow.loc[(flow.trade_date == day) & (flow.stock_code == '000002.SZ'), 'moneyflow_positive_ratio_10d'] = np.inf
    expected = legacy_signals(bars, flow.replace(np.inf, np.nan), monkeypatch)
    actual = build_signals(bars, flow, store, set(bars.stock_code))
    pd.testing.assert_frame_equal(comparable(expected), comparable(actual), check_dtype=False)
    assert build_signals(bars, flow, store, set()).empty
    with pytest.raises(ValueError, match='重复'):
        build_signals(bars, pd.concat([flow, flow.iloc[:1]]), store, set(bars.stock_code))


def test_strategy_taxonomy_is_orthogonal_and_unknown_stays_unknown():
    payload = annotate_strategies({'strategies': [{'id': 'moneyflow_arm'}, {'id': 'unknown'}]})
    assert payload['strategies'][0]['factor_design']['implementation_version'] == 'original'
    assert payload['strategies'][1]['factor_design'] is None
    variants = families()[0]['variants']
    assert len(variants) == 3 and variants[1]['factor_refs'][0]['version'] == 1
    assert variants[2]['status'] == 'planned'


def test_api_save_reload_errors_preview_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTOR_POOL_DB', str(tmp_path / 'api.sqlite3'))
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as c:
        assert len(c.get('/api/factor-pool').json()['factors']) == 6
        old = c.get('/api/factor-pool/mf_strength').json()
        assert c.post('/api/factor-pool/validate', json={'formula': 'delay(close,-1)'}).status_code == 422
        req = {**old, 'formula': 'moneyflow_net_z_5d * 2', 'expected_version': 1}
        saved = c.post('/api/factor-pool/mf_strength/versions', json=req)
        assert saved.status_code == 200 and saved.json()['version'] == 2
        assert c.post('/api/factor-pool/mf_strength/versions', json=req).status_code == 409
        assert c.get('/api/factor-pool/mf_strength?version=1').json() == old
        assert len(c.get('/api/factor-pool/mf_strength/history').json()['versions']) == 2
        assert c.get('/api/factor-pool/absent').status_code == 404
        p = c.post('/api/factor-pool/preview', json={'formula': 'delay(close,1)', 'records': [dict(stock_code='a',trade_date='2025-01-01',close=10),dict(stock_code='a',trade_date='2025-01-02',close=12)]})
        assert p.status_code == 200
        assert [r['value'] for r in p.json()['rows']] == [None, 10]
        assert c.post('/api/factor-pool/preview', json={'formula': 'close', 'records': []}).status_code == 422
    with TestClient(app) as reopened:
        assert reopened.get('/api/factor-pool/mf_strength').json()['version'] == 2


def test_new_path_has_no_legacy_model_dependencies():
    for name in ('factor_pool', 'factor_dsl', 'factor_moneyflow'):
        source = (ROOT / f'backend/services/{name}.py').read_text()
        assert 'import lightgbm' not in source
        assert 'import moneyflow_study' not in source
        assert 'import wyckoff_study' not in source


def test_sparse_provider_rows_are_not_silently_deduplicated(store, frames, monkeypatch):
    bars, flow = frames
    primary = flow.assign(source='moneyflow')
    sparse = flow.assign(source='moneyflow_dc', moneyflow_net_z_5d=np.nan, moneyflow_positive_ratio_10d=np.nan)
    raw = pd.concat([sparse, primary], ignore_index=True)
    expected = legacy_signals(bars, raw, monkeypatch)
    actual = build_signals(bars, raw, store, set(bars.stock_code))
    pd.testing.assert_frame_equal(comparable(expected), comparable(actual), check_dtype=False)
    raw.loc[0, 'moneyflow_net_z_5d'] = 1
    with pytest.raises(ValueError, match='数据契约'):
        build_signals(bars, raw, store, set(bars.stock_code))


@pytest.mark.parametrize('change,should_pass', [
    ({}, True), ({'z': .999}, False), ({'p': .5999}, False),
    ({'amount': 19999999}, False), ({'amount': 20000000}, True),
    ({'close': 9}, True), ({'close': 11}, False),
    ({'close': 2}, False), ({'close': np.nan}, False), ({'z': np.nan}, False),
])
def test_each_hard_gate_boundary(store, monkeypatch, change, should_pass):
    dates = pd.bdate_range('2025-01-01', periods=21)
    bars = pd.DataFrame({'stock_code': ['x']*21, 'trade_date': dates, 'close': [10]+[8]*19+[10.5], 'amount': [2e7]*21})
    flow = pd.DataFrame({'stock_code': ['x']*21, 'trade_date': dates, 'moneyflow_net_z_5d': [1.0]*21, 'moneyflow_positive_ratio_10d': [.6]*21})
    for k, v in change.items():
        if k in ('amount', 'close'):
            # Entire-window amount exactly tests the rolling threshold.
            if k == 'amount': bars[k] = v
            else: bars.loc[20, k] = v
        else:
            flow.loc[20, 'moneyflow_net_z_5d' if k == 'z' else 'moneyflow_positive_ratio_10d'] = v
    expected = legacy_signals(bars, flow, monkeypatch)
    actual = build_signals(bars, flow, store, {'x'})
    assert bool(len(actual)) == should_pass
    if should_pass:
        pd.testing.assert_frame_equal(comparable(expected), comparable(actual), check_dtype=False)
    else:
        assert expected.empty and actual.empty
