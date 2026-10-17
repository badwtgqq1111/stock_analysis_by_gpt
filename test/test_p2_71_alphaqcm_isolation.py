import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

from backend.services.alphaqcm_adapter import (
    AlphaQCMMiner, MinerBudget, MiningArtifact, MiningRequest, evaluate_artifact, run_miner,
)
from backend.services.alphagen_expression import parse_expression


def panel(n_assets=12, n_days=650):
    rng = np.random.default_rng(5)
    dates = pd.bdate_range('2018-01-01', periods=n_days)
    rows = []
    for i in range(n_assets):
        close = 20 * np.exp(np.cumsum(rng.normal(0, .01, n_days)))
        open_ = close * (1 + rng.normal(0, .002, n_days))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, .004, n_days)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, .004, n_days)))
        vol = rng.lognormal(9, .2, n_days)
        for d, o, h, l, c, v in zip(dates, open_, high, low, close, vol):
            rows.append({'trade_date': d, 'stock_code': f'{i:06d}.SZ', 'open': o,
                         'high': h, 'low': l, 'close': c, 'volume': v,
                         'vwap': (h + l + c) / 3})
    return pd.DataFrame(rows)


def tiny_budget(**overrides):
    values = dict(num_steps=150, start_steps=20, eval_interval=30, num_eval_steps=200,
                  max_episode_steps=40, batch_size=8, N=4, N_dash=4, K=4,
                  num_cosines=8, memory_size=1000, update_interval=4,
                  target_update_interval=30, epsilon_decay_steps=20, log_interval=30,
                  pool_capacity=3)
    values.update(overrides)
    return MinerBudget(**values)


def test_miner_request_contains_no_test_panel(tmp_path):
    p = panel()
    train = p[p.trade_date <= '2020-02-28']
    valid = p[(p.trade_date >= '2019-06-01') & (p.trade_date <= '2020-05-15')]
    split = {'train': ('2019-01-01', '2019-12-31'),
             'validation': ('2020-01-02', '2020-03-31'),
             'test': ('2020-04-01', '2020-06-30')}
    request = MiningRequest('iso', train, valid, split, tmp_path / 'run', tiny_budget())
    artifact = run_miner(request)
    files = {p.name for p in artifact.output_dir.iterdir()}
    assert {'train.parquet', 'valid.parquet', 'request.json'} <= files
    assert 'test.parquet' not in files
    req = json.loads((artifact.output_dir / 'request.json').read_text())
    assert 'test_path' not in json.dumps(req)
    assert artifact.status in ('COMPLETED', 'FAILED')
    assert artifact.steps_completed > 0
    assert len(artifact.artifact_sha256) == 64
    assert AlphaQCMMiner().export(artifact).name == 'factors.json'


def test_frozen_artifact_eval_does_not_touch_miner_artifact(tmp_path):
    p = panel()
    test = p[(p.trade_date >= '2019-06-01') & (p.trade_date <= '2020-05-15')]
    split = {'train': ('2019-01-01', '2019-12-31'),
             'validation': ('2020-01-02', '2020-03-31'),
             'test': ('2020-01-02', '2020-03-31')}
    run = tmp_path / 'run'
    run.mkdir()
    payload = {
        'run_id': 'eval', 'status': 'EXPORTED_VALID_BEST',
        'factors': [{'name': 'f0', 'expression': 'Mean($close,10)',
                     'weight': 1.0, 'valid_ic': .01}],
        'ensemble_valid_ic': .01, 'meta': {}, 'artifact_sha256': 'x' * 64,
    }
    (run / 'factors.json').write_text(json.dumps(payload))
    artifact = MiningArtifact('eval', run, payload['factors'], .01,
                              payload['artifact_sha256'], 'COMPLETED', 1,
                              run / 'manifest.json')
    before = (artifact.output_dir / 'factors.json').read_text()
    metrics = evaluate_artifact(artifact, test, split=split, output=tmp_path / 'eval')
    after = (artifact.output_dir / 'factors.json').read_text()
    assert before == after
    assert metrics['test_visible_to_miner'] is False
    assert 'ensemble' in metrics


@pytest.mark.parametrize('source', [
    '__import__("os")', '$close.__class__', '$close[0]', 'Mean($close, -5)',
    'Unknown($close)', 'Ref($close, 1.5)',
])
def test_expression_parser_rejects_non_grammar(source):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/alphaqcm'))
    with pytest.raises(ValueError):
        parse_expression(source)


def test_expression_parser_accepts_exported_grammar():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/alphaqcm'))
    for source in ('Ref($close, 20) / $close - 1', 'Mean($close,10)',
                   'Add(Mul($open,0.5),$low)'):
        assert parse_expression(source) is not None
