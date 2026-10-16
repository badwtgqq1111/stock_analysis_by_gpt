"""Deterministic realtime fixtures: no exchange/network calls in this suite."""
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import time

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.services import realtime_market as rt, live_quotes as lq, market_board_service as mbs
from backend.services import auction_service as auc
from backend.api import intraday as ia

DAY = '2026-10-16'
NOW = datetime(2026,10,16,10,30,tzinfo=lq.CN_TZ)

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(lq,'now_cn',lambda: NOW)
    monkeypatch.setattr(rt.requests,'get',lambda *a,**kw: pytest.fail('unexpected HTTP'))
    monkeypatch.setattr(lq,'tencent_quotes',lambda *a,**kw: ({},'fixture missing'))
    lq.cache_clear(); rt._VERSION_CACHE.clear()
    ia.live_daily_bar.cache_clear()

def quote(price=11.,prev=10.,name='样本'):
    return {'price':price,'prev_close':prev,'name':name,'open':10.,'high':11.,'low':9.9,
            'volume':10000.,'amount':110000.,'net_amount':1e8,'time':DAY+' 10:30:00'}

def snap(quotes=None):
    return {'available':True,'date':DAY,'asof':DAY+' 10:30:00','quotes':quotes or {'000001.SZ':quote()},
            'flow_available':True,'valid':1,'expected':1,'excluded':0,'source':'fixture'}

def row(code='000001',**kw):
    return {'f12':code,'f124':NOW.timestamp(),'f2':11,'f18':10,'f17':10,'f15':11,'f16':9.9,
            'f5':10,'f6':11000,'f62':-1e8,'f14':'样本',**kw}

def history():
    return pd.DataFrame([{'stock_code':'000001.SZ','trade_date':pd.Timestamp(d),'open':p,'high':p,
        'low':p,'close':p,'volume':100,'amount':1000} for d,p in [('2026-10-14',4.5),('2026-10-15',5.),(DAY,99.)]])

@pytest.fixture
def client():
    from backend.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c: yield c


def test_paginated_snapshot_has_full_unique_coverage(monkeypatch):
    calls=[]
    def page(n):
        calls.append(n)
        return {'total':201,'diff':[row(f'{i:06}') for i in range((n-1)*100,min(n*100,201))]}
    monkeypatch.setattr(rt,'_page',page)
    data,error=rt._build_snapshot()
    assert error is None and sorted(calls)==[1,2,3]
    assert data['received']==201 and data['valid']==201 and data['coverage']==1
    assert data['quotes']['000001.SZ']['volume']==1000
    assert data['quotes']['000001.SZ']['net_amount']==-1e8

@pytest.mark.parametrize('bad',[{'f124':NOW.timestamp()-86400},{'f124':NOW.timestamp()-181},
    {'f124':NOW.timestamp()+61},{'f2':'-'},{'f2':'nan'},{'f18':0},{'f5':0}])
def test_snapshot_excludes_wrong_date_stale_invalid_and_untraded(monkeypatch,bad):
    monkeypatch.setattr(rt,'_page',lambda n:{'total':2,'diff':[row(),row('000002',**bad)]})
    data,error=rt._build_snapshot()
    assert error is None and data['valid']==1 and data['excluded']==1
    assert '000002.SZ' not in data['quotes']


def test_incomplete_duplicate_pagination_rejected(monkeypatch):
    monkeypatch.setattr(rt,'_page',lambda n:{'total':200,'diff':[row()]*100})
    data,error=rt._build_snapshot()
    assert data is None and 'incomplete' in error


def test_snapshot_failure_not_relabelled_today(monkeypatch):
    monkeypatch.setattr(rt,'_page',lambda n: (_ for _ in ()).throw(ValueError('source down')))
    data=rt.snapshot()
    assert data['available'] is False and data['quotes']=={} and 'source down' in data['reason']


def test_eligible_respects_historical_day_weekend_and_open(monkeypatch):
    assert rt.eligible(DAY) and not rt.eligible('2026-10-15')
    for now in [datetime(2026,10,16,9,20,tzinfo=lq.CN_TZ),datetime(2026,10,17,10,tzinfo=lq.CN_TZ)]:
        monkeypatch.setattr(lq,'now_cn',lambda:now)
        assert not rt.eligible()


def test_overlay_aligns_adjustment_replaces_today_without_mutating_archive():
    old=history(); original=old.copy(deep=True)
    out=rt.overlay(old,snap()['quotes'],DAY)
    pd.testing.assert_frame_equal(old,original)
    assert len(out)==3 and out.iloc[-1].close==11 and out.iloc[-2].close==10
    assert out.iloc[-1].close/out.iloc[-2].close-1==pytest.approx(.1)
    assert out.iloc[-1].volume==10000


def test_version_cache_is_singleflight_and_bounded():
    calls=[]
    def build():calls.append(1);time.sleep(.02);return 42
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _:rt.versioned(('bars',70),'v1',build),range(4)))==[42]*4
    assert len(calls)==1
    for i in range(30): rt.versioned(('bars',70),str(i),lambda:i)
    assert len(rt._VERSION_CACHE)==1


def test_explicit_limit_band_and_streak_open_board_reversal(monkeypatch):
    monkeypatch.setattr(mbs,'_st_matrix',lambda *args:pd.DataFrame())
    basics=pd.DataFrame({'name':['样本'],'market':['主板'],'industry':['银行'],'list_date':['19910101']},index=['000001.SZ'])
    monkeypatch.setattr(mbs,'basic_frame',lambda:basics)
    monkeypatch.setattr(lq,'tencent_quotes',lambda *a,**kw:({'000001':{'limit_up':11.,'limit_down':9.,'time':'20261016103000'}},None))
    f=rt.overlay(history(),snap()['quotes'],DAY)
    flagged=rt.flag_current(f,snap())
    assert flagged.iloc[-1].is_limit_up and flagged.iloc[-1].boards==2
    q=quote(price=10.99)
    opened=rt.flag_current(rt.overlay(history(),{'000001.SZ':q},DAY),snap({'000001.SZ':q}))
    assert not opened.iloc[-1].is_limit_up and opened.iloc[-1].boards==0


def test_new_listing_not_falsely_classified_limit(monkeypatch):
    monkeypatch.setattr(mbs,'_st_matrix',lambda *a:pd.DataFrame())
    monkeypatch.setattr(mbs,'basic_frame',lambda:pd.DataFrame({'name':['新股'],'market':['主板'],'list_date':['20261015']},index=['000001.SZ']))
    out=rt.flag_current(rt.overlay(history(),snap()['quotes'],DAY),snap())
    assert not out.iloc[-1].is_limit_up


def test_live_change_and_limitup_share_current_bars(client,monkeypatch):
    monkeypatch.setattr(mbs,'_st_matrix',lambda *a:pd.DataFrame())
    qs={'000001.SZ':quote()}; snapshot=snap(qs)
    basics=pd.DataFrame({'name':['样本'],'market':['主板'],'industry':['银行'],'list_date':['19910101']},index=['000001.SZ'])
    monkeypatch.setattr(mbs,'basic_frame',lambda:basics)
    frame=rt.flag_current(rt.overlay(history(),qs,DAY),snapshot)
    monkeypatch.setattr(rt,'snapshot',lambda:snapshot)
    monkeypatch.setattr(rt,'bars',lambda *a:frame)
    change=client.get('/api/rankings/change?live=1').json()
    ladder=client.get('/api/limitup/ladder?live=1').json()
    detail=client.get('/api/limitup/detail?live=1').json()
    assert change['date']==DAY and change['rows'][0]['value']==pytest.approx(.1)
    assert ladder['dates'][-1]==DAY==detail['date']
    assert ladder['latest']['counts']==detail['counts'] and detail['total']==1
    assert detail['rows'][0]['source']=='realtime'

@pytest.mark.parametrize('url',['/api/rankings/change','/api/rankings/moneyflow','/api/industry/heat',
    '/api/industry/heatmap','/api/industry/银行/stocks','/api/limitup/ladder','/api/limitup/detail'])
def test_history_live_flag_never_requests_current_snapshot(client,monkeypatch,url):
    monkeypatch.setattr(rt,'snapshot',lambda:pytest.fail('history called realtime'))
    body=client.get(url+'?date=2026-09-24&live=1')
    assert body.status_code==200


def test_live_funds_units_sort_and_no_fabricated_long_windows(client,monkeypatch):
    qs={'000001.SZ':quote(),'000002.SZ':{**quote(),'net_amount':-2e8}}
    monkeypatch.setattr(rt,'snapshot',lambda:snap(qs))
    body=client.get('/api/rankings/moneyflow?live=1&direction=out').json()
    assert body['source']=='moneyflow_dc' and body['date']==DAY
    assert body['rows'][0]['stock_code']=='000002.SZ' and body['rows'][0]['value']==-2
    assert body['rows'][0]['net_10d_yi'] is None and body['stats']['n_in']==1


def test_week_funds_need_four_complete_same_source_trading_days(client,monkeypatch):
    monkeypatch.setattr(rt,'snapshot',lambda:snap())
    dates=pd.to_datetime(['2026-10-12','2026-10-13','2026-10-14','2026-10-15'])
    monkeypatch.setattr(mbs,'trade_dates',lambda *_:list(dates))
    def dayrows(day,source):
        assert source=='moneyflow_dc'
        return pd.DataFrame({'trade_date':[pd.Timestamp(day)],'stock_code':['000001.SZ'],'net_amount':[10000.]})
    monkeypatch.setattr(mbs,'moneyflow_rows',dayrows)
    body=client.get('/api/rankings/moneyflow?live=1&period=weekly').json()
    assert body['rows'][0]['value']==5  # 4x1yi past +1yi today
    monkeypatch.setattr(mbs,'moneyflow_rows',lambda d,s: dayrows(d,s).iloc[:0] if d=='2026-10-14' else dayrows(d,s))
    body=client.get('/api/rankings/moneyflow?live=1&period=weekly').json()
    assert body['rows']==[] and not body['available']


def test_daily_bar_provisional_adjusted_ohlc_and_units(client,monkeypatch):
    q={'price':11.,'prev_close':10.,'open':10.,'high':12.,'low':9.,'volume_lots':123.,'time':'20261016103000'}
    monkeypatch.setattr(lq,'tencent_quotes',lambda *a,**kw:({'000001':q},None))
    monkeypatch.setattr(ia,'_prev_close',lambda *a:5.)
    body=client.get('/api/intraday/daily/000001.SZ').json()
    assert body['available'] and body['provisional']
    assert body['candle']['close']==5.5 and body['candle']['high']==6. and body['candle']['low']==4.5
    assert body['candle']['volume']==12300 and body['candle']['change_pct']==10.
    ia.live_daily_bar.cache_clear();q['time']='20261015150000'
    assert not client.get('/api/intraday/daily/000001.SZ').json()['available']


def trends():
    return {'preClose':10.,'trends':[
        f'{DAY} 09:15,10,10.1,10.1,10,0,0,10',
        f'{DAY} 09:26,10,10.2,10.2,10,20,20400,10.2',
        f'{DAY} 09:31,10,10.3,10.3,10,10,10300,10.3',
        '2026-10-15 09:16,10,10,10,10,0,0,10',
        f'{DAY} 09:17,nan,nan,10,10,0,0,10']}


def test_auction_indicative_vs_opening_and_original_timestamp_units():
    points=auc.parse(trends(),DAY)
    assert len(points)==2 and points[0]['kind']=='indicative'
    assert points[0]['volume_shares']==0
    assert points[1]['clock']=='09:26' and points[1]['kind']=='opening_result'
    assert points[1]['volume_shares']==2000 and points[1]['amount_yuan']==20400


def test_auction_fetch_archive_and_history_reads_only_archive(monkeypatch,tmp_path):
    monkeypatch.setattr(auc,'ARCHIVE',tmp_path)
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'data':trends()}
    calls=[]
    def request(url,**kw):calls.append(kw);return Response()
    monkeypatch.setattr(auc.requests,'get',request)
    body=auc.auction('000001.SZ',DAY)
    assert body['available'] and calls[0]['params']['iscr']==1
    assert json.loads((tmp_path/DAY/'000001.SZ.json').read_text())['points'][1]['clock']=='09:26'
    monkeypatch.setattr(lq,'now_cn',lambda: datetime(2026,10,19,10,tzinfo=lq.CN_TZ))
    lq.cache_clear()
    assert auc.auction('000001.SZ',DAY)['available'] and len(calls)==1
    assert not auc.auction('000002.SZ',DAY)['available']

@pytest.mark.parametrize('code',['../../outside','ABC','600001.XX'])
def test_auction_invalid_code_never_writes_outside_archive(code):
    assert auc.auction(code,DAY)['available'] is False


def test_live_quotes_cache_singleflight():
    calls=[]
    def build(): calls.append(1);time.sleep(.02);return {'ok':True},None
    with ThreadPoolExecutor(max_workers=4) as pool:
        out=list(pool.map(lambda _:lq._cached(('test-single',),10,build),range(4)))
    assert len(calls)==1 and all(x[0]['ok'] for x in out)


def test_all_intraday_consumers_use_common_auction_chart():
    for name in ['KlinePage','Intraday','LiveAccount']:
        text=(ROOT/f'frontend/src/pages/{name}.vue').read_text()
        assert '<IntradayChart' in text and ':code=' in text and ':date=' in text
    chart=(ROOT/'frontend/src/components/IntradayChart.vue').read_text()
    assert '/api/intraday/auction/' in chart and '09:15 竞价' in chart
    assert '/ 255' in chart and "p.volume_shares > 0" in chart and 'auctionPrev.value' in chart
    for name in ['Rankings','IndustryHeat','LimitUp']:
        text=(ROOT/f'frontend/src/pages/{name}.vue').read_text()
        assert 'RealtimeStatus' in text and 'useMarketPoll(' in text


def test_preopen_does_not_wait_for_regular_minute_sources(client,monkeypatch):
    monkeypatch.setattr(lq,'now_cn',lambda:datetime(2026,10,16,9,20,tzinfo=lq.CN_TZ))
    monkeypatch.setattr(ia,'_live_frame',lambda *a:pytest.fail('preopen requested regular minute'))
    monkeypatch.setattr(auc,'auction',lambda *a:{'available':True,'points':auc.parse(trends(),DAY),
        'prev_close':10.,'note':'fixture indicative'})
    ia.intraday.cache_clear()
    body=client.get('/api/intraday/000001.SZ?mode=live').json()
    assert body['date']==DAY and body['auction_only'] and body['points']==[]
    assert body['source']=='eastmoney_preopen'


def test_live_industry_heat_stocks_and_map_share_same_current_frame(client,monkeypatch):
    from backend.services import industry_heat_service as ihs
    qs={'000001.SZ':quote(),'000002.SZ':quote(price=9.,name='样本二')}
    snapshot=snap(qs)
    basics=pd.DataFrame({'name':['样本','样本二'],'market':['主板','主板'],'industry':['银行','证券'],
        'list_date':['19910101','19910101']},index=list(qs))
    hist=pd.concat([history().iloc[:2],history().iloc[:2].assign(stock_code='000002.SZ')],ignore_index=True)
    monkeypatch.setattr(mbs,'basic_frame',lambda:basics)
    monkeypatch.setattr(mbs,'_st_matrix',lambda *a:pd.DataFrame())
    monkeypatch.setattr(rt,'snapshot',lambda:snapshot)
    frame=rt.flag_current(rt.overlay(hist,qs,DAY),snapshot)
    monkeypatch.setattr(rt,'bars',lambda *a:frame)
    ihs.cache_clear()
    heat=client.get('/api/industry/heat?live=1').json()
    assert heat['market']['date']==DAY and heat['market']['source']=='eastmoney_snapshot'
    assert all(r['heat_delta'] is None for r in heat['rows'])
    stocks=client.get('/api/industry/银行/stocks?live=1').json()
    tree=client.get('/api/industry/heatmap?live=1').json()
    assert stocks['market']['date']==DAY==tree['date']
    assert heat['realtime']['asof']==stocks['realtime']['asof']==tree['realtime']['asof']
