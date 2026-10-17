#!/usr/bin/env python3
"""P2.71 outer walk-forward framework for AlphaQCM factor mining.

Wraps the miner + frozen-artifact evaluation in rolling half-year test
windows.  Every window uses strictly point-in-time slices:

* panel selection: top-N by median amount inside ``[test_start-4y, test_start)``,
* miner train/valid split: ``[test_start-4y, test_start-6m)`` /
  ``[test_start-6m, test_start-32td)`` — the valid end is trimmed by the label
  horizon so its padded slice still ends strictly before test_start,
* tensor padding: every slice carries >=110 extra trading days before its start
  and 30 after its split end, because the AlphaQCM tensors need 100 backtrack
  and 25 label-horizon days inside the panel they receive,
* test:  the half-year window (+ padding), seen only by the evaluator after the
  artifact hash is frozen.

This intentionally differs from the single-window pilot, whose valid slice
extended two months into the test period; the difference is documented in the
plan file written before any run.

The default budget (20k steps per window) is a framework demo: the AlphaQCM
pool needs tens of thousands of steps to fill (index-panel evidence: 62..410
submitted expressions per 60k steps).  Windows whose pool stays empty are
recorded with an ``eval_error`` and skipped, not treated as crashes.  The same
script with ``--steps 2000000`` is the planned full-budget outer evaluation.
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
from backend.services.alphaqcm_adapter import (  # noqa: E402
    MinerBudget, MiningRequest, evaluate_artifact, run_miner,
)
from backend.services import pit_data  # noqa: E402
from backend.services.market_contract import load_bars, normalize_panel  # noqa: E402
from scripts.p2_71_alphaqcm_pilot import _slice, select_panel  # noqa: E402

MINER_LOOKBACK_YEARS = 4
VALID_MONTHS = 6
BACKTRACK_PAD_DAYS = 110   # tensor requires >=100 trading days before a split start
FUTURE_PAD_DAYS = 30       # tensor requires >=25 trading days after a split end
VALID_LABEL_TRIM_DAYS = 32  # valid end = test_start-32td, so valid+30td stays pre-test


def _windows(first_year: int, last_year: int) -> list[dict]:
    out = []
    for year in range(first_year, last_year + 1):
        for half, (start, end) in ((1, (f'{year}-01-01', f'{year}-06-30')),
                                   (2, (f'{year}-07-01', f'{year}-12-31'))):
            out.append({'name': f'{year}H{half}', 'test_start': start,
                        'test_end': end})
    return out


def _budget(steps: int, seed: int) -> MinerBudget:
    return MinerBudget(
        seed=seed, std_lam=1.0, num_steps=steps,
        start_steps=min(500, steps // 10),
        batch_size=32, N=16, N_dash=16, K=8, num_cosines=16,
        memory_size=min(20000, steps * 2), update_interval=4,
        target_update_interval=max(100, steps // 20),
        epsilon_decay_steps=max(1, steps), eval_interval=max(100, steps // 10),
        num_eval_steps=5000, max_episode_steps=100, pool_capacity=10,
        log_interval=max(100, steps // 10))


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _padded_slice(panel: pd.DataFrame, dates: pd.DatetimeIndex, start, end, *,
                  future: int = FUTURE_PAD_DAYS) -> pd.DataFrame:
    """``[start, end]`` plus exact trading-day padding for the qcm tensors."""
    i0 = int(dates.searchsorted(pd.Timestamp(start)))
    i1 = int(dates.searchsorted(pd.Timestamp(end), side='right')) - 1
    if i1 < i0:
        raise ValueError(f'empty slice {start}..{end}')
    lo = max(i0 - BACKTRACK_PAD_DAYS, 0)
    hi = min(i1 + future + 1, len(dates))
    return _slice(panel, str(dates[lo].date()), str(dates[hi - 1].date()))


def _pit_counts(test_slice: pd.DataFrame) -> dict:
    try:
        flagged = pit_data.annotate_bars(test_slice)
    except Exception as exc:  # audit is optional context, never a blocker
        return {'pit_error': f'{type(exc).__name__}: {exc}'[:200]}
    return {
        'limit_flags_coverage': float(flagged['close_at_up'].notna().mean()),
        'close_at_up_rows': int(flagged['close_at_up'].fillna(False).sum()),
        'close_at_down_rows': int(flagged['close_at_down'].fillna(False).sum()),
        'is_st_rows': int(flagged['is_st'].sum()),
        'suspended_rows': int(flagged['suspended_official'].sum()),
    }


def run(output: Path, *, steps: int, seeds: tuple[int, ...], n_assets: int,
        first_year: int, last_year: int,
        windows_filter: tuple[str, ...] | None = None,
        with_fundamentals: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    windows = _windows(first_year, last_year)
    if windows_filter:
        windows = [w for w in windows if w['name'] in windows_filter]
    if not windows:
        raise SystemExit('no walk-forward windows selected')

    plan = {
        'created_at': _now(),
        'registered_before_runs': True,
        'purpose': 'outer walk-forward evaluation of the AlphaQCM mining pipeline',
        'windows': windows,
        'steps_per_window': steps,
        'seeds': list(seeds),
        'n_assets': n_assets,
        'features': 'extended: 6 price/volume + 11 PIT fundamentals '
                    '(fundamental_data.attach_fundamentals; model feature head is 17-wide)',
        'with_fundamentals': with_fundamentals,
        'selection': {
            'rule': 'top-N by median amount, 90% date coverage within selection window',
            'selection_window': f'[test_start-{MINER_LOOKBACK_YEARS}y, test_start)',
        },
        'slices': {
            'train': f'[test_start-{MINER_LOOKBACK_YEARS}y, test_start-{VALID_MONTHS}m) '
                     f'+ {BACKTRACK_PAD_DAYS}/{FUTURE_PAD_DAYS} trading-day tensor padding',
            'valid': f'[test_start-{VALID_MONTHS}m, test_start-{VALID_LABEL_TRIM_DAYS}td) '
                     f'+ {BACKTRACK_PAD_DAYS}/30 trading-day tensor padding; '
                     'slice end stays strictly before test_start',
            'test': f'the half-year window + {BACKTRACK_PAD_DAYS}/30 trading-day tensor '
                    'padding; evaluator only, after artifact hash freeze',
            'note': 'AlphaQCM tensors need >=100 backtrack and >=25 future (label-horizon) '
                    'trading days inside each panel; the pilot did not trim valid, so its '
                    'valid tensor reached ~2 months into test. The walk-forward trims the '
                    'valid split by the label horizon (documented variant, no test leakage).',
        },
        'metrics': ['valid IC (miner selection)', 'test RankIC/RankICIR/NW t per window',
                    'cross-window mean, dispersion, hit rate'],
        'decision_rule': 'framework demo at small budget; no promotion; '
                         '--steps 2000000 is the planned full-budget evaluation',
        'source_hashes': {str(p.relative_to(ROOT)): _hash(p) for p in [
            ROOT / 'scripts/p2_71_walk_forward.py',
            ROOT / 'scripts/p2_71_alphaqcm_pilot.py',
            ROOT / 'backend/services/alphaqcm_adapter.py',
            ROOT / 'backend/services/alphaqcm_checkpoint.py',
        ]},
    }
    (output / 'walk_forward_plan.json').write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, default=str) + '\n')

    bars = normalize_panel(load_bars('2016-08-01', '2026-06-30'))
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(bars.trade_date).unique()))
    rows: list[dict] = []
    for window in windows:
        t0 = pd.Timestamp(window['test_start'])
        t1 = pd.Timestamp(window['test_end'])
        valid_start = t0 - pd.DateOffset(months=VALID_MONTHS)
        train_start = max(pd.Timestamp('2017-08-01'),
                          t0 - pd.DateOffset(years=MINER_LOOKBACK_YEARS))
        panel = select_panel(n_assets, sel_start=train_start, sel_end=valid_start,
                             bars=bars, with_fundamentals=with_fundamentals)
        t0_index = int(dates.searchsorted(t0))
        valid_end = dates[t0_index - VALID_LABEL_TRIM_DAYS]
        train = _padded_slice(panel, dates, train_start, valid_start)
        valid = _padded_slice(panel, dates, valid_start, valid_end)
        test = _padded_slice(panel, dates, t0, t1)
        if min(len(train), len(valid)) < 1000 or len(test) < 100:
            rows.append({'window': window['name'], 'status': 'SKIPPED_TOO_SMALL',
                         'train_rows': len(train), 'valid_rows': len(valid),
                         'test_rows': len(test)})
            continue
        split = {'train': (str(train_start.date()), str(valid_start.date())),
                 'validation': (str(valid_start.date()), str(valid_end.date())),
                 'test': (str(t0.date()), str(t1.date()))}
        test.to_parquet(output / f"test_panel_{window['name']}.parquet", index=False)
        pit_stats = _pit_counts(test)
        for seed in seeds:
            run_id = f"wf_{window['name']}_s{seed}"
            request = MiningRequest(
                run_id, train, valid, split, output / run_id, _budget(steps, seed),
                metadata={'walk_forward': True, 'window': window['name'],
                          'steps': steps, 'seed': seed, 'n_assets': n_assets})
            artifact = run_miner(request)
            metrics, eval_error = None, None
            if not artifact.factors:
                eval_error = 'empty factor pool (budget too small or all candidates rejected)'
            else:
                try:
                    metrics = evaluate_artifact(artifact, test, split=split,
                                                output=artifact.output_dir / 'test_eval',
                                                label='paper')
                except RuntimeError as exc:
                    eval_error = str(exc)[-500:]
            ens = (metrics or {}).get('ensemble', {}).get('spearman', {})
            rows.append({
                'window': window['name'], 'seed': seed, 'run_id': run_id,
                'test_start': window['test_start'], 'test_end': window['test_end'],
                'panel_assets': int(panel.stock_code.nunique()),
                'train_rows': len(train), 'valid_rows': len(valid),
                'test_rows': len(test),
                'status': artifact.status, 'steps': artifact.steps_completed,
                'evaluations': artifact.evaluations,
                'factor_count': len(artifact.factors),
                'valid_ic': artifact.ensemble_valid_ic,
                'test_rank_ic': ens.get('mean'),
                'test_rank_icir': ens.get('icir'),
                'test_nw_t': ens.get('nw_t'),
                'test_dates': ens.get('dates'),
                'test_limit_up_rows': pit_stats.get('close_at_up_rows'),
                'test_limit_down_rows': pit_stats.get('close_at_down_rows'),
                'test_st_rows': pit_stats.get('is_st_rows'),
                'test_suspended_rows': pit_stats.get('suspended_rows'),
                'eval_error': eval_error,
                'artifact_sha256': artifact.artifact_sha256,
            })
            print(json.dumps({k: rows[-1][k] for k in
                              ('window', 'seed', 'status', 'evaluations',
                               'factor_count', 'valid_ic', 'test_rank_ic')},
                             ensure_ascii=False, default=str), flush=True)

    frame = pd.DataFrame(rows)
    frame.to_csv(output / 'window_runs.csv', index=False)

    valid_rows = frame[frame['test_rank_ic'].notna()]
    per_window = (valid_rows.groupby('window')['test_rank_ic'].mean()
                  .rename('rank_ic').reset_index()) if len(valid_rows) else pd.DataFrame()
    summary = {}
    if len(per_window):
        values = per_window['rank_ic'].astype(float)
        summary = {
            'windows_completed': int(len(per_window)),
            'mean_test_rank_ic': float(values.mean()),
            'std_test_rank_ic': float(values.std(ddof=1)) if len(values) > 1 else None,
            'min_test_rank_ic': float(values.min()),
            'max_test_rank_ic': float(values.max()),
            'hit_rate': float((values > 0).mean()),
            't_across_windows': float(values.mean() / (values.std(ddof=1) / np.sqrt(len(values))))
            if len(values) > 1 and values.std(ddof=1) > 0 else None,
        }

    manifest = {
        'status': 'WALK_FORWARD_FRAMEWORK_COMPLETED_NOT_PROMOTION',
        'created_at': _now(),
        'plan_path': str(output / 'walk_forward_plan.json'),
        'summary': summary,
        'runs': int(len(frame)),
        'limitations': [
            f'{steps} steps/window is a framework demo, not the 2M full budget',
            f'{n_assets}-asset PIT-selected liquid panel; not full market',
            'test evaluation is retrospective, not a forward shadow',
            'no costed portfolio replay on the mined factors',
            'PIT limit/ST/suspension flags are reported per window but not yet enforced in '
            'the evaluator sample masks',
            'feature set is the fundamental-extended 17-column space; OHLCV-only (6-feature) '
            'mining is not runnable under the extended vendored model',
        ],
    }
    (output / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')

    lines = ['# P2.71 AlphaQCM 外层 walk-forward（框架）', '',
             f"状态：**WALK_FORWARD_FRAMEWORK_COMPLETED_NOT_PROMOTION**；"
             f"窗口={first_year}H1至{last_year}H2，半年度test，"
             f"每窗口{steps:,}步×seed={list(seeds)}，面板{n_assets}只（按窗口前4年PIT流动性选样）。", '',
             '## 窗口明细', '',
             '| window | test区间 | 面板 | factors | evals | valid IC | test RankIC | test ICIR | NW t |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for _, row in frame.iterrows():
        if row.get('status') in ('SKIPPED_TOO_SMALL',) or pd.isna(row.get('test_rank_ic', np.nan)):
            continue
        fmt = lambda v: f'{float(v):+.4f}' if pd.notna(v) else 'n/a'  # noqa: E731
        lines.append(f"| {row['window']} | {row['test_start']}..{row['test_end']} | "
                     f"{int(row['panel_assets'])} | {int(row['factor_count'])} | "
                     f"{int(row['evaluations'])} | {fmt(row['valid_ic'])} | "
                     f"{fmt(row['test_rank_ic'])} | {fmt(row['test_rank_icir'])} | "
                     f"{fmt(row['test_nw_t'])} |")
    if summary:
        tval = f"{summary['t_across_windows']:+.2f}" if summary['t_across_windows'] is not None else 'n/a'
        lines += ['', '## 跨窗口汇总', '',
                  f"- 完成窗口数：{summary['windows_completed']}",
                  f"- 窗口均值 RankIC：{summary['mean_test_rank_ic']:+.4f}"
                  f"（std {summary['std_test_rank_ic'] if summary['std_test_rank_ic'] is not None else float('nan'):.4f}）",
                  f"- 范围：{summary['min_test_rank_ic']:+.4f} .. {summary['max_test_rank_ic']:+.4f}",
                  f"- 正IC窗口占比（hit rate）：{summary['hit_rate']:.0%}",
                  f"- 跨窗口t值：{tval}", '']
    lines += ['## 边界', '',
              f'- {steps:,}步/窗口是框架验证演示，不是200万步全量预算；此脚本以`--steps 2000000`即为全量外层评测。',
              '- 每个窗口的面板仅用该窗口test开始前的数据按流动性选样（严格PIT），与pilot固定面板不同。',
              '- 严格PIT切分：valid不进入test（pilot的±2月padding策略未沿用），故两套结果不可直接对比。',
              '- PIT涨跌停/ST/停牌标志仅作为每窗口的上下文统计，尚未接入评测掩码；也未接入成本后组合回放。',
              '- 回顾性test，不是前向影子；本报告不构成收益晋级。', '',
              '[逐窗口结果](window_runs.csv) · [预登记计划](walk_forward_plan.json) · '
              '[manifest](manifest.json)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/walk_forward_v1')
    parser.add_argument('--steps', type=int, default=20000,
                        help='每窗口挖掘步数；2000000=计划中的全量预算；'
                             '明显低于2万步时池大概率填不满（记录 eval_error 并跳过）')
    parser.add_argument('--seeds', default='0')
    parser.add_argument('--assets', type=int, default=60)
    parser.add_argument('--first-year', type=int, default=2021)
    parser.add_argument('--last-year', type=int, default=2025)
    parser.add_argument('--windows', default=None,
                        help='逗号分隔的窗口子集（如 2024H1,2024H2），默认全部')
    parser.add_argument('--fundamentals', action='store_true', default=True,
                        help='attach PIT financial and industry-relative valuation features (default: on)')
    parser.add_argument('--no-fundamentals', dest='fundamentals', action='store_false',
                        help='disable fundamental columns')
    args = parser.parse_args()
    run(args.output.resolve(), steps=args.steps,
        seeds=tuple(int(x) for x in args.seeds.split(',')),
        n_assets=args.assets, first_year=args.first_year, last_year=args.last_year,
        windows_filter=tuple(args.windows.split(',')) if args.windows else None,
        with_fundamentals=args.fundamentals)


if __name__ == '__main__':
    main()
