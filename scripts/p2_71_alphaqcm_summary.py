#!/usr/bin/env python3
"""Summarise the P2.71 AlphaQCM pilot without changing any run artifact."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        ROOT / 'output/research/p2_71/alphaqcm_pilot_v1')
    df = pd.read_csv(out / 'pilot_runs.csv')
    df['mode'] = df.get('mode', pd.Series(index=df.index, dtype=object))
    df.loc[df.run_id.str.startswith('random'), 'mode'] = 'random'
    df.loc[df['mode'].isna() & (df.std_lam == 0), 'mode'] = 'qcm_std0'
    df.loc[df['mode'].isna(), 'mode'] = 'qcm_std1'
    df.loc[df['mode'] == 'random', 'std_lam'] = 0.0
    df['group'] = df['mode'].astype(str) + '_lam' + df['std_lam'].astype(str)
    df.to_csv(out / 'pilot_runs.csv', index=False)
    group = df.groupby('group').agg(
        runs=('run_id', 'count'),
        valid_ic_mean=('valid_ic', 'mean'),
        test_rank_ic_mean=('test_rank_ic', 'mean'),
        test_rank_ic_std=('test_rank_ic', 'std'),
        test_icir_mean=('test_rank_icir', 'mean'),
        test_nw_t_mean=('test_nw_t', 'mean'),
        factors_mean=('factor_count', 'mean'),
    ).reset_index()
    group.to_csv(out / 'pilot_summary.csv', index=False)
    payload = {'status': 'NEGATIVE_OR_INCONCLUSIVE_PILOT',
               'interpretation': ('At 20k CPU steps on 60 names, QCM std_lam=1 does not '
                                  'outperform the random-search baseline in mean test RankIC; '
                                  'no promotion is justified.'),
               'group_summary': group.to_dict('records'),
               'n_runs': int(len(df)), 'seeds': sorted(df.seed.unique().tolist()),
               'steps': int(df.steps.max())}
    (out / 'summary.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                 default=str) + '\n')
    lines = ['# P2.71 AlphaQCM CPU pilot（否定/不确定结果）', '',
             '状态：**NEGATIVE_OR_INCONCLUSIVE_PILOT**。固定60只训练期流动性股票，'
             '20,000步/seed，seed 0/1/2；测试段2025-01-01至2025-10-31，冻结后单次评测。', '',
             '| 组 | runs | valid IC均值 | test RankIC均值 | test RankIC标准差 | test ICIR均值 | 平均因子数 |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for row in group.itertuples():
        lines.append(f'| {row.group} | {row.runs} | {row.valid_ic_mean:+.4f} | '
                     f'{row.test_rank_ic_mean:+.4f} | {row.test_rank_ic_std:.4f} | '
                     f'{row.test_icir_mean:+.4f} | {row.factors_mean:.2f} |')
    lines += ['', '## 逐run', '',
              '| run | mode | seed | factors | valid IC | test RankIC | test ICIR | NW t |',
              '|---|---|---:|---:|---:|---:|---:|---:|']
    for row in df.sort_values(['mode', 'seed']).itertuples():
        lines.append(f'| {row.run_id} | {row.mode} | {row.seed} | {row.factor_count} | '
                     f'{row.valid_ic:+.4f} | {row.test_rank_ic:+.4f} | '
                     f'{row.test_rank_icir:+.4f} | {row.test_nw_t:+.4f} |')
    lines += ['', '## 结论', '',
              '- 三组的valid IC均为负；valid-best池没有显示稳定的样本内预测优势。',
              '- QCM `std_lam=1` 的测试RankIC点估计低于随机搜索基线；3个seed不足以做显著性或晋级。',
              '- 该结果只说明20k CPU步数下的通路与预算不足，不否定论文的2M步/CUDA设置，也不构成生产结论。',
              '- 按预注册原则保留基线；不据测试表现回选seed、超参数或表达式。',
              '- trial ledger保留1次随机搜索初始化失败（PER beta_steps=0），修复后重跑；失败未静默丢弃。',
              '', '[pilot_runs.csv](pilot_runs.csv) · [trial_ledger.csv](trial_ledger.csv) · '
              '[pilot_summary.csv](pilot_summary.csv) · '
              '[summary.json](summary.json)']
    (out / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(group.to_string(index=False))


if __name__ == '__main__':
    main()
