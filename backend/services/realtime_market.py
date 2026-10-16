"""One bounded, shared full-market snapshot for intraday boards (never persisted as EOD)."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import RLock

_VERSION_LOCK = RLock()
_VERSION_CACHE = {}

def versioned(key, version, builder):
    """Keep only the newest snapshot per logical key, not one large frame per tick."""
    with _VERSION_LOCK:
        hit = _VERSION_CACHE.get(key)
        if hit and hit[0] == version:
            return hit[1]
        value = builder()
        _VERSION_CACHE[key] = (version, value)
        while len(_VERSION_CACHE) > 12:
            del _VERSION_CACHE[next(iter(_VERSION_CACHE))]
        return value


import pandas as pd
import requests

from backend.services import live_quotes as lq, market_board_service as mbs

FS = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048'
FIELDS = 'f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f20,f21,f62,f124'


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def eligible(day=''):
    now = lq.now_cn()
    from data.model import get_market_calendar
    return (not day or day == now.strftime('%Y-%m-%d')) and get_market_calendar('CN').is_trading_day(now.date()) and now.strftime('%H:%M') >= '09:30'


def _page(page):
    response = requests.get('https://push2.eastmoney.com/api/qt/clist/get', params={
        'pn': page, 'pz': 100, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
        'fid': 'f12', 'fs': FS, 'fields': FIELDS,
    }, headers=lq._TX_HEADERS, timeout=(2, 4))
    response.raise_for_status()
    data = response.json().get('data')
    if not data or not isinstance(data.get('diff'), list):
        raise ValueError('empty market snapshot page')
    return data


def _build_snapshot():
    now = lq.now_cn(); day = now.strftime('%Y-%m-%d')
    try:
        first = _page(1); total = int(first['total'])
        pages = math.ceil(total / 100)
        if not 1 <= pages <= 120:
            raise ValueError(f'unexpected total: {total}')
        with ThreadPoolExecutor(max_workers=8) as pool:
            rest = list(pool.map(_page, range(2, pages + 1)))
        records = first['diff'] + [row for p in rest for row in p['diff']]
        quotes = {}; dated = set(); times = []
        for row in records:
            code = lq.to_ts_code(row['f12'])
            timestamp = number(row.get('f124'))
            if not timestamp:
                continue
            stamp = datetime.fromtimestamp(timestamp, lq.CN_TZ)
            if stamp.strftime('%Y-%m-%d') != day:
                continue
            dated.add(code)
            # Active-session stale quotes are excluded, never replaced with yesterday's close.
            if (now - stamp).total_seconds() < -60 or (lq.trading_phase(now)['live'] and (now - stamp).total_seconds() > 180):
                continue
            price, prev = number(row.get('f2')), number(row.get('f18'))
            if not price or not prev or price <= 0 or prev <= 0 or not number(row.get('f5')):
                continue
            times.append(stamp)
            quotes[code] = {'stock_code': code, 'name': row.get('f14', ''), 'price': price,
                'prev_close': prev, 'open': number(row.get('f17')), 'high': number(row.get('f15')),
                'low': number(row.get('f16')), 'volume': (number(row.get('f5')) or 0) * 100,
                'amount': number(row.get('f6')), 'net_amount': number(row.get('f62')),
                'time': stamp.strftime('%Y-%m-%d %H:%M:%S')}
        unique = len({str(r['f12']) for r in records})
        coverage = unique / total if total else 0
        if coverage < .98 or not quotes:
            raise ValueError(f'incomplete/stale snapshot: {unique}/{total}, current valid {len(quotes)}')
        return {'available': True, 'date': day, 'asof': max(times).strftime('%Y-%m-%d %H:%M:%S'),
                'oldest_asof': min(times).strftime('%Y-%m-%d %H:%M:%S'), 'source': 'eastmoney_snapshot', 'expected': total, 'received': unique,
                'valid': len(quotes), 'coverage': round(coverage, 4),
                'excluded': total-len(quotes), 'flow_valid': sum(q['net_amount'] is not None for q in quotes.values()), 'flow_available': any(q['net_amount'] is not None for q in quotes.values()),
                'quotes': quotes, 'reason': None}, None
    except Exception as exc:
        return None, f'{type(exc).__name__}: {exc}'[:250]


def snapshot():
    value, error = lq._cached(('market_snapshot',), 15, _build_snapshot, error_ttl=5)
    return value or {'available': False, 'date': lq.now_cn().strftime('%Y-%m-%d'), 'reason': error, 'quotes': {}}


def metadata(snap):
    return {k: v for k, v in snap.items() if k != 'quotes'}


def overlay(history, quotes, day):
    """Align historical adjusted bars to today's raw reference, then append today's OHLC.

    A quote's previous close is authoritative on ex-right days. Neither archived bars nor
    EOD files are mutated. Only stocks with current quotes can enter current-day ranking.
    """
    frame = history[history.trade_date < pd.Timestamp(day)].copy()
    previous = frame.groupby('stock_code')['close'].last()
    factors = {c: q['prev_close']/previous[c] for c,q in quotes.items() if c in previous and previous[c] > 0}
    scale = frame.stock_code.map(factors).fillna(1.)
    for field in ['open','high','low','close']:
        frame[field] = frame[field] * scale
    today = pd.DataFrame([{'trade_date': pd.Timestamp(day), 'stock_code': c,
        'open': q.get('open') or q['price'], 'high': q.get('high') or q['price'],
        'low': q.get('low') or q['price'], 'close': q['price'],
        'volume': q.get('volume') or 0, 'amount': q.get('amount')} for c,q in quotes.items()])
    return pd.concat([frame,today],ignore_index=True).sort_values(['stock_code','trade_date']).reset_index(drop=True)


def bars(days=70, snap=None):
    snap = snap or snapshot()
    if not snap['available']:
        return pd.DataFrame()
    return versioned(('bars', days), snap['asof'], lambda: flag_current(overlay(mbs.load_daily(days), snap['quotes'], snap['date']), snap))


def industry_frame(period, snap):
    from backend.services import industry_heat_service as ihs
    def build():
        end = pd.Timestamp(snap['date']); span = ihs.PERIODS[period][1]
        hist = ihs._moneyflow(str((end-pd.Timedelta(days=80)).date()), str((end-pd.Timedelta(days=1)).date()), 'moneyflow_dc') if span > 1 else pd.DataFrame()
        flow = pd.DataFrame([{'stock_code':c,'net_amount':q['net_amount']/10000 if q['net_amount'] is not None else float('nan'), 'trade_date': end} for c,q in snap['quotes'].items()])
        flow = pd.concat([hist,flow],ignore_index=True)
        frame = ihs._build_frame(period, source='moneyflow_dc', bars_override=bars(70,snap), flow_override=flow)
        if frame.get('available'):
            frame['market']['source'] = 'eastmoney_snapshot'
        return frame
    return versioned(('industry', period), snap['asof'], build)


def flag_current(frame, snap):
    """Current raw reference prevents a stale local prior bar becoming today's return.

    Exchange band fields are not in clist: use Tencent's explicit bands for near-limit
    candidates. Other/history rows retain the established archive board convention.
    """
    flagged = mbs.with_limit_flags(frame)
    mask = flagged.trade_date == pd.Timestamp(snap['date'])
    candidates = [c for c,q in snap['quotes'].items() if abs(q['price']/q['prev_close']-1) >= .02]
    bands = {}
    # Small, bounded batches. No network needed for ordinary stocks far from a band.
    if candidates:
        def fetch(batch):
            return lq.tencent_quotes(batch, timeout=2., attempts=1)[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for part in pool.map(fetch, [candidates[i:i+80] for i in range(0,len(candidates),80)]):
                bands.update(part)
    dates = sorted(flagged.trade_date.unique())
    basics = mbs.basic_frame()
    for idx in flagged.index[mask]:
        code = flagged.at[idx,'stock_code']; q = snap['quotes'][code]
        prev = q['prev_close']; price = q['price']
        flagged.at[idx,'prev_close'] = prev
        flagged.at[idx,'pct_chg'] = price/prev-1
        flagged.at[idx,'name'] = q['name']
        explicit = bands.get(code[:6], {})
        if str(explicit.get('time',''))[:8] == snap['date'].replace('-',''):
            for side, field in [('up','limit_up'),('down','limit_down')]:
                band = number(explicit.get(field))
                if band is not None:
                    flagged.at[idx,'is_limit_'+side] = bool(band > 0 and abs(price-band) < .005)
        # Newly listed names have special/no daily limits: exclude rather than misclassify.
        listed = pd.to_datetime(basics.loc[code,'list_date'], errors='coerce') if code in basics.index else None
        if listed is not None and not pd.isna(listed):
            cutoff = pd.Timestamp(dates[-1] if code.endswith('.BJ') else dates[max(0,len(dates)-5)])
            if listed >= cutoff:
                flagged.at[idx,'is_limit_up'] = False
                flagged.at[idx,'is_limit_down'] = False
    # Recompute streak once after current flags changed, preserving historical algorithm.
    streak = []; last = None; run = 0
    for code, hit in zip(flagged.stock_code, flagged.is_limit_up):
        if code != last: last,run = code,0
        run = run+1 if hit else 0
        streak.append(run)
    flagged['boards'] = streak
    return flagged
