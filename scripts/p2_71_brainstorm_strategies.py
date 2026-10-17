#!/usr/bin/env python3
"""Backtest the two stock-brainstorm candidates against CSI500.

1. quality_lowpe_momentum: cross-sectional rank(roe) - rank(pe_ind_pct)
   + 0.5 * rank(20d momentum), top20 equal weight, 20-session rebalance.
2. panic_reversal_quality: panic day <= -6%, volume >= 2x 20d, close<MA20,
   PE industry percentile <= 0.2; next day positive return and positive
   moneyflow z5; enter next open, hold 20 sessions, stop below panic low.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.alphaqcm_portfolio import PortfolioConfig, backtest_signal
from backend.services.factor_replay import metrics
from backend.services.market_contract import benchmark_series
from scripts.p2_71_alphaqcm_pilot import select_panel


def _annual(nav: pd.Series) -> float | None:
    n = len(nav) - 1
    return float((nav.iloc[-1] / nav.iloc[0]) ** (252 / n) - 1) if n > 0 else None


def _write_result(output: Path, name: str, nav: pd.Series, bench: pd.Series,
                  portfolio: dict, benchmark: dict, *, config: dict,
                  trigger_count: int, methodology: str) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({'nav': nav.values, 'benchmark_nav': bench.reindex(nav.index).values},
                         index=nav.index)
    frame.index.name = 'trade_date'
    frame.to_csv(output / 'nav.csv')
    payload = {
        'status': 'PASS_BRAINSTORM_STRATEGY_REPLAY',
        'strategy': name, 'start': str(nav.index[0].date()),
        'end': str(nav.index[-1].date()), 'config': config,
        'portfolio': {**portfolio, 'annual_return_252': _annual(nav)},
        'csi500': {**benchmark, 'annual_return_252': _annual(bench.reindex(nav.index))},
        'excess_return': float(nav.iloc[-1] / bench.reindex(nav.index).iloc[-1] - 1),
        'observations': len(nav) - 1, 'trigger_count': int(trigger_count),
        'methodology': methodology,
        'limitations': [
            'research universe is a 300-stock liquid panel, not full-market PIT',
            'fractional shares; no lots/limit/ST/suspension/capacity/borrow model',
            'CSI500 is a price index; dividends are not included',
            'retrospective research window; not a forward shadow or production result',
        ],
    }
    (output / 'metrics.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                    default=str) + '\n')
    lines = [f'# {name} · 中证500对比', '',
             f"区间 {payload['start']} → {payload['end']}；{methodology}", '',
             '| 指标 | 策略 | 中证500 |', '|---|---:|---:|']
    for key, label, pct in [('total_return', '累计收益', True),
                            ('annual_return_252', '年化收益(252日)', True),
                            ('max_drawdown', '最大回撤', True),
                            ('sharpe', 'Sharpe(rf=0)', False)]:
        a, b = payload['portfolio'].get(key), payload['csi500'].get(key)
        fmt = lambda v: f'{v*100:+.2f}%' if pct and v is not None else (
            f'{v:+.3f}' if v is not None else '—')
        lines.append(f'| {label} | {fmt(a)} | {fmt(b)} |')
    lines += ['', f"相对中证500累计超额：**{payload['excess_return']*100:+.2f}%**。", '',
              '[净值](nav.csv) · [指标](metrics.json)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: payload[k] for k in ('strategy', 'observations', 'trigger_count',
                                              'excess_return')}, ensure_ascii=False, indent=2,
                     default=str))
    return payload


def _panic_backtest(panel: pd.DataFrame, triggers: pd.DataFrame, bench: pd.Series, *,
                    max_positions: int = 10, max_hold: int = 20,
                    cost_one_way: float = 15.5 / 1e4) -> dict:
    panel = panel.sort_values(['stock_code', 'trade_date'])
    dates = pd.DatetimeIndex(sorted(panel.trade_date.unique()))
    opens = panel.pivot(index='trade_date', columns='stock_code', values='open')
    closes = panel.pivot(index='trade_date', columns='stock_code', values='close')
    b = bench.reindex(dates)
    if b.isna().any() or (b <= 0).any():
        raise ValueError('CSI500 未覆盖事件回测日历')
    by_confirm = {pd.Timestamp(k): v for k, v in triggers.groupby('confirm_date')}
    cash = 1.0
    positions: dict[str, dict] = {}
    pending_entries: list[dict] = []
    pending_exits: list[str] = []
    rows = []
    trades = []
    for day in dates:
        # exits at open
        for code in list(pending_exits):
            pos = positions.get(code)
            px = opens.at[day, code] if code in opens.columns else np.nan
            if pos is None or not np.isfinite(px):
                continue
            gross = pos['shares'] * float(px)
            fee = gross * cost_one_way
            cash += gross - fee
            trades.append({'trade_date': day, 'code': code, 'side': 'SELL',
                           'price': float(px), 'gross': gross, 'fee': fee,
                           'reason': pos.get('exit_reason', 'hold')})
            del positions[code]
        # entries at open, rank by moneyflow z5 and lower PE
        if pending_entries:
            equity = cash + sum(p['shares'] * float(opens.at[day, c])
                                for c, p in positions.items()
                                if c in opens.columns and np.isfinite(opens.at[day, c]))
            slots = max(0, max_positions - len(positions))
            candidates = [x for x in pending_entries if x['code'] not in positions]
            candidates.sort(key=lambda x: (-float(x.get('moneyflow_z5', 0) or 0),
                                           float(x.get('pe_ind_pct', 1) or 1), x['code']))
            for item in candidates[:slots]:
                code = item['code']
                px = opens.at[day, code] if code in opens.columns else np.nan
                if not np.isfinite(px):
                    continue
                gross = equity / max_positions
                gross = min(gross, max(cash, 0.0) / (1 + cost_one_way))
                if gross <= 0:
                    continue
                fee = gross * cost_one_way
                cash -= gross + fee
                positions[code] = {
                    'shares': gross / float(px), 'entry': float(px),
                    'panic_low': float(item['panic_low']), 'holding': 0,
                    'entry_date': day, 'exit_reason': None,
                }
                trades.append({'trade_date': day, 'code': code, 'side': 'BUY',
                               'price': float(px), 'gross': gross, 'fee': fee,
                               'reason': 'panic_reversal'})
            pending_entries = []
        equity = cash + sum(p['shares'] * float(closes.at[day, c])
                            for c, p in positions.items()
                            if c in closes.columns and np.isfinite(closes.at[day, c]))
        rows.append({'trade_date': day, 'nav': equity, 'cash': cash,
                     'n_positions': len(positions), 'benchmark_nav': float(b.loc[day] / b.iloc[0])})
        # decisions at close
        for code, pos in positions.items():
            px = closes.at[day, code] if code in closes.columns else np.nan
            if not np.isfinite(px):
                continue
            pos['holding'] += 1
            if px < pos['panic_low']:
                pos['exit_reason'] = 'panic_low'
                if code not in pending_exits:
                    pending_exits.append(code)
            elif pos['holding'] >= max_hold:
                pos['exit_reason'] = 'max_hold'
                if code not in pending_exits:
                    pending_exits.append(code)
        # next-day entries from today's confirmation
        if day in by_confirm and day != dates[-1]:
            for row in by_confirm[day].itertuples():
                pending_entries.append({'code': row.stock_code,
                                        'panic_low': row.panic_low,
                                        'moneyflow_z5': row.moneyflow_net_z_5d,
                                        'pe_ind_pct': row.pe_ind_pct})
    nav = pd.DataFrame(rows).set_index('trade_date')['nav']
    nav = nav / nav.iloc[0]
    bnorm = b / b.iloc[0]
    return {'nav': nav, 'benchmark': bnorm, 'trades': pd.DataFrame(trades)}


def run(output: Path, *, start: str, end: str, assets: int) -> dict:
    panel = select_panel(assets, with_fundamentals=True)
    for col in ('pe_ind_pct', 'pb_ind_pct', 'size_ind_pct'):
        panel[col] = panel.groupby('stock_code')[col].ffill()
    panel = panel.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)
    g = panel.groupby('stock_code', sort=False)
    panel['ret1'] = g.close.pct_change()
    panel['mom20'] = panel.close / g.close.shift(20) - 1
    panel['ma20'] = g.close.transform(lambda s: s.rolling(20).mean())
    panel['vma20'] = g.volume.transform(lambda s: s.rolling(20).mean())
    panel['vr'] = panel.volume / panel.vma20
    flow = pd.read_parquet(ROOT / 'assets/data/derived/cn_moneyflow_features.parquet')
    flow = flow[flow.source.eq('moneyflow')][['trade_date', 'stock_code', 'moneyflow_net_z_5d']]
    flow['trade_date'] = pd.to_datetime(flow.trade_date).dt.normalize()
    panel = panel.merge(flow, on=['trade_date', 'stock_code'], how='left')
    panel['next_ret'] = panel.groupby('stock_code').ret1.shift(-1)
    panel['next_mfz'] = panel.groupby('stock_code').moneyflow_net_z_5d.shift(-1)

    # quality score
    rank_roe = panel.groupby('trade_date').roe.rank(pct=True)
    rank_pe = panel.groupby('trade_date').pe_ind_pct.rank(pct=True)
    rank_mom = panel.groupby('trade_date').mom20.rank(pct=True)
    panel['quality_score'] = rank_roe - rank_pe + 0.5 * rank_mom
    full_index = pd.MultiIndex.from_frame(panel[['trade_date', 'stock_code']])
    quality_signal = pd.Series(panel.quality_score.to_numpy(), index=full_index).dropna()
    sub = panel[(panel.trade_date >= start) & (panel.trade_date <= end)].copy()
    bench = benchmark_series('000905.SH', start, end)
    qres = backtest_signal(sub, quality_signal, bench,
                           PortfolioConfig(top_n=20, rebalance_every=20))
    q_output = output / 'quality_lowpe_momentum'
    _write_result(q_output, 'quality_lowpe_momentum', qres['nav'].portfolio_nav,
                  qres['nav'].benchmark_nav, qres['portfolio_metrics'],
                  qres['benchmark_metrics'], config=qres['config'], trigger_count=0,
                  methodology='rank(roe) - rank(pe_ind_pct) + 0.5*rank(20d momentum)，top20等权，20日调仓，t+1开盘，15.5bp/边。')

    # panic trigger: confirmation day is t+1; entry is t+2 open
    panic = (panel.ret1 <= -.06) & (panel.vr >= 2) & (panel.close < panel.ma20) & (panel.pe_ind_pct <= .2)
    panel['panic'] = panic
    panel['confirm'] = (panel.groupby('stock_code').panic.shift(1).fillna(False)
                        & (panel.ret1 > 0) & (panel.moneyflow_net_z_5d > 0))
    panel['panic_low'] = panel.groupby('stock_code').low.shift(1)
    triggers = panel[panel.confirm & panel.trade_date.between(start, end)].copy()
    triggers = triggers.rename(columns={'trade_date': 'confirm_date'})
    triggers = triggers[['confirm_date', 'stock_code', 'panic_low',
                         'moneyflow_net_z_5d', 'pe_ind_pct']].dropna()
    pres = _panic_backtest(sub, triggers, bench)
    p_output = output / 'panic_reversal_quality'
    _write_result(p_output, 'panic_reversal_quality', pres['nav'], pres['benchmark'],
                  metrics(pres['nav']), metrics(pres['benchmark']),
                  config={'max_positions': 10, 'max_hold': 20,
                          'cost_one_way': 15.5 / 1e4},
                  trigger_count=len(triggers),
                  methodology='恐慌日<-6%、量比>=2、close<MA20、PE行业分位<=20%；次日收涨且资金流z5>0，再次日开盘入场；最多10仓，持有20日或跌破恐慌低点退出，15.5bp/边。')
    return {'quality': qres['portfolio_metrics'], 'panic': metrics(pres['nav']),
            'trigger_count': len(triggers)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/brainstorm_strategies')
    parser.add_argument('--start', default='2023-01-01')
    parser.add_argument('--end', default='2026-10-15')
    parser.add_argument('--assets', type=int, default=300)
    args = parser.parse_args()
    print(json.dumps(run(args.output.resolve(), start=args.start, end=args.end,
                         assets=args.assets), ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
