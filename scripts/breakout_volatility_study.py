#!/usr/bin/env python
"""Causal contraction x breakout event study; independent of production selectors."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factor_engine.portfolio.execution_assumptions import load_assumptions, cost_model, is_limit_up, is_limit_down

THRESHOLD = .7


def signal_features(bars: pd.DataFrame, cfg=None) -> pd.DataFrame:
    """One stock only. Setup information is <= decision close, volatility <= breakout t-1."""
    cfg = cfg or load_assumptions()
    f = bars.sort_values('trade_date').reset_index(drop=True).copy()
    if f.stock_code.nunique() != 1 or f.trade_date.duplicated().any():
        raise ValueError('one stock and unique dates required')
    close, high, low, volume = (f[c] for c in ('close','high','low','volume'))
    ret = close.pct_change(fill_method=None)
    vol20 = ret.rolling(20).std().shift(1)
    f['contraction_ratio'] = (ret.rolling(5).std().shift(1) / vol20).where(vol20 > 0)
    f['donchian'] = (close > high.rolling(20).max().shift(1)) & (close <= 1.05 * high.rolling(20).max().shift(1)) & (close.pct_change(5, fill_method=None) <= .2) & (volume >= 1.3 * volume.rolling(20).mean().shift(1))
    hi60, lo60 = high.rolling(60).max().shift(1), low.rolling(60).min().shift(1)
    code = f.stock_code.iloc[0]
    zt = pd.Series([is_limit_up(code, d, c, p, cfg) for d,c,p in zip(f.trade_date, close, close.shift(1))], dtype=int)
    first = (zt == 1) & (zt.shift(1) == 0) & (zt.rolling(60).sum().shift(1) == 0) & (close > hi60) & (hi60 / lo60 - 1 <= .45)
    f['box'] = False
    f['box_contraction_ratio'] = np.nan
    # Choose first observed retest for each first-board breakout, not all 5 offsets.
    for pos in np.flatnonzero(first.to_numpy()):
        level, peak_close = hi60.iloc[pos], close.iloc[pos]
        for offset in range(1, 6):
            i = pos + offset
            if i >= len(f):
                break
            if close.iloc[i] / peak_close - 1 <= -.03 and -.03 <= close.iloc[i] / level - 1 <= .01:
                f.loc[i, 'box'] = True
                f.loc[i, 'box_contraction_ratio'] = f.contraction_ratio.iloc[pos]
                break
    rh, rl = high.rolling(60).max().shift(26), low.rolling(60).min().shift(26)
    sideways = (rh / rl - 1 <= .30) & ((close.shift(26) / close.shift(85) - 1).abs() <= .15)
    spring = (low.rolling(25).min() < .997 * rl) & (close > rl)
    f['wyckoff'] = sideways & spring & (close > rh) & (volume >= 1.5 * volume.rolling(20).mean().shift(1)) & (f.amount.rolling(20).mean().shift(1) >= 2e7) & (close > 2)
    return f


def execute_event(code, decision, horizon, daily, calendar, cfg=None, capital=10000.):
    """t+1 open -> t+h close. Missing/limit buy skipped; constrained exits delayed."""
    cfg = cfg or load_assumptions()
    if horizon < 2:
        raise ValueError('A-share T+1 requires horizon >= 2')
    costs = cost_model(cfg)
    t = calendar.get_indexer([pd.Timestamp(decision)])[0]
    row = {'entry_date': None, 'exit_date': None, 'net_return': None, 'quantity': 0, 'exit_delay': 0, 'fees': 0., 'slippage_est': 0.}
    if t < 0 or t+1 >= len(calendar):
        return {**row, 'status': 'pending_entry'}
    entry_day = calendar[t+1]
    row['entry_date'] = entry_day.strftime('%Y-%m-%d')
    if entry_day not in daily.index:
        return {**row, 'status': 'skip_missing_entry'}
    e = daily.loc[entry_day]
    prev = daily.loc[:pd.Timestamp(decision)].close
    if not len(prev) or not np.isfinite(e.open) or e.open <= 0 or not np.isfinite(e.volume) or e.volume <= 0:
        return {**row, 'status': 'skip_invalid_entry'}
    if is_limit_up(code, entry_day, e.open, prev.iloc[-1], cfg):
        return {**row, 'status': 'skip_limit_up'}
    buy_price = float(e.open) * (1+costs['slippage'])
    lot = cfg['lot_size']
    qty = int(capital / (buy_price * (1+costs['commission'])) / lot) * lot
    row['quantity'] = qty
    if not qty:
        return {**row, 'status': 'skip_lot_budget'}
    buy_fee = qty * buy_price * costs['commission']
    cash = capital - qty * buy_price - buy_fee
    for j in range(t+horizon, min(t+horizon+21, len(calendar))):
        day = calendar[j]
        if day not in daily.index:
            continue
        exit_bar = daily.loc[day]
        prior = daily.loc[daily.index < day].close
        if not len(prior) or not np.isfinite(exit_bar.close) or exit_bar.close <= 0 or exit_bar.volume <= 0:
            continue
        if is_limit_down(code, day, exit_bar.close, prior.iloc[-1], cfg) or (cfg['price_limit'].get('no_sell_on_limit_up', True) and is_limit_up(code, day, exit_bar.close, prior.iloc[-1], cfg)):
            continue
        sell_price = float(exit_bar.close) * (1-costs['slippage'])
        sell_fee = qty * sell_price * (costs['commission'] + costs['stamp_tax_sell'])
        return {**row, 'status': 'closed', 'exit_date': day.strftime('%Y-%m-%d'), 'exit_delay': j-t-horizon,
                'fees': buy_fee+sell_fee, 'slippage_est': qty * costs['slippage'] * (float(e.open)+float(exit_bar.close)),
                'net_return': (cash + qty*sell_price-sell_fee) / capital - 1}
    return {**row, 'status': 'censored_exit', 'fees': buy_fee, 'slippage_est': qty * float(e.open) * costs['slippage']}


def hac_t(daily, calendar, lag):
    # Estimator = mean of signal-date portfolio returns. Empty market dates have zero score,
    # not a fictitious 0% investment return; lags are market sessions, not event-row offsets.
    if len(daily) < 20:
        return None
    score = (daily - daily.mean()).reindex(calendar, fill_value=0).to_numpy()
    var = float(score @ score)
    for k in range(1, min(lag+1, len(score))):
        var += 2*(1-k/(lag+1)) * float(score[k:] @ score[:-k])
    se = np.sqrt(max(var, 0)) / len(daily)
    return float(daily.mean()/se) if se > 0 else None


def summarize(events, calendar):
    rows = []
    for (strategy, horizon, state, split, year), group in events.groupby(['strategy','horizon','contraction_state','split','year'], dropna=False):
        filled = group[group.status == 'closed']
        daily = filled.groupby('trade_date').net_return.mean()
        daily.index = pd.to_datetime(daily.index)
        rows.append(dict(strategy=strategy, horizon=int(horizon), contraction_state=state, split=split, year=int(year),
                         signals=len(group), closed=len(filled), censored=int((group.status == 'censored_exit').sum()),
                         skipped_or_pending=int((~group.status.isin(['closed','censored_exit'])).sum()), decision_days=len(daily),
                         mean_net=float(filled.net_return.mean()) if len(filled) else None,
                         date_equal_mean=float(daily.mean()) if len(daily) else None,
                         nw_t=hac_t(daily, calendar, int(horizon)+20)))
    summary = pd.DataFrame(rows)
    interaction = []
    for (strategy, horizon, split, year), g in summary.groupby(['strategy','horizon','split','year']):
        states = g.set_index('contraction_state')
        a = states.loc['contracted'] if 'contracted' in states.index else None
        b = states.loc['not_contracted'] if 'not_contracted' in states.index else None
        interaction.append(dict(strategy=strategy,horizon=int(horizon),split=split,year=int(year),
                                contracted_days=int(a.decision_days) if a is not None else 0,
                                control_days=int(b.decision_days) if b is not None else 0,
                                mean_difference=float(a.date_equal_mean-b.date_equal_mean) if a is not None and b is not None else None))
    return summary, pd.DataFrame(interaction)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='2024-01-01')
    p.add_argument('--end', default='2026-10-15')
    p.add_argument('--test-start', default='2026-01-01')
    p.add_argument('--output', type=Path, default=ROOT / 'output/research/breakout_volatility')
    args = p.parse_args()
    cfg = load_assumptions()
    bench = pd.read_parquet(ROOT / 'assets/data/meta/cn_benchmark_daily.parquet')
    calendar = pd.DatetimeIndex(pd.to_datetime(bench[bench.ts_code == '000905.SH'].trade_date)).sort_values().unique()
    calendar = calendar[calendar <= pd.Timestamp(args.end)]
    basic = pd.read_parquet(ROOT / 'assets/data/meta/cn_stock_basic.parquet')
    codes = sorted(c for c in basic.ts_code.astype(str) if c.endswith(('.SH','.SZ')) and c.startswith(('0','3','6')))
    events, hashes, counts = [], {}, {}
    for exchange in ('SSE','SZSE'):
        files = [p for year in range(pd.Timestamp(args.start).year-1,pd.Timestamp(args.end).year+1)
                 for p in (ROOT / 'assets/data/clean/ohlcv/market=CN').glob(f'exchange={exchange}/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet')]
        for path in files:
            hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        bars = ds.dataset([str(x) for x in files], format='parquet').to_table(columns=['trade_date','stock_code','open','high','low','close','volume','amount'],
                      filter=ds.field('stock_code').isin(codes)).to_pandas()
        bars.trade_date = pd.to_datetime(bars.trade_date)
        bars = bars[bars.trade_date <= pd.Timestamp(args.end)]
        if bars.duplicated(['stock_code','trade_date']).any():
            raise ValueError('duplicate source bars')
        counts.update({d.strftime('%Y-%m-%d'): counts.get(d.strftime('%Y-%m-%d'),0)+int(n) for d,n in bars.groupby('trade_date').size().items()})
        print(f'{exchange}: rows={len(bars)}; stocks={bars.stock_code.nunique()}',flush=True)
        for code, frame in bars.groupby('stock_code', sort=False):
            f = signal_features(frame, cfg)
            daily = f.set_index('trade_date')
            for strategy in ('donchian','box','wyckoff'):
                selected = f[f[strategy] & (f.trade_date >= pd.Timestamp(args.start))]
                for signal in selected.itertuples():
                    ratio = signal.box_contraction_ratio if strategy == 'box' else signal.contraction_ratio
                    state = 'unknown' if not np.isfinite(ratio) else 'contracted' if ratio <= THRESHOLD else 'not_contracted'
                    for horizon in (5,20):
                        events.append({'stock_code': code, 'trade_date': signal.trade_date.strftime('%Y-%m-%d'),
                                       'strategy': strategy,'horizon': horizon,'contraction_ratio': ratio,'contraction_state': state,
                                       'year': signal.trade_date.year, 'split': 'retrospective_test' if signal.trade_date >= pd.Timestamp(args.test_start) else 'development',
                                       **execute_event(code,signal.trade_date,horizon,daily,calendar,cfg)})
        print(f'{exchange}: cumulative_events={len(events)}',flush=True)
    ordered_counts = pd.Series(counts).sort_index()
    if len(ordered_counts) > 1 and ordered_counts.iloc[-1] < .6 * ordered_counts.iloc[-2]:
        raise ValueError('partial market tail: use an earlier --end, not incomplete cross-section')
    out = args.output; out.mkdir(parents=True,exist_ok=True)
    events = pd.DataFrame(events)
    summary, interaction = summarize(events, calendar)
    events.to_parquet(out / 'events.parquet', index=False)
    summary.to_csv(out / 'summary.csv',index=False)
    interaction.to_csv(out / 'interaction.csv',index=False)
    paired = []
    closed = events[events.status == 'closed']
    for (strategy, horizon, split, year), g in closed.groupby(['strategy','horizon','split','year']):
        daily = g[g.contraction_state.isin(['contracted','not_contracted'])].groupby(['trade_date','contraction_state']).net_return.mean().unstack()
        if {'contracted','not_contracted'} <= set(daily.columns):
            delta = (daily.contracted - daily.not_contracted).dropna()
            delta.index = pd.to_datetime(delta.index)
        else:
            delta = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        paired.append(dict(strategy=strategy,horizon=int(horizon),split=split,year=int(year),paired_days=len(delta),
                           paired_date_mean_difference=float(delta.mean()) if len(delta) else None,
                           paired_nw_t=hac_t(delta, calendar, int(horizon)+20)))
    paired = pd.DataFrame(paired)
    paired.to_csv(out / 'paired_date_interaction.csv',index=False)
    audit = events.groupby(['strategy','horizon','status']).size().rename('count').reset_index()
    audit.to_csv(out / 'execution_audit.csv', index=False)
    metadata = {'start':args.start,'end':args.end,'test_start':args.test_start,'contraction_threshold': THRESHOLD,
                'contraction': 'std(close returns,t-5:t-1) / std(close returns,t-20:t-1) <= 0.7; box uses breakout-day prehistory, not retest day',
                'definitions': {'donchian': 'previous20 high breakout <=5%, 5day gain<=20%, volume>=1.3 previous20 mean',
                                'box':'prior60 amplitude<=45%, no prior limit-up; first-board close>prior60 high; first retest within5 bars, drawdown>=3%, level band[-3%,+1%]',
                                'wyckoff':'cause[t-85,t-26] width<=30%, net<=15%; spring[t-24,t] below lower*.997; close>upper; volume>=1.5 prior20; prior20 amount>=20m; close>2'},
                'execution_assumptions':cfg, 'cost_model':cost_model(cfg),'capital_per_event':10000,'holding_sessions':[5,20],
                'timing':'t close signal -> next market session open buy -> t+h close sell; A-share T+1; missing entry/limit-up skipped, constrained exits deferred <=20 market sessions, otherwise censored; retained failed events',
                'inference':'date-equal cohorts; calendar-spaced Newey-West score HAC lag=h+20, minimum20 decision days; descriptive, multiple testing not corrected',
                'input_sha256':hashes,'daily_coverage':counts,
                'caveats':['Current stock_basic causes survivorship bias; historical delisted universe not reconstructed.',
                           'Current QFQ vintages and historical ST/IPO limit exceptions not PIT; BJ excluded; limits approximate config.',
                           'Retrospective year split, not untouched prospective validation; fixed thresholds not optimized.',
                           'Events overlap and are not deployable portfolio returns. Censored positions excluded from realized mean, counts retained; missing/censored may bias estimates.',
                           '10k per event, lot100, cash residual included; no capacity/orderbook guarantees; no production changes.']}
    (out/'methodology.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    report = ['# 波动收缩 × 三类突破：事件研究','',f'窗口 {args.start}～{args.end}；{len(events):,} 条策略/持有期事件；固定收缩阈值0.7。',
              '费用复用统一配置：当前买5.5bp、卖15.5bp，滑点以成交价格计、佣金印花税按成交额计。每事件1万元、100股整手；现金尾数计入收益。',
              '次日市场开盘买，t+5/t+20收盘卖；停牌/涨停跳过买入，退出受限最多顺延20个市场交易日，未实现保留censored。',
              '对照为同形态的未收缩样本；波动只用突破前一日以前数据；箱体在首板突破前测收缩，回踩后才决策。',
              'summary.csv 按年/回顾开发与测试段/状态分层；interaction.csv 为日期等权均值差。并非真实前向或可交易组合业绩，不据此晋级。','', '## 执行审计','```',audit.to_string(index=False),'```','', '## 收缩交互（同日配对净收益差，主要描述统计）','```',paired.to_string(index=False),'```',
              'interaction.csv 另列非配对日期等权差，可能含不同市场时段构成影响；优先查看 paired_date_interaction.csv。','', '## 已知局限',*['- '+c for c in metadata['caveats']]]
    (out/'REPORT.md').write_text('\n'.join(report)+'\n')
    print(audit.to_string(index=False)); print(f'output={out}')


if __name__ == '__main__':
    main()
