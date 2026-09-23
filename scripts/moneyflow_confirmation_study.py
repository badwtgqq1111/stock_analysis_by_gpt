#!/usr/bin/env python3
"""Test whether positive money-flow features raise forward return win rates.

Signals are observable at the close of T; return windows use next-session open
entry and T+1/T+3/T+5 close exits. Sources stay separate.
"""
from __future__ import annotations
import glob, json
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/verification/moneyflow_confirmation_20260923'

def main():
    bars=[]
    pattern=ROOT/'assets/data/clean/ohlcv/market=CN/exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/part-*.parquet'
    for p in glob.glob(str(pattern)):
        d=pd.read_parquet(p,columns=['stock_code','trade_date','open','close'])
        d['trade_date']=pd.to_datetime(d.trade_date,errors='coerce')
        d=d[d.trade_date.between('2024-12-19','2026-09-17')]
        if not d.empty: bars.append(d)
    x=pd.concat(bars,ignore_index=True).drop_duplicates(['stock_code','trade_date']).sort_values(['stock_code','trade_date'])
    g=x.groupby('stock_code',sort=False)
    x['next_open']=g.open.shift(-1);x['next_close']=g.close.shift(-1)
    x['ret1_open_close']=x.next_close/x.next_open-1
    x['ret3_from_open']=g.close.shift(-3)/x.next_open-1
    x['ret5_from_open']=g.close.shift(-5)/x.next_open-1
    flows=pd.read_parquet(ROOT/'assets/data/derived/cn_moneyflow_features.parquet')
    flows['trade_date']=pd.to_datetime(flows.trade_date,errors='coerce')
    flows=flows[flows.trade_date.between('2024-12-19','2026-09-17')].drop_duplicates(['stock_code','trade_date','source'])
    x=x.merge(flows,on=['stock_code','trade_date'],how='left')
    rules=[]
    for source in ('moneyflow','moneyflow_dc','moneyflow_ths'):
        net=f'{source}_net_5d';z=f'{source}_net_z_5d';pos=f'{source}_positive_ratio_5d'
        valid=x[z].notna()&x['ret5_from_open'].notna()&x[net].notna()&x[pos].notna()
        candidates={
            'net5_positive':x[net].gt(0),
            'positive_ratio_ge_0_6':x[pos].ge(.6),
            'z5_ge_1':x[z].ge(1),
            'net_positive_and_ratio60':x[net].gt(0)&x[pos].ge(.6),
            'z1_net_positive_ratio60':x[z].ge(1)&x[net].gt(0)&x[pos].ge(.6),
        }
        for rule,condition in candidates.items():
            confirm=valid&condition
            for label,mask in (('confirmed',confirm),('not_confirmed',valid&~condition)):
                q=x.loc[mask]
                if q.empty: continue
                rules.append({'source':source,'rule':rule,'group':label,'n':len(q),'date_count':q.trade_date.nunique(),
                    'win_next_open_to_close':float(q.ret1_open_close.gt(0).mean()),
                    'win_3d_from_next_open':float(q.ret3_from_open.gt(0).mean()),
                    'win_5d_from_next_open':float(q.ret5_from_open.gt(0).mean()),
                    'mean_5d_from_next_open':float(q.ret5_from_open.mean()),
                    'median_5d_from_next_open':float(q.ret5_from_open.median())})
    summary=pd.DataFrame(rules)
    monthly=[]
    for source in ('moneyflow','moneyflow_dc','moneyflow_ths'):
        net=f'{source}_net_5d';z=f'{source}_net_z_5d';pos=f'{source}_positive_ratio_5d'
        valid=x[z].notna()&x['ret5_from_open'].notna()&x[net].notna()&x[pos].notna()
        signal=valid&x[z].ge(1)&x[net].gt(0)&x[pos].ge(.6)
        q=x.loc[valid,['trade_date','ret5_from_open']].copy();q['confirmed']=signal.loc[valid].to_numpy();q['month']=q.trade_date.dt.to_period('M').astype(str)
        m=q.groupby(['month','confirmed']).ret5_from_open.mean().unstack().dropna()
        monthly.append({'source':source,'months_compared':len(m),'confirmed_month_mean':float(m[True].mean()),'not_confirmed_month_mean':float(m[False].mean()),'confirmed_wins_month_share':float((m[True]>m[False]).mean())})
    OUT.mkdir(parents=True,exist_ok=True)
    summary.to_csv(OUT/'confirmation_summary.csv',index=False)
    pd.DataFrame(monthly).to_csv(OUT/'monthly_comparison.csv',index=False)
    meta={'start':'2024-12-19','signal_end':'2026-09-17','bar_observations':len(x),'moneyflow_features_path':'assets/data/derived/cn_moneyflow_features.parquet','execution':'signal at T close; entry T+1 open; exits T+1/T+3/T+5 closes','independence_note':'five-day windows overlap; cross-sectional stock-day observations are dependent','survivorship_note':'uses local available OHLCV universe; no explicit delisting/survivorship correction','costs':'no commission, stamp duty or slippage'}
    (OUT/'study_meta.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(summary[(summary.rule=='z1_net_positive_ratio60')].to_string(index=False));print(pd.DataFrame(monthly).to_string(index=False));print(OUT)
if __name__=='__main__': main()
