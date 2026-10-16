"""Reported opening auction minute samples; indicative prices are not executed trades."""
from __future__ import annotations
import json
import os
import math
import re
from pathlib import Path
import pandas as pd
import requests
from backend.services import live_quotes as lq
from backend.services import market_board_service as mbs

ARCHIVE = mbs.ROOT / 'assets/data/raw/opening_auction'


def parse(data, day):
    result = []
    for line in data.get('trends', []):
        fields = line.split(',')
        if len(fields) < 8:
            continue
        stamp = pd.to_datetime(fields[0], errors='coerce')
        if pd.isna(stamp) or str(stamp.date()) != day or not '09:15' <= stamp.strftime('%H:%M') <= '09:30':
            continue
        try:
            price, volume, amount = float(fields[2]), float(fields[5])*100, float(fields[6])
            if not all(math.isfinite(float(v)) for v in fields[1:8]) or price <= 0 or volume < 0 or amount < 0:
                continue
        except ValueError:
            continue
        result.append({'time':str(stamp), 'clock':stamp.strftime('%H:%M'), 'price':price,
            'avg':None, 'open':float(fields[1]), 'high':float(fields[3]), 'low':float(fields[4]),
            'volume_shares':int(volume), 'amount_yuan':amount,
            'kind':'indicative' if stamp.strftime('%H:%M') < '09:25' else 'opening_result'})
    return result


def auction(code, day=''):
    code = lq.to_ts_code(code)
    if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',code):
        return {'available':False,'points':[],'reason':'股票代码格式错误'}
    if day and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
        return {'available':False,'points':[],'reason':'日期格式错误'}
    today = lq.now_cn().strftime('%Y-%m-%d'); day = day or today
    def build():
        path = ARCHIVE / day / (code+'.json')
        saved = None
        if path.exists():
            try: saved = json.loads(path.read_text())
            except (OSError,ValueError): pass
        if day != today:
            return saved or {'available':False, 'date':day, 'points':[], 'reason':'该日尚无竞价采样归档'}, None
        try:
            symbol = code.split('.')[0]; market = '1' if code.endswith('.SH') else '0'
            response = requests.get('https://push2.eastmoney.com/api/qt/stock/trends2/get', params={
                'secid':f'{market}.{symbol}', 'fields1':'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13',
                'fields2':'f51,f52,f53,f54,f55,f56,f57,f58', 'ndays':1,'iscr':1,'iscca':0,
            }, headers=lq._TX_HEADERS, timeout=(2,3))
            response.raise_for_status(); data = response.json().get('data') or {}
            points = parse(data, day)
            if not points: raise ValueError('源尚未返回该日竞价数据')
            result = {'available':True,'code':code,'date':day,'points':points,'source':'eastmoney_preopen',
                'prev_close':data.get('preClose'), 'received_at':lq.now_cn().strftime('%Y-%m-%d %H:%M:%S'),
                'reason':None, 'note':'09:15–09:25 为源报告的指示价格；量额按源报告，不代表未成交委托量。分钟采样非逐笔/L2，09:25 后保留源时间戳。'}
            try:
                path.parent.mkdir(parents=True,exist_ok=True)
                tmp = path.with_suffix(f'.{os.getpid()}.tmp'); tmp.write_text(json.dumps(result,ensure_ascii=False)); tmp.replace(path)
            except OSError:
                pass  # disk archival never blocks live display
            return result,None
        except Exception as exc:
            reason=f'{type(exc).__name__}: {exc}'[:220]
            if saved:
                return {**saved,'cached':True,'reason':reason},None
            return {'available':False,'date':day,'points':[],'reason':reason},reason
    payload, _ = lq._cached(('auction',code,day),5,build,error_ttl=5)
    return payload
