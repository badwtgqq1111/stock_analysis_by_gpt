"""P2.71 AlphaQCM checkpoint / resume / equal-evaluation-budget tests.

The resume contract under test:

* an interrupted run writes a full-state checkpoint and status
  ``STOPPED_*`` (never exported as a completed experiment);
* resuming from that checkpoint reaches the same steps, evaluation count,
  pool state, memory and network weights as an uninterrupted run with the
  same seed (bitwise, thanks to the seeded explore RNG contract);
* incompatible panels/identities are rejected instead of silently restarting;
* ``stop_evals`` is the expression-evaluation budget used for equal-budget
  QCM vs random-search comparisons, and it is recorded in status/manifest.
"""
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
import torch

from backend.services.alphaqcm_adapter import MinerBudget, MiningRequest, run_miner
from backend.services.alphaqcm_checkpoint import StopTraining, TrainingTracker

VENDOR = Path(__file__).resolve().parents[1] / 'vendor/alphaqcm'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


def panel(n_assets=12, n_days=650, seed=5):
    rng = np.random.default_rng(seed)
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


def slices(source):
    train = source[source.trade_date <= '2020-02-28']
    valid = source[(source.trade_date >= '2019-06-01') & (source.trade_date <= '2020-05-15')]
    split = {'train': ('2019-01-01', '2019-12-31'),
             'validation': ('2020-01-02', '2020-03-31'),
             'test': ('2020-04-01', '2020-06-30')}
    return train, valid, split


def tiny_budget(**overrides):
    values = dict(num_steps=200, start_steps=40, eval_interval=50, num_eval_steps=200,
                  max_episode_steps=40, batch_size=8, N=4, N_dash=4, K=4,
                  num_cosines=8, memory_size=1000, update_interval=4,
                  target_update_interval=30, epsilon_decay_steps=20, log_interval=30,
                  pool_capacity=3)
    values.update(overrides)
    return MinerBudget(**values)


def _run(dir_path, train, valid, split, budget, *, run_id='resume', resume=False):
    request = MiningRequest(run_id, train, valid, split, dir_path, budget,
                            metadata={'test': 'checkpoint'}, resume=resume)
    return run_miner(request)


def _factors(output_dir):
    return json.loads((output_dir / 'factors.json').read_text())


def _latest(output_dir):
    return torch.load(output_dir / 'checkpoints' / 'latest.pt',
                      map_location='cpu', weights_only=False)


def _assert_tree_equal(left, right, path=''):
    if isinstance(left, dict):
        assert isinstance(right, dict) and set(left) == set(right), path
        for key in left:
            _assert_tree_equal(left[key], right[key], f'{path}.{key}')
    elif isinstance(left, (list, tuple)):
        assert len(left) == len(right), path
        for i, (a, b) in enumerate(zip(left, right)):
            _assert_tree_equal(a, b, f'{path}[{i}]')
    elif isinstance(left, torch.Tensor):
        assert torch.equal(left, right), f'{path} 不一致'
    else:
        assert left == right, f'{path} 不一致'


def _assert_memory_equal(left, right):
    assert left['n'] == right['n'] and left['p'] == right['p']
    for key in ('state', 'next_state'):
        assert len(left[key]) == len(right[key])
        for i, (a, b) in enumerate(zip(left[key], right[key])):
            assert np.array_equal(a, b), f'memory.{key}[{i}] 不一致'
    for key in ('action', 'reward', 'done'):
        assert np.array_equal(left[key], right[key]), f'memory.{key} 不一致'


def test_resume_matches_uninterrupted_run(tmp_path):
    source = panel()
    train, valid, split = slices(source)

    straight_dir = tmp_path / 'straight'
    straight = _run(straight_dir, train, valid, split,
                    tiny_budget(num_steps=6000, checkpoint_interval=500))
    assert straight.status == 'COMPLETED'
    assert straight.resumed is False
    assert straight.evaluations >= 1, '该配置下应至少完成一次表达式评价'

    resumed_dir = tmp_path / 'resumed'
    interrupted = _run(resumed_dir, train, valid, split,
                       tiny_budget(num_steps=6000, stop_steps=3000,
                                   checkpoint_interval=500))
    assert interrupted.status == 'STOPPED_STEP_BUDGET'
    assert interrupted.stop_reason == 'step_budget'
    assert interrupted.steps_completed == 3000
    assert interrupted.checkpoint_latest is not None
    assert (resumed_dir / 'checkpoints' / 'latest.pt').exists()

    resumed = _run(resumed_dir, train, valid, split,
                   tiny_budget(num_steps=6000, checkpoint_interval=500), resume=True)
    assert resumed.status == 'COMPLETED'
    assert resumed.resumed is True
    assert resumed.resumed_from_steps == 3000

    assert resumed.steps_completed == straight.steps_completed
    assert resumed.evaluations == straight.evaluations
    assert resumed.expr_errors == straight.expr_errors
    assert resumed.valid_evals == straight.valid_evals

    left, right = _latest(resumed_dir), _latest(straight_dir)
    assert left['agent']['steps'] == right['agent']['steps']
    _assert_tree_equal(left['agent']['nets'], right['agent']['nets'], 'nets')
    _assert_tree_equal(left['agent']['optims'], right['agent']['optims'], 'optims')
    _assert_memory_equal(left['agent']['memory'], right['agent']['memory'])
    for key in ('exprs', 'size', 'eval_cnt'):
        assert left['agent']['pool'][key] == right['agent']['pool'][key], key
    for key in ('single_ics', 'mutual_ics', 'weights'):
        assert np.array_equal(left['agent']['pool'][key],
                              right['agent']['pool'][key], equal_nan=True), key
    assert left['agent']['train_return_stats'] == right['agent']['train_return_stats']
    for key in ('expr_attempts', 'expr_errors', 'valid_evals'):
        assert left['tracker'][key] == right['tracker'][key], key

    factors_left, factors_right = _factors(resumed_dir), _factors(straight_dir)
    assert [f['expression'] for f in factors_left['factors']] == \
           [f['expression'] for f in factors_right['factors']]
    if factors_left['factors']:
        assert np.allclose([f['weight'] for f in factors_left['factors']],
                           [f['weight'] for f in factors_right['factors']], atol=0)
        assert np.allclose([f['valid_ic'] for f in factors_left['factors']],
                           [f['valid_ic'] for f in factors_right['factors']], atol=0)

    manifest = json.loads((resumed_dir / 'manifest.json').read_text())
    assert manifest['resumed_from_steps'] == 3000
    assert manifest['evaluations'] == resumed.evaluations
    assert manifest['status'] == 'COMPLETED'
    assert manifest['explore_rng'] == 'seeded_from_run_seed'


def test_eval_budget_stop_is_recorded(tmp_path):
    source = panel()
    train, valid, split = slices(source)
    out = tmp_path / 'evalbudget'
    artifact = _run(out, train, valid, split,
                    tiny_budget(num_steps=20000, start_steps=40, stop_evals=5))
    assert artifact.status == 'STOPPED_EVAL_BUDGET'
    assert artifact.stop_reason == 'eval_budget'
    # one completed expression per step at most, so overshoot is <= 1
    assert 5 <= artifact.evaluations <= 6
    assert artifact.steps_completed < 20000

    status = json.loads((out / 'status.json').read_text())
    assert status['evaluations'] == artifact.evaluations
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['evaluations'] == artifact.evaluations
    assert manifest['status'] == 'STOPPED_EVAL_BUDGET'
    factors = _factors(out)
    assert factors['meta']['evaluations'] == artifact.evaluations
    assert factors['meta']['stop_reason'] == 'eval_budget'


def test_resume_rejects_incompatible_panel(tmp_path):
    source_a = panel(seed=5)
    train_a, valid_a, split = slices(source_a)
    out = tmp_path / 'reject'
    _run(out, train_a, valid_a, split,
         tiny_budget(num_steps=200, stop_steps=60, checkpoint_interval=30))

    source_b = panel(seed=6)  # same shape, different prices -> different sha256
    train_b, valid_b, _ = slices(source_b)
    with pytest.raises(RuntimeError) as excinfo:
        _run(out, train_b, valid_b, split,
             tiny_budget(num_steps=200), resume=True)
    assert '不兼容' in str(excinfo.value)


def test_resume_without_checkpoint_fails(tmp_path):
    source = panel()
    train, valid, split = slices(source)
    with pytest.raises(RuntimeError) as excinfo:
        _run(tmp_path / 'empty', train, valid, split,
             tiny_budget(num_steps=200), resume=True)
    assert 'resume 需要 checkpoint' in str(excinfo.value)


class _DummyAgent:
    steps = 0


def test_stop_request_raises_stop_training(tmp_path):
    tracker = TrainingTracker(_DummyAgent(), tmp_path)
    tracker.request_stop('signal_SIGTERM')
    with pytest.raises(StopTraining) as excinfo:
        tracker._after_step()
    assert excinfo.value.reason == 'signal_SIGTERM'
