#!/usr/bin/env python3
"""Isolated AlphaQCM worker.

The parent process writes only train and valid panels into a request folder.
This worker never receives the test panel.  Training-side model selection uses
valid IC only; the official ``test_best_table`` path is disabled.

The worker also owns the complete-state checkpointing / resume path:

* ``request['resume']``      load ``checkpoints/latest.pt`` and continue
* ``budget.checkpoint_interval``  full-state snapshot cadence in steps (0=off)
* ``budget.stop_steps`` / ``budget.stop_evals`` / ``budget.max_seconds``
  pre-registered budget stops; a stopped run writes status
  ``STOPPED_*`` and is never exported as a completed 2M-step experiment.
* SIGTERM/SIGINT request a stop at the next step boundary and still checkpoint.

``evaluations`` counts completed candidate expressions handed to the pool
(``AlphaEnvCore._evaluate`` attempts), the budget unit for equal-evaluation
comparisons between QCM and random search.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import signal
import sys
import time
import types

import numpy as np
import pandas as pd
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.alphaqcm_checkpoint import StopTraining, TrainingTracker
from backend.services.alphaqcm_data import prepare_vendor_path, stock_data_class

prepare_vendor_path()
from alphagen.data.expression import Feature, FeatureType, Ref  # noqa: E402
from alphagen.models.alpha_pool import AlphaPool  # noqa: E402
from alphagen.rl.env.wrapper import AlphaEnv  # noqa: E402
from alphagen_qlib.calculator import QLibStockDataCalculator  # noqa: E402
from fqf_iqn_qrdqn.agent import IQCMAgent, QRQCMAgent  # noqa: E402

AGENTS = {'iqn': IQCMAgent, 'qrdqn': QRQCMAgent}

_STOP_STATUS = {
    'wall_clock_budget': 'STOPPED_WALL_CLOCK',
    'step_budget': 'STOPPED_STEP_BUDGET',
    'eval_budget': 'STOPPED_EVAL_BUDGET',
}


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _guard_qcm_moments(agent) -> None:
    original = agent.online_net.calculate_higher_moments

    def guarded(*args, **kwargs):
        last = None
        for _ in range(5):
            try:
                return original(*args, **kwargs)
            except RuntimeError as exc:
                if 'singular' not in str(exc).lower():
                    raise
                last = exc
        raise last

    agent.online_net.calculate_higher_moments = guarded


def _guard_per_sample(agent) -> None:
    memory = agent.memory
    original = memory.sample

    def guarded(*args, **kwargs):
        last = None
        for _ in range(10):
            try:
                return original(*args, **kwargs)
            except IndexError as exc:
                if 'out of bounds' not in str(exc):
                    raise
                memory._cached = None
                last = exc
        raise last

    memory.sample = guarded


def _valid_only_evaluate(agent, output: Path):
    """Disable the official test-calculator access in the training process."""
    def evaluate(self):
        valid_ic = self.env.pool.test_ensemble(self.valid_calculator)
        if valid_ic > self.best_eval_score:
            self.best_eval_score = valid_ic
            self.save_models(str(Path(self.model_dir) / 'best'))
            self.save_exprs(str(output), valid_ic, float('nan'), 'valid')
        self.writer.add_scalar('ic/valid', valid_ic, self.steps)

    agent.evaluate = types.MethodType(evaluate, agent)


def _export(agent, output: Path, run_id: str, meta: dict) -> dict:
    table_path = output / 'valid_best_table.csv'
    factors = []
    if table_path.exists():
        table = pd.read_csv(table_path, index_col=0)
        rows = table[table['exprs'] != 'Ensemble']
        for row in rows.itertuples(index=False):
            factors.append({'name': f'alphaqcm_{run_id}_{len(factors):02d}',
                            'expression': str(row.exprs), 'weight': float(row.weight),
                            'valid_ic': float(row.ic)})
        ensemble_valid = table.loc[table['exprs'] == 'Ensemble', 'ic']
        ensemble_valid = float(ensemble_valid.iloc[0]) if len(ensemble_valid) else None
    else:
        state = agent.env.pool.state
        for expr, weight, ic in zip(state['exprs'], state['weights'], state['ics_ret']):
            factors.append({'name': f'alphaqcm_{run_id}_{len(factors):02d}',
                            'expression': str(expr), 'weight': float(weight),
                            'valid_ic': float(ic)})
        ensemble_valid = float(state['best_ic_ret'])
    payload = {
        'run_id': run_id, 'status': 'EXPORTED_VALID_BEST', 'factors': factors,
        'ensemble_valid_ic': ensemble_valid, 'meta': meta,
    }
    base = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    payload['artifact_sha256'] = hashlib.sha256(base.encode()).hexdigest()
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    (output / 'factors.json').write_text(text + '\n')
    return payload


def _load_agent(request: dict, output: Path):
    budget = dict(request['budget'])
    model = budget.pop('model', 'iqn')
    seed = int(budget.pop('seed', 0))
    pool_capacity = int(budget.pop('pool_capacity', 10))
    std_lam = float(budget.pop('std_lam', 1.0))
    device = torch.device(budget.pop('device', 'cpu'))
    for tracked_key in ('num_steps', 'checkpoint_interval', 'keep_checkpoints',
                        'stop_steps', 'stop_evals', 'max_seconds'):
        budget.pop(tracked_key, None)
    budget['num_steps'] = int(request['num_steps'])
    agent_cls = AGENTS.get(model)
    if agent_cls is None:
        raise ValueError(f'未知模型: {model}')

    train = pd.read_parquet(request['train_path'])
    valid = pd.read_parquet(request['valid_path'])
    for frame in (train, valid):
        frame['trade_date'] = pd.to_datetime(frame['trade_date'])
    PanelStockData = stock_data_class()
    train_data = PanelStockData(train, request['train_start'], request['train_end'],
                                max_backtrack_days=int(request.get('max_backtrack_days', 100)),
                                max_future_days=int(request.get('max_future_days', 25)),
                                device=device, market='cn')
    valid_data = PanelStockData(valid, request['valid_start'], request['valid_end'],
                                max_backtrack_days=int(request.get('max_backtrack_days', 100)),
                                max_future_days=int(request.get('max_future_days', 25)),
                                device=device, market='cn')
    close = Feature(FeatureType.CLOSE)
    target = Ref(close, -20) / close - 1
    train_calc = QLibStockDataCalculator(train_data, target)
    valid_calc = QLibStockDataCalculator(valid_data, target)
    pool = AlphaPool(capacity=pool_capacity, calculator=train_calc,
                     ic_lower_bound=None, l1_alpha=5e-3)
    env = AlphaEnv(pool=pool, device=device, print_expr=False)
    env.pool = pool
    # Remove unsupported constructor keys for older agents, then create.
    allowed = {k: v for k, v in budget.items()
               if k not in {'num_steps', 'cuda', 'seed', 'std_lam'}}
    agent = agent_cls(env=env, valid_calculator=valid_calc, test_calculator=None,
                      log_dir=str(output), seed=seed, std_lam=std_lam,
                      cuda=device.type == 'cuda', num_steps=int(request['num_steps']),
                      **allowed)
    _guard_qcm_moments(agent)
    _guard_per_sample(agent)
    _valid_only_evaluate(agent, output)
    # Local reproducibility contract: the official ``explore()`` samples the gym
    # ``action_space`` whose generator is seeded from OS entropy, so same-seed
    # runs are not reproducible across processes and resume-after-crash cannot
    # be verified.  Seed it (and python ``random``) from the run seed.  The
    # reward/target/optimizer logic is unchanged; only the entropy source of
    # the exploration draw is pinned.
    env.action_space.seed(seed)
    random.seed(seed)
    feature_names = [f.name.lower() for f in train_data._features]
    return agent, model, seed, pool_capacity, std_lam, feature_names


def run(request_path: Path) -> dict:
    request = json.loads(request_path.read_text())
    output = Path(request['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    budget = request['budget']

    agent, model, seed, pool_capacity, std_lam, feature_names = _load_agent(request, output)
    train_sha = _hash(Path(request['train_path']))
    valid_sha = _hash(Path(request['valid_path']))

    tracker = TrainingTracker(
        agent, output,
        interval_steps=int(budget.get('checkpoint_interval', 0) or 0),
        keep_last=int(budget.get('keep_checkpoints', 2) or 2),
        stop_steps=budget.get('stop_steps'),
        stop_evals=budget.get('stop_evals'),
        max_seconds=budget.get('max_seconds'),
        meta={
            'run_id': request['run_id'], 'model': model, 'seed': seed,
            'pool_capacity': pool_capacity, 'std_lam': std_lam,
            'official_commit': request.get('official_commit'),
            'train_sha256': train_sha, 'valid_sha256': valid_sha,
            'features': feature_names,
        })
    tracker.install()

    resumed_from_steps = None

    def _handle_signal(signum, _frame):
        tracker.request_stop(f'signal_{signal.Signals(signum).name}')

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.signal(signum, _handle_signal)
        except ValueError:  # not the main thread (tests)
            pass

    started = time.time()
    status, stop_reason = 'FAILED', 'exception'
    failure: BaseException | None = None
    checkpoint_path: Path | None = None
    try:
        if request.get('resume'):
            latest = output / 'checkpoints' / 'latest.pt'
            if not latest.exists():
                raise FileNotFoundError(f'resume 需要 checkpoint: {latest}')
            tracker.restore(latest)
            resumed_from_steps = int(agent.steps)
        if agent.steps > agent.num_steps:
            status, stop_reason = 'COMPLETED', 'already_complete'
        else:
            agent.run()
            status, stop_reason = 'COMPLETED', 'step_budget'
    except StopTraining as exc:
        stop_reason = exc.reason
        status = _STOP_STATUS.get(
            exc.reason,
            'STOPPED_SIGNAL' if exc.reason.startswith('signal_') else 'STOPPED')
    except BaseException as exc:  # export + status in finally, then re-raise
        failure = exc
        stop_reason = f'exception:{type(exc).__name__}'
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except ValueError:
                pass
        if failure is None:
            try:
                checkpoint_path = tracker.save(reason=stop_reason)
            except Exception as exc:  # checkpoint failure must not mask status
                stop_reason = f'{stop_reason}; checkpoint_failed:{type(exc).__name__}'
        else:
            latest = output / 'checkpoints' / 'latest.pt'
            checkpoint_path = latest if latest.exists() else None
        counters = tracker.counters()
        payload = _export(agent, output, request['run_id'], {
            'repo': 'https://github.com/ZhuZhouFan/AlphaQCM',
            'commit': request.get('official_commit'),
            'model': model, 'seed': seed, 'pool_capacity': pool_capacity,
            'std_lam': std_lam, 'num_steps': int(request['num_steps']),
            'steps_completed': int(agent.steps), 'status': status,
            'stop_reason': stop_reason, 'elapsed_seconds': time.time() - started,
            'train_sha256': train_sha, 'valid_sha256': valid_sha,
            'test_visible_to_worker': False,
            'evaluations': counters['expr_attempts'],
            'expr_errors': counters['expr_errors'],
            'valid_evals': counters['valid_evals'],
            'features': feature_names,
            'explore_rng': 'seeded_from_run_seed',
            'resumed': bool(request.get('resume')),
            'resumed_from_steps': resumed_from_steps,
        })
        (output / 'status.json').write_text(json.dumps({
            'status': status, 'stop_reason': stop_reason,
            'steps_completed': int(agent.steps),
            'evaluations': counters['expr_attempts'],
            'expr_errors': counters['expr_errors'],
            'valid_evals': counters['valid_evals'],
            'features': feature_names,
            'resumed': bool(request.get('resume')),
            'resumed_from_steps': resumed_from_steps,
            'checkpoint_latest': str(checkpoint_path) if checkpoint_path else None,
            'elapsed_seconds': time.time() - started,
            'factor_artifact_sha256': payload['artifact_sha256'],
        }, ensure_ascii=False, indent=2) + '\n')
    if failure is not None:
        raise failure
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    run(args.request)


if __name__ == '__main__':
    main()
