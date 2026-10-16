"""K-line intraday date/race regressions; run against a built server or Vite.
KLINE_TEST_URL=http://127.0.0.1:5174 uv run --no-sync python -m pytest -q test/test_kline_today_browser.py
All /api responses are deterministic; no provider network calls.
"""
import asyncio
import json
import os
from urllib.parse import urlsplit,parse_qs

import pytest
from playwright.async_api import async_playwright

TODAY='2026-10-16'
HISTORY='2026-09-30'
CODE='000001.SZ'
OTHER='600519.SH'
BASE=os.environ.get('KLINE_TEST_URL','http://127.0.0.1:8000')


def bars(code):
    return {'code':code,'name':code,'data':[{'time':HISTORY,'open':10,'high':11,'low':9,'close':10,
      'volume':1000,'buy_signal':False,'sell_signal':False}],
      'latest':{'close':10,'change_pct':0,'total_bars':1},'signal_stats':None,'chips':None}


def coverage():
    return {'today':TODAY,'store':{'available':True,'first_date':HISTORY,'last_date':HISTORY,'n_dates':1},
      'store_dates':[HISTORY],'live':{'available':False,'dates':[]},'session':{'phase':'am'}}


def intraday(code,day):
    return {'code':code,'name':code,'date':day,'period':'1','source':'tx_minute' if day==TODAY else 'store',
      'source_label':'腾讯分时' if day==TODAY else '本地 5 分钟','prev_close':10,
      'points':[{'time':day+' 09:31:00','clock':'09:31','price':11,'avg':11,'open':11,'high':11,'low':11,
        'volume_shares':100,'amount_yuan':1100}],
      'latest':{'price':11,'change_pct':.1,'change':1,'avg':11,'high':11,'low':11,'open':11,
        'amount_yuan':1100,'volume_shares':100,'points':1},
      'live':{'available':day==TODAY,'reason':None},'session':{'phase':'am'},'store':{},'notes':[]}


async def scenario(case):
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        page=await browser.new_page()
        # Fixed CN trading session, independent of host timezone/day.
        await page.clock.install(time=__import__('datetime').datetime(2026,10,16,2,0,tzinfo=__import__('datetime').timezone.utc))
        calls=[]; errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def api(route):
            url=urlsplit(route.request.url); path=url.path;q=parse_qs(url.query)
            if not path.startswith('/api/'):
                await route.continue_();return
            payload={}
            if path.endswith('/ohlcv'): payload=bars(path.split('/')[-2])
            elif '/intraday/coverage/' in path:
                await asyncio.sleep(1.2 if case=='slow_coverage' else .05)
                payload=coverage()
            elif '/intraday/daily/' in path:payload={'available':False}
            elif '/intraday/auction/' in path:payload={'available':False,'reason':'fixture','points':[]}
            elif '/api/intraday/' in path and path.split('/')[-1] in [CODE,OTHER]:
                code=path.split('/')[-1]; day=q.get('date',[TODAY])[0]
                calls.append({'code':code,'date':day,'mode':q.get('mode',['auto'])[0]})
                if case=='stale_response' and code==CODE:await asyncio.sleep(1)
                if case=='failure' and day==TODAY:
                    await route.fulfill(status=404,json={'detail':'今日无有效分时'});return
                payload=intraday(code,day)
            elif path=='/api/rankings/overview':payload={'session':{'phase':'am'}}
            elif path.endswith('/wyckoff'):payload={'available':False,'reason':'fixture'}
            elif path=='/api/stocks':payload={'stocks':[]}
            elif '/selection' in path:payload={'rows':[],'records':[],'results':[]}
            elif path=='/health':payload={'status':'ok','frontend':{}}
            await route.fulfill(status=200,json=payload)
        await page.route('**/api/**',api)
        suffix='&date='+HISTORY if case in ['explicit_history','switch_stock','return_today','route_remove'] else ''
        await page.goto(BASE+'/#/kline?code='+CODE+suffix)
        await page.locator('.control-panel').wait_for(timeout=15000)
        await page.wait_for_timeout(1800)
        if case=='slow_coverage':
            assert calls and all(c['date']==TODAY for c in calls),calls
            assert all(c['mode']=='live' for c in calls),calls
            assert 'date='+HISTORY not in page.url,page.url
        elif case=='explicit_history':
            assert any(c['date']==HISTORY for c in calls),calls
            assert HISTORY in await page.locator('.intraday-panel h6').inner_text()
        elif case in ['switch_stock','stale_response']:
            field=page.get_by_placeholder('如 00700 / 600861 / 301230.SZ')
            if case=='stale_response':
                # Start a second old request, then immediately switch stock.
                await page.evaluate("location.hash='/kline?code=000001.SZ&date=2026-09-30'")
                await page.wait_for_timeout(100)
            await field.fill(OTHER);await field.press('Enter')
            await page.wait_for_timeout(1600)
            other=[c for c in calls if c['code']==OTHER]
            assert other and all(c['date']==TODAY for c in other),calls
            assert OTHER in await page.locator('.intraday-head').inner_text()
            assert TODAY in await page.locator('.intraday-panel h6').inner_text()
            assert 'date='+HISTORY not in page.url,page.url
        elif case=='return_today':
            await page.get_by_role('button',name='返回当日实时',exact=True).click()
            await page.wait_for_timeout(200)
            assert calls[-1]['date']==TODAY and calls[-1]['mode']=='live',calls
            assert 'date=' not in page.url,page.url
            assert '当日实时分时' in await page.locator('.intraday-panel h6').inner_text()
        elif case=='route_remove':
            await page.evaluate("location.hash='/kline?code=000001.SZ'")
            await page.wait_for_timeout(300)
            assert calls[-1]['date']==TODAY,calls
        elif case=='failure':
            assert calls and all(c['date']==TODAY for c in calls),calls
            assert await page.locator('.intraday-panel .ic').count()==0
            assert '今日无有效分时' in await page.locator('.intraday-panel').inner_text()
        elif case=='poll':
            before=len(calls)
            await page.clock.fast_forward(31000)
            await page.wait_for_timeout(150)
            assert len(calls)>before,calls
            assert all(c['date']==TODAY and c['mode']=='live' for c in calls),calls
        # Other stock-info mock responses are intentionally empty; capture app crashes separately.
        assert not errors,errors
        await browser.close()
        return calls


@pytest.mark.parametrize('case',['slow_coverage','explicit_history','switch_stock','return_today','route_remove','stale_response','failure','poll'])
def test_kline_today(case):
    asyncio.run(scenario(case))
