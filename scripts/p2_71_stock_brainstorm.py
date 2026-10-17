#!/usr/bin/env python3
"""Stock-level AlphaQCM brainstorm board around requested months.

This is a research idea generator, not an auto-trading strategy.  It combines
price/volume, moneyflow, PIT financials, valuation percentiles, industry peers
and CSI500 into:

* monthly diagnostics;
* event/panic days and forward returns;
* testable factor/strategy hypotheses;
* native AlphaGen factor candidates that can be imported into the factor pool.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BARS_ROOT = ROOT / 'assets/data/clean/ohlcv/market=CN'
FLOW_PATH = ROOT / 'assets/data/derived/cn_moneyflow_features.parquet'
FIN_PATH = ROOT / 'assets/data/meta/cn_financial_indicators.parquet'
VAL_PATH = ROOT / 'assets/data/meta/cn_valuation_features_long.parquet'
IND_PATH = ROOT / 'assets/data/meta/cn_sw2021_industry.parquet'
BENCH_PATH = ROOT / 'assets/data/meta/cn_benchmark_daily.parquet'


def _bars(codes: list[str], start: str = '2014-01-01', end: str = '2026-10-15') -> pd.DataFrame:
    files = sorted(BARS_ROOT.glob('exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet'))
    files = [p for p in files if start[:4] <= p.parent.name.split('=')[1] <= end[:4]]
    table = ds.dataset([str(p) for p in files], format='parquet').to_table(
        columns=['trade_date', 'stock_code', 'open', 'high', 'low', 'close',
                 'volume', 'amount', 'turnover'],
        filter=(ds.field('stock_code').isin(codes)) &
               (ds.field('trade_date') >= pd.Timestamp(start)) &
               (ds.field('trade_date') <= pd.Timestamp(end)))
    out = table.to_pandas()
    out['trade_date'] = pd.to_datetime(out.trade_date).dt.normalize()
    return out.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)


def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values('trade_date').copy()
    out['ret'] = out.close.pct_change()
    out['vol20'] = out.volume.rolling(20).mean()
    out['vr'] = out.volume / out.vol20
    out['ma20'] = out.close.rolling(20).mean()
    out['ma60'] = out.close.rolling(60).mean()
    out['hi60'] = out.high.rolling(60).max()
    out['dd60'] = out.close / out.hi60 - 1
    return out


def _monthly(bar: pd.DataFrame, month: str, flow: pd.DataFrame, fin: pd.DataFrame,
             val: pd.DataFrame, bench: pd.Series, peers: pd.DataFrame) -> dict:
    start = pd.Timestamp(month + '-01')
    end = start + pd.offsets.MonthEnd(0)
    x = bar[(bar.trade_date >= start) & (bar.trade_date <= end)].copy()
    if x.empty:
        raise ValueError(f'{month} 无行情')
    f = flow[(flow.trade_date >= start) & (flow.trade_date <= end)]
    b = bench[(bench.index >= start) & (bench.index <= end)]
    peer_month = peers[(peers.trade_date >= start) & (peers.trade_date <= end)]
    peer_ret = (peer_month.groupby('stock_code').close.agg(['first', 'last'])
                .assign(ret=lambda d: d['last'] / d['first'] - 1)['ret'])
    latest_fin = fin[fin.ann_date <= x.trade_date.iloc[-1]].sort_values('ann_date').tail(1)
    latest_val = val[val.trade_date <= x.trade_date.iloc[-1]].sort_values('trade_date').tail(1)
    return {
        'month': month,
        'open': float(x.open.iloc[0]),
        'close': float(x.close.iloc[-1]),
        'return': float(x.close.iloc[-1] / x.open.iloc[0] - 1),
        'high': float(x.high.max()),
        'low': float(x.low.min()),
        'max_drawdown_intramonth': float((x.close / x.close.cummax() - 1).min()),
        'avg_volume_ratio': float(x.vr.mean()),
        'max_volume_ratio': float(x.vr.max()),
        'moneyflow_net_sum': float(f.moneyflow_net_amount.sum()) if len(f) else None,
        'moneyflow_z5_mean': float(f.moneyflow_net_z_5d.mean()) if len(f) else None,
        'moneyflow_positive_ratio_mean': float(f.moneyflow_positive_ratio_10d.mean()) if len(f) else None,
        'csi500_return': float(b.iloc[-1] / b.iloc[0] - 1) if len(b) else None,
        'peer_median_return': float(peer_ret.median()) if len(peer_ret) else None,
        'peer_n': int(len(peer_ret)),
        'latest_financial': latest_fin[['ann_date', 'end_date', 'roe', 'or_yoy',
                                        'netprofit_yoy', 'eps', 'bps', 'ocfps']].iloc[0].to_dict()
        if len(latest_fin) else None,
        'latest_valuation': latest_val[['trade_date', 'pe_ind_pct', 'pb_ind_pct',
                                        'size_ind_pct']].iloc[0].to_dict()
        if len(latest_val) else None,
    }


def _forward(bar: pd.DataFrame, day: str, horizons=(5, 10, 20, 40)) -> dict:
    i = bar.index[bar.trade_date.eq(pd.Timestamp(day))]
    if len(i) == 0:
        return {}
    i = int(i[0])
    out = {'signal_date': day, 'close': float(bar.close.iloc[i])}
    for n in horizons:
        if i + n < len(bar):
            out[f'fwd_{n}d'] = float(bar.close.iloc[i + n] / bar.close.iloc[i] - 1)
    return out


def _events(bar: pd.DataFrame, fin: pd.DataFrame, flow: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for row in bar[bar.ret <= -.06].itertuples():
        f = flow[flow.trade_date.eq(row.trade_date)]
        rows.append({
            'event_date': row.trade_date, 'event': 'panic_down',
            'ret': row.ret, 'volume_ratio': row.vr,
            'moneyflow_z5': float(f.moneyflow_net_z_5d.iloc[0]) if len(f) else None,
            'next_ret': float(bar.ret.iloc[row.Index + 1]) if row.Index + 1 < len(bar) else None,
        })
    for row in fin.itertuples():
        if row.ann_date in set(bar.trade_date):
            b = bar[bar.trade_date.eq(row.ann_date)].iloc[0]
            rows.append({
                'event_date': row.ann_date, 'event': 'financial_announcement',
                'ret': b.ret, 'volume_ratio': b.vr,
                'moneyflow_z5': None, 'next_ret': None,
                'period': str(row.end_date.date()), 'roe': row.roe,
                'revenue_yoy': row.or_yoy, 'netprofit_yoy': row.netprofit_yoy,
            })
    return pd.DataFrame(rows).sort_values('event_date')


def run(code: str, months: list[str], output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    bars = _bars([code])
    if bars.empty:
        raise ValueError(f'缺少 {code} 行情')
    bar = _indicators(bars)
    flow = pd.read_parquet(FLOW_PATH)
    flow = flow[(flow.stock_code.eq(code)) & (flow.source.eq('moneyflow'))].copy()
    flow['trade_date'] = pd.to_datetime(flow.trade_date).dt.normalize()
    fin = pd.read_parquet(FIN_PATH)
    fin = fin[fin.ts_code.eq(code)].copy()
    fin['ann_date'] = pd.to_datetime(fin.ann_date).dt.normalize()
    fin['end_date'] = pd.to_datetime(fin.end_date).dt.normalize()
    val = pd.read_parquet(VAL_PATH)
    val = val[val.stock_code.eq(code)].copy()
    val['trade_date'] = pd.to_datetime(val.trade_date).dt.normalize()
    ind = pd.read_parquet(IND_PATH)
    my_ind = ind[ind.stock_code.eq(code)]
    industry_code = my_ind.industry_code.iloc[0] if len(my_ind) else None
    peer_codes = ind[ind.industry_code.eq(industry_code)].stock_code.unique().tolist() if industry_code else []
    peers = _bars(peer_codes, '2026-01-01', '2026-10-15') if peer_codes else pd.DataFrame()
    bench = pd.read_parquet(BENCH_PATH)
    bench['trade_date'] = pd.to_datetime(bench.trade_date).dt.normalize()
    bench = bench[bench.ts_code.eq('000905.SH')].set_index('trade_date').close.sort_index()

    monthly = [_monthly(bar, m, flow, fin, val, bench, peers) for m in months]
    events = _events(bar, fin, flow)
    events.to_csv(output / 'events.csv', index=False)
    pd.DataFrame(monthly).to_csv(output / 'monthly_metrics.csv', index=False)
    key_days = ['2026-03-31', '2026-04-01', '2026-06-04', '2026-06-16', '2026-06-17']
    forwards = [_forward(bar, d) for d in key_days]
    forwards = [x for x in forwards if x]

    hypotheses = [
        {'id': 'H1', 'name': '年报质量+分红重估',
         'evidence': '2026-03-31披露2025年报：ROE 38.27%、营收+11.31%、净利+34.94%，并10派20；3月股价+21.2%，同期中证500 -12.0%。',
         'trigger': '年报/业绩快报公告前20日相对行业强度>10%，且ROE/PE行业分位处于质量-低估值区间；公告日不追高，等待次日承接。',
         'testable': '按公告日事件研究，分组检验公告后5/20/40日超额收益；控制行业、市值、前期动量。',
         'status': 'hypothesis'},
        {'id': 'H2', 'name': '恐慌杀跌+资金回流的反转',
         'evidence': '2026-06-16单日-6.72%、量比2.91、PE行业分位4.9%；6-17收涨2.96%且moneyflow_z5=+1.49；6-16收盘后20日+10.6%、40日+13.1%。',
         'trigger': '单日跌幅<=-6%、量比>=2、收盘<MA20、PE行业分位<=20%；次日收涨且moneyflow_z5>0，次日开盘入场，止损放恐慌日低点。',
         'testable': '在宇通及同业上做事件研究，统计触发次数、20/40日收益、最大不利偏移和止损命中率；当前宇通仅1个确认样本。',
         'status': 'hypothesis_too_few_events'},
        {'id': 'H3', 'name': '涨停/大涨日的资金流背离',
         'evidence': '2026-03-31涨停+9.99%，但moneyflow_net_amount=-11.0亿（字段单位：万元）、z5=-1.77；4-01资金流转正+4.83亿但股价随后20日-4.9%，提示追高/派发风险。',
         'trigger': '价格涨幅>=8%但moneyflow_z5< -1，视为分布/兑现信号；不追高，持仓者观察次日承接。',
         'testable': '对大涨日按资金流符号分组，比较未来5/20日收益；需要更多样本。',
         'status': 'hypothesis'},
        {'id': 'H4', 'name': '基本面走弱下的战术反弹',
         'evidence': '2026Q1营收-7.92%、净利-12.69%；5月销量单月+14.0%但累计-3.3%、大客累计-9.7%。6月大跌有基本面背景，6-16后反弹更像超跌修复而非趋势反转。',
         'trigger': '只在恐慌反转信号出现时做战术多头，目标MA20/前高，不把反弹外推为年度趋势。',
         'testable': '把“基本面恶化+超跌反弹”与“基本面改善+突破”分开建模，比较持有期和胜率。',
         'status': 'hypothesis'},
    ]
    factor_candidates = [
        {'name': 'quality_lowpe_momentum', 'language': 'p2_71_dsl',
         'formula': 'rank(roe) - rank(pe_ind_pct) + 0.5 * rank(close / delay(close, 20) - 1)',
         'family': 'quality/value/momentum', 'status': 'candidate'},
        {'name': 'panic_reversal_quality', 'language': 'event_rule',
         'formula': 'ret_1d <= -0.06 and volume / mean(volume,20) >= 2 and close < mean(close,20) and pe_ind_pct <= 0.2 and next_ret > 0 and moneyflow_net_z_5d > 0',
         'family': 'event_reversal', 'status': 'candidate_too_few_events'},
        {'name': 'earnings_quality_breakout', 'language': 'event_rule',
         'formula': 'announcement_window and roe >= 20 and pe_ind_pct <= 0.25 and ret_20d > 0.1 and volume_ratio > 1.5',
         'family': 'earnings_drift', 'status': 'candidate'},
        {'name': 'moneyflow_divergence_warning', 'language': 'event_rule',
         'formula': 'ret_1d >= 0.08 and moneyflow_net_z_5d < -1',
         'family': 'distribution_warning', 'status': 'diagnostic'},
    ]
    payload = {
        'status': 'BRAINSTORM_COMPLETED_NOT_PROMOTION',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'code': code, 'months': months, 'industry_code': industry_code,
        'monthly': monthly, 'forward_returns': forwards,
        'hypotheses': hypotheses, 'factor_candidates': factor_candidates,
        'sample_size_warning': '宇通2024-08以来仅1个“恐慌+次日资金回流”确认样本；不能据此声称统计显著。',
    }
    (output / 'brainstorm.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                       default=str) + '\n')
    lines = [f'# {code} 头脑风暴：{", ".join(months)}', '',
             '状态：**BRAINSTORM_COMPLETED_NOT_PROMOTION**。以下是有数据依据的假设，不是已验证策略。', '',
             '## 月度快照', '',
             '| 月份 | 收益 | 中证500 | 行业中位 | 最高/最低 | 月内最大回撤 | 平均量比 | 最大量比 | 资金流z5均值 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for m in monthly:
        lines.append(f"| {m['month']} | {m['return']:+.2%} | {m['csi500_return']:+.2%} | "
                     f"{m['peer_median_return']:+.2%} | {m['high']:.2f}/{m['low']:.2f} | "
                     f"{m['max_drawdown_intramonth']:+.2%} | {m['avg_volume_ratio']:.2f} | "
                     f"{m['max_volume_ratio']:.2f} | {m['moneyflow_z5_mean']:+.2f} |")
    lines += ['', '## 关键日与前瞻收益', '',
              '| 日期 | 收盘 | 5日 | 10日 | 20日 | 40日 |', '|---|---:|---:|---:|---:|---:|']
    for f in forwards:
        def pct(k):
            v = f.get(k)
            return f'{v:+.2%}' if v is not None else '—'
        lines.append(f"| {f['signal_date']} | {f['close']:.2f} | {pct('fwd_5d')} | "
                     f"{pct('fwd_10d')} | {pct('fwd_20d')} | {pct('fwd_40d')} |")
    lines += ['', '## 事件', '',
              '| 日期 | 事件 | 收益 | 量比 | 资金流z5 | 次日收益 |',
              '|---|---|---:|---:|---:|---:|']
    for row in events.itertuples():
        lines.append(f"| {row.event_date.date()} | {row.event} | {row.ret:+.2%} | "
                     f"{row.volume_ratio:.2f} | "
                     f"{row.moneyflow_z5 if pd.notna(row.moneyflow_z5) else '—'} | "
                     f"{row.next_ret if pd.notna(row.next_ret) else '—'} |")
    lines += ['', '## 假设与可测试策略', '']
    for h in hypotheses:
        lines += [f"### {h['id']} {h['name']}", '', f"- 证据：{h['evidence']}",
                  f"- 触发：{h['trigger']}", f"- 检验：{h['testable']}",
                  f"- 状态：`{h['status']}`", '']
    lines += ['## 因子候选', '']
    for f in factor_candidates:
        lines += [f"- **{f['name']}**（{f['family']}，`{f['status']}`）：`{f['formula']}`"]
    lines += ['', '## 结论', '',
              '- 3月更像“年报质量/分红重估 + 行业相对强势”的事件驱动机会；3/31涨停但资金流大幅流出，追高并不安全。',
              '- 6月更像“基本面走弱 + 行业普跌 + 恐慌超跌”的战术反弹；6/16放量长下影、6/17资金流转正后，20/40日反弹明显。',
              '- 朋友说的“6月机会”如果指6/16附近，数据支持一次超跌反弹；如果指6月整月，则6月是显著负收益，不能把反弹当成趋势。',
              '- 当前确认样本只有1个，不能声称统计显著；应先做同业事件研究、更多年份和成本后回测。', '',
              '[brainstorm.json](brainstorm.json) · [monthly_metrics.csv](monthly_metrics.csv) · [events.csv](events.csv)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'status': payload['status'], 'code': code, 'months': months,
                      'output': str(output)}, ensure_ascii=False, indent=2))
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--code', default='600066.SH')
    parser.add_argument('--months', default='2026-03,2026-06')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/stock_brainstorm/600066_2026_03_06')
    args = parser.parse_args()
    run(args.code, [m.strip() for m in args.months.split(',')], args.output.resolve())


if __name__ == '__main__':
    main()
