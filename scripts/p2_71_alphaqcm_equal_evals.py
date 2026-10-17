#!/usr/bin/env python3
"""P2.71 AlphaQCM equal-expression-evaluation comparison (pre-registered).

The 20k-step pilot compared QCM (std_lam=1) and random search (std_lam=0) at
equal *steps*.  Expression lengths differ between the two policies, so the
number of completed candidate expressions actually evaluated differs as well;
a step budget is therefore not an equal-budget comparison.

This script fixes the budget to equal completed candidate expressions
(``evaluations`` in the worker status, counted at ``AlphaEnvCore._evaluate``):

1. Reference phase (measurement only, no result-based selection): the six
   runs of the pre-registered 20k-step pilot budget (both modes x seeds) are
   re-instrumented; their evaluation counts are recorded.
2. ``T = min(evaluations)`` over the six reference runs.  The rule is written
   into ``equal_evals_config.json`` before the comparison phase starts.
3. Comparison phase: the same six runs are re-executed with
   ``stop_evals = T``.  Every arm stops at exactly T evaluated expressions;
   the steps used are an outcome, not the budget.
4. Test metrics are computed only after each artifact hash is frozen, by the
   separate evaluator process (the miner never sees test).

The result is descriptive at N=3 seeds on one fixed liquid panel; it is not a
promotion and not a substitute for the PIT panel + walk-forward program.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.alphaqcm_adapter import (  # noqa: E402
    MinerBudget, MiningRequest, evaluate_artifact, run_miner,
)
from scripts.p2_71_alphaqcm_pilot import _slice, select_panel  # noqa: E402

MODES = ('qcm', 'random')


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _budget(steps: int, seed: int, mode: str, *, checkpoint_interval: int,
            stop_evals: int | None, max_seconds: float | None) -> MinerBudget:
    random_search = mode == 'random'
    return MinerBudget(
        seed=seed, std_lam=(0.0 if random_search else 1.0), num_steps=steps,
        start_steps=(max(1, steps - 100) if random_search else min(500, steps // 10)),
        batch_size=32, N=16, N_dash=16, K=8, num_cosines=16,
        memory_size=min(20000, steps * 2), update_interval=4,
        target_update_interval=max(100, steps // 20),
        epsilon_decay_steps=max(1, steps), eval_interval=max(100, steps // 10),
        num_eval_steps=5000, max_episode_steps=100, pool_capacity=10,
        log_interval=max(100, steps // 10),
        checkpoint_interval=checkpoint_interval, stop_evals=stop_evals,
        max_seconds=max_seconds)


def _run_arm(*, output: Path, phase: str, mode: str, seed: int, steps: int,
             train: pd.DataFrame, valid: pd.DataFrame, split: dict,
             test: pd.DataFrame, checkpoint_interval: int,
             stop_evals: int | None, max_seconds: float | None) -> dict:
    run_id = f'{mode}_s{seed}_{phase}'
    budget = _budget(steps, seed, mode, checkpoint_interval=checkpoint_interval,
                     stop_evals=stop_evals, max_seconds=max_seconds)
    request = MiningRequest(
        run_id, train, valid, split, output / run_id, budget,
        metadata={'experiment': 'alphaqcm_equal_evals', 'phase': phase,
                  'mode': mode, 'seed': seed, 'steps': steps,
                  'target_evals': stop_evals})
    started = time.time()
    artifact = run_miner(request)
    wall = time.time() - started
    metrics = evaluate_artifact(artifact, test, split=split,
                                output=artifact.output_dir / 'test_eval',
                                label='paper')
    ens = metrics['ensemble'].get('spearman', {})
    return {
        'run_id': run_id, 'phase': phase, 'mode': mode, 'seed': seed,
        'steps': steps, 'status': artifact.status,
        'stop_reason': artifact.stop_reason,
        'steps_completed': artifact.steps_completed,
        'evaluations': artifact.evaluations,
        'factor_count': len(artifact.factors),
        'valid_ic': artifact.ensemble_valid_ic,
        'test_rank_ic': ens.get('mean'),
        'test_rank_icir': ens.get('icir'),
        'test_nw_t': ens.get('nw_t'),
        'test_dates': ens.get('dates'),
        'wall_seconds': round(wall, 2),
        'checkpoint_latest': artifact.checkpoint_latest,
        'artifact_sha256': artifact.artifact_sha256,
    }


def run(output: Path, *, seeds: tuple[int, ...], steps: int, n_assets: int,
        checkpoint_interval: int, ref_max_seconds: float,
        cmp_max_seconds: float, with_fundamentals: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    panel = select_panel(n_assets, with_fundamentals=with_fundamentals)
    split = {'train': ('2018-01-01', '2022-12-31'),
             'validation': ('2023-01-01', '2024-12-31'),
             'test': ('2025-01-01', '2025-10-31')}
    train = _slice(panel, '2017-08-01', '2023-02-28')
    valid = _slice(panel, '2022-08-01', '2025-02-28')
    test = _slice(panel, '2024-07-01', '2026-01-31')
    test.to_parquet(output / 'test_panel.parquet', index=False)
    (output / 'split.json').write_text(
        json.dumps(split, ensure_ascii=False, indent=2) + '\n')

    config_path = output / 'equal_evals_config.json'
    config = {
        'purpose': 'equal-expression-evaluation comparison of QCM (std_lam=1) '
                   'vs random search (std_lam=0) on the fixed pilot panel',
        'created_at': _now(),
        'registered_before_runs': True,
        'panel': {'n_assets': n_assets, 'selection': 'train-period median amount',
                  'split': split, 'with_fundamentals': with_fundamentals},
        'seeds': list(seeds),
        'steps': steps,
        'phases': {
            'reference': {'budget': f'{steps} steps (fixed step cap)', 'modes': list(MODES),
                          'seeds': list(seeds), 'max_seconds': ref_max_seconds},
            'comparison': {'budget': 'stop_evals = T', 'modes': list(MODES),
                           'seeds': list(seeds), 'max_seconds': cmp_max_seconds},
        },
        'target_evals_rule': 'T = min(evaluations) across the six reference runs; '
                             'computed before the comparison phase and not changed afterwards',
        'target_evals': None,
        'metrics': ['ensemble valid IC (training-side selection)',
                    'test RankIC / RankICIR / NW t of the frozen ensemble (separate process)'],
        'decision_rule': 'descriptive only at N=3 seeds on one panel; '
                         'no promotion without PIT panel, walk-forward and costed replay',
        'checkpoint_interval': checkpoint_interval,
        'source_hashes': {str(p.relative_to(ROOT)): _hash(p) for p in [
            ROOT / 'scripts/p2_71_alphaqcm_equal_evals.py',
            ROOT / 'scripts/p2_71_alphaqcm_pilot.py',
            ROOT / 'backend/services/alphaqcm_adapter.py',
            ROOT / 'backend/services/alphaqcm_checkpoint.py',
            ROOT / 'scripts/alphaqcm_worker.py']},
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')

    rows: list[dict] = []
    reference: list[dict] = []
    for mode in MODES:
        for seed in seeds:
            row = _run_arm(output=output / 'ref', phase='ref', mode=mode, seed=seed,
                           steps=steps, train=train, valid=valid, split=split,
                           test=test, checkpoint_interval=checkpoint_interval,
                           stop_evals=None, max_seconds=ref_max_seconds)
            reference.append(row)
            print('reference', row['run_id'], row['status'], 'evals=',
                  row['evaluations'], 'steps=', row['steps_completed'], flush=True)
    target = int(min(row['evaluations'] for row in reference))
    if target < 1:
        raise SystemExit('reference runs produced no completed expression evaluations')
    config['target_evals'] = target
    config['reference_evaluations'] = {row['run_id']: row['evaluations']
                                       for row in reference}
    config['reference_phase_completed_at'] = _now()
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')

    comparison: list[dict] = []
    for mode in MODES:
        for seed in seeds:
            row = _run_arm(output=output / 'cmp', phase='cmp', mode=mode, seed=seed,
                           steps=steps, train=train, valid=valid, split=split,
                           test=test, checkpoint_interval=checkpoint_interval,
                           stop_evals=target, max_seconds=cmp_max_seconds)
            comparison.append(row)
            print('comparison', row['run_id'], row['status'], 'evals=',
                  row['evaluations'], 'steps=', row['steps_completed'], flush=True)

    rows = reference + comparison
    frame = pd.DataFrame(rows)
    frame.to_csv(output / 'equal_evals_runs.csv', index=False)

    cmp_frame = pd.DataFrame(comparison)
    ok = cmp_frame[cmp_frame.evaluations == target]
    summary = {}
    for mode in MODES:
        sub = ok[ok['mode'] == mode]
        values = sub['test_rank_ic'].dropna().astype(float)
        summary[mode] = {
            'n_runs_at_target': int(len(sub)),
            'mean_test_rank_ic': float(values.mean()) if len(values) else None,
            'min_test_rank_ic': float(values.min()) if len(values) else None,
            'max_test_rank_ic': float(values.max()) if len(values) else None,
            'mean_steps_used': float(sub['steps_completed'].mean()) if len(sub) else None,
            'evals_used': int(sub['evaluations'].sum()) if len(sub) else 0,
        }

    manifest = {
        'status': 'EQUAL_EVALS_COMPARISON_COMPLETED_NOT_PROMOTION',
        'created_at': _now(),
        'target_evals': target,
        'seeds': list(seeds), 'steps_cap': steps, 'panel_assets': n_assets,
        'split': split,
        'modes': list(MODES),
        'summary': summary,
        'config_path': str(config_path),
        'runs_csv': str(output / 'equal_evals_runs.csv'),
        'limitations': [
            'N=3 seeds on one fixed liquid panel; not a promotion test',
            f'common evaluation budget T={target} is bounded by the {steps}-step cap',
            'panel selection and test window are from the pre-existing pilot, not PIT',
            'no costed portfolio replay on the mined factors in this comparison',
        ],
    }
    (output / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')

    lines = ['# P2.71 AlphaQCM 等表达式评价次数对照', '',
             f"状态：**EQUAL_EVALS_COMPARISON_COMPLETED_NOT_PROMOTION**；"
             f"共同预算 T={target} 次表达式评价/arm（min over 六个参考run，"
             f"上限{steps:,}步），seed={list(seeds)}，面板{n_assets}只。", '',
             '## 预登记', '',
             f'- 参考相：{steps:,}步预算（固定步数上限，先于结果确定）× 2模式 × '
             f'{len(seeds)} seeds；先测量各run完成的表达式评价次数。',
             f'- 对照相：stop_evals=T，T=参考相最小值，先写盘后开跑；'
             f'所有arm恰好在 T 次评价处停止。',
             '- test仅在artifact哈希冻结后由独立进程评测；步数是结果而非预算。', '',
             f'## 参考相（{steps:,}步，评价次数为观测量）', '',
             '| run | evals | steps | factors | test RankIC |',
             '|---|---:|---:|---:|---:|']
    for row in reference:
        ic = row['test_rank_ic']
        lines.append(f"| {row['run_id']} | {row['evaluations']} | {row['steps_completed']} | "
                     f"{row['factor_count']} | {ic:+.4f} |" if ic is not None else
                     f"| {row['run_id']} | {row['evaluations']} | {row['steps_completed']} | "
                     f"{row['factor_count']} | n/a |")
    lines += ['', '## 对照相（stop_evals=T，等评价次数）', '',
              '| run | mode | evals | steps | factors | valid IC | test RankIC | test ICIR | NW t | wall s |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in comparison:
        ric = row['test_rank_ic']
        ricir = row['test_rank_icir']
        nwt = row['test_nw_t']
        vic = row['valid_ic']
        fmt = lambda v: f'{v:+.4f}' if v is not None else 'n/a'  # noqa: E731
        lines.append(f"| {row['run_id']} | {row['mode']} | {row['evaluations']} | "
                     f"{row['steps_completed']} | {row['factor_count']} | {fmt(vic)} | "
                     f"{fmt(ric)} | {fmt(ricir)} | {fmt(nwt)} | {row['wall_seconds']} |")
    lines += ['', '## 汇总（等评价次数）', '',
              '| mode | 完成arm | 平均test RankIC | 范围 | 平均步数 |',
              '|---|---:|---:|---|---:|']
    for mode in MODES:
        s = summary[mode]
        rng = (f"{s['min_test_rank_ic']:+.4f}..{s['max_test_rank_ic']:+.4f}"
               if s['mean_test_rank_ic'] is not None else 'n/a')
        mean = f"{s['mean_test_rank_ic']:+.4f}" if s['mean_test_rank_ic'] is not None else 'n/a'
        lines.append(f"| {mode} | {s['n_runs_at_target']} | {mean} | {rng} | "
                     f"{s['mean_steps_used']:.0f} |")
    lines += ['', '## 边界', '',
              f'- 仅3个seed、单一固定流动性面板；T受{steps:,}步上限约束，数值小则结论力弱。',
              '- 面板与test窗口沿用pilot，不是PIT全市场；未接入成本后组合回放。',
              '- 本对照只回答"等评价次数下两搜索策略的test表现差异"，不构成收益晋级。', '',
              '[逐run结果](equal_evals_runs.csv) · [manifest](manifest.json) · '
              '[预登记](equal_evals_config.json) · [参考相](ref) · [对照相](cmp)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(pd.DataFrame(rows).to_string(index=False))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/alphaqcm_equal_evals_v1')
    parser.add_argument('--seeds', default='0,1,2')
    parser.add_argument('--steps', type=int, default=20000)
    parser.add_argument('--assets', type=int, default=60)
    parser.add_argument('--checkpoint-interval', type=int, default=5000)
    parser.add_argument('--ref-max-seconds', type=float, default=1800)
    parser.add_argument('--cmp-max-seconds', type=float, default=1800)
    parser.add_argument('--fundamentals', action='store_true', default=True,
                        help='attach PIT financial and industry-relative valuation features '
                             '(default: on; the extended model feature head is 17-wide)')
    parser.add_argument('--no-fundamentals', dest='fundamentals', action='store_false',
                        help='disable fundamental columns; only valid for scoped synthetic panels')
    args = parser.parse_args()
    run(args.output.resolve(), seeds=tuple(int(x) for x in args.seeds.split(',')),
        steps=args.steps, n_assets=args.assets,
        checkpoint_interval=args.checkpoint_interval,
        ref_max_seconds=args.ref_max_seconds, cmp_max_seconds=args.cmp_max_seconds,
        with_fundamentals=args.fundamentals)


if __name__ == '__main__':
    main()
