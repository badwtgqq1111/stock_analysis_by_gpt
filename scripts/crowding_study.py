#!/usr/bin/env python
"""Frozen, reproducible crowding proxy study. Does not write strategy registry."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.dataset as ds
from scipy.stats import norm

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from factor_engine.risk.crowding import features, breadth, VERSION
from scripts.audit_crowding_inputs import sha256, calendar
from scripts.breakout_volatility_study import execute_event
from scripts.capitulation_reclaim_study import risk_on
from factor_engine.portfolio.execution_assumptions import load_assumptions

OUT=ROOT/'output/research/crowding'

def ensure_methodology(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    p=out/'methodology.json'
    if not p.exists():
        with p.open('xb') as f:f.write((ROOT/'config/research/crowding_v1.json').read_bytes())
    return p

def block_stats(series, cal, block, seed=70):
    """Calendar-spaced circular resampling; no-signal days are missing, not zero return."""
    s=series.dropna().sort_index()
    if len(s)<20:return {'paired_dates':len(s),'mean_delta':s.mean(),'ci_low':np.nan,'ci_high':np.nan,'p_value':np.nan}
    x=s.reindex(cal[(cal>=s.index.min())&(cal<=s.index.max())]).to_numpy();n=len(x)
    rng=np.random.default_rng(seed);draws=[]
    for _ in range(500):
        starts=rng.integers(n,size=int(np.ceil(n/block)))
        sample=x[((starts[:,None]+np.arange(block))%n).ravel()[:n]]
        if np.isfinite(sample).any():draws.append(np.nanmean(sample))
    sd=np.std(draws,ddof=1)
    return {'paired_dates':len(s),'mean_delta':s.mean(),'ci_low':np.quantile(draws,.025),'ci_high':np.quantile(draws,.975),
            'p_value':2*norm.sf(abs(s.mean()/sd)) if sd>0 else np.nan}

def holm(values):
    p=np.asarray(values,dtype=float);out=np.full(len(p),np.nan);order=np.flatnonzero(np.isfinite(p));order=order[np.argsort(p[order])]
    if len(order):out[order]=np.minimum(1,np.maximum.accumulate(p[order]*(len(order)-np.arange(len(order)))))
    return out

def period(day):
    return 'development' if day.year<=2023 else ('rolling_validation' if day.year<=2025 else 'seen_2026')

def completed_month_ends(cal):
    """Conservatively omit endpoint month even if its tail may be month-end."""
    cal=pd.DatetimeIndex(cal)
    if len(cal)==0:return set()
    market=pd.DataFrame({'d':cal})
    prior=market[market.d.dt.to_period('M')<cal[-1].to_period('M')]
    return set(prior.groupby(prior.d.dt.to_period('M')).d.max())

def group_flags(g):
    # Neither direction of correlation/skew is pre-selected as a winning signal.
    p=g.turnover_pct252
    return {'turnover95':(p>=.95,p<.5),'turnover99':(p>=.99,p<.5),
            'amount_bias':(g.amount_bias20>=1,g.amount_bias20<=0),
            'volume_bias':(g.volume_bias20>=1,g.volume_bias20<=0),
            'corr_positive':(g.price_amount_corr40>=.5,g.price_amount_corr40<=-.5),
            'return_skew_positive':(g.return_skew60>=1,g.return_skew60<=-1)}

def summarize(events,cal):
    rows=[];pairs=[]
    for (h,split),g in events.groupby(['horizon','period']):
        # Controls are computed before looking at future labels, on decision-date features only.
        for col in ['momentum20','vol20','amount20']:
            g=g.copy();g[col+'_bin']=g.groupby('trade_date')[col].transform(lambda s:np.minimum((s.rank(pct=True,method='average')*3).astype(int),2))
        flags=group_flags(g)
        for factor,(hi,lo) in flags.items():
            for name,mask in [('high',hi),('low',lo)]:
                sub=g[mask]; good=sub[sub.status=='closed'];r=good.net_return
                tail=r[r<=r.quantile(.05)] if len(r) else r
                rows.append({'factor':factor,'group':name,'horizon':h,'period':split,'signals':len(sub),'stocks':sub.stock_code.nunique(),
                    'decision_dates':sub.trade_date.nunique(),'closed':len(good),'status_counts':json.dumps(sub.status.value_counts().to_dict(),sort_keys=True),
                    'event_mean':r.mean(),'event_median':r.median(),'loss_probability':(r<0).mean() if len(r) else np.nan,
                    'cvar95':tail.mean() if len(tail)>=5 else np.nan,'tail_n':len(tail),'mae_mean':good.mae.mean(),'mfe_mean':good.mfe.mean()})
            pair=g.assign(group=np.where(hi,'high',np.where(lo,'low','other')))
            pair=pair[(pair.group!='other')&(pair.status=='closed')]
            daily=pair.groupby(['trade_date','group']).net_return.mean().unstack('group').reindex(columns=['high','low'])
            delta=daily.high-daily.low
            st=block_stats(delta,cal,int(h)+20)
            pairs.append({'factor':factor,'horizon':h,'period':split,'comparison':'same_date',**st})
            controls=['trade_date','momentum20_bin','vol20_bin','amount20_bin']
            cell=pair.groupby(controls+['group']).net_return.mean().unstack('group').reindex(columns=['high','low'])
            matched=(cell.high-cell.low).dropna().groupby('trade_date').mean()
            pairs.append({'factor':factor,'horizon':h,'period':split,'comparison':'date_momentum_vol_liquidity_matched',**block_stats(matched,cal,int(h)+20)})
            # State interactions: upside continuation and weakening are distinct, not hand-picked after returns.
            for label,mask in [('uptrend',g.momentum20>0),('weakening',g.momentum20<=0)]:
                q=pair[pair.index.isin(g.index[mask])]
                dd=q.groupby(['trade_date','group']).net_return.mean().unstack('group').reindex(columns=['high','low'])
                pairs.append({'factor':factor,'horizon':h,'period':split,'comparison':label,**block_stats(dd.high-dd.low,cal,int(h)+20)})
    pairs=pd.DataFrame(pairs);pairs['p_holm']=holm(pairs.p_value)
    return pd.DataFrame(rows),pairs

def build(out=OUT,end=None):
    ensure_methodology(out)
    spec=json.loads((out/'methodology.json').read_text());audit=json.loads((out/'audit.json').read_text())
    spec_hash=sha256(out/'methodology.json');cal=calendar();cal=cal[cal<=pd.Timestamp(end or audit['bar_end'])]
    if len(cal)==0:raise ValueError('empty calendar')
    universe=pd.read_csv(out/'stock_coverage.csv'); universe=universe[universe.stock_code.str.match(r'^(000|001|002|003|300|301|600|601|603|605|688)\d{3}\.(SH|SZ)$')]
    sampled=set(universe.loc[universe.stock_code.map(lambda c:int(hashlib.sha256(c.encode()).hexdigest(),16)%8==0),'stock_code'])
    lp=ROOT/'output/results_cn/selection_ledger.parquet';ledger=pd.read_parquet(lp)
    selected=set(ledger.loc[ledger.source.isin(spec['shadow_sources']),'stock_code']);codes=sorted(sampled|selected)
    (out/'sample_stocks.csv').write_text('stock_code\n'+'\n'.join(sorted(sampled))+'\n')
    decisions=completed_month_ends(cal)
    names=pd.read_parquet(ROOT/'assets/data/meta/cn_stock_namechange.parquet')
    for c in ['start_date','end_date','ann_date']:names[c]=pd.to_datetime(names[c])
    ng={code:g for code,g in names.groupby('ts_code')}
    events=[];breadth_rows=[];writer=None;factor_rows=0;invalids=[]
    paths=[]
    for exchange in ['SSE','SZSE']:
        fs=sorted((ROOT/'assets/data/clean/ohlcv/market=CN').glob(f'exchange={exchange}/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet'))
        fs=[p for p in fs if 2017<=int(p.parent.name.split('=')[1])<=cal[-1].year];paths+=fs
        raw=ds.dataset([str(p) for p in fs],format='parquet').to_table(columns=['trade_date','stock_code','open','high','low','close','volume','amount','turnover','source'],filter=ds.field('stock_code').isin(codes)).to_pandas()
        raw.trade_date=pd.to_datetime(raw.trade_date);raw=raw[raw.trade_date<=cal[-1]]
        print(f'{exchange}: selected rows={len(raw)}, stocks={raw.stock_code.nunique()}',flush=True)
        for j,(code,g) in enumerate(raw.groupby('stock_code',sort=True)):
            f=features(g,cal,audit['validated_turnover_sources']);factor_rows+=len(f)
            table=pa.Table.from_pandas(f,preserve_index=False)
            if writer is None:writer=pq.ParquetWriter(out/'factors.parquet',table.schema,compression='zstd')
            writer.write_table(table)
            if code in sampled:
                breadth_rows.append(f[['trade_date','stock_code','breadth_eligible','new_high252','new_low252']])
                event_f=f[(f.trade_date.isin(decisions))&(f.trade_date.dt.year>=2018)&(f.amount20>=spec['event_min_amount20'])&f.turnover_pct252.notna()&f.valid_bar&f.vol20.notna()&f.momentum20.notna()]
                daily=f.set_index('trade_date');nh=ng.get(code,names.iloc[:0])
                cfg={**load_assumptions(),'lot_size':200 if code.startswith('688') else 100}
                for row in event_f.itertuples():
                    d=row.trade_date;risk=risk_on(nh,d)
                    for h in spec['horizons']:
                        if risk is None or risk: r={'status':'skip_unknown_risk' if risk is None else 'skip_risk'}
                        else:r=execute_event(code,d,h,daily,cal,cfg,capital=spec['event_capital'])
                        mae=mfe=np.nan
                        if r['status']=='closed':
                            span=daily.loc[r['entry_date']:r['exit_date']];entry=float(daily.loc[r['entry_date']].open)
                            mae=span.low.min()/entry-1;mfe=span.high.max()/entry-1
                        events.append({**{k:getattr(row,k) for k in ['stock_code','trade_date','turnover_pct252','amount_bias20','volume_bias20','price_amount_corr40','return_skew60','momentum20','vol20','amount20']},'period':period(d),'horizon':h,'mae':mae,'mfe':mfe,**r})
            invalids.append({'stock_code':code,'rows':len(f),'ready':int((f.data_status=='ready').sum()),'missing_or_invalid':int((~f.valid_bar).sum()),'unvalidated_turnover':int((f.data_status=='turnover_unvalidated').sum())})
            if j%100==0:print(f'{exchange}: processed={j}; events={len(events)}',flush=True)
    if writer:writer.close()
    events=pd.DataFrame(events)
    for c in ['net_return','mae','mfe']:
        if c not in events:events[c]=np.nan
    events.to_parquet(out/'events.parquet',index=False)
    pd.DataFrame(invalids).to_csv(out/'factor_coverage.csv',index=False)
    b=breadth(pd.concat(breadth_rows));b['scope']='fixed_hash_sample_NOT_market';b.to_csv(out/'sample_breadth.csv',index=False)
    summary,pairs=summarize(events,cal);summary.to_csv(out/'summary.csv',index=False);pairs.to_csv(out/'paired_comparison.csv',index=False)
    events.groupby(['period','horizon','status']).size().rename('count').to_csv(out/'execution_audit.csv')
    # Explicit censor sensitivity rather than hiding the unmatched/failed events.
    sensitivity=[]
    for (split,h),g in events.groupby(['period','horizon']):
        for label,fill in [('unresolved_minus100pct',-1.),('unresolved_zero',0.)]:
            x=g.net_return.copy();unresolved=g.status.eq('censored_exit');x.loc[unresolved]=fill
            sensitivity.append({'period':split,'horizon':h,'assumption':label,'censored':int(unresolved.sum()),'mean_with_censor_assumption':x.mean()})
    pd.DataFrame(sensitivity).to_csv(out/'censor_sensitivity.csv',index=False)
    manifest=json.loads((out/'input_manifest.json').read_text());manifest[str(lp.relative_to(ROOT))]=sha256(lp)
    for p in paths:manifest[str(p.relative_to(ROOT))]=sha256(p)
    for name in ['config/research/crowding_v1.json','scripts/crowding_study.py','scripts/audit_crowding_inputs.py',
                 'factor_engine/risk/crowding.py','scripts/breakout_volatility_study.py',
                 'scripts/capitulation_reclaim_study.py','config/execution_assumptions_cn.json',
                 'factor_engine/portfolio/execution_assumptions.py']:
        manifest[name]=sha256(ROOT/name)
    (out/'input_manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False))
    run={'factor_version':VERSION,'methodology_sha256':spec_hash,'sample_stocks':len(sampled),'factor_stocks':len(invalids),'factor_rows':factor_rows,
         'events':len(events),'closed':int(events.status.eq('closed').sum()),'real_forward_decisions':0,'industry_control_available':False,'size_control_available':False,
         'production_changed':False,'promotion':'not_eligible','units_excluded_sources':['baostock','akshare_eastmoney'],
         'caveats':spec['limitations']+['Matching controls only momentum/volatility/amount, NOT industry or size; incremental alpha is unproven.',
             'Universe is observed lake codes, not independently reconstructed historical listings/delistings.',
             'ST screened at decision with known announced history; trading-period ST/IPO changes, capacity and corporate actions remain approximate.',
             'Events overlap, do not equal deployable portfolio NAV. sample_breadth is not a full-market Hindenburg signal.',
             'Validation of source units uses retrospective reference pairs, not a point-in-time data lineage proof.']}
    (out/'run.json').write_text(json.dumps(run,indent=2,ensure_ascii=False))
    main=pairs[(pairs.factor=='turnover95')&(pairs.comparison=='date_momentum_vol_liquidity_matched')]
    lines=['# 交易拥挤度第一轮研究','',f'固定hash抽样{len(sampled)}只（不按现行上市状态过滤）；因子涵盖抽样与影子名单并集{len(invalids)}只、{factor_rows:,}行。',
        f'月末信号×5/20日共{len(events):,}条事件；完成{run["closed"]:,}条。不是独立事件数，也不是组合业绩。',
        '主换手仅使用审计通过来源；Baostock/少量Eastmoney单位不同，未改原始数据，独立研究隔离。',
        'monthly basic仅105个交易日，不构造伪日频股本；行业与真实日市值控制缺失，以下匹配仍不是完整增量证明。','',
        '## 主假设：高换手历史分位95% vs 低于50%','高组减低组的同日/动量/波动率/成交额分层净收益差；区间为日历分块bootstrap，至少20个配对日期。',
        '```',main.to_string(index=False),'```','',
        '## 结论与下一步','当前只是代理指标的回顾证据；不把高拥挤直接定义为卖点，不晋级生产。',
        '完整单因子、双尾、趋势状态及控制消融见summary.csv与paired_comparison.csv；所有已报告比较统一Holm校正。',
        'sample_breadth.csv仅为固定抽样宽度；全市场/行业PIT宽度与时点市值/行业控制留待数据补齐。','',
        '## 局限',*['- '+x for x in run['caveats']]]
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    assert sha256(out/'methodology.json')==spec_hash,'methodology changed during run'
    print(main.to_string(index=False));print(f'STUDY: stocks={len(invalids)}; factors={factor_rows}; events={len(events)}; closed={run["closed"]}; promotion=not_eligible')
    return run

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=OUT);p.add_argument('--end');a=p.parse_args();build(a.output,a.end)
