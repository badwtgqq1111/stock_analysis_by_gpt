#!/usr/bin/env python
"""Observed-at, protocol-isolated crowding signal archives, never backfill credit.

Run after audit/study/shadow. This is a signal/input archive, NOT a forward NAV
or an automatic production promotion. No network or production writes.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.audit_crowding_inputs import sha256
OUT=ROOT/'output/research/crowding'

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def protocol_id(out=OUT):
    paths=['scripts/capture_crowding_forward.py','scripts/run_crowding_shadow.py',
           'factor_engine/risk/crowding.py','factor_engine/portfolio/paper_account.py',
           'factor_engine/portfolio/volatility_sizing.py','factor_engine/portfolio/volatility.py',
           'config/execution_assumptions_cn.json']
    return digest({**{p:sha256(ROOT/p) for p in paths},'methodology.json':sha256(out/'methodology.json')})

def validate_payload(payload):
    """A local same-day close with complete inputs; timezone mandatory."""
    try:
        at=pd.Timestamp(payload['captured_at'])
        if at.tzinfo is None:return False
        local=at.tz_convert('Asia/Shanghai')
        day=local.strftime('%Y-%m-%d')
        return (payload.get('inputs_ready') is True and local.hour>=15
                and payload['decision_date']==day and payload['as_of']==day)
    except (KeyError,ValueError,TypeError):return False

def write_capture(folder,payload):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    path=folder/f"{payload['captured_at'].replace(':','').replace('+','_')}_{uuid.uuid4().hex}.json"
    with path.open('x',encoding='utf-8') as f:
        json.dump({'sha256':digest(payload),'payload':payload},f,ensure_ascii=False,indent=2,allow_nan=False)
    return path

def read_captures(folder):
    captures=[]
    for path in sorted(Path(folder).glob('*.json')):
        envelope=json.loads(path.read_text());payload=envelope['payload']
        if digest(payload)!=envelope['sha256']:raise ValueError('archive hash mismatch: '+path.name)
        captures.append(payload)
    return sorted(captures,key=lambda p:p['captured_at'])

def evaluate_archive(captures,protocol):
    # Earliest valid capture per source/date; other protocol versions earn no credit.
    eligible={};foreign=0
    for capture in sorted(captures,key=lambda p:p['captured_at']):
        if capture.get('protocol_id')!=protocol:
            foreign+=1;continue
        for signal in capture.get('signals',[]):
            row={**capture,**signal}
            if validate_payload(row):eligible.setdefault((signal['source'],signal['decision_date']),capture['captured_at'])
    counts={s:sum(k[0]==s for k in eligible) for s in sorted({s['source'] for c in captures for s in c.get('signals',[])})}
    return {'archives':len(captures),'foreign_protocol_archives':foreign,'eligible_decisions':counts,
            'eligible_source_dates':[{'source':s,'decision_date':d,'first_capture':at} for (s,d),at in sorted(eligible.items())],
            'status':'accumulating' if eligible else 'no_eligible_same_day_signal',
            'forward_nav_available':False,'automatic_promotion':False,
            'limitations':['Signal capture counts are not independent return observations or a forward account.',
                          'Hash verification detects changes, not proof of external PIT provenance.']}

def capture(out=OUT):
    now=datetime.now(ZoneInfo('Asia/Shanghai'));today=now.strftime('%Y-%m-%d')
    latest_path=out/'shadow/latest_decisions.csv';audit_path=out/'shadow/decision_audit.csv'
    latest=pd.read_csv(latest_path);audit=pd.read_csv(audit_path)
    latest.trade_date=pd.to_datetime(latest.trade_date).dt.strftime('%Y-%m-%d')
    audit.trade_date=pd.to_datetime(audit.trade_date).dt.strftime('%Y-%m-%d')
    codes=sorted(latest.stock_code.unique())
    factor_path=out/'factors.parquet'
    f=ds.dataset(str(factor_path),format='parquet').to_table(columns=['stock_code','trade_date','turnover_pct252','data_status'],
        filter=ds.field('stock_code').isin(codes)).to_pandas()
    f.trade_date=pd.to_datetime(f.trade_date).dt.strftime('%Y-%m-%d');f=f.sort_values('trade_date').groupby('stock_code').tail(1)
    raw_paths=sorted((ROOT/'assets/data/clean/ohlcv/market=CN').glob(f'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={now.year}/*.parquet'))
    raw=ds.dataset([str(p) for p in raw_paths],format='parquet').to_table(columns=['stock_code','trade_date','open','high','low','close','volume','amount','source'],filter=ds.field('stock_code').isin(codes)).to_pandas()
    raw.trade_date=pd.to_datetime(raw.trade_date).dt.strftime('%Y-%m-%d');raw=raw[raw.trade_date==today]
    if raw.duplicated(['stock_code','trade_date']).any():raise ValueError('duplicate current bars')
    signals=[]
    for source,g in latest.groupby('source'):
        decision=g.trade_date.max();wanted=set(g.stock_code)
        ff=f[f.stock_code.isin(wanted)]
        asof=ff.trade_date.min() if len(ff) else ''
        rr=raw[raw.stock_code.isin(wanted)]
        valid=(rr[['open','high','low','close','volume','amount']]>0).all(axis=1)&np.isfinite(rr[['open','high','low','close','volume','amount']]).all(axis=1)
        valid&=(rr.high>=rr[['open','close','low']].max(axis=1))&(rr.low<=rr[['open','close']].min(axis=1))
        aa=audit[(audit.source==source)&(audit.trade_date==decision)]
        ready=(len(ff)==len(wanted) and ff.data_status.eq('ready').all() and ff.trade_date.eq(today).all()
               and set(rr.stock_code)==wanted and bool(valid.all()) and len(aa)>0
               and aa.unknown_baseline_weight.eq(0).all() and aa.risk_error.fillna('').eq('').all())
        row={'source':source,'decision_date':decision,'as_of':asof,'inputs_ready':bool(ready),
             'captured_at':now.isoformat(),'weights':json.loads(g.to_json(orient='records'))}
        row['eligible']=validate_payload(row);signals.append(row)
    paths=[latest_path,audit_path,factor_path,out/'methodology.json',*raw_paths]
    payload={'captured_at':now.isoformat(),'observed_date':today,'protocol_id':protocol_id(out),
             'signals':signals,'factors':json.loads(f.to_json(orient='records')),
             'prices':json.loads(raw.to_json(orient='records')),
             'input_sha256':{str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p):sha256(p) for p in paths},
             'role':'observed_signal_archive_not_forward_NAV'}
    folder=out/'forward';path=write_capture(folder/'captures',payload)
    result=evaluate_archive(read_captures(folder/'captures'),payload['protocol_id'])
    result.update(protocol_id=payload['protocol_id'],captured_at=now.isoformat(),latest_capture=str(path),
                  stale_signals=[{'source':r['source'],'decision_date':r['decision_date'],'as_of':r['as_of']} for r in signals if not r['eligible']])
    temp=folder/f'.latest.{uuid.uuid4().hex}.json';temp.write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False));temp.replace(folder/'latest.json')
    print('FORWARD: eligible_decisions='+json.dumps(result['eligible_decisions'],sort_keys=True)+'; status='+result['status']+'; automatic_promotion=False')
    print('capture='+str(path));return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=OUT);a=p.parse_args();capture(a.output)
