#!/usr/bin/env python3
"""P2.71 full-budget (2,000,000-step) AlphaQCM run: pre-registration, resource
probe, resumable execution and launch.

The P2.71 plan only approves the full ~2M-step budget after (a) the complete
checkpoint snapshot exists (done, ``backend/services/alphaqcm_checkpoint.py``)
and (b) a per-1k-step time/memory estimate is recorded.  This script:

1. ``--plan``    writes ``run_plan.json`` before any full run (pre-registered
   budget, official constants vs CPU adaptations, status/resume protocol).
2. ``--probe``   runs a short warmup at the *full* config and records
   seconds/1k-step and child peak RSS into ``budget_estimate.json``.
3. ``--run``     executes the single-seed 2M run (resumable; every
   ``checkpoint_interval`` steps a full-state snapshot is written).
4. ``--launch``  spawns ``--run`` detached with a timestamped log, so the run
   survives the invoking session and can be resumed from the latest snapshot.

Statuses: COMPLETED / STOPPED_* / FAILED come from the worker status.json; a
session that dies mid-run leaves the latest checkpoint for ``--resume``.
Partial runs are archived as partial and are never extrapolated to the paper
budget.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.alphaqcm_adapter import (  # noqa: E402
    MinerBudget, MiningRequest, evaluate_artifact, run_miner,
)
from scripts.p2_71_alphaqcm_pilot import _slice, select_panel  # noqa: E402

FULL_STEPS = 2_000_000
BUDGET_2M = {
    # official FactorBench/IQN constants (doc §3.1)
    'num_steps': FULL_STEPS, 'start_steps': 10_000, 'eval_interval': 1_000,
    'pool_capacity': 10, 'gamma': 1.0, 'use_per': True,
    # CPU adaptations (same as the pilot, documented as deviations)
    'batch_size': 32, 'N': 16, 'N_dash': 16, 'K': 8, 'num_cosines': 16,
    'memory_size': 20_000, 'update_interval': 4, 'max_episode_steps': 100,
    'num_eval_steps': 5_000, 'log_interval': 10_000,
    # long-run constants chosen locally (not listed in the doc)
    'target_update_interval': 1_000, 'epsilon_decay_steps': 10_000,
    # checkpoint / budget controls
    'checkpoint_interval': 25_000, 'keep_checkpoints': 2,
}

SPLIT = {'train': ('2018-01-01', '2022-12-31'),
         'validation': ('2023-01-01', '2024-12-31'),
         'test': ('2025-01-01', '2025-10-31')}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _budget(seed: int, **overrides) -> MinerBudget:
    values = dict(BUDGET_2M)
    values.update(overrides)
    return MinerBudget(seed=seed, std_lam=1.0, **values)


def _slices(with_fundamentals: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    panel = select_panel(60, with_fundamentals=with_fundamentals)
    train = _slice(panel, '2017-08-01', '2023-02-28')
    valid = _slice(panel, '2022-08-01', '2025-02-28')
    test = _slice(panel, '2024-07-01', '2026-01-31')
    return panel, train, valid, test


def write_plan(output: Path, *, steps: int, with_fundamentals: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / 'run_plan.json'
    plan = {
        'created_at': _now(),
        'registered_before_runs': True,
        'purpose': f'full {steps:,}-step AlphaQCM/IQN run (single seed, CPU)',
        'steps': steps,
        'seed': 0,
        'panel': {'n_assets': 60,
                  'selection': 'train-period liquidity, same as pilot v1',
                  'with_fundamentals': with_fundamentals},
        'slices': SPLIT,
        'budget': {**BUDGET_2M, 'num_steps': steps},
        'official_constants': ['num_steps~2M', 'start_steps=10,000', 'eval_interval=1,000',
                               'pool_capacity=10', 'gamma=1', 'use_per=True'],
        'cpu_adaptations': ['batch_size=32 vs 128', 'N/N_dash/K=16/16/8 vs 64/64/32',
                            'memory_size=20,000 (RAM cap)', 'target_update/epsilon_decay chosen locally'],
        'resource_gate': 'per-1k-step time and worker peak RSS must be recorded '
                         '(budget_estimate.json) before launch',
        'checkpointing': {'interval_steps': BUDGET_2M['checkpoint_interval'],
                          'keep': BUDGET_2M['keep_checkpoints'],
                          'resume': 'scripts/p2_71_alphaqcm_2m.py --run --resume'},
        'status_semantics': ('COMPLETED = full step budget reached; STOPPED_* = budget/signal '
                             'stop with full-state checkpoint; FAILED = not exported'),
        'decision_rule': ('partial runs are archived as partial and never extrapolated to the '
                          'paper budget; this is not a promotion test'),
        'source_hashes': {str(p.relative_to(ROOT)): _hash(p) for p in [
            ROOT / 'scripts/p2_71_alphaqcm_2m.py',
            ROOT / 'scripts/p2_71_alphaqcm_pilot.py',
            ROOT / 'backend/services/alphaqcm_adapter.py',
            ROOT / 'backend/services/alphaqcm_checkpoint.py',
            ROOT / 'scripts/alphaqcm_worker.py',
        ]},
    }
    if plan_path.exists():
        prior = json.loads(plan_path.read_text())
        plan['created_at'] = prior.get('created_at', plan['created_at'])
        plan['amended_at'] = _now()
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    return plan


def probe(output: Path, *, seed: int, probe_steps: int,
          with_fundamentals: bool = True) -> dict:
    """Measure seconds per 1k steps and worker peak RSS at the full config."""
    _, train, valid, _ = _slices(with_fundamentals)
    budget = _budget(seed, num_steps=probe_steps,
                     start_steps=max(200, probe_steps - 400),
                     eval_interval=1_000, checkpoint_interval=0,
                     log_interval=max(100, probe_steps // 2))
    request = MiningRequest('probe', train, valid, SPLIT, output / 'probe',
                            budget, metadata={'purpose': 'budget probe'})
    started = time.time()
    artifact = run_miner(request)
    wall = time.time() - started
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    rss_mb = rss / (1024 * 1024) if sys.platform == 'darwin' else rss / 1024
    estimate = {
        'created_at': _now(),
        'probe_steps': probe_steps,
        'wall_seconds': round(wall, 2),
        'seconds_per_1k_steps': round(wall / probe_steps * 1000, 3),
        'worker_peak_rss_mb': round(rss_mb, 1),
        'steps_completed': artifact.steps_completed,
        'status': artifact.status,
        'projection_full_run_hours': round(wall / probe_steps * FULL_STEPS / 3600, 2),
        'projection_checkpoint_writes': FULL_STEPS // BUDGET_2M['checkpoint_interval'],
        'config': {**BUDGET_2M, 'num_steps': FULL_STEPS},
    }
    (output / 'budget_estimate.json').write_text(
        json.dumps(estimate, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(estimate, ensure_ascii=False, indent=2))
    return estimate


def execute(output: Path, *, seed: int, steps: int, resume: bool,
            max_seconds: float | None, with_fundamentals: bool = True) -> dict:
    _, train, valid, test = _slices(with_fundamentals)
    request = MiningRequest(
        f'iqn_2m_s{seed}', train, valid, SPLIT, output / f'run_s{seed}',
        _budget(seed, num_steps=steps, max_seconds=max_seconds),
        metadata={'full_budget': True, 'seed': seed, 'steps': steps,
                  'plant': str(output / 'run_plan.json')},
        resume=resume)
    started = time.time()
    artifact = run_miner(request)
    wall = time.time() - started
    metrics = evaluate_artifact(artifact, test, split=SPLIT,
                                output=artifact.output_dir / 'test_eval',
                                label='paper')
    ens = metrics['ensemble'].get('spearman', {})
    record = {
        'run_id': artifact.run_id, 'status': artifact.status,
        'stop_reason': artifact.stop_reason,
        'steps_completed': artifact.steps_completed,
        'evaluations': artifact.evaluations,
        'resumed': artifact.resumed,
        'resumed_from_steps': artifact.resumed_from_steps,
        'factor_count': len(artifact.factors),
        'ensemble_valid_ic': artifact.ensemble_valid_ic,
        'test_rank_ic': ens.get('mean'), 'test_rank_icir': ens.get('icir'),
        'test_nw_t': ens.get('nw_t'), 'test_dates': ens.get('dates'),
        'wall_seconds': round(wall, 2),
        'checkpoint_latest': artifact.checkpoint_latest,
        'artifact_sha256': artifact.artifact_sha256,
        'at': _now(),
    }
    with (output / 'runs.jsonl').open('a') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    return record


def launch(output: Path, *, seed: int, steps: int, resume: bool,
           max_seconds: float | None, with_fundamentals: bool = True) -> dict:
    log_dir = output / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    log_path = log_dir / f'run_{stamp}.log'
    command = [sys.executable, str(Path(__file__).resolve()), '--run',
               '--output', str(output), '--seed', str(seed), '--steps', str(steps)]
    if resume:
        command.append('--resume')
    if max_seconds is not None:
        command += ['--max-seconds', str(max_seconds)]
    if with_fundamentals:
        command.append('--fundamentals')
    with log_path.open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, cwd=str(ROOT))
    launch_doc = {'pid': process.pid, 'command': command, 'log': str(log_path),
                  'at': _now(), 'resume': resume}
    (output / 'launch.json').write_text(
        json.dumps(launch_doc, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(launch_doc, ensure_ascii=False, indent=2))
    return launch_doc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/alphaqcm_2m_v1')
    parser.add_argument('--steps', type=int, default=FULL_STEPS)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--probe-steps', type=int, default=1500)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--launch', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--max-seconds', type=float, default=None)
    parser.add_argument('--fundamentals', action='store_true', default=True,
                        help='attach PIT financial and industry-relative valuation features '
                             '(default: on; the extended model feature head is 17-wide)')
    parser.add_argument('--no-fundamentals', dest='fundamentals', action='store_false',
                        help='disable fundamental columns; only valid for scoped synthetic panels')
    args = parser.parse_args()
    output = args.output.resolve()
    step = args.run or args.launch
    if step and not (output / 'run_plan.json').exists():
        write_plan(output, steps=args.steps, with_fundamentals=args.fundamentals)
    if args.probe:
        probe(output, seed=args.seed, probe_steps=args.probe_steps,
              with_fundamentals=args.fundamentals)
    elif args.launch:
        launch(output, seed=args.seed, steps=args.steps, resume=args.resume,
               max_seconds=args.max_seconds, with_fundamentals=args.fundamentals)
    elif args.run:
        execute(output, seed=args.seed, steps=args.steps, resume=args.resume,
                max_seconds=args.max_seconds, with_fundamentals=args.fundamentals)
    else:
        plan = write_plan(output, steps=args.steps, with_fundamentals=args.fundamentals)
        print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
