#!/usr/bin/env python
"""Independent paired crowding shadows; original rank/names and production untouched."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pyarrow.dataset as ds
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from factor_engine.risk.crowding import soft_weights
from factor_engine.portfolio.paper_account import run_paper_account, persist_paper_account
from factor_engine.portfolio.volatility_sizing import predicted_covariance,cap_weights
from factor_engine.portfolio.execution_assumptions import load_assumptions
from scripts.run_volatility_shadow import account_metrics
from scripts.audit_crowding_inputs import calendar,sha256
OUT=ROOT/'output/research/crowding'

def variant_weights(weights,high,cov=None):
    v={'A':weights.copy(),'B':weights.copy(),'C':soft_weights(weights,high)}
    if cov is not None:
        v['E_volcap']=cap_weights(weights,cov,target_vol=.15)
        v['E_volcap_crowding']=soft_weights(v['E_volcap'],high)
    return v

def gate(candidate,baseline,decisions,unknown):
    p=pd.concat([candidate.rename('c'),baseline.rename('b')],axis=1).dropna();r=p.pct_change(fill_method=None).dropna();n=len(r)
    dd=p/p.cummax()-1
    tails={k:r.loc[r[k]<=r[k].quantile(.05),k] for k in ['c','b']}
    cv={k:float(s.mean()) if len(s)>=5 else None for k,s in tails.items()}
    ann={k:float((p[k].iloc[-1]/p[k].iloc[0])**(252/n)-1) if n>=252 else None for k in ['c','b']}
    quarters=[]
    for q,g in r.groupby(r.index.to_period('Q')):
        if len(g)>=40:quarters.append({'quarter':str(q),'returns':len(g),'risk_improved':bool(g.c.std()<=g.b.std())})
    checks={'returns252':n>=252,'decisions63':decisions>=63,'quarters3':len(quarters)>=3,
       'quarter_stability':len(quarters)>=3 and sum(q['risk_improved'] for q in quarters)>=np.ceil(len(quarters)*2/3),
       'drawdown_not_worse':len(p)>1 and dd.c.min()>=dd.b.min(),
       'cvar_not_worse':cv['c'] is not None and cv['b'] is not None and cv['c']>=cv['b'],
       'risk_improves10pct':bool(len(p)>1 and ((dd.c.min()>=dd.b.min()*.9 and dd.b.min()<0) or (cv['c'] is not None and cv['b'] is not None and cv['b']<0 and cv['c']>=cv['b']*.9))),
       'ann_shortfall_le2pct':ann['c'] is not None and ann['b']-ann['c']<=.02,
       'unknown_selected_exposure_zero':not unknown,'independent_pit_review':False,'prospective_not_reconstruction':False}
    return {'status':'not_eligible','checks':{k:bool(v) for k,v in checks.items()},'daily_returns':n,'paired_decisions':decisions,'quarters':quarters,'automatic_promotion':False}

def run(out=OUT,capital=45000):
    spec=json.loads((out/'methodology.json').read_text());spec_hash=sha256(out/'methodology.json')
    ledger_path=ROOT/'output/results_cn/selection_ledger.parquet';ledger=pd.read_parquet(ledger_path);ledger.trade_date=pd.to_datetime(ledger.trade_date)
    ledger=ledger[ledger.source.isin(spec['shadow_sources'])].copy();codes=sorted(ledger.stock_code.unique())
    factors=ds.dataset(str(out/'factors.parquet'),format='parquet').to_table(columns=['trade_date','stock_code','turnover_pct252','data_status'],filter=ds.field('stock_code').isin(codes)).to_pandas()
    factors.trade_date=pd.to_datetime(factors.trade_date);lookup=factors.set_index(['trade_date','stock_code'])
    files=[p for y in [2024,2025,2026] for p in (ROOT/'assets/data/clean/ohlcv/market=CN').glob(f'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet')]
    bars=ds.dataset([str(p) for p in files],format='parquet').to_table(columns=['trade_date','stock_code','open','close','volume','amount'],filter=ds.field('stock_code').isin(codes)).to_pandas();bars.trade_date=pd.to_datetime(bars.trade_date)
    if bars.duplicated(['stock_code','trade_date']).any():raise ValueError('duplicate bars')
    cal=calendar();prices=bars.pivot(index='trade_date',columns='stock_code',values='close').reindex(cal);returns=prices.pct_change(fill_method=None)
    dest=out/'shadow';dest.mkdir(parents=True,exist_ok=True);histories={};audit=[];latest=[];metrics=[];gates={};pending=[]
    for source,lg in ledger.groupby('source'):
        for date,g in lg.groupby('trade_date'):
            g=g.sort_values(['rank','model_score','stock_code'],ascending=[True,False,True])
            g=g.head(10) if source=='preselected' else g[pd.to_numeric(g.weight,errors='coerce').fillna(0)>0]
            if g.empty:continue
            if g.stock_code.duplicated().any():raise ValueError('duplicate selected stocks')
            w=pd.Series(1/len(g),index=g.stock_code)
            high={};missing=[]
            for code in w.index:
                key=(date,code)
                if key not in lookup.index or not np.isfinite(lookup.loc[key].turnover_pct252):missing.append(code);high[code]=None
                else:high[code]=bool(lookup.loc[key].turnover_pct252>=.95)
            cov=None;reason=''
            try:cov,_=predicted_covariance(returns.reindex(columns=w.index).loc[:date])
            except ValueError as e:reason=str(e)
            variants=variant_weights(w,high,cov)
            if cov is None:
                variants.update(E_volcap=w*0,E_volcap_crowding=w*0)
            # D is deliberately NOT claimed as exact no-new-buy until execution guard is implemented.
            for variant,weight in variants.items():
                h=g[['trade_date','stock_code','rank','name','model_score']].copy();h['target_weight']=h.stock_code.map(weight)
                histories.setdefault((source,variant),[]).append(h)
                audit.append({'source':source,'variant':variant,'trade_date':date,'selected':len(w),'high_count':sum(v is True for v in high.values()),
                    'unknown_codes':','.join(missing),'unknown_baseline_weight':sum(w[c] for c in missing),'exposure':weight.sum(),'risk_error':reason if variant.startswith('E_') else ''})
        pending.extend([{'source':source,'variant':'D','status':'pending_exact_no_new_buy_guard','reason':'Target-weight proxy cannot guarantee held shares unchanged across next-open gaps; not substituted as completed D.'},
                        {'source':source,'variant':'F_dev_exposure','status':'pending_development_ledger','reason':'Current source ledger starts after 2023; no development-only exposure calibration. No OOS fitting.'}])
    records={};navs={}
    for (source,variant),parts in histories.items():
        h=pd.concat(parts,ignore_index=True);start=h.trade_date.min();end=bars.trade_date.max()
        account_bars=bars[(bars.trade_date>=start-pd.Timedelta(days=10))&(bars.trade_date<=end)]
        result=run_paper_account(h,account_bars,initial_capital=capital,causal_execution=True,strategy_version='crowding.v1:'+source+':'+variant)
        result['nav']=result['nav'][result['nav'].asof_date>=start].copy()
        folder=dest/source/variant;persist_paper_account(result,str(folder),assumptions=load_assumptions())
        h.to_csv(folder/'decisions.csv',index=False);records[(source,variant)]=result
        navs[(source,variant)]=result['nav'].set_index('asof_date').nav
        nav=result['nav'];gross=nav.market_value/nav.nav
        n=max(len(nav)-1,0)
        ann=float((nav.nav.iloc[-1]/nav.nav.iloc[0])**(252/n)-1) if n>=252 else None
        metrics.append({'source':source,'variant':variant,'decisions':h.trade_date.nunique(),'mean_exposure':gross.mean(),
                        'annualized_net_return':ann,**account_metrics(result,capital)})
        last=h[h.trade_date==h.trade_date.max()].copy();last['source']=source;last['variant']=variant;latest.append(last)
        if variant=='B':
            a=records[(source,'A')]
            for kind in ['orders','fills','positions','nav']:
                aa=a[kind];bb=result[kind];cols=[c for c in aa if c not in ['run_id','order_id','account_id','strategy_version']]
                pd.testing.assert_frame_equal(aa[cols].reset_index(drop=True),bb[cols].reset_index(drop=True))
    ad=pd.DataFrame(audit);ad.to_csv(dest/'decision_audit.csv',index=False)
    pd.concat(latest,ignore_index=True).to_csv(dest/'latest_decisions.csv',index=False)
    for source in ledger.source.unique():
        if (source,'A') not in navs:continue
        unknown=bool((ad[(ad.source==source)&(ad.variant=='C')].unknown_baseline_weight>0).any())
        for c,b in [('C','A'),('E_volcap_crowding','E_volcap')]:
            h=pd.concat(histories[(source,c)]);gates[source+':'+c]=gate(navs[(source,c)],navs[(source,b)],h.trade_date.nunique(),unknown)
    pd.DataFrame(metrics).to_csv(dest/'comparison.csv',index=False);pd.DataFrame(pending).to_csv(dest/'pending_variants.csv',index=False)
    (dest/'gate.json').write_text(json.dumps(gates,indent=2,ensure_ascii=False))
    attrs={'methodology_sha256':spec_hash,'source_ledger_sha256':sha256(ledger_path),'factor_file_sha256':sha256(out/'factors.parquet'),
       'execution_assumptions_sha256':sha256(ROOT/'config/execution_assumptions_cn.json'),'capital':capital,'causal_execution':True,
       'status':'retrospective_only_not_production','AB_order_nav_exact_equality':True,'unimplemented_exact_variants':pending,
       'limitations':['Stock lake and reconstructed ledger are not independently reviewed PIT inputs.',
       'Shared account engine keeps legacy limit/lot/suspension/delist approximations; no orderbook capacity guarantee.',
       'Equal ARM baseline is a research control on same positive-weight names, not original ARM weight replication.',
       'Missing crowding retains baseline names and is audited; it does not count as a valid risk signal.',
       'Family horizons differ; no cross-family return ranking. All displayed variants within a family have identical decision calendars.']}
    (dest/'manifest.json').write_text(json.dumps(attrs,indent=2,ensure_ascii=False))
    (dest/'REPORT.md').write_text('# 拥挤度配对影子\n\nA/B原始名单等权且订单、成交、持仓、净值逐字段完全一致；C只对高换手分位做0.5软降权，留现金；E复用volcap做增量对照。\n\n'+
        '```\n'+pd.DataFrame(metrics).to_string(index=False)+'\n```\n\n## 未冒充完成的对照\n\n'+pd.DataFrame(pending).to_string(index=False)+'\n\n'+
        '\n'.join('- '+s for s in attrs['limitations'])+'\n\n所有变体仅历史重建，前向和PIT闸门未通过，不进入生产。\n')
    assert spec_hash==sha256(out/'methodology.json')
    print(pd.DataFrame(metrics).to_string(index=False));print('SHADOW: AB_exact=True; variants_per_source=5; D=pending_execution_guard; F=pending_development_data; promotion=False')
    return attrs

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=OUT);p.add_argument('--capital',type=float,default=45000);a=p.parse_args()
    if a.capital<=0:raise ValueError('capital must be positive')
    run(a.output,a.capital)
