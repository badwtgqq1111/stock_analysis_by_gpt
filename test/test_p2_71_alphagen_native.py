import numpy as np
import pandas as pd
import pytest
from pathlib import Path
import json

from backend.services.alphaqcm_data import prepare_vendor_path
from backend.services.alphagen_native import evaluate_native, validate_native
from backend.services.factor_pool import FactorStore
from scripts.p2_71_import_alphaqcm_factors import import_run

prepare_vendor_path()


def panel(n=120, assets=3):
    dates = pd.bdate_range('2024-01-01', periods=n)
    rows = []
    for i in range(assets):
        close = 10 + i + np.arange(n) * .01
        for d, c in zip(dates, close):
            rows.append({'trade_date': d, 'stock_code': f'A{i}',
                         'open': c, 'high': c * 1.01, 'low': c * .99, 'close': c,
                         'volume': 1000 + i, 'vwap': c, 'roe': 10 + i})
    return pd.DataFrame(rows)


def test_native_rolling_and_comparison_semantics():
    p = panel()
    got = evaluate_native('Mean($close,3)', p)
    expected = p.sort_values(['stock_code', 'trade_date']).groupby('stock_code').close.transform(
        lambda s: s.rolling(3, min_periods=3).mean())
    np.testing.assert_allclose(got.to_numpy(), expected.to_numpy(), equal_nan=True)
    maxed = evaluate_native('Greater($close,$open)', p)
    np.testing.assert_allclose(maxed.to_numpy(), p.close.to_numpy())


def test_native_future_perturbation_is_causal_and_validation_shape():
    p = panel()
    expr = 'Corr($close,$volume,10)'
    base = evaluate_native(expr, p)
    changed = p.copy()
    changed.loc[changed.index[-1], 'close'] *= 2
    perturbed = evaluate_native(expr, changed)
    pd.testing.assert_series_equal(base.iloc[:-1], perturbed.iloc[:-1])
    info = validate_native('Mean($close,10)')
    assert info['dsl_version'] == 'alphagen_v1'
    assert info['variables'][0]['name'] == 'close'
    assert any(o['name'] == 'Mean' for o in info['operators'])


def test_native_financial_feature_and_invalid_future_ref():
    p = panel()
    got = evaluate_native('Mean($roe,5)', p)
    assert got.notna().any()
    with pytest.raises(ValueError):
        validate_native('Ref($close,-5)')


def test_alphaqcm_import_keeps_native_language_and_versioning(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'factors.json').write_text(json.dumps({
        'run_id': 'demo_run', 'factors': [
            {'name': 'f0', 'expression': 'Mean($close,10)', 'weight': 1.0, 'valid_ic': .01},
            {'name': 'f1', 'expression': 'Corr($close,$volume,20)', 'weight': .5, 'valid_ic': .02},
        ],
    }))
    result = import_run(run, db=tmp_path / 'pool.sqlite3')
    assert result['status'] == 'IMPORTED'
    store = FactorStore(tmp_path / 'pool.sqlite3')
    factors = store.list()
    imported = [f for f in factors if f['factor_id'].startswith('aqcm_demo_run_')]
    assert len(imported) == 2
    assert all(f['expression_language'] == 'alphagen_v1' for f in imported)
    assert all(f['semantics']['dsl_version'] == 'alphagen_v1' for f in imported)
    again = import_run(run, db=tmp_path / 'pool.sqlite3')
    assert len(again['skipped']) == 2
