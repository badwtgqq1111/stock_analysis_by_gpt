#!/usr/bin/env python
"""Read-only CN lake audit. No network, no production cache writes."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'output/research/crowding'

def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()

def calendar(root=ROOT):
    b = pd.read_parquet(root/'assets/data/meta/cn_benchmark_daily.parquet')
    return pd.DatetimeIndex(pd.to_datetime(b.loc[b.ts_code == '000905.SH', 'trade_date'])).unique().sort_values()

def audit_matches(bars, basic):
    """Same-day identities, no forward-filled shares; raw daily_basic units retained."""
    m = bars.merge(basic, left_on=['stock_code','trade_date'], right_on=['ts_code','trade_date'], suffixes=('_bar','_raw'))
    for name, a, b in [('volume_float_turnover', m.volume/(m.float_share*10000), m.turnover_rate/100),
                        ('volume_free_turnover', m.volume/(m.free_share*10000), m.turnover_rate_f/100),
                        ('bar_turnover_float', m.turnover, m.turnover_rate/100),
                        ('total_mv_identity', m.close_raw*m.total_share, m.total_mv),
                        ('circ_mv_identity', m.close_raw*m.float_share, m.circ_mv)]:
        m[name] = (a/b).where((a>0)&(b>0))
    m['vwap_rawclose_ratio'] = m.amount/m.volume.replace(0,np.nan)/m.close_raw
    m['qfq_rawclose_ratio'] = m.close_bar/m.close_raw
    return m

def share_changes(basic):
    """Adjacent observed snapshots, NOT the actual effective dates of changes."""
    f=basic.sort_values(['ts_code','trade_date']).copy()
    f['previous_observed_date']=f.groupby('ts_code').trade_date.shift()
    changed=pd.Series(False,index=f.index)
    for c in ['total_share','float_share','free_share']:
        prev=f.groupby('ts_code')[c].shift();f['previous_'+c]=prev
        f[c+'_ratio']=f[c]/prev.where(prev>0)
        changed|=(f[c]-prev).abs().gt(1e-5)&prev.notna()
    return f.loc[changed,['ts_code','previous_observed_date','trade_date']+
                 [v for c in ['total_share','float_share','free_share'] for v in ['previous_'+c,c,c+'_ratio']]]

def run(root=ROOT, out=OUT, start=2017, end=2026):
    out.mkdir(parents=True, exist_ok=True)
    cal=calendar(root); files=sorted((root/'assets/data/raw/daily_basic_relay').glob('month=*.parquet'))
    basic_frames=[]; freq=[]; manifest={}
    for p in files:
        f=pq.ParquetFile(p).read().to_pandas();f.trade_date=pd.to_datetime(f.trade_date)
        basic_frames.append(f); manifest[str(p.relative_to(root))]=sha256(p)
        freq.append(dict(file=p.name,rows=len(f),dates=f.trade_date.nunique(),stocks=f.ts_code.nunique(),
                         duplicate_rows=int(f.duplicated(['ts_code','trade_date']).sum()),first=str(f.trade_date.min().date()),last=str(f.trade_date.max().date())))
    basic=pd.concat(basic_frames,ignore_index=True)
    if basic.duplicated(['ts_code','trade_date']).any(): raise ValueError('duplicate basic keys')
    changes=share_changes(basic);changes.to_csv(out/'share_changes.csv',index=False)
    sources=[]; coverage=[]; codes=[]; samples=[]; matches=[]
    for y in range(start,end+1):
        for p in sorted((root/'assets/data/clean/ohlcv/market=CN').glob(f'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet')):
            manifest[str(p.relative_to(root))]=sha256(p)
            cols=['trade_date','stock_code','open','high','low','close','volume','amount','turnover','source','ingest_time']
            f=pq.ParquetFile(p).read(columns=cols).to_pandas(); f.trade_date=pd.to_datetime(f.trade_date)
            if f.duplicated(['stock_code','trade_date']).any(): raise ValueError(f'duplicate bars: {p}')
            exchange=next(x for x in p.parts if x.startswith('exchange='))
            valid=(f[['open','high','low','close','volume','amount']]>0).all(axis=1) & np.isfinite(f[['open','high','low','close','volume','amount']]).all(axis=1)
            valid &= (f.high>=f[['open','close','low']].max(axis=1)) & (f.low<=f[['open','close']].min(axis=1))
            f['valid']=valid
            # A zero turnover on a trading bar is treated as unknown, not as no trading.
            for source,g in f.groupby('source',dropna=False):
                sources.append(dict(year=y,exchange=exchange,source=source,rows=len(g),valid_bars=int(g.valid.sum()),
                                    turnover_positive=int((g.turnover>0).sum()),turnover_median=float(g.turnover.median()),
                                    ingest_first=str(g.ingest_time.min()),ingest_last=str(g.ingest_time.max())))
            c=f.groupby('trade_date').agg(rows=('stock_code','size'),valid_bars=('valid','sum'),stocks=('stock_code','nunique')).reset_index()
            c['exchange']=exchange; coverage.append(c)
            k=f.groupby('stock_code').agg(first=('trade_date','min'),last=('trade_date','max'),rows=('trade_date','size')).reset_index();codes.append(k)
            m=audit_matches(f,basic[basic.trade_date.dt.year==y]); matches.append(m)
            samples.append(m.sort_values(['stock_code','trade_date']).groupby('source').head(3))
        print(f'audit year={y} complete',flush=True)
    cov=pd.concat(coverage);cov.to_csv(out/'coverage.csv',index=False)
    pd.DataFrame(sources).to_csv(out/'coverage_by_source.csv',index=False)
    pd.DataFrame(freq).to_csv(out/'basic_frequency.csv',index=False)
    code=pd.concat(codes).groupby('stock_code').agg(first=('first','min'),last=('last','max'),rows=('rows','sum')).reset_index()
    code['expected_sessions']=[int(((cal>=a)&(cal<=b)).sum()) for a,b in zip(code['first'],code['last'])]
    code['missing_sessions']=code.expected_sessions-code.rows;code.to_csv(out/'stock_coverage.csv',index=False)
    m=pd.concat(matches,ignore_index=True); rows=[]
    metrics=['volume_float_turnover','volume_free_turnover','bar_turnover_float','total_mv_identity','circ_mv_identity','vwap_rawclose_ratio','qfq_rawclose_ratio']
    for source,g in m.groupby('source'):
        for metric in metrics:
            x=g[metric].replace([np.inf,-np.inf],np.nan).dropna()
            rows.append(dict(source=source,metric=metric,n=len(x),median=x.median(),p01=x.quantile(.01),p99=x.quantile(.99),within_5pct=float(x.between(.95,1.05).mean())))
    units=pd.DataFrame(rows); units.to_csv(out/'unit_checks.csv',index=False)
    pd.concat(samples).to_csv(out/'unit_samples.csv',index=False)
    bad=m[(~m.volume_float_turnover.between(.95,1.05))|(~m.bar_turnover_float.between(.95,1.05))]
    bad.sort_values(['source','trade_date','stock_code']).groupby('source').head(20).to_csv(out/'unit_outliers.csv',index=False)
    # Only sources with a large direct validation sample and consistent turnover identity qualify.
    ok=units[(units.metric=='bar_turnover_float')&(units.n>=100)&(units.within_5pct>=.98)].source.tolist()
    pd.DataFrame({'source':ok}).to_csv(out/'validated_turnover_sources.csv',index=False)
    meta={}
    for name in ['cn_stock_basic.parquet','cn_stock_namechange.parquet','cn_sw2021_industry.parquet','cn_benchmark_daily.parquet']:
        p=root/'assets/data/meta'/name; manifest[str(p.relative_to(root))]=sha256(p)
        f=pq.ParquetFile(p).read().to_pandas();meta[name]={'rows':len(f),'columns':list(f.columns)}
        if 'snapshot' in f:meta[name]['snapshots']=sorted(f.snapshot.astype(str).unique().tolist())
    listing=pd.read_parquet(root/'assets/data/meta/cn_stock_basic.parquet')
    names=pd.read_parquet(root/'assets/data/meta/cn_stock_namechange.parquet')
    listing=listing[['ts_code','list_date','list_status']].merge(code,left_on='ts_code',right_on='stock_code',how='outer')
    listing['has_name_history']=listing.ts_code.isin(names.ts_code)
    listing['has_lake_bars']=listing.rows.fillna(0)>0
    listing.to_csv(out/'listing_coverage.csv',index=False)
    name_audit={'rows':len(names),'stocks':names.ts_code.nunique(),
                'missing_ann_date':int(names.ann_date.isna().sum()),'risk_rows':int(names.is_risk.sum()),
                'ann_after_start':int((names.ann_date>names.start_date).sum()),
                'lake_stocks_without_name_history':int((~code.stock_code.isin(names.ts_code)).sum()),
                'lake_stocks_without_listing_metadata':int((~code.stock_code.isin(listing.ts_code.dropna())).sum()),
                'current_status_counts':listing.list_status.fillna('unknown').value_counts().to_dict(),
                'warning':'Current stock_basic has no complete historical delisting/effective-date lineage. Name announcements are retrospective imports, not independently verified PIT.'}
    (out/'name_listing_audit.json').write_text(json.dumps(name_audit,ensure_ascii=False,indent=2))
    bd=pd.DatetimeIndex(basic.trade_date.unique()); eligible=cal[(cal>=basic.trade_date.min())&(cal<=basic.trade_date.max())]
    dates=pd.DataFrame({'trade_date':cal[(cal.year>=start)&(cal.year<=end)]})
    dates=dates.merge(cov.groupby('trade_date')[['rows','valid_bars']].sum(),on='trade_date',how='left')
    dates['basic_stocks']=dates.trade_date.map(basic.groupby('trade_date').ts_code.nunique()).fillna(0)
    dates.to_csv(out/'daily_completeness.csv',index=False)
    result={'version':'crowding-audit.v1','bar_rows':int(cov.rows.sum()),'stocks':len(code),'bar_start':str(code['first'].min().date()),'bar_end':str(code['last'].max().date()),
            'basic_files':len(files),'basic_rows':len(basic),'basic_dates':len(bd),'basic_expected_dates':len(eligible),
            'basic_date_coverage':len(bd.intersection(eligible))/len(eligible),'validated_turnover_sources':ok,
            'observed_share_changes':len(changes),'name_listing_audit':name_audit,
            'metadata':meta,'status':'retrospective_only','pit_industry':False,'pit_qfq':False,
            'notes':['Monthly basic is not daily coverage; no share forward-fill is used in main factors.',
                     'Same-date relay identities verify units only, not historical availability or publication time.',
                     'share_changes.csv compares adjacent observed snapshots, not exact corporate-action dates; no daily forward-fill.',
                     'Stock lake may have pre-existing survivorship bias; current list_status is never used as a historical filter.',
                     'Missing sessions include suspensions/data gaps; they are not automatically identified as data errors.',
                     'Industry snapshots and name histories need independent PIT review.']}
    (out/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    (out/'input_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    (out/'AUDIT.md').write_text('# 拥挤度输入审计\n\n'+f'日线{result["bar_rows"]:,}条，{len(code)}只；daily_basic仅{len(bd)}/{len(eligible)}个交易日。\n\n'+
        '单位检验为同日交叉源比值，不是PIT证明。主换手使用已验证来源的日线流通换手小数；不以月末股本前填构造。\n\n```\n'+units.to_string(index=False)+'\n```\n\n'+ '\n'.join('- '+s for s in result['notes'])+'\n')
    print(f'AUDIT: rows={result["bar_rows"]}; stocks={len(code)}; basic_dates={len(bd)}/{len(eligible)}; validated_sources={ok}',flush=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=OUT);p.add_argument('--start-year',type=int,default=2017);p.add_argument('--end-year',type=int,default=2026)
    a=p.parse_args();run(out=a.output,start=a.start_year,end=a.end_year)
