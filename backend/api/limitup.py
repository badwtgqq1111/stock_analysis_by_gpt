"""连板记录统计 API（一板 / 两板 / 三板 / 四板+）。

页面：`frontend/src/pages/LimitUp.vue`（导航「行情 → 连板统计」）。

两个数据源、两种角色：
  1. **本地日线**（`market_board_service`）：离线可算，覆盖任意历史区间，用来做
     "近 N 个交易日的阶梯走势 + 晋级率"这类统计。涨停判定按板块上限（10/20/30%）
     加**按价格浮动的容差**（交易所涨停价四舍五入到分，见 service 头部标定说明）。
     2026-10-08 与东财涨停池逐只对账：本地 44 只 vs 东财 43 只，重合 42 只，
     多出的 2 只是东财不收录的 ST 涨停，漏的 1 只是本地日线里没有的北交所新股。
  2. **东财涨停池**（`live_quotes.zt_pool`）：盘中实时，且能给出本地日线**算不出来**的
     封板资金 / 首次封板时间 / 炸板次数 / 涨停统计。`detail` 会把它按代码并进本地明细，
     `live` 字段里说明成功与否 —— 页面因此在开盘时也能看到当天（而不是昨天）的连板梯队。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.services import live_quotes
from backend.services import market_board_service as mbs

router = APIRouter(prefix="/api/limitup", tags=["limitup"])

BUCKET_LABELS = {"b1": "一板", "b2": "两板", "b3": "三板", "b4p": "四板+"}


@router.get("/ladder")
def limitup_ladder(lookback: int = Query(30, ge=3, le=120)) -> dict:
    """近 N 个交易日的连板阶梯：逐日各板家数、当日最高板、昨日→今日晋级率。"""
    data = mbs.ladder(lookback)
    latest = data["series"][-1] if data["series"] else None
    return {
        "buckets": [{"key": k, "label": BUCKET_LABELS[k]} for k in data["buckets"]],
        "dates": data["dates"],
        "series": data["series"],
        "latest": latest,
        "lookback": lookback,
        "source": "local",
        "notes": [
            "连板数 = 同一只票连续涨停的交易日数（当天没涨停则不计入）。",
            "晋级率 = 昨日恰好 N 板、今日变成 N+1 板及以上的家数 ÷ 昨日恰好 N 板的家数。",
            "涨停判定：板块上限（主板 10% / 创业板·科创板 20% / 北交所 30%）+ 按价格浮动的舍入容差。",
        ],
    }


@router.get("/detail")
def limitup_detail(
    date: str = Query("", description="交易日 YYYY-MM-DD，留空 = 最新可用"),
    limit: int = Query(300, ge=1, le=1000),
    live: str = Query("auto", pattern="^(auto|1|0)$"),
) -> dict:
    """某日涨停明细（含连板数），并把东财涨停池的封板资金/首封时间并进来。"""
    dates = [d.strftime("%Y-%m-%d") for d in mbs.trade_dates(30)]
    day = date or (dates[-1] if dates else "")
    if not day:
        return {"available": False, "rows": [], "hint": "本地日线不可用，先跑数据同步脚本"}

    local_rows = mbs.limit_up_detail(day, limit=1000)
    local_map = {r["stock_code"]: r for r in local_rows}

    pool, live_error = (None, None)
    if live != "0":
        pool, live_error = live_quotes.zt_pool(day)
    live_used = bool(pool)

    merged: list[dict] = []
    for code, row in local_map.items():
        item = dict(row)
        item["source"] = "local"
        info = (pool or {}).get(code)
        if info:
            item["source"] = "both"
            item["boards_live"] = int(info["boards"]) if info.get("boards") is not None else None
            item["seal_amount"] = info.get("seal_amount")
            item["first_seal"] = info.get("first_seal")
            item["last_seal"] = info.get("last_seal")
            item["open_times"] = info.get("open_times")
            item["stat"] = info.get("stat")
            item["industry"] = info.get("industry") or ""
        merged.append(item)

    # 东财池里、本地日线没有的票（新股/北交所/日线缺口）也要出现在榜上，否则"漏报"就悄悄消失了
    for code, info in (pool or {}).items():
        if code in local_map:
            continue
        merged.append({
            "stock_code": code,
            "name": info.get("name") or "",
            "market": "",
            "boards": int(info["boards"]) if info.get("boards") is not None else 0,
            "bucket": mbs.board_bucket(int(info["boards"] or 0)),
            "close": info.get("close"),
            "pct_chg": (info.get("pct_chg") / 100.0) if info.get("pct_chg") is not None else None,
            "amount": info.get("amount"),
            "st_pit": False,
            "source": "live",
            "boards_live": int(info["boards"]) if info.get("boards") is not None else None,
            "seal_amount": info.get("seal_amount"),
            "first_seal": info.get("first_seal"),
            "last_seal": info.get("last_seal"),
            "open_times": info.get("open_times"),
            "stat": info.get("stat"),
            "industry": info.get("industry") or "",
        })

    merged.sort(key=lambda r: (-int(r["boards"] or 0), -(r.get("amount") or 0)))
    counts = {k: 0 for k in BUCKET_LABELS}
    for row in merged:
        bucket = mbs.board_bucket(int(row["boards"] or 0))
        if bucket:
            counts[bucket] += 1
    for i, row in enumerate(merged, 1):
        row["rank"] = i

    return {
        "available": bool(merged),
        "date": day,
        "dates": dates,
        "counts": counts,
        "counts_label": {BUCKET_LABELS[k]: v for k, v in counts.items()},
        "total": len(merged),
        "max_boards": max([int(r["boards"] or 0) for r in merged], default=0),
        "rows": merged[: int(limit)],
        "live": {"requested": live != "0", "available": live_used, "reason": live_error},
        "session": live_quotes.trading_phase(),
        "notes": [
            "封板资金 / 首次封板时间 / 炸板次数 只有东财涨停池能给（本地日线算不出来），"
            "行里 `source=both` 表示本地与东财都有、`source=live` 表示本地日线缺失（北交所新股等）。",
            "连板数以 10% / 20% / 30% 板块上限判定；ST 用 PIT 标记展示，但不改变上限（见后端注释）。",
        ],
    }


@router.get("/live")
def limitup_live() -> dict:
    """盘中实时连板梯队（东财涨停池）。失败时返回原因，页面据此显示"实时不可用"。"""
    phase = live_quotes.trading_phase()
    day = phase["now"][:10]
    payload = {"date": day, "session": phase, "available": False, "reason": None,
               "rows": [], "counts": {k: 0 for k in BUCKET_LABELS},
               "counts_label": {v: 0 for v in BUCKET_LABELS.values()}, "total": 0, "max_boards": 0}

    pool, error = live_quotes.zt_pool(day)
    if pool is None:
        # 今天没有（非交易日/未开盘）→ 顺延回看最近几个自然日，避免页面空着
        for candidate in live_quotes.recent_day_candidates(back=7)[1:]:
            pool, error = live_quotes.zt_pool(candidate)
            if pool:
                payload["date"] = candidate
                break
    if pool is None:
        payload["reason"] = error
        return payload

    rows = []
    for code, info in pool.items():
        boards = int(info.get("boards") or 0)
        rows.append({
            "stock_code": code,
            "name": info.get("name") or "",
            "industry": info.get("industry") or "",
            "boards": boards,
            "bucket": mbs.board_bucket(boards),
            "close": info.get("close"),
            "pct_chg": (info.get("pct_chg") / 100.0) if info.get("pct_chg") is not None else None,
            "amount": info.get("amount"),
            "seal_amount": info.get("seal_amount"),
            "first_seal": info.get("first_seal"),
            "open_times": info.get("open_times"),
            "stat": info.get("stat"),
        })
    rows.sort(key=lambda r: (-r["boards"], -(r.get("amount") or 0)))
    counts = {k: 0 for k in BUCKET_LABELS}
    for row in rows:
        bucket = mbs.board_bucket(row["boards"])
        if bucket:
            counts[bucket] += 1
    payload.update({
        "available": True,
        "rows": rows,
        "counts": counts,
        "counts_label": {BUCKET_LABELS[k]: v for k, v in counts.items()},
        "total": len(rows),
        "max_boards": max([r["boards"] for r in rows], default=0),
    })
    return payload
