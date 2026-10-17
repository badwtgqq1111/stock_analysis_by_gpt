#!/usr/bin/env python3
"""Read-only real-data rule-vs-pinned-factor signal audit (not an OOS return test).

No legacy model or production output is run/rewritten. The old rule is imported
ONLY inside this comparison CLI; the new path has no dependency on it.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from backend.services.factor_pool import FactorStore
from backend.services.factor_moneyflow import build_signals, equivalent_spec, moneyflow_panel
from backend.services.factor_dsl import panel


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def run(start, end, warmup, output):
    if not pd.Timestamp(warmup) < pd.Timestamp(start) <= pd.Timestamp(end):
        raise ValueError('需要warmup < start <= end')
    output.mkdir(parents=True, exist_ok=True)
    base = ROOT / 'assets/data/clean/ohlcv/market=CN'
    files = sorted(p for year in range(pd.Timestamp(warmup).year, pd.Timestamp(end).year+1)
                   for p in base.glob(f'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet'))
    if not files:
        raise ValueError('缺少日线')
    lo, hi = pd.Timestamp(warmup).to_pydatetime(), pd.Timestamp(end).to_pydatetime()
    bounds = (ds.field('trade_date') >= lo) & (ds.field('trade_date') <= hi)
    bars = ds.dataset([str(p) for p in files], format='parquet').to_table(
        columns=['stock_code','trade_date','open','close','amount'], filter=bounds).to_pandas()
    flow_path = ROOT / 'assets/data/derived/cn_moneyflow_features.parquet'
    flow = ds.dataset(str(flow_path), format='parquet').to_table(
        columns=['stock_code','trade_date','source','moneyflow_net_z_5d','moneyflow_positive_ratio_10d'], filter=bounds).to_pandas()
    basic_path = ROOT / 'assets/data/meta/cn_stock_basic.parquet'
    universe = set(pd.read_parquet(basic_path, columns=['ts_code']).ts_code.astype(str))
    bars = panel(bars[bars.stock_code.isin(universe)])
    flow = flow[flow.stock_code.isin(universe)].copy()
    normalized_flow = moneyflow_panel(flow)
    if bars.empty or flow.empty:
        raise ValueError('指定区间无行情或资金流')
    counts = bars.groupby('stock_code').trade_date.apply(lambda x: (x < pd.Timestamp(start)).sum())
    insufficient = int((counts < 20).sum())
    # Audit must not add/edit real manual factor versions. Own DB freezes seed v1.
    store = FactorStore(output / 'audit_factor_pool.sqlite3')
    actual = build_signals(bars, flow, store, universe)
    import moneyflow_study as legacy
    expected = legacy.build_signals(bars, flow)
    expected = expected[expected.trade_date >= pd.Timestamp(start)].reset_index(drop=True)
    actual = actual[actual.trade_date >= pd.Timestamp(start)].reset_index(drop=True)
    if expected.empty or actual.empty:
        raise ValueError('空信号不计为等价验证通过')
    columns = ['stock_code','trade_date','close','rh','rl','cause_width',
               'moneyflow_net_z_5d','moneyflow_positive_ratio_10d','ret20','amount_ma']
    def canonical(df):
        return df[columns].sort_values(['stock_code','trade_date']).reset_index(drop=True)
    pd.testing.assert_frame_equal(canonical(expected), canonical(actual), check_dtype=False, check_exact=True)
    order = expected.sort_values(['trade_date','cause_width','stock_code'], kind='stable')
    pd.testing.assert_frame_equal(order[['stock_code','trade_date']].reset_index(drop=True), actual[['stock_code','trade_date']])
    actual[['stock_code','trade_date','score','rank']].to_csv(output / 'signals.csv', index=False)
    frozen_paths = [ROOT/'config/strategies.json', ROOT/'scripts/moneyflow_study.py', ROOT/'scripts/select_moneyflow_arm.py',
                    ROOT/'output/research/moneyflow/curve/nav_engine.csv', ROOT/'output/research/moneyflow/curve/curve.json']
    spec = equivalent_spec()
    report = {
        'status': 'PASS_SIGNAL_PARITY', 'created_at': datetime.now(timezone.utc).isoformat(),
        'start': start, 'end': end, 'warmup_start': warmup,
        'bars_rows': len(bars), 'flow_rows': len(flow), 'normalized_flow_rows': len(normalized_flow),
        'moneyflow_source': 'moneyflow (generic fields); other provider generic fields verified all null', 'universe_kind': 'current_basic_not_PIT',
        'assets_with_less_than_20_prior_observations': insufficient,
        'signal_rows': len(actual), 'signal_dates': actual.trade_date.nunique(),
        'daily_eligible_sets_equal': True, 'all_signal_numeric_fields_exact_equal': True,
        'rank_order_equal': True, 'tie_policy': 'stock_code_ascending (explicit deterministic policy)',
        'nav_parity': 'NOT_RUN', 'annual_return_target': 'NOT_EVALUATED',
        'production_original_hashes': {str(p.relative_to(ROOT)): digest(p) for p in frozen_paths if p.exists()},
        'inputs': [{'path': str(p.relative_to(ROOT)), 'size': p.stat().st_size, 'sha256': digest(p)}
                   for p in [*files, flow_path, basic_path, ROOT/'config/factor_strategy_families.json', ROOT/'config/factor_pool_seed.json']],
        'strategy_spec': spec,
        'factor_versions': [{'factor_id': r['factor_id'], 'version': r['version'],
                             'sha256': store.get(r['factor_id'], r['version'])['sha256'],
                             'formula': store.get(r['factor_id'], r['version'])['formula']} for r in spec['factor_refs']],
        'remaining': ['因子保存/信号等价不证明收益提高', '逐笔交易与NAV等价尚未验证',
                      '历史复权/PIT/资金流可得时点/成交额单位待审计',
                      '旧回放成本每边15.5bp、开盘估值引用当日close、未预留买费、缺价估值和止损基准需独立审计',
                      '因子组合优化、AlphaQCM训练、成本后年化达标均未完成'],
    }
    (output/'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    (output/'REPORT.md').write_text(f'''# P2.71 主力资金流信号等价审计

- 状态：**{report['status']}**。
- 区间：{start} → {end}；warmup自{warmup}；股票池为当前basic（非PIT）。
- 日线：{len(bars):,}行；资金流：{len(flow):,}行。
- 通过：{len(actual):,}条信号 / {report['signal_dates']}个信号日；逐日入选集合、全部信号数值字段与显式同分排序完全一致。
- 对照：原规则build_signals与独立DSL/固定v1因子版。无旧模型、preselect、PK依赖。
- 因子编辑不改变固定v1策略绑定。人工因子库与本次审计数据库分离。
- **逐笔交易/NAV等价未执行，收益目标未评估。此报告不是收益达标/OOS证据。**
- 旧回放含前视仓位估值等问题；后续先建无前视公共回放，双边费用明确为每边15.5bp，再重跑两版。
- 同分代码排序为新路径明确契约；旧回放排序不稳定时的持仓影响另测，不冒称NAV已等价。
- [完整配置与数据hash](manifest.json)；[逐日得分与排序](signals.csv)。
''')
    print(f"PASS_SIGNAL_PARITY rows={len(actual)} dates={report['signal_dates']} nav=NOT_RUN annual_target=NOT_EVALUATED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2026-07-01')
    parser.add_argument('--end', default='2026-10-15')
    parser.add_argument('--warmup', default='2026-05-01')
    parser.add_argument('--output', type=Path, default=ROOT/'output/research/p2_71/signal_audit')
    args = parser.parse_args()
    run(args.start, args.end, args.warmup, args.output.resolve())


if __name__ == '__main__':
    main()
