#!/usr/bin/env python
"""JoinQuant size/ROE research audit. Never changes production configuration.

Signals use data at t close; trades use t+1 open. LLM sees train IC only.
Historical monthly index weights are backward-matched, not current constituents.
The engine is a fractional-share, adjusted-price research replay, not broker QA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.llm_factor_mining import safe_eval, call_llm, bh_fdr

OUT = ROOT / 'output/research/jq_multi_factor'
FIELDS = ['market_cap', 'roe', 'roa', 'netprofit_yoy', 'debt_to_assets', 'ret_20', 'vol_20']


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def nw_t(x, lag=1):
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    if len(x) < 6:
        return np.nan
    dev = x - x.mean()
    var = dev @ dev / len(x)
    for k in range(1, min(lag, len(x)-1)+1):
        var += 2*(1-k/(lag+1))*(dev[k:] @ dev[:-k])/len(x)
    return float(x.mean()/np.sqrt(max(var, 1e-12)/len(x)))


def finance_asof(grid, fin):
    """Use latest reported period known strictly before/at signal close.

    Announcement time within day unknown; exclude same-day announcements.
    Older-period delayed reports do not displace newer-period known reports.
    """
    fin = fin.copy()
    fin['ann_date'] = pd.to_datetime(fin['ann_date'])
    fin['end_date'] = pd.to_datetime(fin['end_date'])
    fin = fin.rename(columns={'ts_code': 'stock_code'})
    fin = fin.dropna(subset=['ann_date', 'end_date']).sort_values(['ann_date', 'end_date'])
    latest = fin.groupby('stock_code')['end_date'].cummax()
    fin = fin[fin.end_date == latest].drop_duplicates(['stock_code', 'ann_date'], keep='last')
    result = pd.merge_asof(grid.sort_values('trade_date'), fin.sort_values('ann_date'),
                          left_on='trade_date', right_on='ann_date', by='stock_code',
                          direction='backward', allow_exact_matches=False)
    assert (result.ann_date.dropna() < result.loc[result.ann_date.notna(), 'trade_date']).all()
    return result


def load_inputs(start, end):
    import pyarrow.dataset as ds
    from data.ingest.providers.cn_tushare_official import TushareOfficialClient
    from data.store.scheduler_env import load_scheduler_env
    load_scheduler_env()
    client = TushareOfficialClient(timeout=40)
    cache = OUT/'inputs'
    cache.mkdir(exist_ok=True)
    chunks = []
    for y in range(pd.Timestamp(start).year-1, pd.Timestamp(end).year+1):
        path = cache/f'hs300_weights_{y}.parquet'
        if not path.exists():
            x = client.get('index_weight', index_code='000300.SH', start_date=f'{y}0101',
                           end_date=min(f'{y}1231', end.replace('-', '')), limit=5000)
            if x.empty:
                raise RuntimeError(f'Empty historical constituents {y}')
            if len(x) >= 5000:
                raise RuntimeError(f'Constituent pagination required {y}')
            x.to_parquet(path, index=False)
        x = pd.read_parquet(path)
        chunks.append(x)
        print(f'[input] hs300 {y}: {len(x)} rows', flush=True)
    weights = pd.concat(chunks).drop_duplicates(['trade_date', 'con_code'])
    weights['trade_date'] = pd.to_datetime(weights.trade_date)
    universe = set(weights.con_code)
    files = sorted(str(p) for p in (ROOT/'assets/data/clean/ohlcv/market=CN').glob(
        'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet'))
    bars = ds.dataset(files, format='parquet').to_table(
        columns=['trade_date','stock_code','open','high','low','close','volume','amount'],
        filter=ds.field('stock_code').isin(sorted(universe))).to_pandas()
    bars.trade_date = pd.to_datetime(bars.trade_date)
    bars = bars.drop_duplicates(['trade_date','stock_code'], keep='last')
    bench = pd.read_parquet(ROOT/'assets/data/meta/cn_benchmark_daily.parquet')
    bench = bench[bench.ts_code.isin(['000905.SH','000300.SH'])].pivot(
        index='trade_date', columns='ts_code', values='close').sort_index()
    bench = bench.loc[start:end]
    dates = bench.index
    full_close = bars.pivot(index='trade_date', columns='stock_code', values='close').sort_index()
    close = full_close.reindex(dates)
    cols = close.columns
    matrices = {k: bars.pivot(index='trade_date', columns='stock_code', values=k).reindex(
        index=dates, columns=cols).to_numpy(float) for k in ['open','high','low','volume','amount']}
    matrices['close'] = close.to_numpy(float)
    raw_signals = dates[:-1:15]
    # Exact signal-date capitalization, not sparse warehouse snapshots or future month-end data.
    from concurrent.futures import ThreadPoolExecutor
    def fetch_cap(day):
        path = cache/f'daily_basic_{day:%Y%m%d}.parquet'
        if not path.exists():
            z = client.get('daily_basic',trade_date=day.strftime('%Y%m%d'),
                fields='ts_code,trade_date,total_mv',limit=6000)
            if z.empty or len(z)>=6000:
                raise RuntimeError(f'Empty/truncated daily_basic {day}')
            z.to_parquet(path,index=False)
        z = pd.read_parquet(path).rename(columns={'ts_code':'stock_code','total_mv':'market_cap'})
        z['market_cap'] = pd.to_numeric(z.market_cap,errors='coerce')/1e4
        return z
    with ThreadPoolExecutor(max_workers=4) as pool:
        cap_list = list(pool.map(fetch_cap,raw_signals))
    caps = pd.concat(cap_list,ignore_index=True)
    caps.trade_date = pd.to_datetime(caps.trade_date)
    caps = caps.drop_duplicates(['trade_date','stock_code'],keep='last')
    print(f'[input] exact daily capitalization: {len(caps):,} rows / {len(raw_signals)} dates',flush=True)
    fin = pd.read_parquet(ROOT/'assets/data/meta/cn_financial_indicators.parquet',
        columns=['ts_code','ann_date','end_date','roe','roa','netprofit_yoy','debt_to_assets'])
    grid = []
    diag = []
    vol = bars.pivot(index='trade_date',columns='stock_code',values='volume').reindex(index=full_close.index,columns=cols).gt(0)
    feasible = vol.rolling(63,min_periods=63).sum().eq(63)
    ret = full_close.pct_change(fill_method=None)
    r20 = full_close/full_close.shift(20)-1
    v20 = ret.rolling(20,min_periods=20).std()
    snapshots = sorted(weights.trade_date.unique())
    for day in raw_signals:
        prior = [d for d in snapshots if d <= day]
        if not prior:
            continue
        asof = prior[-1]
        members = set(weights.loc[weights.trade_date==asof,'con_code'])
        # A monthly roster older than 65 calendar days is not silently reused.
        if (day-pd.Timestamp(asof)).days>65:
            raise RuntimeError(f'Stale roster {day}: {asof}')
        usable = sorted(members & set(cols[feasible.loc[day].to_numpy()]))
        z = pd.DataFrame({'trade_date':day,'stock_code':usable})
        z['ret_20'] = r20.loc[day].reindex(usable).to_numpy()
        z['vol_20'] = v20.loc[day].reindex(usable).to_numpy()
        grid.append(z)
        diag.append({'date':day,'roster_date':asof,'members':len(members),
                     'members_with_bar':len(members & set(cols[close.loc[day].notna()])),
                     'feasible_63':len(usable)})
    frame = finance_asof(pd.concat(grid,ignore_index=True),fin)
    frame = frame.merge(caps,on=['trade_date','stock_code'],how='left')
    # Fundamental coverage audit before dropping missing inputs.
    coverage = frame.groupby('trade_date').agg(n=('stock_code','size'),cap=('market_cap','count'),roe=('roe','count'))
    coverage.to_csv(OUT/'financial_coverage.csv')
    frame = frame.replace([np.inf,-np.inf],np.nan).dropna(subset=['market_cap','roe'])
    frame = frame[frame.market_cap>0].sort_values(['trade_date','stock_code']).reset_index(drop=True)
    # t-close signal, t+1 open entry, t+16 open exit for a 15-session holding label.
    op = pd.DataFrame(matrices['open'],index=dates,columns=cols)
    fwd = (op.shift(-16)/op.shift(-1)-1).stack().rename('fwd15').reset_index()
    frame = frame.merge(fwd,on=['trade_date','stock_code'],how='left')
    pd.DataFrame(diag).to_csv(OUT/'universe_coverage.csv',index=False)
    frame.to_parquet(OUT/'validation_panel.parquet',index=False)
    return frame, matrices, bench, cols, coverage


def ic_stats(frame, values):
    w = frame[['trade_date','fwd15']].copy()
    w['factor'] = values.to_numpy()
    rows=[]
    for day,x in w.groupby('trade_date'):
        x=x.replace([np.inf,-np.inf],np.nan).dropna()
        if len(x)>=50 and x.factor.nunique()>1:
            rows.append((day,x.factor.corr(x.fwd15,method='spearman')))
    s=pd.Series(dict(rows),dtype=float).dropna()
    t=nw_t(s,lag=2)
    return {'ic_mean':float(s.mean()),'icir':float(s.mean()/s.std()) if s.std()>0 else np.nan,
            'nw_t':t,'p_value':float(2*norm.sf(abs(t))), 'n_periods':len(s),
            'pos_ratio':float((s>0).mean())},s


def performance(nav, bench):
    nav=pd.Series(nav,dtype=float); b=pd.Series(bench,dtype=float)
    r=nav.pct_change().dropna(); br=b.pct_change().dropna()
    years=(len(nav)-1)/252
    ann=float((nav.iloc[-1]/nav.iloc[0])**(1/years)-1)
    excess=(r-br).dropna(); beta=float(r.cov(br)/br.var()) if br.var()>0 else np.nan
    return {'total_return':float(nav.iloc[-1]/nav.iloc[0]-1),'annual_return':ann,
        'max_drawdown':float((nav/nav.cummax()-1).min()),
        'sharpe_rf0':float(r.mean()/r.std()*np.sqrt(252)) if r.std()>0 else 0.,
        'information_ratio':float(excess.mean()/excess.std()*np.sqrt(252)) if excess.std()>0 else 0.,
        'excess_annual_arithmetic':float(excess.mean()*252), 'beta':beta,
        'alpha_annual_rf0':float((r.mean()-beta*br.mean())*252),
        'relative_terminal':float((nav.iloc[-1]/nav.iloc[0])/(b.iloc[-1]/b.iloc[0])-1)}


def replay(frame, score, matrices, bench, cols, slippage=0.001, buy_cost=0.0003,
           sell_tax=0.001, top_n=20, capital=2_000_000, interval=15):
    dates=bench.index; codes=list(cols); ix={c:i for i,c in enumerate(codes)}
    schedules={}; sigdays=sorted(frame.trade_date.unique())
    # interval stress uses the same verified signal panel, subsampling to 30 sessions.
    for day in sigdays[::max(1,interval//15)]:
        pos=dates.get_loc(day)
        if pos+1>=len(dates):continue
        g=frame[frame.trade_date==day].copy(); g['_score']=score.loc[g.index]
        g=g.replace([np.inf,-np.inf],np.nan).dropna(subset=['_score'])
        g=g.sort_values(['_score','stock_code'],ascending=[False,True]).head(top_n)
        schedules[pos+1]=[ix[c] for c in g.stock_code if c in ix]
    first=min(schedules); holdings=np.zeros(len(cols)); last=np.full(len(cols),np.nan)
    cash=float(capital); nav=[]; logs=[]; blocked=0; stale_marks=0
    for t in range(first,len(dates)):
        o=matrices['open'][t]; c=matrices['close'][t]; v=matrices['volume'][t]
        valid=np.isfinite(o)&(o>0)&np.isfinite(v)&(v>0)
        prev=matrices['close'][t-1]
        limit=np.array([.198 if x.startswith(('300','688')) and dates[t]>=pd.Timestamp('2020-08-24')
                        else .098 for x in codes])
        gap=o/prev-1
        one_price=np.isclose(matrices['high'][t],matrices['low'][t],rtol=0,atol=1e-8)
        # Uses t+1 daily high/low only as ex-post execution feasibility, never a signal.
        buyable=valid&~(one_price&(gap>=limit))
        sellable=valid&~(one_price&(gap<=-limit))
        mark=np.where(valid,o,last)
        if t in schedules:
            target=np.zeros(len(cols)); target[schedules[t]]=1/top_n
            equity=cash+np.nansum(holdings*mark)
            desired=np.divide(equity*target,o,out=np.zeros(len(cols)),where=valid)
            delta=desired-holdings
            sold=bought=fees=0.
            # Sell first; blocked sells stay in portfolio, never disappear.
            for j in np.where(delta< -1e-10)[0]:
                if not sellable[j]:blocked+=1;continue
                qty=-delta[j]; price=o[j]*(1-slippage); value=qty*price
                tax=sell_tax if dates[t]<pd.Timestamp('2023-08-28') else sell_tax/2
                fee=max(5.,value*buy_cost)+value*tax
                cash+=value-fee;holdings[j]-=qty;sold+=value;fees+=fee
            for j in np.where(delta>1e-10)[0]:
                if not buyable[j]:blocked+=1;continue
                price=o[j]*(1+slippage);value=delta[j]*price
                spend=min(value,max(0.,(cash-5)/(1+buy_cost)))
                if spend<=0:continue
                fee=max(5.,spend*buy_cost)
                if spend+fee>cash+1e-7:continue
                cash-=spend+fee;holdings[j]+=spend/price;bought+=spend;fees+=fee
            logs.append({'date':dates[t],'buy_value':bought,'sell_value':sold,'fees':fees,
                         'two_way_turnover':(bought+sold)/equity,'cash':cash})
        current=np.isfinite(c)&(c>0)
        stale_marks+=int(((holdings>0)&~current).sum())
        last=np.where(valid & ~np.isfinite(last),o,last)
        last=np.where(current,c,last)
        nav.append((cash+np.nansum(holdings*last))/capital)
        assert cash>=-1e-6 and np.all(holdings>=-1e-6)
    result=pd.DataFrame({'trade_date':dates[first:],'nav':nav}).set_index('trade_date')
    # Include a NAV=1 anchor on the preceding signal day; first-trade costs are retained.
    result.loc[dates[first-1]]=1.
    result=result.sort_index()
    for code,name in [('000905.SH','csi500'),('000300.SH','hs300')]:
        b=bench[code].reindex(result.index);result[name]=b/b.iloc[0]
    metrics=performance(result.nav,result.csi500)
    metrics.update({'blocked_orders':blocked,'stale_position_marks':stale_marks,
                    'rebalance_count':len(logs),'avg_two_way_turnover':float(pd.DataFrame(logs).two_way_turnover.mean())})
    return result,metrics,pd.DataFrame(logs)


def self_test():
    # Reproduce the original's true direction, not its potentially confusing rank comments.
    ns={'np':np,'isnan':np.isnan}
    exec((OUT/'ORIGINAL.md').read_text(),ns)
    data=[[10.,1.],[20.,2.],[30.,3.],[40.,4.]]
    original=np.asarray(ns['getRank']([r[:] for r in data])) @ np.array([[1],[-1]])
    direct=pd.Series([10,20,30,40]).rank(ascending=False)-pd.Series([1,2,3,4]).rank(ascending=False)
    assert np.array_equal(original.ravel(),direct.to_numpy())
    grid=pd.DataFrame({'trade_date':pd.to_datetime(['2020-01-02','2020-08-03']), 'stock_code':['A','A']})
    fin=pd.DataFrame({'ts_code':['A','A'],'ann_date':pd.to_datetime(['2019-10-01','2020-08-03']),
                      'end_date':pd.to_datetime(['2019-06-30','2020-06-30']),'roe':[2,99]})
    assert finance_asof(grid,fin).roe.tolist()==[2,2]
    x=np.arange(12.)/10
    d=x-x.mean();var=d@d/len(d)+(d[1:]@d[:-1])/len(d)
    assert abs(nw_t(x)-x.mean()/np.sqrt(var/len(d)))<1e-10
    assert bh_fdr(pd.Series([.001,.02,.8])).tolist()==[True,True,False]
    f=pd.DataFrame({'trade_date':pd.to_datetime(['2020-01-01']*3),'stock_code':['A','B','C'],'market_cap':[1.,2.,3.]})
    s,e=safe_eval('-rank(market_cap)',f,pd.Series(['market_cap']))
    assert not e and s.iloc[0]>s.iloc[2]
    # Cash/fees, blocked sell retention, no order for missing open.
    dates=pd.date_range('2020-01-01',periods=5)
    bm=pd.DataFrame({'000905.SH':[10.]*5,'000300.SH':[10.]*5},index=dates)
    matrices={k:np.full((5,1),10.) for k in ['open','close','high','low','volume','amount']}
    tiny=pd.DataFrame({'trade_date':[dates[0],dates[2]],'stock_code':['A','A']})
    sig=pd.Series([1.,np.nan])
    matrices['open'][3,0]=8.;matrices['high'][3,0]=8.;matrices['low'][3,0]=8.;matrices['close'][3,0]=8.
    nav,metrics,log=replay(tiny,sig,matrices,bm,pd.Index(['A']),top_n=1)
    assert metrics['blocked_orders']==1 and nav.loc[dates[3],'nav']>0.79
    assert nav.iloc[1].nav<1 and nav.iloc[0].nav==1
    print('PASS: original direction; strict announcement PIT; positional NW; BH-FDR; expression; cash/fees/blocked sell')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--self-test',action='store_true')
    ap.add_argument('--start',default='2015-01-05');ap.add_argument('--end',default='2026-10-09')
    ap.add_argument('--rounds',type=int,default=2);ap.add_argument('--proposals',type=int,default=4)
    ap.add_argument('--reuse-proposals',action='store_true')
    ap.add_argument('--prompt-mode',choices=['dynamic','static'],default='dynamic',
                    help='dynamic=baseline-aware net-return feedback; static=reproduce the first audit')
    args=ap.parse_args();OUT.mkdir(parents=True,exist_ok=True)
    if args.self_test:self_test();return 0
    if args.prompt_mode == 'dynamic':
        if (args.start, args.end) != ('2015-01-05', '2026-10-09'):
            raise SystemExit('dynamic mode uses fixed pre-registered dates; use --prompt-mode static for custom dates')
        from scripts.dynamic_jq_factor_mining import main as dynamic_main
        sys.argv = [sys.argv[0], '--rounds', str(args.rounds), '--proposals', str(args.proposals)] + (['--reuse-proposals'] if args.reuse_proposals else [])
        return dynamic_main()
    frame,matrices,bench,cols,cov=load_inputs(args.start,args.end)
    dump(OUT/'hypothesis.json',{'asof':'2026-10-10','baseline':'rank(roe) - rank(market_cap)',
        'train':'2015-2020; label exit <= 2020-12-31','validation':'2021-2023; label exit <= 2023-12-31',
        'holdout':'2024-2026-10-09; historical retrospective, not prospectively untouched',
        'selection':'largest positive train ICIR passing pooled train BH-FDR; validate without reselection',
        'benchmark':'000905.SH price index; 000300.SH secondary','proposals_per_round':args.proposals,
        'rounds':args.rounds,'cost':'commission 3bp (min CNY5), historical tax 10bp/5bp, slippage 10bp each side'})
    train=frame[(frame.trade_date<'2020-12-01')].copy()
    scores={};history=[];proposals=[
        {'name':'original_size_roe','expression':'rank(roe) - rank(market_cap)','reason':'original equal-rank score'},
        {'name':'size_only','expression':'-rank(market_cap)','reason':'size ablation'},
        {'name':'roe_only','expression':'rank(roe)','reason':'quality ablation'}]
    for rnd in range(1,args.rounds+1):
        path=OUT/f'round_{rnd}_proposals.json'
        if args.reuse_proposals and path.exists():items=json.loads(path.read_text())
        else:
            note='所有字段均为t日收盘已知、原始数值，不是行业分位。ROE为累计报告口径。rank当日横截面。只用给定字段与rank/log/clip/zscore，不用shift或rolling。股票池为历史沪深300成分。不要改变持仓20股和15交易日调仓。'
            direction='验证/改进低总市值+高ROE等秩合成策略；每个候选都保持规模和盈利质量解释；请提出少量简单交互或稳健化，不做参数网格'
            dump(OUT/f'round_{rnd}_request.json',{'direction':direction,'fields':FIELDS,'train_history':history,'note':note,'model':'deepseek-flash'})
            items=call_llm(direction,FIELDS,args.proposals,history,'deepseek-flash',temperature=.4,note=note)
            dump(path,items)
        for j,item in enumerate(items):
            item={**item,'name':f'llm_r{rnd}_{j+1}_{item.get("name","factor")}'}
            s,err=safe_eval(item['expression'],train,pd.Series(FIELDS))
            row={**item,'round':rnd,'error':err}
            if s is not None:row.update(ic_stats(train,s)[0])
            history.append(row);proposals.append(item)
            print('[train]',row,flush=True)
    records=[];ic_series={}
    for item in proposals:
        s,err=safe_eval(item['expression'],frame,pd.Series(FIELDS))
        if err:
            records.append({**item,'error':err});continue
        scores[item['name']]=s
        for split,mask in [('train',frame.trade_date<'2020-12-01'),
            ('validation',(frame.trade_date>='2021-01-01')&(frame.trade_date<'2023-12-01')),
            ('holdout',frame.trade_date>='2024-01-01')]:
            m,series=ic_stats(frame[mask],s[mask])
            records.append({**item,'split':split,**m})
            ic_series[item['name']+'_'+split]=series
    table=pd.DataFrame(records)
    train_stats=table[table.split=='train'].copy()
    train_stats['fdr_pass']=bh_fdr(train_stats.p_value).to_numpy()
    table=table.merge(train_stats[['name','fdr_pass']],on='name',how='left')
    table.to_csv(OUT/'factor_metrics.csv',index=False)
    pd.DataFrame(ic_series).to_csv(OUT/'ic_series.csv')
    eligible=train_stats[train_stats.name.str.startswith('llm_')&train_stats.fdr_pass&(train_stats.ic_mean>0)].sort_values('icir',ascending=False)
    winner=eligible.iloc[0]['name'] if len(eligible) else None
    diagnostic=train_stats[train_stats.name.str.startswith('llm_')].sort_values('icir',ascending=False)
    comparison=winner or (diagnostic.iloc[0]['name'] if len(diagnostic) else None)
    # A non-significant train leader may be plotted diagnostically, never called validated.
    names=['original_size_roe','size_only','roe_only']+([comparison] if comparison else [])
    results={}; curves={}; all_metrics={}
    for name in names:
        curve,metric,logs=replay(frame,scores[name],matrices,bench,cols)
        curve.to_csv(OUT/f'nav_{name}.csv');logs.to_csv(OUT/f'trades_{name}.csv',index=False)
        curves[name]=curve; results[name]=metric
        all_metrics[name]={}
        for split,a,b in [('full',None,None),('train',None,'2020-12-31'),
            ('validation','2021-01-01','2023-12-31'),('holdout','2024-01-01',None)]:
            z=curve.loc[a:b]
            all_metrics[name][split]=performance(z.nav,z.csi500)
        print('[backtest]',name,metric,flush=True)
    base=curves['original_size_roe']
    results['csi500']=performance(base.csi500,base.csi500)
    results['hs300']=performance(base.hs300,base.csi500)
    stress={}
    for label,kwargs in [('slippage_0bp',{'slippage':0.}),('slippage_30bp',{'slippage':.003}),
        ('30_sessions',{'interval':30}),('10_stocks',{'top_n':10}),('30_stocks',{'top_n':30})]:
        c,m,_=replay(frame,scores['original_size_roe'],matrices,bench,cols,**kwargs)
        stress[label]={'full':m,'holdout':performance(c.loc['2024':].nav,c.loc['2024':].csi500)}
    annual=[]
    for name,c in curves.items():
        for y,z in c.groupby(c.index.year):
            previous=c[c.index<z.index[0]].tail(1)
            z=pd.concat([previous,z])
            annual.append({'strategy':name,'year':y,'return':float(z.nav.iloc[-1]/z.nav.iloc[0]-1),
                          'benchmark':float(z.csi500.iloc[-1]/z.csi500.iloc[0]-1)})
    pd.DataFrame(annual).to_csv(OUT/'annual_returns.csv',index=False)
    metrics={'results':results,'splits':all_metrics,'stress':stress,'llm_train_selected':winner,
        'llm_diagnostic_train_leader':comparison,
        'frame_rows':len(frame),'stocks':frame.stock_code.nunique(),'signal_dates':frame.trade_date.nunique(),
        'start':str(base.index[0].date()),'end':str(base.index[-1].date()),
        'production_decision':'research_only','stage':'candidate',
        'data_gates':{'daily_exact_index_membership':False,'raw_unadjusted_limit_prices':False,
            'financial_vintage_restatements':False,'execution_lot_t1_capacity_verified':False,
            'prospective_paper_trading':False},'source_sha256':hashlib.sha256((OUT/'ORIGINAL.md').read_bytes()).hexdigest()}
    dump(OUT/'metrics.json',metrics)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axs=plt.subplots(2,1,figsize=(12,9))
    for name,c in curves.items():axs[0].plot(c.index,c.nav,label=name)
    axs[0].plot(base.index,base.csi500,label='CSI 500 price index',color='black',linewidth=1.8)
    axs[0].plot(base.index,base.hs300,label='CSI 300 price index',color='gray',linestyle='--')
    axs[0].set_yscale('log');axs[0].set_title('Historical HS300 size + ROE / net research NAV (log scale)')
    axs[0].legend(fontsize=8);axs[0].grid(alpha=.2)
    for name,c in curves.items():
        z=c.loc['2024':];axs[1].plot(z.index,z.nav/z.nav.iloc[0],label=name)
    z=base.loc['2024':];axs[1].plot(z.index,z.csi500/z.csi500.iloc[0],label='CSI 500',color='black')
    axs[1].set_title('2024+ retrospective holdout / rebased NAV');axs[1].legend(fontsize=8);axs[1].grid(alpha=.2)
    fig.tight_layout();fig.savefig(OUT/'equity_vs_csi500.png',dpi=160);plt.close(fig)
    print('[output]',OUT,flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
