"""Reported opening auction minute samples; indicative prices are not executed trades."""
from __future__ import annotations
import json
import os
import math
import re
import time
from pathlib import Path
import pandas as pd
import requests
from backend.services import live_quotes as lq
from backend.services import market_board_service as mbs

ARCHIVE = mbs.ROOT / 'assets/data/raw/opening_auction'
_EM_DOWN_UNTIL = 0.0


def _save(path, result):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(result, ensure_ascii=False))
        tmp.replace(path)
    except OSError:
        pass  # disk archival never blocks live display


def _eastmoney_trends(code: str):
    """东财 trend2；先尊重环境代理，再试一次直连，失败返回原因。"""
    symbol = code.split('.')[0]
    market = '1' if code.endswith('.SH') else '0'
    params = {
        'secid': f'{market}.{symbol}',
        'fields1': 'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58',
        'ndays': 1, 'iscr': 1, 'iscca': 0,
    }
    last = None
    for direct in (False, True):
        try:
            kwargs = {'proxies': {'http': '', 'https': ''}} if direct else {}
            response = requests.get(
                'https://push2.eastmoney.com/api/qt/stock/trends2/get',
                params=params, headers=lq._TX_HEADERS, timeout=(1.5, 2.5), **kwargs)
            response.raise_for_status()
            return response.json().get('data') or {}, None
        except Exception as exc:  # noqa: BLE001
            last = f'{type(exc).__name__}: {exc}'[:220]
    return None, last


def _tencent_snapshot(code: str):
    """东财不可用时的竞价快照：价格/昨收/量额来自腾讯行情，不能伪造 09:15-09:24 历史。"""
    quotes, _ = lq.tencent_quotes([code])
    quote = quotes.get(code.split('.')[0])
    if not quote or not quote.get('price'):
        return None
    now = lq.now_cn()
    price = float(quote['price'])
    prev = quote.get('prev_close')
    return {
        'time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'clock': now.strftime('%H:%M'),
        'price': price,
        'avg': None,
        'open': float(quote.get('open') or price),
        'high': float(quote.get('high') or price),
        'low': float(quote.get('low') or price),
        'volume_shares': int((quote.get('volume_lots') or 0) * 100),
        'amount_yuan': float(quote.get('amount_wan') or 0) * 10000.0,
        'kind': 'snapshot',
        'prev_close': float(prev) if prev else None,
    }


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
    global _EM_DOWN_UNTIL
    code = lq.to_ts_code(code)
    if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',code):
        return {'available':False,'points':[],'reason':'股票代码格式错误'}
    if day and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
        return {'available':False,'points':[],'reason':'日期格式错误'}
    today = lq.now_cn().strftime('%Y-%m-%d'); day = day or today
    def build():
        global _EM_DOWN_UNTIL
        path = ARCHIVE / day / (code+'.json')
        saved = None
        if path.exists():
            try: saved = json.loads(path.read_text())
            except (OSError,ValueError): pass
        if day != today:
            return saved or {'available':False, 'date':day, 'points':[], 'reason':'该日尚无竞价采样归档'}, None
        if time.time() < _EM_DOWN_UNTIL:
            data, em_error = None, '东财竞价源熔断中'
        else:
            data, em_error = _eastmoney_trends(code)
            if data is None:
                _EM_DOWN_UNTIL = time.time() + 60.0
            else:
                _EM_DOWN_UNTIL = 0.0
        points = parse(data, day) if data else []
        if points:
            result = {'available':True,'code':code,'date':day,'points':points,'source':'eastmoney_preopen',
                'prev_close':data.get('preClose'), 'received_at':lq.now_cn().strftime('%Y-%m-%d %H:%M:%S'),
                'reason':None, 'note':'09:15–09:25 为源报告的指示价格；量额按源报告，不代表未成交委托量。分钟采样非逐笔/L2，09:25 后保留源时间戳。'}
            _save(path, result)
            return result,None

        # 东财域名在当前网络下会被代理/直连同时拒绝。
        # 已有完整东财采样则直接复用；已有腾讯快照则继续追加新的分钟快照。
        snap = _tencent_snapshot(code)
        if saved and saved.get('source') == 'tencent_preopen_snapshot':
            if snap:
                points = list(saved.get('points') or [])
                if not any(p.get('clock') == snap.get('clock') for p in points):
                    points.append(snap)
                result = {**saved, 'points': points, 'received_at': snap['time'],
                          'degraded': True,
                          'reason': '东财竞价源不可达，使用腾讯快照采样'}
                _save(path, result)
                return result, None
            return {**saved, 'cached': True,
                    'reason': '东财竞价源不可达，使用当日已采样归档'}, None
        if saved:
            return {**saved, 'cached': True,
                    'reason': '东财竞价源不可达，使用当日已采样归档'}, None

        # 没有历史采样时，用腾讯快照给出“当前竞价指示价”；不把它冒充 09:15-09:24 的逐分钟历史。
        if snap:
            result = {
                'available': True, 'code': code, 'date': day, 'points': [snap],
                'source': 'tencent_preopen_snapshot',
                'prev_close': snap.get('prev_close'),
                'received_at': snap['time'],
                'degraded': True,
                'reason': '东财竞价源不可达，已回退腾讯当前快照',
                'note': '当前竞价快照，不是 09:15–09:24 的逐分钟历史；量额仅在源提供时显示。',
            }
            _save(path, result)
            return result, None

        reason = '东财竞价源不可达，腾讯快照也不可用'
        return {'available':False,'date':day,'points':[],'reason':reason}, reason
    payload, _ = lq._cached(('auction',code,day),5,build,error_ttl=5)
    return payload
