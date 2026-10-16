#!/usr/bin/env python
"""Retrospective capitulation -> reclaim hypothesis. Research only, no production writes."""
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
from scripts.breakout_volatility_study import execute_event, hac_t
from factor_engine.portfolio.execution_assumptions import load_assumptions

STRATEGIES = ('early_reclaim', 'panic_confirm', 'no_volume_confirm', 'basic_rebound', 'donchian')


def features(bars):
    f = bars.sort_values('trade_date').reset_index(drop=True).copy()
    if f.empty or f.stock_code.nunique() != 1 or f.trade_date.duplicated().any():
        raise ValueError('one nonempty stock with unique dates required')
    f['trade_date'] = pd.to_datetime(f.trade_date)
    for c in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        f[c] = pd.to_numeric(f[c], errors='coerce')
    valid = (np.isfinite(f[['open','high','low','close','volume','amount']]).all(axis=1)
             & (f[['open','high','low','close','volume','amount']] > 0).all(axis=1)
             & (f.high >= f[['open','close','low']].max(axis=1))
             & (f.low <= f[['open','close']].min(axis=1)))
    # Invalid bars interrupt the entire 120-record warmup, not just today's signal.
    warm = valid.rolling(120).sum().eq(120)
    c,h,l,v = (f[k].where(valid) for k in ('close','high','low','volume'))
    f['ret1'] = c.pct_change(fill_method=None)
    for n in (20,60):
        f[f'ma{n}'] = c.rolling(n).mean()
        f[f'prior_high{n}'] = h.rolling(n).max().shift(1)
    f['prior_low60'] = l.rolling(60).min().shift(1)
    f['dd120'] = c / h.rolling(120).max() - 1
    f['dd60'] = c / h.rolling(60).max() - 1
    f['vr20'] = v / v.rolling(20).mean().shift(1)
    f['clv'] = (c-l)/(h-l).replace(0,np.nan)
    f['atr20_pct'] = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1).rolling(20).mean()/c
    f['eligible'] = warm & (f.amount.rolling(20).mean().shift(1) >= 2e7) & (c > 2)
    f['panic_base'] = f.eligible & (f.dd120 <= -.20) & (l < f.prior_low60) & (f.ret1 <= -.03) & (f.clv >= .5)
    f['panic'] = f.panic_base & (f.vr20 >= 2)
    for s in STRATEGIES:
        f[s] = False
        f[s+'_setup'] = pd.NaT
    # Each setup can generate one early and one confirmed signal; a lower low invalidates it.
    for need_volume, label in ((True,'panic_confirm'),(False,'no_volume_confirm')):
        for p in np.flatnonzero(f['panic' if need_volume else 'panic_base'].to_numpy()):
            reclaimed = False
            for i in range(p+1,min(p+21,len(f))):
                if not f.eligible.iloc[i] or l.iloc[i] < l.iloc[p]:
                    break
                if not reclaimed and i-p <= 10 and c.iloc[i] > max(f.prior_low60.iloc[p],f.ma20.iloc[i]):
                    reclaimed = True
                    if need_volume:
                        f.loc[i,'early_reclaim'] = True
                        f.loc[i,'early_reclaim_setup'] = f.trade_date.iloc[p]
                if not reclaimed and i-p > 10:
                    break
                if reclaimed and c.iloc[i] > max(f.ma60.iloc[i], f.prior_high20.iloc[i]):
                    f.loc[i,label] = True
                    f.loc[i,label+'_setup'] = f.trade_date.iloc[p]
                    break
    f['basic_rebound'] = f.eligible & (f.dd60 <= -.2) & (c > f.ma20) & (f.ret1 > 0)
    f['donchian'] = f.eligible & (c > f.prior_high20)
    return f


def dedupe_positions(mask, dates, calendar, gap=40):
    result=[]; previous=-10**9
    for i in np.flatnonzero(np.asarray(mask)):
        pos=calendar.get_indexer([dates.iloc[i]])[0]
        if pos >= 0 and pos-previous >= gap:
            result.append(i); previous=pos
    return result


def risk_on(names, day):
    """Known, announced history only. Unknown history is not assumed non-ST."""
    if names.empty:
        return None
    d=pd.Timestamp(day)
    visible=names[(names.start_date <= d) & (names.ann_date <= d) & (names.end_date.isna() | (names.end_date >= d))]
    if visible.empty:
        return None
    return bool(visible.sort_values(['start_date','ann_date']).iloc[-1].is_risk)


def summarize(events, calendar, seed=832):
    rows=[]; rng=np.random.default_rng(seed)
    for (s,h,period),g in events.groupby(['strategy','horizon','period']):
        good=g[g.status=='closed']; daily=good.groupby('trade_date').net_return.mean()
        daily.index=pd.to_datetime(daily.index)
        cal=calendar[(calendar>=pd.to_datetime(g.trade_date).min()) & (calendar<=pd.to_datetime(g.trade_date).max())]
        x=daily.reindex(cal).to_numpy(); ci=[None,None]
        if len(daily)>=20:
            n=len(x); block=max(20,int(h)); draws=[]
            for _ in range(1000):
                starts=rng.integers(0,n,size=int(np.ceil(n/block)))
                ix=((starts[:,None]+np.arange(block))%n).ravel()[:n]
                sample=x[ix]
                if np.isfinite(sample).any(): draws.append(float(np.nanmean(sample)))
            ci=np.quantile(draws,[.025,.975]).tolist()
        rows.append(dict(strategy=s,horizon=int(h),period=period,signals=len(g),closed=len(good),
             status_counts=json.dumps(g.status.value_counts().to_dict(),sort_keys=True),decision_days=len(daily),
             event_mean=good.net_return.mean(),median=good.net_return.median(),win_rate=(good.net_return>0).mean() if len(good) else np.nan,
             date_equal_mean=daily.mean(),block_ci_low=ci[0],block_ci_high=ci[1],nw_t=hac_t(daily,cal,int(h)+20),
             mean_index_reference_excess=good.index_reference_excess.mean()))
    return pd.DataFrame(rows)


def main():
    p=argparse.ArgumentParser();p.add_argument('--start',default='2020-01-01');p.add_argument('--end',default='2026-10-14')
    p.add_argument('--output',type=Path,default=ROOT/'output/research/capitulation_reclaim_20261016')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    benchmark=pd.read_parquet(ROOT/'assets/data/meta/cn_benchmark_daily.parquet')
    b=benchmark[benchmark.ts_code=='000905.SH'].copy();b.trade_date=pd.to_datetime(b.trade_date)
    benchmark=b.set_index('trade_date').close.sort_index();calendar=benchmark.index[benchmark.index<=pd.Timestamp(a.end)].unique()
    names=pd.read_parquet(ROOT/'assets/data/meta/cn_stock_namechange.parquet')
    for c in ('start_date','end_date','ann_date'):names[c]=pd.to_datetime(names[c])
    groups={code:g for code,g in names.groupby('ts_code')}
    events=[];hashes={};counts={};sample=None
    for exchange in ('SSE','SZSE'):
        files=sorted(p for year in range(pd.Timestamp(a.start).year-1,pd.Timestamp(a.end).year+1)
                     for p in (ROOT/'assets/data/clean/ohlcv/market=CN').glob(f'exchange={exchange}/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet'))
        for path in files:hashes[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
        bars=ds.dataset([str(x) for x in files],format='parquet').to_table(columns=['trade_date','stock_code','open','high','low','close','volume','amount']).to_pandas()
        bars.trade_date=pd.to_datetime(bars.trade_date)
        bars=bars[(bars.trade_date<=pd.Timestamp(a.end)) & bars.stock_code.str.match(r'^(000|001|002|003|300|301|600|601|603|605|688)\d{3}\.(SH|SZ)$')]
        if bars.duplicated(['stock_code','trade_date']).any():raise ValueError('duplicate source bars')
        for d,n in bars.groupby('trade_date').size().items():counts[d.strftime('%Y-%m-%d')]=counts.get(d.strftime('%Y-%m-%d'),0)+int(n)
        print(f'{exchange}: rows={len(bars)}, codes={bars.stock_code.nunique()}',flush=True)
        for j,(code,frame) in enumerate(bars.groupby('stock_code',sort=True)):
            f=features(frame);daily=f.set_index('trade_date');nh=groups.get(code,names.iloc[:0]);cache={}
            cfg={**load_assumptions(), 'lot_size':200 if code.startswith('688') else 100}
            if code=='300832.SZ':sample=f.copy()
            for s in STRATEGIES:
                for i in dedupe_positions(f[s] & (f.trade_date>=pd.Timestamp(a.start)),f.trade_date,calendar):
                    d=f.trade_date.iloc[i];risk=risk_on(nh,d)
                    for h in (5,10,20,40):
                        key=(i,h)
                        if key not in cache:
                            if risk is None or risk:
                                result={'status':'skip_unknown_name' if risk is None else 'skip_risk_decision'}
                            else:
                                t=calendar.get_indexer([d])[0]
                                entry=calendar[t+1] if t+1<len(calendar) else None
                                if entry is not None and risk_on(nh,entry) is not False:
                                    result={'status':'skip_risk_or_unknown_entry'}
                                else:
                                    result=execute_event(code,d,h,daily,calendar,cfg)
                                    if result['status']=='closed':
                                        end=pd.Timestamp(result['exit_date'])
                                        # Future risk does not remove the signal, it is a reported censor.
                                        changed=nh[nh.is_risk & (nh.start_date>entry) & (nh.start_date<=end) & (nh.ann_date<=end)]
                                        if len(changed): result={**result,'status':'censored_risk_change','net_return':None}
                                        else:
                                            reference=benchmark.get(end,np.nan)/benchmark.get(entry,np.nan)-1
                                            result['index_reference_return']=reference
                                            result['index_reference_excess']=result['net_return']-reference
                            cache[key]=result
                        events.append({'stock_code':code,'trade_date':d.strftime('%Y-%m-%d'),'strategy':s,'horizon':h,
                          'setup_date':f[s+'_setup'].iloc[i].strftime('%Y-%m-%d') if pd.notna(f[s+'_setup'].iloc[i]) else None,
                          'period':'2020_2024' if d.year<=2024 else str(d.year),'year':d.year,
                          'vr20':f.vr20.iloc[i],'dd120':f.dd120.iloc[i],**cache[key]})
            if (j+1)%500==0:print(f'{exchange}: stocks={j+1}, events={len(events)}',flush=True)
        del bars
    cs=pd.Series(counts).sort_index()
    if len(cs)>1 and cs.iloc[-1]<.6*cs.iloc[-2]:raise ValueError('partial market tail')
    e=pd.DataFrame(events);e.to_parquet(a.output/'events.parquet',index=False)
    summarize(e,calendar).to_csv(a.output/'summary.csv',index=False)
    yearly=e[e.status=='closed'].groupby(['strategy','horizon','year']).net_return.agg(['size','mean','median']).reset_index()
    yearly.to_csv(a.output/'yearly.csv',index=False)
    sample.to_csv(a.output/'300832_features.csv',index=False)
    e[(e.stock_code=='300832.SZ') & (e.trade_date>='2026-06-01') & (e.trade_date<='2026-08-01')].to_csv(a.output/'300832_events.csv',index=False)
    failures=e[(e.strategy=='panic_confirm') & (e.horizon==20) & (e.status=='closed')].sort_values('net_return').head(20)
    failures.to_csv(a.output/'failures.csv',index=False)
    meta={'design':'DESIGN.md; thresholds fixed after viewing example; retrospective, not independent OOS','start':a.start,'end':a.end,
          'events':len(e),'stocks_with_events':e.stock_code.nunique(),'market_counts':counts,'source_hashes':hashes,
          'execution_dependency_sha256':hashlib.sha256((ROOT/'scripts/breakout_volatility_study.py').read_bytes()).hexdigest(),
          'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (a.output/'manifest.json').write_text(json.dumps(meta,indent=2))
    print(f'RESULT: events={len(e)}; stocks_with_events={e.stock_code.nunique()}; output={a.output}',flush=True)


if __name__=='__main__':main()
