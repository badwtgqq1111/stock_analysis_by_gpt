#!/usr/bin/env python3
"""P2.71 exact Alpha101 baseline audit.

Builds a qfq OHLCV panel, computes the FactorBench-compatible 71 formulas,
reports daily IC/RankIC on both named labels, and records the difference from
the legacy single-stock proxy library.  No portfolio optimisation or test-time
selection is performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.alpha101_exact import (Alpha101Metadata, NAMESPACE, VENDOR_COMMIT,
                                             build_alpha101, factor_panel)
from backend.services.factor_evaluation import daily_ic_wide, summarize_ic
from backend.services.market_contract import (
    CONTRACT, forward_return_wide, load_bars, normalize_panel, split_date_map,
    unit_audit, validate_panel,
)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def evaluate(signals: dict[str, pd.DataFrame], labels: dict[str, pd.DataFrame],
             seg_map: pd.Series, min_assets: int) -> pd.DataFrame:
    rows = []
    for name, wide in signals.items():
        for label_name, label_wide in labels.items():
            for segment in ('train', 'validation', 'test'):
                seg_dates = seg_map.index[seg_map == segment]
                dates = wide.index.intersection(seg_dates)
                if len(dates) == 0:
                    continue
                f = wide.loc[dates]
                y = label_wide.reindex(index=dates, columns=wide.columns)
                for method in ('pearson', 'spearman'):
                    values = daily_ic_wide(f, y, method=method, min_assets=min_assets)
                    summary = summarize_ic(values)
                    rows.append({'alpha': name, 'label': label_name, 'segment': segment,
                                 'method': method, **summary,
                                 'coverage': float(f.notna().to_numpy().mean()),
                                 'nan_ratio': float(f.isna().to_numpy().mean())})
    return pd.DataFrame(rows)


def legacy_comparison(panel: pd.DataFrame, signals: dict[str, pd.DataFrame],
                      max_assets: int = 40) -> pd.DataFrame:
    """Compare the first 20 names with the legacy time-series-rank proxy."""
    from factor_engine.expressions.alpha101 import Alpha101FactorSet

    codes = panel.groupby('stock_code').size().sort_values(ascending=False).head(max_assets).index
    sample = panel[panel.stock_code.isin(codes)].copy()
    engine = Alpha101FactorSet()
    rows = []
    for code, group in sample.groupby('stock_code', sort=False):
        frame = group.copy()
        frame['vwap'] = frame['vwap_proxy']
        out = engine.transform(frame)
        for i in range(1, 21):
            name = f'alpha{i:03d}'
            legacy_col = f'ALPHA101_{i:03d}'
            exact = signals[name].reindex(frame.trade_date).get(code)
            legacy = out[legacy_col]
            joined = pd.concat([exact.rename('exact'), legacy.rename('legacy')], axis=1).dropna()
            corr = float(joined.exact.rank().corr(joined.legacy.rank())) if len(joined) >= 3 else None
            rows.append({'alpha': name, 'stock_code': code, 'observations': len(joined),
                         'rank_correlation_exact_vs_legacy': corr,
                         'exact_equals_legacy': bool(
                             len(joined) > 0 and np.allclose(joined.exact, joined.legacy,
                                                             rtol=0, atol=1e-12))})
    return pd.DataFrame(rows)


def run(start: str, end: str, output: Path, *, warmup: int = 300,
        min_assets: int = 30, legacy_assets: int = 40) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    bars = load_bars('2017-01-01', end, warmup_days=0)
    # PIT listing filter: no bar before the recorded listing date.
    basic = pd.read_parquet(ROOT / 'assets/data/meta/cn_stock_basic.parquet')
    listed = pd.to_datetime(basic.set_index('ts_code').list_date, errors='coerce')
    bars['list_date'] = bars.stock_code.map(listed)
    bars = bars[bars.list_date.notna() & (bars.trade_date >= bars.list_date)]
    bars = normalize_panel(bars.drop(columns=['list_date']))
    info = validate_panel(bars)
    units = unit_audit(bars)
    split = {'train': ('2018-01-01', '2021-12-31'),
             'validation': ('2022-01-01', '2023-12-31'),
             'test': ('2024-01-01', '2025-10-31')}
    seg_map = split_date_map(split, horizon=20, dates=bars.trade_date.unique())
    labels = {
        'label_paper': forward_return_wide(bars, 20, kind='paper'),
        'label_executable': forward_return_wide(bars, 20, kind='executable'),
    }
    calc = build_alpha101(bars)
    signals = calc.compute_all(strict=True)
    names = calc.alpha_names()
    if len(names) != 71:
        raise AssertionError('精确 Alpha101 清单必须为71条')
    ic = evaluate(signals, labels, seg_map, min_assets)
    ic.to_csv(output / 'alpha101_ic.csv', index=False)
    summary = (ic[ic.method == 'spearman']
               .groupby(['alpha', 'label', 'segment'], as_index=False)
               .agg(mean_ic=('mean', 'first'), dates=('dates', 'first'),
                    icir=('icir', 'first'), nw_t=('nw_t', 'first')))
    summary.to_csv(output / 'alpha101_summary.csv', index=False)
    legacy = legacy_comparison(bars, signals, legacy_assets)
    legacy.to_csv(output / 'legacy_vs_exact_sample.csv', index=False)

    # A compact, machine-readable factor inventory.
    inventory = []
    for name in names:
        wide = signals[name]
        inventory.append({'alpha': name, 'rows': int(wide.notna().sum().sum()),
                          'nan_ratio': float(wide.isna().to_numpy().mean()),
                          'constant_dates': int((wide.nunique(axis=1) <= 1).sum())})
    pd.DataFrame(inventory).to_csv(output / 'alpha101_inventory.csv', index=False)

    manifest = {
        'status': 'PASS_EXACT_ALPHA101_AUDIT',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'namespace': NAMESPACE,
        'vendor_commit': VENDOR_COMMIT,
        'start': start, 'end': end, 'warmup_sessions': warmup,
        'split': split, 'label_horizon': 20,
        'contract': CONTRACT.as_dict(),
        'panel_audit': info, 'unit_audit': units,
        'formula_count': len(names),
        'metadata': Alpha101Metadata().as_dict(),
        'legacy_sample_assets': legacy_assets,
        'legacy_mean_rank_correlation': float(
            legacy.rank_correlation_exact_vs_legacy.dropna().mean()),
        'legacy_exact_equal_fraction': float(legacy.exact_equals_legacy.mean()),
        'source_hashes': {str(p.relative_to(ROOT)): digest(p) for p in [
            ROOT / 'backend/services/alpha101_exact.py',
            ROOT / 'backend/services/market_contract.py',
            ROOT / 'backend/services/factor_evaluation.py',
            ROOT / 'scripts/p2_71_alpha101_audit.py',
        ]},
        'limitations': [
            'qfq history is provider-adjusted; the adjustment factor itself is not independently audited',
            'native vwap is on a different scale from qfq OHLC; OHLC3 proxy is used and labelled',
            'listing dates are used for PIT inclusion, but delisting dates and historical index membership are unavailable',
            'the 30 industry-neutralised formulas are absent by design',
            'IC is retrospective, not a blind OOS or a portfolio result',
        ],
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                                                     allow_nan=False) + '\n')
    test = ic[(ic.label == 'label_paper') & (ic.segment == 'test') & (ic.method == 'spearman')]
    top = test.sort_values('mean', ascending=False).head(10)
    lines = [
        '# P2.71 精确 Alpha101 基线审计',
        '',
        f'状态：**PASS_EXACT_ALPHA101_AUDIT**。命名空间 `{NAMESPACE}`，'
        f'{len(names)} 条公式，FactorBench commit `{VENDOR_COMMIT}`。',
        '',
        f'- 面板：{info["rows"]:,} 行，{info["assets"]:,} 只股票，{info["dates"]:,} 个日期，'
        f'{info["first_date"]} → {info["last_date"]}。',
        f'- 原始 VWAP 审计：{units["status"]}；`vwap/close` 中位数 '
        f'{units["vwap_over_close_median"]:.4f}，按 OHLC3 代理进入公式。',
        f'- legacy 对照：{legacy_assets} 只抽样股票，前20条平均秩相关 '
        f'{manifest["legacy_mean_rank_correlation"]:.4f}，逐值相等比例 '
        f'{manifest["legacy_exact_equal_fraction"]:.2%}。',
        '',
        '| 测试期 RankIC 最高（paper label） | 均值 | 日期数 | ICIR | NW t |',
        '|---|---:|---:|---:|---:|',
    ]
    for row in top.itertuples():
        lines.append(f'| {row.alpha} | {row.mean:+.4f} | {row.dates} | '
                     f'{row.icir if row.icir is not None else float("nan"):+.4f} | '
                     f'{row.nw_t if row.nw_t is not None else float("nan"):+.4f} |')
    lines += [
        '',
        '## 口径',
        '',
        '- 标签1 `close[t+20]/close[t]-1`，与 FactorBench/AlphaGen 兼容；'
        '标签2 `open[t+21]/open[t+1]-1`，对应本地次日开盘入场/退出。',
        '- 横截面 rank 为同日百分位；`_safe_corr` 未定义相关按0，沿用基准实现并在 manifest 记录。',
        '- 训练/验证边界按标签结束日期 purge 20个交易日；测试段不回流选参。',
        '- 结果仅说明精确基线在本地面板的预测统计，不等于组合成本后收益或盲测晋级。',
        '',
        '[逐条IC](alpha101_ic.csv) · [汇总](alpha101_summary.csv) · '
        '[legacy对照](legacy_vs_exact_sample.csv) · [manifest](manifest.json)',
    ]
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(f'PASS_EXACT_ALPHA101_AUDIT formulas={len(names)} rows={info["rows"]} '
          f'assets={info["assets"]} dates={info["dates"]} '
          f'legacy_rank_corr={manifest["legacy_mean_rank_correlation"]:.4f}')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2018-01-01')
    parser.add_argument('--end', default='2025-10-31')
    parser.add_argument('--warmup', type=int, default=300)
    parser.add_argument('--min-assets', type=int, default=30)
    parser.add_argument('--legacy-assets', type=int, default=40)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/alpha101_exact_v1')
    args = parser.parse_args()
    run(args.start, args.end, args.output.resolve(), warmup=args.warmup,
        min_assets=args.min_assets, legacy_assets=args.legacy_assets)
    # The legacy factor-engine import can leave non-daemon UI threads at exit;
    # all artifacts are already flushed, so the CLI terminates explicitly.
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
