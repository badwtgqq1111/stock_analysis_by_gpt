#!/usr/bin/env python3
"""P2.71 data/label/PIT contract audit (no model or portfolio imports)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.market_contract import (
    CONTRACT, forward_return_wide, load_bars, normalize_panel, split_date_map,
    unit_audit, validate_panel,
)


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    raw = load_bars('2014-01-01', '2026-10-15')
    basic = pd.read_parquet(ROOT / 'assets/data/meta/cn_stock_basic.parquet')
    listed = pd.to_datetime(basic.set_index('ts_code').list_date, errors='coerce')
    raw['list_date'] = raw.stock_code.map(listed)
    before = len(raw)
    panel = raw[raw.list_date.notna() & (raw.trade_date >= raw.list_date)].drop(columns=['list_date'])
    panel = normalize_panel(panel)
    info = validate_panel(panel)
    unit = unit_audit(panel)
    by_year = []
    for year, group in panel.groupby(panel.trade_date.dt.year):
        by_year.append({'year': int(year), 'rows': len(group),
                        'assets': int(group.stock_code.nunique()),
                        'dates': int(group.trade_date.nunique()),
                        'zero_ohlc_rows': int((group[['open', 'high', 'low', 'close']] == 0)
                                              .any(axis=1).sum()),
                        'suspension_proxy_rows': int(group.open.isna().sum())})
    pd.DataFrame(by_year).to_csv(output / 'coverage_by_year.csv', index=False)

    split = {'train': ('2018-01-01', '2021-12-31'),
             'validation': ('2022-01-01', '2023-12-31'),
             'test': ('2024-01-01', '2025-10-31')}
    seg = split_date_map(split, horizon=20, dates=panel.trade_date.unique())
    labels = {
        'paper': forward_return_wide(panel, 20, kind='paper'),
        'executable': forward_return_wide(panel, 20, kind='executable'),
    }
    label_stats = {}
    for name, wide in labels.items():
        label_stats[name] = {}
        for segment in ('train', 'validation', 'test'):
            dates = wide.index.intersection(seg.index[seg == segment])
            block = wide.loc[dates]
            label_stats[name][segment] = {
                'dates_available': int(dates.size),
                'non_missing': int(block.notna().sum().sum()),
                'assets_with_label': int((block.notna().sum(axis=0) > 0).sum()),
            }

    delisted = basic[basic.list_status != 'L']
    st_names = basic[basic.name.astype(str).str.contains('ST', case=False, na=False)]
    payload = {
        'status': 'PASS_DATA_CONTRACT_AUDIT',
        'contract': CONTRACT.as_dict(),
        'raw_rows': before,
        'rows_after_listing_filter': int(len(panel)),
        'rows_removed_pre_listing': int(before - len(panel)),
        'panel': info,
        'unit_audit': unit,
        'split': split,
        'label_stats': label_stats,
        'coverage_by_year': by_year,
        'listing_metadata': {
            'rows': int(len(basic)),
            'listed_status': int((basic.list_status == 'L').sum()),
            'delisted_status': int(len(delisted)),
            'delist_date_available': False,
            'current_st_name_rows': int(len(st_names)),
            'historical_st_available': False,
            'historical_index_membership_available': False,
        },
        'limitations': [
            'list_status is a current snapshot; delisted names have no reliable delist date in this table',
            'historical ST/*ST status and historical index membership are not available',
            'bar presence is used as the tradability/listing gate; suspensions are inferred from zero OHLC',
            'qfq history is provider-adjusted and not independently reconstructed from raw prices/actions',
            'native vwap is raw-scale while OHLC is qfq; OHLC3 proxy is used by default',
        ],
    }
    (output / 'manifest.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                     allow_nan=False) + '\n')
    lines = [
        '# P2.71 数据、标签与时点契约审计', '',
        '状态：**PASS_DATA_CONTRACT_AUDIT**（契约缺口显式记录，不把缺失包装成PIT完成）。', '',
        f'- 原始日线 {before:,} 行；按上市日过滤后 {len(panel):,} 行；'
        f'{info["assets"]:,} 只股票，{info["dates"]:,} 个日期，'
        f'{info["first_date"]} → {info["last_date"]}。',
        f'- 零OHLC/停牌占位行在规范化中转为缺失；仍为0的行 {info["zero_ohlc_rows"]}。',
        f'- 原生VWAP：{unit["status"]}，`vwap/close`中位数 {unit["vwap_over_close_median"]:.4f}；'
        '默认使用OHLC3代理，原生VWAP仅单列对照。',
        f'- 当前basic表：{payload["listing_metadata"]["listed_status"]}只L、'
        f'{payload["listing_metadata"]["delisted_status"]}只D；无退市日期/历史ST/历史成分。',
        '',
        '## 标签与purge',
        '',
        '| 标签 | 段 | 可用日期 | 非缺失标签 | 有标签股票 |',
        '|---|---|---:|---:|---:|',
    ]
    for label_name, parts in label_stats.items():
        for segment, stats in parts.items():
            lines.append(f'| {label_name} | {segment} | {stats["dates_available"]} | '
                         f'{stats["non_missing"]:,} | {stats["assets_with_label"]:,} |')
    lines += [
        '',
        '- `paper`: `close[t+20]/close[t]-1`，FactorBench/AlphaGen兼容。',
        '- `executable`: `open[t+21]/open[t+1]-1`，次日开盘入场、20个交易日后开盘退出。',
        '- 训练/验证各段最后20个**可观测交易日**标为purged；不使用未来价格填充标签。',
        '',
        '## 未完成的PIT项',
        '',
        '- 退市日期、历史ST/*ST、历史指数成分、真实涨跌停/停牌状态、逐日复权因子。',
        '- 这些缺口在 `manifest.json` 的 `limitations` 中保留；不把当前股票池结果称为全市场PIT盲测。',
        '',
        '[年度覆盖](coverage_by_year.csv) · [manifest](manifest.json)',
    ]
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(f'PASS_DATA_CONTRACT_AUDIT rows={len(panel)} assets={info["assets"]} '
          f'dates={info["dates"]} removed_pre_listing={before-len(panel)}')
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/data_contract_v1')
    args = parser.parse_args()
    run(args.output.resolve())


if __name__ == '__main__':
    main()
