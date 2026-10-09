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
    dates = [d.strftime("%Y-%m-%d") for d in mbs.trade_dates(30)]
    return {
        "latest_trade_date": dates[-1] if dates else None,
        "trade_dates": dates,
        "moneyflow_dates": mbs.moneyflow_dates(30),
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
) -> dict:
    rows, stats = mbs.change_rank(period=period, limit=limit, direction=direction,
                                  board=board, exclude_st=exclude_st, min_amount=min_amount)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
        row["value"] = row["period_ret"]
        row["value_label"] = "涨跌幅"
        row["unit"] = "%"
    return {
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
    if live:
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
