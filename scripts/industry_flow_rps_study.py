#!/usr/bin/env python3
"""Compare industry-relative price momentum and industry money-flow ranks.

Exploratory, point-in-time caveats documented in the output: industry metadata
is the latest local registry snapshot, so this is not a leakage-safe OOS result.
"""
from __future__ import annotations
import json
from pathlib import Path
import glob
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/verification/industry_flow_rps_20260923'


def build_panel(start='2024-08-23', end='2026-09-22'):
    files = glob.glob(str(ROOT / 'assets/data/clean/ohlcv/market=CN/exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/part-*.parquet'))
    bars=[]
    for path in files:
        frame=pd.read_parquet(path, columns=['stock_code','trade_date','close'])
        frame['trade_date']=pd.to_datetime(frame.trade_date,errors='coerce')
        frame=frame[frame.trade_date.between(start,end)]
        if not frame.empty: bars.append(frame)
    x=pd.concat(bars,ignore_index=True).drop_duplicates(['stock_code','trade_date']).sort_values(['stock_code','trade_date'])
    g=x.groupby('stock_code',sort=False)
    x['ret20']=x.close/g.close.shift(20)-1
    x['forward_5d']=g.close.shift(-5)/x.close-1
    registry=glob.glob(str(ROOT/'assets/data/meta/stock_info_registry/market=CN/**/*.parquet'),recursive=True)
    info=pd.concat([pd.read_parquet(p,columns=['stock_code','industry_l2']) for p in registry],ignore_index=True).drop_duplicates('stock_code',keep='last')
    x=x.merge(info,on='stock_code',how='left')
    x['industry_l2']=x.industry_l2.fillna('UNK')
    x['industry_price_rps20']=x.groupby(['trade_date','industry_l2']).ret20.rank(pct=True)
    flow=pd.read_parquet(ROOT/'assets/data/derived/cn_moneyflow_features.parquet',columns=['stock_code','trade_date','source','moneyflow_net_amount','moneyflow_amount_scale'])
    flow=flow[flow.source.eq('moneyflow')].copy()
    flow['trade_date']=pd.to_datetime(flow.trade_date,errors='coerce')
    flow=flow.drop_duplicates(['stock_code','trade_date']).sort_values(['stock_code','trade_date'])
    amount=pd.to_numeric(flow.moneyflow_net_amount,errors='coerce')
    scale=pd.to_numeric(flow.moneyflow_amount_scale,errors='coerce').replace(0,np.nan)
    flow['flow_pct']=amount/scale
    flow['flow_5d']=flow.groupby('stock_code',sort=False).flow_pct.transform(lambda s:s.rolling(5,min_periods=3).mean())
    x=x.merge(flow[['stock_code','trade_date','flow_5d']],on=['stock_code','trade_date'],how='left')
    x['industry_flow_rank5']=x.groupby(['trade_date','industry_l2']).flow_5d.rank(pct=True)
    x['combined_rank']=(x.industry_price_rps20+x.industry_flow_rank5)/2
    x=x[x.trade_date.between('2025-01-01','2026-09-15')].dropna(subset=['forward_5d','industry_price_rps20','industry_flow_rank5'])
    return x

def deciles(frame):
    rows=[]
    for factor in ['industry_price_rps20','industry_flow_rank5','combined_rank']:
        bucket=pd.qcut(frame[factor],10,labels=False,duplicates='drop')
        for decile,group in frame.assign(decile=bucket).groupby('decile'):
            ret=group.forward_5d
            rows.append({'factor':factor,'decile_low_to_high':int(decile),'n':len(group),'win_rate':float(ret.gt(0).mean()),'mean_forward_5d':float(ret.mean()),'median_forward_5d':float(ret.median())})
    return pd.DataFrame(rows)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    panel=build_panel(); summary=deciles(panel)
    summary.to_csv(OUT/'decile_summary.csv',index=False)
    panel[panel.trade_date.eq('2026-09-15')][['trade_date','stock_code','industry_l2','industry_price_rps20','industry_flow_rank5','combined_rank','forward_5d']].to_csv(OUT/'latest_cross_section.csv',index=False)
    meta={'rows':len(panel),'stocks':int(panel.stock_code.nunique()),'start':str(panel.trade_date.min().date()),'end':str(panel.trade_date.max().date()),'forward_horizon_trading_days':5,'source_flow':'moneyflow (not pooled with DC/THS)','industry_source':'latest local stock_info_registry industry_l2 snapshot; not point-in-time','flow_formula':'rolling 5-session mean of net flow / amount scale, min_periods=3','rank_formula':'within trade_date x industry_l2, ascending percentile rank','price_formula':'20-session close return percentile within trade_date x industry_l2','limitations':['overlapping 5-session forward labels; observations are not independent','latest industry labels may differ from historical point-in-time labels','exploratory in-sample descriptive, no fees, no tradability constraints','does not establish persistent predictiveness or 30% monthly attainable return']}
    (OUT/'study_meta.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(summary.to_string(index=False));print(json.dumps(meta,ensure_ascii=False,indent=2));print(OUT)
if __name__=='__main__': main()
