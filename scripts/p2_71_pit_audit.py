#!/usr/bin/env python3
"""P2.71 PIT data audit: coverage, cross-checks and quality findings.

Reads the caches written by ``scripts/p2_71_pit_fetch.py`` together with the
existing meta tables, audits them against the pilot panel, and writes
``output/research/p2_71/pit_v1/REPORT.md`` + ``manifest.json`` + CSV detail.

This is a data-layer audit, not a factor result: it answers "which PIT fields
are available, over what coverage, and are they internally consistent".
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
from backend.services import pit_data  # noqa: E402
from backend.services.market_contract import load_bars, normalize_panel  # noqa: E402
from scripts.p2_71_alphaqcm_pilot import select_panel  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def run(output: Path, *, n_assets: int, index_code: str) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    panel = select_panel(n_assets, with_fundamentals=False)  # same liquidity selection, no fundamental merge
    codes = sorted(panel.stock_code.unique())
    bars = normalize_panel(load_bars('2017-08-01', '2026-01-31'))
    bars = bars[bars.stock_code.isin(codes)].copy()
    annotated = pit_data.annotate_bars(bars, index_code=index_code)
    annotated.to_parquet(output / 'panel_pit.parquet', index=False)

    coverage = pit_data.coverage_report()
    (output / 'coverage.json').write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2, default=str) + '\n')

    # --- delist cross-check (official vs last raw bar)
    delist = pit_data.delist_dates().merge(pit_data.listing_dates(),
                                           on='stock_code', how='outer')
    raw_last = (pit_data.raw_daily()
                .groupby('stock_code')['trade_date'].max()
                .rename('last_raw_bar').reset_index())
    delist = delist.merge(raw_last, on='stock_code', how='left')
    delist['gap_days'] = (delist['delist_date'] - delist['last_raw_bar']).dt.days
    delist.to_csv(output / 'delist_crosscheck.csv', index=False)

    # --- panel PIT stats
    stats = {
        'panel_assets': int(annotated.stock_code.nunique()),
        'panel_rows': int(len(annotated)),
        'bar_coverage': {
            'limit_flags': float(annotated['close_at_up'].notna().mean()),
            'raw_close': float(annotated['raw_close'].notna().mean()),
            'st_flags': float(annotated['is_st'].notna().mean()),
            'suspension': float(annotated['suspended_official'].notna().mean()),
            'adj_source_rows': int(len(pit_data.adj_factors(codes))),
        },
        'flags': {
            'close_at_up_rows': int(annotated['close_at_up'].fillna(False).sum()),
            'close_at_down_rows': int(annotated['close_at_down'].fillna(False).sum()),
            'open_at_up_rows': int(annotated['open_at_up'].fillna(False).sum()),
            'open_at_down_rows': int(annotated['open_at_down'].fillna(False).sum()),
            'is_st_rows': int(annotated['is_st'].sum()),
            'is_delist_risk_rows': int(annotated['is_delist_risk'].sum()),
            'suspended_rows': int(annotated['suspended_official'].sum()),
            'not_listed_rows': int((~annotated['is_listed']).sum()),
            'in_index_rows': int(annotated['in_index'].sum()),
        },
        'st_codes_in_panel': int(annotated[annotated.is_st].stock_code.nunique()),
        'limit_codes_in_panel': int(
            annotated[annotated['close_at_up'].fillna(False)].stock_code.nunique()),
    }

    # --- limit-flag sanity: magnitude of raw daily change on limit days
    limit_days = annotated[annotated['close_at_up'].fillna(False)]
    raw = pit_data.raw_daily(codes)
    limit_days = limit_days.merge(
        raw[['stock_code', 'trade_date', 'pct_chg']],
        on=['stock_code', 'trade_date'], how='left', suffixes=('', '_raw'))
    pct = limit_days['pct_chg'].dropna()
    if len(pct):
        bins = np.array([0, 4.9, 5.1, 9.9, 10.1, 19.9, 20.1, 29.9, 30.1, 100])
        hist = pd.cut(pct, bins=bins).value_counts().sort_index()
        stats['limit_day_pct_chg_hist'] = {str(k): int(v) for k, v in hist.items()}
        stats['limit_days_checked'] = int(len(pct))
        stats['limit_days_within_1pct_of_a_rule'] = int(
            (pct.between(4.9, 5.1) | pct.between(9.9, 10.1)
             | pct.between(19.9, 20.1) | pct.between(29.9, 30.1)).sum())

    # --- index membership coverage for the panel
    weights = pit_data.index_weights(index_code)
    member_counts = (weights.groupby('trade_date').size()
                     .rename('members').reset_index())
    member_counts.to_csv(output / 'index_members_by_snapshot.csv', index=False)
    stats['index'] = {
        'index_code': index_code,
        'snapshots': int(len(member_counts)),
        'first_snapshot': str(member_counts.trade_date.min().date()) if len(member_counts) else None,
        'last_snapshot': str(member_counts.trade_date.max().date()) if len(member_counts) else None,
        'panel_codes_ever_member': int(weights[weights.stock_code.isin(codes)]
                                       .stock_code.nunique()),
        'panel_in_index_rows': int(annotated['in_index'].sum()),
    }

    # --- qfq vintage consistency on the panel
    vintage = pit_data.qfq_vintage_report(bars)
    vintage.to_csv(output / 'qfq_vintage.csv', index=False)
    if len(vintage):
        year_mean = vintage.pivot(index='stock_code', columns='year', values='base_mean')
        jumps = year_mean.pct_change(axis=1).abs()
        stats['qfq_vintage'] = {
            'codes_checked': int(len(year_mean)),
            'codes_with_year_jump_gt_0.5pct': int((jumps > 0.005).any(axis=1).sum()),
            'codes_with_year_jump_gt_2pct': int((jumps > 0.02).any(axis=1).sum()),
        }
    else:
        stats['qfq_vintage'] = {'codes_checked': 0}

    # --- suspension record shape
    susp = pit_data.suspend_records()
    if len(susp):
        stats['suspension'] = {
            'records': int(len(susp)),
            'codes': int(susp.stock_code.nunique()),
            'by_type': susp['suspend_type'].astype(str).str.upper()
                      .value_counts().to_dict(),
        }

    manifest = {
        'status': 'PIT_AUDIT_COMPLETED',
        'created_at': _now(),
        'n_assets': n_assets,
        'index_code': index_code,
        'coverage': coverage,
        'stats': stats,
        'source_hashes': {str(p.relative_to(ROOT)): _hash(p) for p in [
            ROOT / 'scripts/p2_71_pit_fetch.py',
            ROOT / 'scripts/p2_71_pit_audit.py',
            ROOT / 'backend/services/pit_data.py',
        ]},
        'limitations': [
            'code-level raw/limit/adj coverage is the P2.71 research universe '
            '(panel top-N + delisted + existing adjustment snapshots), not full market',
            'stk_limit history starts where the exchange feed starts; early bars may miss',
            'suspension records are official exchange records; derived calendar gaps are not audited here',
            'qfq vintage finding: local qfq keeps a rebuild-era normalization base and recent appends '
            'are raw-scale; prefer raw + adj_factors for adjustment-sensitive studies',
        ],
    }
    (output / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')

    bar = stats['bar_coverage']
    flags = stats['flags']
    lines = ['# P2.71 PIT 数据层审计', '',
             f"状态：**PIT_AUDIT_COMPLETED**；审计面板：{n_assets}只流动性面板，"
             f"bars={stats['panel_rows']:,}（2017-08至2026-01）。", '',
             '## 覆盖', '',
             '| 数据 | 面板bar覆盖率 |', '|---|---:|',
             f"| 涨跌停标志（stk_limit+raw） | {bar['limit_flags']:.2%} |",
             f"| 原始收盘价 | {bar['raw_close']:.2%} |",
             f"| ST/退市风险标志 | {bar['st_flags']:.2%} |",
             f"| 官方停复牌记录 | {bar['suspension']:.2%} |",
             '', '## 面板标志计数', '',
             '| 标志 | bars |', '|---|---:|',
             f"| 收盘涨停 | {flags['close_at_up_rows']:,} |",
             f"| 收盘跌停 | {flags['close_at_down_rows']:,} |",
             f"| 开盘涨停 | {flags['open_at_up_rows']:,} |",
             f"| 开盘跌停 | {flags['open_at_down_rows']:,} |",
             f"| ST | {flags['is_st_rows']:,} |",
             f"| 退市风险 | {flags['is_delist_risk_rows']:,} |",
             f"| 官方停牌 | {flags['suspended_rows']:,} |",
             f"| 未上市/已退市区间 | {flags['not_listed_rows']:,} |",
             f"| 指数成分内 | {flags['in_index_rows']:,} |",
             '',
             f"ST覆盖股票数：{stats['st_codes_in_panel']}；涨停股票数："
             f"{stats['limit_codes_in_panel']}；指数快照数："
             f"{stats['index']['snapshots']}（{stats['index']['first_snapshot']} "
             f"至 {stats['index']['last_snapshot']}）。", '',
             f"退市代码数：{coverage['delist_codes']}（"
             f"{coverage['delist_by_source']}）；ST区间数：{coverage['st_intervals']}。", '']
    if 'limit_days_within_1pct_of_a_rule' in stats:
        share = stats['limit_days_within_1pct_of_a_rule'] / max(1, stats['limit_days_checked'])
        lines += ['## 涨跌停交叉验证', '',
                  f"收盘涨停日中，原始涨跌幅落在常见涨跌停规则(±5/10/20/30%)±0.1pp内的比例："
                  f"**{share:.2%}**（{stats['limit_days_within_1pct_of_a_rule']}/"
                  f"{stats['limit_days_checked']}）。", '']
    if stats['qfq_vintage']['codes_checked']:
        lines += ['## qfq复权基期一致性', '',
                  f"检查 {stats['qfq_vintage']['codes_checked']} 只；年度复权基期跳变>0.5%的股票数："
                  f"**{stats['qfq_vintage']['codes_with_year_jump_gt_0.5pct']}**，>2%的数："
                  f"**{stats['qfq_vintage']['codes_with_year_jump_gt_2pct']}**。"
                  '本地qfq序列在重建基期与增量追加之间存在基期不一致，'
                  '复权敏感研究应使用原始价格+`adj_factors`。', '']
    lines += ['## 边界', '',
              '- 代码级数据覆盖为P2.71研究universe（流动性面板top-N+退市+已有复权快照），非全市场。',
              '- 审计只验证覆盖与内部一致性，不构成任何收益结论。',
              '- 涨跌停标志基于交易所stk_limit与原始收盘价，不使用本地qfq序列。', '',
              '[覆盖明细](coverage.json) · [退市交叉核对](delist_crosscheck.csv) · '
              '[面板PIT面板](panel_pit.parquet) · [qfq基期](qfq_vintage.csv) · '
              '[manifest](manifest.json)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/pit_v1')
    parser.add_argument('--assets', type=int, default=60)
    parser.add_argument('--index', default='000905.SH')
    args = parser.parse_args()
    run(args.output.resolve(), n_assets=args.assets, index_code=args.index)


if __name__ == '__main__':
    main()
