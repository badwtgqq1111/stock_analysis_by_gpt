#!/usr/bin/env python3
"""Read-only rule/factor parity on an independent causal research ledger.

This CLI alone imports the old signal rule as a comparator. It never calls the old
replay, old model, preselect, PK, or conditional risk. Historical data is not PIT.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from backend.services.factor_dsl import panel
from backend.services.factor_pool import FactorStore
from backend.services.factor_moneyflow import build_signals, equivalent_spec, moneyflow_panel
from backend.services.factor_replay import ReplayConfig, replay, metrics
from p2_71_factor_audit import digest


def run(start, end, warmup, output, cost_bps=15.5):
    if not pd.Timestamp(warmup) < pd.Timestamp(start) < pd.Timestamp(end):
        raise ValueError('需要warmup < start < end')
    spec = equivalent_spec()
    execution = spec['execution']
    config = ReplayConfig(**{k: execution[k] for k in ['max_positions', 'max_hold', 'stop_pct', 'stop_basis', 'holding_clock']},
                          cost_one_way=cost_bps / 10000)
    output.mkdir(parents=True, exist_ok=True)
    refs = [ROOT / p for p in ['config/strategies.json', 'scripts/moneyflow_study.py',
            'scripts/select_moneyflow_arm.py', 'output/research/moneyflow/curve/nav_engine.csv',
            'output/research/moneyflow/curve/curve.json', 'frontend/vite.config.ts']]
    original_hashes = {str(p.relative_to(ROOT)): digest(p) for p in refs}
    base = ROOT / 'assets/data/clean/ohlcv/market=CN'
    files = sorted(p for year in range(pd.Timestamp(warmup).year, pd.Timestamp(end).year + 1)
                   for p in base.glob(f'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet'))
    if not files:
        raise ValueError('缺少日线')
    flow_path = ROOT / 'assets/data/derived/cn_moneyflow_features.parquet'
    basic_path = ROOT / 'assets/data/meta/cn_stock_basic.parquet'
    bench_path = ROOT / 'assets/data/meta/cn_benchmark_daily.parquet'
    inputs = [*files, flow_path, basic_path, bench_path, ROOT / 'config/factor_pool_seed.json',
              ROOT / 'config/factor_strategy_families.json']
    # Hash before loading; verify again after execution to detect concurrent ingestion.
    input_hashes = {str(p.relative_to(ROOT)): digest(p) for p in inputs}
    bounds = (ds.field('trade_date') >= pd.Timestamp(warmup).to_pydatetime()) & (ds.field('trade_date') <= pd.Timestamp(end).to_pydatetime())
    bars = ds.dataset([str(p) for p in files], format='parquet').to_table(
        columns=['stock_code', 'trade_date', 'open', 'close', 'amount'], filter=bounds).to_pandas()
    flow = ds.dataset(str(flow_path), format='parquet').to_table(
        columns=['stock_code', 'trade_date', 'source', 'moneyflow_net_z_5d', 'moneyflow_positive_ratio_10d'], filter=bounds).to_pandas()
    universe = set(pd.read_parquet(basic_path, columns=['ts_code']).ts_code.astype(str))
    bars = panel(bars[bars.stock_code.isin(universe)])
    flow = flow[flow.stock_code.isin(universe)].copy()
    normalized_flow_rows = len(moneyflow_panel(flow))
    bench = pd.read_parquet(bench_path)
    bench.trade_date = pd.to_datetime(bench.trade_date)
    bench = bench[(bench.ts_code == '000905.SH') & bench.trade_date.between(start, end)].sort_values('trade_date')
    if bench.empty or str(bench.trade_date.iloc[0].date()) != start or str(bench.trade_date.iloc[-1].date()) != end:
        raise ValueError('基准未覆盖请求边界；请明确选择共同交易日期')
    benchmark = bench.set_index('trade_date').close
    calendar = pd.DatetimeIndex(benchmark.index)
    outside = bars[bars.trade_date.between(start, end) & ~bars.trade_date.isin(calendar)]
    if len(outside):
        raise ValueError('行情出现基准日历之外日期')
    store = FactorStore(output / 'audit_factor_pool.sqlite3')
    actual = build_signals(bars, flow, store, universe)
    import moneyflow_study as legacy  # comparator only; never call legacy.replay
    expected = legacy.build_signals(bars, flow)
    actual = actual[actual.trade_date.between(start, end)].reset_index(drop=True)
    expected = expected[expected.trade_date.between(start, end)].reset_index(drop=True)
    if expected.empty or actual.empty:
        raise ValueError('空信号不算通过')
    cols = ['stock_code', 'trade_date', 'close', 'rh', 'rl', 'cause_width',
            'moneyflow_net_z_5d', 'moneyflow_positive_ratio_10d', 'ret20', 'amount_ma']
    def canonical(frame):
        return frame[cols].sort_values(['trade_date', 'cause_width', 'stock_code'], kind='stable').reset_index(drop=True)
    pd.testing.assert_frame_equal(canonical(expected), canonical(actual), check_dtype=False, check_exact=True)
    audit_bars = bars[bars.trade_date.isin(calendar)]
    rule = replay(expected, audit_bars, benchmark, config, calendar)
    factor = replay(actual, audit_bars, benchmark, config, calendar)
    for key in ['nav', 'benchmark_nav']:
        pd.testing.assert_series_equal(rule[key], factor[key], check_exact=True)
    for key in ['daily', 'ledger', 'holdings', 'events']:
        pd.testing.assert_frame_equal(rule[key], factor[key], check_exact=True)
    # Independently reconcile the cash and end-of-day inventory to NAV.
    daily, ledger = factor['daily'], factor['ledger']
    flows = ledger.groupby('trade_date').net_cash.sum().reindex(calendar, fill_value=0)
    np.testing.assert_allclose(daily.cash, config.initial_cash + flows.cumsum(), rtol=0, atol=1e-12)
    inventory = factor['holdings'].groupby('trade_date').market_value.sum().reindex(calendar, fill_value=0)
    np.testing.assert_allclose(daily.equity, daily.cash + inventory, rtol=0, atol=1e-12)
    np.testing.assert_allclose(ledger.fee, ledger.gross * config.cost_one_way, rtol=0, atol=1e-15)
    assert (daily.cash >= 0).all()
    assert daily.n_positions.max() <= config.max_positions
    assert (ledger.trade_date > ledger.decision_date).all()
    nav_table = pd.DataFrame({'rule_nav': rule['nav'], 'factor_nav': factor['nav'], 'csi500_nav': factor['benchmark_nav']})
    legacy_frame = pd.read_csv(ROOT / 'output/research/moneyflow/curve/nav_engine.csv', parse_dates=['trade_date']).set_index('trade_date')
    legacy_nav = legacy_frame.nav.reindex(calendar)
    legacy_metrics = None
    if legacy_nav.notna().all():
        nav_table['legacy_reference_nav_rebased'] = legacy_nav / legacy_nav.iloc[0]
        legacy_metrics = metrics(legacy_nav)
    nav_table.to_csv(output / 'nav.csv')
    for prefix, result in [('rule', rule), ('factor', factor)]:
        for key in ['daily', 'ledger', 'holdings', 'events']:
            result[key].to_csv(output / f'{prefix}_{key}.csv', index=key == 'daily')
    canonical(actual).to_csv(output / 'signals.csv', index=False)
    factor_metrics, benchmark_metrics = metrics(factor['nav']), metrics(factor['benchmark_nav'])
    years = (calendar[-1] - calendar[0]).days / 365.25
    summary = {
        'buy_count': int((ledger.side == 'BUY').sum()), 'sell_count': int((ledger.side == 'SELL').sum()),
        'open_positions': len(factor['open_positions']), 'pending_exits_at_end': len(factor['pending_exits']),
        'total_fees_initial_cash_units': float(ledger.fee.sum()),
        'annual_two_sided_turnover': float(daily.two_sided_turnover.sum() / years),
        'mean_close_exposure': float((daily.market_value / daily.equity).mean()),
        'stale_position_days': int(daily.stale_positions.sum()),
        'expired_buys': int((factor['events'].event == 'expired_buy_missing_open').sum()),
        'deferred_exit_days': int((factor['events'].event == 'deferred_exit_missing_open').sum()),
        'min_cash': float(daily.cash.min()),
    }
    final_hashes = {str(p.relative_to(ROOT)): digest(p) for p in inputs}
    if final_hashes != input_hashes:
        raise ValueError('运行期间数据变化；结果不发布为通过')
    if {str(p.relative_to(ROOT)): digest(p) for p in refs} != original_hashes:
        raise ValueError('运行期间冻结参考发生变化')
    spec = equivalent_spec()
    manifest = {
        'status': 'PASS_CAUSAL_REPLAY_PARITY', 'created_at': datetime.now(timezone.utc).isoformat(),
        'start': start, 'end': end, 'warmup': warmup, 'calendar': '000905.SH observed sessions, no fill',
        'bars_rows_with_warmup': len(bars), 'flow_rows_with_warmup': len(flow),
        'normalized_flow_rows': normalized_flow_rows, 'signal_rows': len(actual), 'signal_dates': actual.trade_date.nunique(),
        'nav_dates': len(calendar), 'config': asdict(config), 'signal_parity_exact': True,
        'ledger_daily_holdings_events_nav_parity_exact': True, 'cash_inventory_fee_reconciliation': 'PASS',
        'same_evaluator_rule_metrics': factor_metrics, 'equivalent_factor_metrics': factor_metrics,
        'csi500_price_index_metrics': benchmark_metrics, 'legacy_reference_different_execution_metrics': legacy_metrics,
        'execution_summary': summary, 'primary_annual_target': 'RETROSPECTIVE_EQUIVALENCE_ONLY_NOT_OOS_OR_OPTIMIZATION',
        'production_original_hashes': original_hashes, 'input_hashes': input_hashes,
        'source_hashes': {p: digest(ROOT / p) for p in ['backend/services/factor_replay.py',
                          'backend/services/factor_moneyflow.py', 'backend/services/factor_dsl.py',
                          'scripts/p2_71_replay_audit.py']},
        'strategy_spec': spec,
        'factor_versions': [{'factor_id': r['factor_id'], 'version': r['version'],
                             'sha256': store.get(r['factor_id'], r['version'])['sha256']} for r in spec['factor_refs']],
        'limitations': ['current basic universe, not PIT; historical qfq and moneyflow availability unverified',
                       'fractional shares; no lots, limit/ST/suspension/capacity verification; no borrow or impact model',
                       'generic net_mf_amount not proven big-orders-only; amount units unverified',
                       'signal-close stop; valid-close session holding clock matches original rule convention',
                       'no terminal forced liquidation; ending inventory marked not realized',
                       'legacy uses different execution/cost/marking; not an apples-to-apples challenger',
                       'retrospective observed data; not blind OOS or forward shadow; no weight optimization or RL training'],
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    def row(name, value):
        return f"| {name} | {value['total_return']:+.4%} | {value['calendar_cagr']:+.4%} | {value['max_drawdown']:+.4%} | {value['sharpe']:.4f} |"
    table = '\n'.join([row('原规则 / 公共因果评测器', factor_metrics), row('等价因子 / 同一评测器', factor_metrics), row('中证500价格指数', benchmark_metrics)] + ([row('legacy冻结参考 / 不同执行口径', legacy_metrics)] if legacy_metrics else []))
    (output / 'REPORT.md').write_text(f'''# P2.71 独立因果回放与逐笔成本审计

状态：**PASS_CAUSAL_REPLAY_PARITY**。区间 {start} → {end}，warmup {warmup}。

- {len(actual):,}条信号；{len(calendar)}个净值日期；两版信号、排序、逐笔成交、每日现金/持仓/事件及NAV完全一致。
- {summary['buy_count']}买 / {summary['sell_count']}卖；期末{summary['open_positions']}仓；费用累计{summary['total_fees_initial_cash_units']:.8f}倍初始现金；现金最小{summary['min_cash']:.8f}。
- 逐日现金累计、持仓市值与NAV、实际成交额×单边费率勾稽通过。年双边成交额/开盘权益换手{summary['annual_two_sided_turnover']:.4f}；平均收盘暴露{summary['mean_close_exposure']:.2%}。

| 对象 | 累计收益 | 日历CAGR | 最大回撤 | Sharpe(日均/日波动×√252, rf=0) |
|---|---:|---:|---:|---:|
{table}

## 明确的执行契约

信号t收盘后形成，t+1指数交易日开盘成交；先卖后买。开盘权益使用当前open或上次已知估值，**不用当天close分配开盘仓位**。每仓目标毛金额为开盘权益/5，现金预留买费一次；每边{cost_bps}bp，按实际毛成交额收费，不再扣一笔虚拟换手费。

止损依据**信号日close**下8%，不是实际入场价（后者仅显式实验配置）。持有10个**有效close观测日**，与原规则计时约定一致；独立选项market_sessions另测。止损/满期在close决定，下一日open退出；缺open保持卖单待执行；缺close沿用已知mark并留stale标志；缺开盘买单当日过期。并列按代码升序；期末不强平，库存保留。当前stale持仓日{summary['stale_position_days']}，延后退出日{summary['deferred_exit_days']}。

## 结论边界

**已完成回顾式同口径等价，不代表组合优化完成、生产可执行或未来年化达标。** 旧曲线完整保留；legacy与公共评测器的差值包含仓位前视、费用预留、缺价和排序约定变化，不能归因于因子版获得alpha。原规则与因子版的共同评测年化才是下一轮优化的基线。

当前basic非PIT，历史复权、资金流发布时点/成交额单位待核；使用小数股，无整手、涨跌停/ST/停牌/容量验证。中证500是价格指数。该区间已被研究使用，不是盲测；尚无新组合权重优化或AlphaQCM训练。成本敏感性、PIT/OOS与前向影子仍待实施。

[净值对照](nav.csv) · [逐笔因子账本](factor_ledger.csv) · [每日现金与估值](factor_daily.csv) · [持仓快照](factor_holdings.csv) · [完整配置/输入hash](manifest.json)
''')
    print(f"PASS_CAUSAL_REPLAY_PARITY signals={len(actual)} dates={len(calendar)} buys={summary['buy_count']} sells={summary['sell_count']} cagr={factor_metrics['calendar_cagr']:.8f} max_drawdown={factor_metrics['max_drawdown']:.8f}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2024-08-27')
    parser.add_argument('--end', default='2026-10-15')
    parser.add_argument('--warmup', default='2024-06-01')
    parser.add_argument('--cost-bps', type=float, default=15.5)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/research/p2_71/replay_audit')
    args = parser.parse_args()
    run(args.start, args.end, args.warmup, args.output.resolve(), args.cost_bps)


if __name__ == '__main__':
    main()
