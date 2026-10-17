#!/usr/bin/env python3
"""P2.71 AlphaQCM CPU pilot on a fixed liquid A-share panel.

This is a pipeline/pilot, not the 2M-step paper budget and not a promotion
test.  Train, valid and test windows are fixed before the runs; the miner
never sees test.  Test metrics are computed after the artifact hash is frozen.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.alphaqcm_adapter import (
    MinerBudget, MiningRequest, evaluate_artifact, run_miner,
)
from backend.services.fundamental_data import attach_fundamentals
from backend.services.market_contract import load_bars, normalize_panel, validate_panel
from backend.services.pit_data import listing_dates


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def select_panel(n_assets: int = 60, *, sel_start: str = '2018-01-01',
                 sel_end: str = '2022-12-31', min_coverage: float = 0.9,
                 bars: pd.DataFrame | None = None,
                 stock_only: bool = True,
                 with_fundamentals: bool = True) -> pd.DataFrame:
    """Top-N liquid panel using only data inside the selection window.

    ``bars`` may be a pre-loaded normalized frame (walk-forward reuses one
    load for all windows); the selection window itself must always end before
    any test window it feeds.

    ``stock_only`` keeps only ``cn_stock_basic`` members.  The clean OHLCV
    layer also carries index series (e.g. ``000001.SH``) whose amount scale
    exceeds any stock's, so without the filter the "liquid panel" silently
    becomes an index panel.  The bug was found on 2026-10-17; earlier pilot
    and equal-evaluation runs predate the fix and are marked as index-panel
    variants in the plan doc.

    ``with_fundamentals`` attaches the 11 PIT fundamental/valuation columns to
    the *selected* panel.  It defaults to True: the P2.71-extended vendored
    model samples the full ``FeatureType`` enum (17 fields), so a panel with
    only the six price/volume columns hits out-of-range feature indices.
    """
    if bars is None:
        bars = normalize_panel(load_bars('2017-01-01', '2026-10-31'))
    base = bars[(bars.trade_date >= sel_start) & (bars.trade_date <= sel_end)]
    counts = base.groupby('stock_code').size()
    dates = base.trade_date.nunique()
    eligible = counts[counts >= min_coverage * dates].index
    if stock_only:
        stocks = set(listing_dates().stock_code)
        if stocks:
            eligible = eligible[eligible.isin(stocks)]
    liquidity = (base[base.stock_code.isin(eligible)].groupby('stock_code').amount.median()
                 .sort_values(ascending=False))
    codes = liquidity.head(n_assets).index
    panel = bars[bars.stock_code.isin(codes)].copy()
    if with_fundamentals and 'roe' not in panel.columns:
        # Attach after selecting the codes: the full clean layer is far larger
        # than the panel and the fundamentals merge is expensive.
        panel = attach_fundamentals(panel)
    validate_panel(panel)
    return panel


def _slice(panel: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return panel[(panel.trade_date >= start) & (panel.trade_date <= end)].copy()


def run(output: Path, *, seeds: tuple[int, ...], steps: int, std_lams: tuple[float, ...],
        n_assets: int, random_search: bool = False,
        checkpoint_interval: int = 0, stop_evals: int | None = None,
        max_seconds: float | None = None, resume: bool = False,
        with_fundamentals: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    history = []
    prior_path = output / 'pilot_runs.csv'
    if prior_path.exists():
        history = pd.read_csv(prior_path).to_dict('records')
    panel = select_panel(n_assets, with_fundamentals=with_fundamentals)
    split = {'train': ('2018-01-01', '2022-12-31'),
             'validation': ('2023-01-01', '2024-12-31'),
             'test': ('2025-01-01', '2025-10-31')}
    train = _slice(panel, '2017-08-01', '2023-02-28')
    valid = _slice(panel, '2022-08-01', '2025-02-28')
    test = _slice(panel, '2024-07-01', '2026-01-31')
    test.to_parquet(output / 'test_panel.parquet', index=False)
    (output / 'split.json').write_text(json.dumps(split, ensure_ascii=False, indent=2) + '\n')
    rows = []
    for std_lam in std_lams:
        for seed in seeds:
            run_id = (f'random_s{seed}_{steps}' if random_search
                      else f'iqn_lam{std_lam:g}_s{seed}_{steps}')
            budget = MinerBudget(
                seed=seed, std_lam=(0.0 if random_search else std_lam), num_steps=steps,
                start_steps=(max(1, steps - 100) if random_search else min(500, steps // 10)),
                batch_size=32, N=16, N_dash=16, K=8, num_cosines=16,
                memory_size=min(20000, steps * 2), update_interval=4,
                target_update_interval=max(100, steps // 20),
                epsilon_decay_steps=max(1, steps), eval_interval=max(100, steps // 10),
                num_eval_steps=5000, max_episode_steps=100, pool_capacity=10,
                log_interval=max(100, steps // 10),
                checkpoint_interval=checkpoint_interval, stop_evals=stop_evals,
                max_seconds=max_seconds)
            request = MiningRequest(run_id, train, valid, split, output / run_id, budget,
                                    metadata={'pilot': 'P2.71 CPU small panel',
                                              'n_assets': n_assets, 'steps': steps,
                                              'std_lam': (0.0 if random_search else std_lam),
                                              'seed': seed, 'random_search': random_search,
                                              'with_fundamentals': with_fundamentals},
                                    resume=resume)
            artifact = run_miner(request)
            metrics = evaluate_artifact(artifact, test, split=split,
                                        output=artifact.output_dir / 'test_eval',
                                        label='paper')
            ens = metrics['ensemble'].get('spearman', {})
            rows.append({'run_id': run_id, 'seed': seed,
                         'std_lam': (0.0 if random_search else std_lam),
                         'mode': ('random' if random_search else 'qcm'),
                         'steps': steps, 'status': artifact.status,
                         'stop_reason': artifact.stop_reason,
                         'steps_completed': artifact.steps_completed,
                         'evaluations': artifact.evaluations,
                         'resumed': artifact.resumed,
                         'factor_count': len(artifact.factors),
                         'valid_ic': artifact.ensemble_valid_ic,
                         'test_rank_ic': ens.get('mean'),
                         'test_rank_icir': ens.get('icir'),
                         'test_nw_t': ens.get('nw_t'),
                         'test_dates': ens.get('dates'),
                         'artifact_sha256': artifact.artifact_sha256})
            pd.DataFrame(rows).to_csv(output / 'pilot_runs.csv', index=False)
    result = pd.DataFrame(history + rows).drop_duplicates('run_id', keep='last')
    result = result.sort_values(['std_lam', 'seed', 'run_id'])
    result.to_csv(output / 'pilot_runs.csv', index=False)
    manifest = {
        'status': 'PILOT_COMPLETED_NOT_PROMOTION',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'panel_assets': int(panel.stock_code.nunique()),
        'panel_rows': int(len(panel)),
        'split': split, 'steps': steps, 'seeds': list(seeds),
        'std_lams': list(std_lams),
        'random_search': random_search,
        'with_fundamentals': with_fundamentals,
        'checkpoint_interval': checkpoint_interval,
        'stop_evals': stop_evals,
        'max_seconds': max_seconds,
        'resume': resume,
        'evaluations_total': int(result['evaluations'].fillna(0).sum())
        if 'evaluations' in result else 0,
        'source_hashes': {str(p.relative_to(ROOT)): _hash(p) for p in [
            ROOT / 'scripts/p2_71_alphaqcm_pilot.py',
            ROOT / 'backend/services/alphaqcm_adapter.py',
            ROOT / 'backend/services/alphaqcm_checkpoint.py',
            ROOT / 'scripts/alphaqcm_worker.py']},
        'limitations': [
            f'{steps} steps is far below the official 2M-step budget',
            'small fixed liquid panel selected on train-period data; not full-market PIT',
            'test is retrospective 2025-01-01..2025-10-31, not a forward shadow',
            'no cost-aware portfolio replay on the mined factors in this pilot',
            'step-budget runs are not equal-expression-evaluation budgets; see '
            'scripts/p2_71_alphaqcm_equal_evals.py for the equal-eval comparison',
        ],
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                                                     default=str) + '\n')
    lines = ['# P2.71 AlphaQCM CPU pilot', '',
             f"状态：**PILOT_COMPLETED_NOT_PROMOTION**；{n_assets}只训练期流动性面板，"
             f"{steps:,}步/seed，seed={list(seeds)}。", '',
             '| run | std_lam | seed | factors | evals | valid IC | test RankIC | test ICIR | NW t |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in result.itertuples():
        lines.append(f'| {row.run_id} | {row.std_lam:g} | {row.seed} | {row.factor_count} | '
                     f'{getattr(row, "evaluations", 0)} | '
                     f'{row.valid_ic if row.valid_ic is not None else float("nan"):+.4f} | '
                     f'{row.test_rank_ic if row.test_rank_ic is not None else float("nan"):+.4f} | '
                     f'{row.test_rank_icir if row.test_rank_icir is not None else float("nan"):+.4f} | '
                     f'{row.test_nw_t if row.test_nw_t is not None else float("nan"):+.4f} |')
    lines += ['', '## 边界', '',
              f'- {steps:,}步不是论文200万步预算；本报告只验证隔离训练、valid选择、冻结后test评测通路。',
              '- 测试区间为回顾式2025-01-01至2025-10-31，不是前向影子。',
              '- 尚未把挖掘表达式接入成本后组合回放，不能据此声称年化超越moneyflow_arm。',
              '- `std_lam=0`组用于消融QCM探索奖励，步数与主组不同时须按预算差异解读。',
              '- 固定步数下两组表达式评价次数不同；等评价次数对照见 '
              '`scripts/p2_71_alphaqcm_equal_evals.py`。', '',
              '[逐run结果](pilot_runs.csv) · [manifest](manifest.json)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(result.to_string(index=False))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/alphaqcm_pilot_v1')
    parser.add_argument('--seeds', default='0,1,2')
    parser.add_argument('--steps', type=int, default=20000)
    parser.add_argument('--std-lams', default='1.0')
    parser.add_argument('--assets', type=int, default=60)
    parser.add_argument('--random', action='store_true')
    parser.add_argument('--fundamentals', action='store_true', default=True,
                        help='attach PIT financial and industry-relative valuation features '
                             '(default: on; the extended model feature head is 17-wide)')
    parser.add_argument('--no-fundamentals', dest='fundamentals', action='store_false',
                        help='disable fundamental columns; only valid for scoped synthetic panels')
    parser.add_argument('--checkpoint-interval', type=int, default=0,
                        help='全状态快照间隔（步）；0=仅在结束时保存')
    parser.add_argument('--stop-evals', type=int, default=None,
                        help='预登记表达式评价次数预算；达到即停止')
    parser.add_argument('--max-seconds', type=float, default=None,
                        help='预登记墙钟预算（秒）；达到即停止并保存checkpoint')
    parser.add_argument('--resume', action='store_true',
                        help='从同一输出目录的 checkpoints/latest.pt 续训')
    args = parser.parse_args()
    run(args.output.resolve(), seeds=tuple(int(x) for x in args.seeds.split(',')),
        steps=args.steps, std_lams=tuple(float(x) for x in args.std_lams.split(',')),
        n_assets=args.assets, random_search=args.random,
        checkpoint_interval=args.checkpoint_interval, stop_evals=args.stop_evals,
        max_seconds=args.max_seconds, resume=args.resume,
        with_fundamentals=args.fundamentals)


if __name__ == '__main__':
    main()
