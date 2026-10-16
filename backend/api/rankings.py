"""每日 / 每周「涨跌排行」与「资金流排行」API。

页面：`frontend/src/pages/Rankings.vue`（导航「行情 → 涨跌/资金排行」）。

数据口径（全部来自本地 parquet，**不依赖网络**）：
  * 涨跌排行：`assets/data/clean/ohlcv/market=CN/.../frequency=daily/adjust=qfq/`
    - `period=daily`  → 最近交易日单日涨跌幅（close/前收-1，**前复权口径**）
    - `period=weekly` → 近 5 个交易日累计（close/5 个交易日前 close-1）
  * 资金流排行：`assets/data/derived/cn_moneyflow_features.parquet`
    - `period=daily`  → 当日净流入额
    - `period=weekly` → 面板已算好的 **5 日累计**净流入额（不是前端再求和，避免口径漂移）
    - `source` 三选一：tushare 净流入额 / 东财主力净额 / 同花顺净额；原始单位万元，对外折算亿元。
  * `live=1` 时额外尝试东财实时资金流排行（盘中用），失败不影响主结果，只在 `live` 字段里说明原因
    —— 本机实测该接口走 push2 的 clist，代理经常掐断（见 `services/live_quotes.py`）。

只在**单一来源**上排序是刻意的：三家的"净流入"定义互不相同，混在一张榜里排名没有意义。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.api._history_date import history_date
from backend.services import realtime_market as rt
from backend.services import live_quotes
from backend.services import market_board_service as mbs

router = APIRouter(prefix="/api/rankings", tags=["rankings"])

NOTES = [
    "涨跌幅为**前复权**口径：除权除息日与交易所的不复权涨幅会有零点几个百分点的差（仓库内所有模块同口径）。",
    "资金流三家口径定义不同（tushare 净流入额 / 东财主力净额 / 同花顺净额），请不要跨口径比较名次。",
    "资金流面板按收盘后入库，当日数据要等晚上流水线跑完才出现；`live=1` 可尝试盘中实时接口。",
]


@router.get("/overview")
def rankings_overview() -> dict:
    """页面首屏要的元信息：最新交易日、可用日期、口径清单、交易时段。"""
    dates = [d.strftime("%Y-%m-%d") for d in mbs.trade_dates(120)]
    return {
        "latest_trade_date": dates[-1] if dates else None,
        "trade_dates": dates,
        "moneyflow_dates": mbs.moneyflow_dates(400),
        "moneyflow_sources": [{"key": k, "label": v} for k, v in mbs.MONEYFLOW_SOURCES.items()],
        "boards": ["主板", "创业板", "科创板", "北交所"],
        "periods": [{"key": "daily", "label": "每日"}, {"key": "weekly", "label": "每周（近 5 个交易日）"}],
        "session": live_quotes.trading_phase(),
        "notes": NOTES,
    }


@router.get("/change")
def rankings_change(
    period: str = Query("daily", pattern="^(daily|weekly)$"),
    direction: str = Query("up", pattern="^(up|down)$"),
    limit: int = Query(50, ge=1, le=500),
    board: str = Query(""),
    exclude_st: bool = Query(False),
    min_amount: float = Query(0.0, ge=0.0, description="最小成交额（元），0 = 不过滤"),
    live: int = Query(0, ge=0, le=1),
    date: str = Query("", description="截止交易日 YYYY-MM-DD，留空 = 最新"),
) -> dict:
    date = history_date(date)
    snap = rt.snapshot() if live and rt.eligible(date) else None
    daily = rt.bars(70, snap) if snap and snap['available'] else None
    rows, stats = mbs.change_rank(period=period, limit=limit, direction=direction,
                                  board=board, exclude_st=exclude_st, min_amount=min_amount,
                                  day=date or None, _daily=daily)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
        row["value"] = row["period_ret"]
        row["value_label"] = "涨跌幅"
        row["unit"] = "%"
    return {
        "realtime": rt.metadata(snap) if snap else {"available": False},
        "period": period,
        "direction": direction,
        "limit": limit,
        "filters": {"board": board, "exclude_st": exclude_st, "min_amount": min_amount},
        "date": stats.get("date"),
        "base_date": stats.get("base_date"),
        "span": stats.get("span"),
        "stats": stats,
        "rows": rows,
        "notes": NOTES[:1],
    }


@router.get("/moneyflow")
def rankings_moneyflow(
    period: str = Query("daily", pattern="^(daily|weekly)$"),
    direction: str = Query("in", pattern="^(in|out)$"),
    limit: int = Query(50, ge=1, le=500),
    source: str = Query("moneyflow"),
    date: str = Query(""),
    board: str = Query(""),
    exclude_st: bool = Query(False),
    live: int = Query(0, ge=0, le=1, description="1 = 额外尝试东财实时接口（盘中用）"),
) -> dict:
    date = history_date(date)
    if live and rt.eligible(date):
        snap = rt.snapshot()
        if snap['available'] and snap.get('flow_available'):
            history = {}
            if period == 'weekly':
                import pandas as pd
                dates = [str(pd.Timestamp(d).date()) for d in mbs.trade_dates(10) if pd.Timestamp(d) < pd.Timestamp(snap['date'])][-4:]
                from data.model import get_market_calendar
                previous = get_market_calendar('CN').get_last_trading_day(pd.Timestamp(snap['date']).date()-pd.Timedelta(days=1))
                if len(dates) == 4 and dates[-1] == str(pd.Timestamp(previous).date()):
                    try:
                        frames = [mbs.moneyflow_rows(d,'moneyflow_dc') for d in dates]
                        combined = pd.concat(frames).drop_duplicates(['stock_code','trade_date'])
                        grouped = combined.groupby('stock_code')['net_amount']
                        summed = grouped.sum(min_count=4)
                        history = summed[grouped.count() == 4].dropna().to_dict()
                    except (OSError, KeyError, ValueError):
                        history = {}
            basics = mbs.basic_frame(); rows = []
            for code,q in snap['quotes'].items():
                if q['net_amount'] is None or (period=='weekly' and code not in history): continue
                info = basics.loc[code] if code in basics.index else {}
                if board and info.get('market') != board: continue
                if exclude_st and mbs.is_st_name(q['name']): continue
                wan=q['net_amount']/10000; net5=wan+history[code] if code in history else None
                rows.append({'stock_code':code,'name':q['name'],'market':info.get('market',''),
                    'industry':info.get('industry',''),'close':q['price'],
                    'pct_chg':q['price']/q['prev_close']-1,'amount':q['amount'],
                    'boards':0,'is_st':mbs.is_st_name(q['name']),
                    'net_amount_wan':wan,'net_amount_yi':mbs.yi(wan),'net_5d_yi':mbs.yi(net5),
                    'net_10d_yi':None,'net_20d_yi':None,'value':mbs.yi(net5 if period=='weekly' else wan),
                    'value_label':'主力净额','unit':'亿元'})
            rows.sort(key=lambda r:r['value'],reverse=direction!='out')
            for i,row in enumerate(rows,1): row['rank']=i
            return {'available':bool(rows),'rows':rows[:limit], 'date':snap['date'],'dates':[snap['date']],
                'period':period,'direction':direction,'limit':limit,'source':'moneyflow_dc',
                'source_label':'东财实时主力净额','realtime':rt.metadata(snap),
                'stats':{'n_total':len(rows),'n_in':sum(r['net_amount_yi']>0 for r in rows),
                    'n_out':sum(r['net_amount_yi']<0 for r in rows), 'sum_net_yi':sum(r['net_amount_yi'] for r in rows),
                    'sum_net_5d_yi':sum(r['net_5d_yi'] for r in rows) if period=='weekly' else None},
                'hint':None if rows else '缺少完整连续的前四个交易日同源资金数据，周排名留空',
                'notes':['实时资金统一使用东财主力净额，与 Tushare/同花顺盘后口径独立。周资金需有前四个交易日的同源数据。']}
        # Keep archived date and expose failed live status rather than relabeling it today.
        archived = rankings_moneyflow(period,direction,limit,source,date,board,exclude_st,0)
        archived['realtime'] = rt.metadata(snap)
        return archived
    available = mbs.moneyflow_dates(400)
    day = date or (available[-1] if available else "")
    if not day:
        return {"available": False, "rows": [],
                "hint": "资金流面板缺失：先跑 scripts/build_capital_flow_locks_features.py 或每日流水线"}
    rows, stats = mbs.moneyflow_rank(day=day, period=period, limit=limit, direction=direction,
                                     source=source, board=board, exclude_st=exclude_st)

    # 单位换算：面板是万元，榜单统一用亿元（原值也保留，便于逐股核对）
    for i, row in enumerate(rows, 1):
        row["rank"] = i
        row["net_amount_wan"] = row["net_amount"]
        row["net_5d_wan"] = row["net_5d"]
        row["value"] = mbs.yi(row["net_period"])
        row["net_amount_yi"] = mbs.yi(row["net_amount"])
        row["net_5d_yi"] = mbs.yi(row["net_5d"])
        row["net_10d_yi"] = mbs.yi(row["net_10d"])
        row["net_20d_yi"] = mbs.yi(row["net_20d"])
        row["value_label"] = "净流入额"
        row["unit"] = "亿元"
    if stats:
        stats["sum_net_yi"] = mbs.yi(stats.get("sum_net"))
        stats["sum_net_5d_yi"] = mbs.yi(stats.get("sum_net_5d"))

    live_block = {"requested": bool(live), "available": False, "reason": None, "rows": []}
    if live and rt.eligible(date):
        frame, error = live_quotes.fund_flow_rank("今日" if period == "daily" else "5日")
        if frame is None:
            live_block["reason"] = error
        else:
            live_block["available"] = True
            live_block["columns"] = [str(c) for c in frame.columns]
            live_block["rows"] = frame.head(int(limit)).to_dict("records")

    return {
        "available": bool(rows) or bool(stats),
        "period": period,
        "direction": direction,
        "limit": limit,
        "source": source,
        "source_label": mbs.MONEYFLOW_SOURCES.get(source, source),
        "date": day,
        "dates": available[-30:],
        "stats": stats,
        "rows": rows,
        "live": live_block,
        "notes": NOTES[1:],
    }
