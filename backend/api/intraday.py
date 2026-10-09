"""分时行情 API（当日 1 分钟 / 5 分钟，web 实时显示）。

页面：`frontend/src/pages/Intraday.vue`（导航「行情 → 分时行情」）。

三条通路，`source` 字段明确写清这次用的是哪条（前端在标题旁显示角标，避免把旧数据当实时价）：

  * `tx_minute` 腾讯 `web.ifzq.gtimg.cn/.../minute/query` —— **默认首选**。实测稳定
    （0.2s、含盘中数据），返回当日逐分钟价 + 累计成交量/成交额，可算出精确均价。
    只能看当天，没有历史。
  * `em`       东财 `stock_zh_a_hist_min_em`（1/5 分钟）。能看到最近几个交易日、
    自带官方均价；但本机走代理时 `push2his` 会间歇性 ProxyError（同一分钟内时好时坏）。
  * `tx_kline` 腾讯 5 分钟 K（`mkline`）——5 分钟口径的兜底；该接口不给成交额，均价留空。
  * `store`    本地 `assets/data/raw/intraday_baostock/frequency=5min/<code>.parquet`（离线，
    由 `scripts/sync_cn_intraday_full.py --refresh` 收盘后补齐；实测止于 2026-09-30）。

`mode` 控制顺序：`auto`（默认）| `live`（只用网络源）| `store`（只用本地）。
`auto` 的取舍（K 线页点某一天看分时就是这个场景）：
  * 目标日在本地归档里（`/api/intraday/coverage/{code}` 给出覆盖区间）→ **先本地**。
    本地是前复权价，和 K 线页用的是同一套价格，蜡烛图上的高价/低价能与分时对得上；
    网络源是不复权价，除权日会对不齐。
  * 目标日不在本地（最近几个交易日 / 当天）→ 先网络，失败再退本地。

单位陷阱（实测踩过）：
  * 东财/腾讯行情的成交量是**手**（腾讯 0931 行：62344 手 × 均价 11.835 ≈ 7,379 万元）；
  * baostock 5 分钟的 `volume` 是**股**。
  对外统一成 `volume_shares`（股）与 `amount_yuan`（元）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from backend.services import live_quotes
from backend.services import market_board_service as mbs

router = APIRouter(prefix="/api/intraday", tags=["intraday"])

NOTES = [
    "均价 = 当日累计成交额 ÷ 累计成交量；东财自带该字段，腾讯分时由累计额/累计量算出。",
    "前收 = 本地日线里该日之前的最后一个收盘价（前复权口径），用于算涨跌幅与分时基准线。",
    "成交量统一折算成股：东财/腾讯给的是手（×100），本地 baostock 本来就是股。",
]

_POOL = ThreadPoolExecutor(max_workers=6)


def _f(value, nd: int = 4):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return round(value, nd)


def _daily_history(code: str, day: pd.Timestamp | None):
    """日线（前复权），窗口**按目标日回溯**。

    K 线页点一根一年前的 K 线时，固定 90 天窗口里根本没有那一天 —— 前收会变成 None
    （实测踩过：2025-10-27 的分时面板显示"前收 —"，涨跌幅也没法算，整条分时线失去基准）。
    """
    if day is None:
        days = 90
    else:
        back = (pd.Timestamp.now().normalize() - pd.Timestamp(day).normalize()).days
        days = max(int(back) + 30, 90)
    try:
        from backend.services.data_service import load_cn_stock_ohlcv
        frame = load_cn_stock_ohlcv(code, days=days)
    except Exception:                                        # noqa: BLE001
        return None
    if frame is None or frame.empty:
        return None
    return frame


def _prev_close(code: str, day: pd.Timestamp | None) -> float | None:
    """该交易日之前的最后一个日线收盘价。"""
    frame = _daily_history(code, day)
    if frame is None:
        return None
    if day is not None:
        frame = frame[frame.index < pd.Timestamp(day)]
    if frame.empty:
        return None
    return float(frame["Close"].iloc[-1])


def _align_store_to_daily(code: str, frame: pd.DataFrame, day: str) -> tuple[pd.DataFrame, dict]:
    """把本地归档的分时**对齐到日线的价格刻度**（K 线页必须做这一步）。

    本机实测：本地 5 分钟归档（baostock 前复权，2026-10-06 同步）与日线面板
    （前复权，2026-10-09 重建）**复权基准不同**，同一交易日整段平移一个常数因子：

        2025-10-27  日线收 11.150 | 分时末根 10.913 | 比值 1.0218
        2026-08-20  日线收 11.400 | 分时末根 11.154 | 比值 1.0220
        2026-09-30  日线收 11.570 | 分时末根 11.570 | 比值 1.0000

    两个序列**各自的日环比完全一致**（-0.0035 vs -0.0036），所以按日线的当日收盘价
    除一次就够：对齐后 2025-10-27 的分时 开/高/低/收 = 11.17 / 11.24 / 11.07 / 11.15，
    与当天那根 K 线（11.17 / 11.24 / 11.08 / 11.15）逐项对上。
    不这么做的话，用户点开一根 K 线看到的分时价格会比蜡烛低 2% 左右。

    **均价要单独处理**：baostock 的 `amount` 是真实成交金额、`volume` 是真实股数，
    两者相除得到的是**不复权**VWAP（2025-10-27：11.523），而 OHLC 列是前复权价
    （同日 10.916）—— 直接用累计额÷累计量算均价，会在图上画出一条高出当日最高价
    5% 的假均价线。这里用当日各根的"复权价 ÷ 不复权VWAP"按量加权估出换算系数 K，
    再把VWAP折算到与 OHLC 同一刻度。
    """
    meta = {"aligned": False, "factor": None, "store_close": None, "daily_close": None}
    if frame.empty:
        return frame, meta
    store_close = float(frame["close"].iloc[-1])
    meta["store_close"] = round(store_close, 4)
    history = _daily_history(code, pd.Timestamp(day))
    daily_close = None
    if history is not None and pd.Timestamp(day) in history.index:
        daily_close = float(history.loc[pd.Timestamp(day), "Close"])
    meta["daily_close"] = None if daily_close is None else round(daily_close, 4)
    if daily_close is None or store_close <= 0:
        return frame, meta
    factor = daily_close / store_close
    aligned = frame.copy()
    for column in ("open", "high", "low", "close"):
        aligned[column] = aligned[column] * factor
    meta["aligned"] = True
    meta["factor"] = round(factor, 6)

    volume = aligned["volume_lots"].astype(float)
    raw_vwap = aligned["amount"].astype(float) / volume.replace(0, float("nan"))
    ratio = aligned["close"] / raw_vwap
    weight = volume.where(ratio.notna() & (volume > 0))
    if weight is not None and weight.sum() > 0:
        k = float((ratio.fillna(0) * weight.fillna(0)).sum() / weight.sum())
        if k == k and k > 0:
            cum_amount = aligned["amount"].astype(float).cumsum()
            cum_volume = volume.cumsum()
            aligned["avg"] = (cum_amount / cum_volume * k).where(cum_volume > 0)
            meta["avg_scale"] = round(k, 6)
    return aligned, meta


def _points(frame: pd.DataFrame, source: str) -> list[dict]:
    """统一成前端要的形状：价 / 均价 / 单根量额（股、元）。

    源里没给均价（本地 5 分钟、腾讯 5 分钟 K）时，用**累计成交额 ÷ 累计股数**补出来，
    与东财口径一致；成交额也缺（腾讯 K 线）就留 None，前端把均价线藏掉而不是画条假线。
    """
    points = []
    cum_volume = cum_amount = 0.0
    for _, row in frame.iterrows():
        lots = _f(row.get("volume_lots"), 2) or 0.0
        amount = _f(row.get("amount"), 2)
        volume_shares = int(lots) if source == "store" else int(round(lots * 100))
        avg = _f(row.get("avg"))
        if amount is not None:
            cum_volume += volume_shares
            cum_amount += amount
            if avg is None and cum_volume > 0:
                avg = round(cum_amount / cum_volume, 4)
        points.append({
            "time": pd.Timestamp(row["clock"]).strftime("%Y-%m-%d %H:%M:%S"),
            "clock": pd.Timestamp(row["clock"]).strftime("%H:%M"),
            "price": _f(row.get("close")),
            "avg": avg,
            "open": _f(row.get("open")),
            "high": _f(row.get("high")),
            "low": _f(row.get("low")),
            "volume_shares": volume_shares,
            "amount_yuan": amount,
        })
    return points


def _store_frame(code: str, day: str | None) -> pd.DataFrame:
    raw = mbs.load_intraday_store(code, day or None)
    if raw.empty:
        return raw
    frame = raw.rename(columns={"close": "close"}).copy()
    frame["volume_lots"] = frame["volume"]
    frame["avg"] = None
    frame["amount"] = frame["amount"]
    return frame[["date", "clock", "open", "high", "low", "close", "volume_lots", "amount", "avg"]]


def _live_frame(code: str, period: str, date: str) -> tuple[pd.DataFrame | None, str | None, str | None]:
    """网络源依次尝试：腾讯当日分时 → 东财 1/5 分钟 → 腾讯 5 分钟 K。"""
    errors: list[str] = []
    today = live_quotes.now_cn().strftime("%Y-%m-%d")

    if period == "1" and (not date or date == today):
        frame, error = live_quotes.tencent_minute(code)
        if frame is not None and not frame.empty:
            return frame, "tx_minute", None
        errors.append(f"腾讯当日分时：{error}")

    frame, error = live_quotes.intraday_minute(code, period)
    if frame is not None and not frame.empty:
        frame = frame.rename(columns={"volume": "volume_lots"}) if "volume" in frame.columns else frame
        return frame, "em", None
    errors.append(f"东财 {period} 分钟：{error}")

    frame, error = live_quotes.tencent_kline(code, "m5")
    if frame is not None and not frame.empty:
        return frame, "tx_kline", None
    errors.append(f"腾讯 5 分钟 K：{error}")
    return None, None, " ｜ ".join(errors)


def _summarize(points: list[dict], prev_close: float | None) -> dict:
    if not points:
        return {}
    price = points[-1]["price"]
    prices = [p["price"] for p in points if p["price"] is not None]
    change = None if (price is None or not prev_close) else round(price - prev_close, 4)
    change_pct = None if (price is None or not prev_close) else round(price / prev_close - 1, 5)
    amounts = [p["amount_yuan"] for p in points if p["amount_yuan"] is not None]
    return {
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "avg": points[-1]["avg"],
        "high": max(prices) if prices else None,
        "low": min(prices) if prices else None,
        "open": points[0].get("open") or points[0]["price"],
        "volume_shares": int(sum(p.get("volume_shares") or 0 for p in points)),
        "amount_yuan": round(sum(amounts), 2) if amounts else None,
        "points": len(points),
        "time": points[-1]["time"],
    }


@router.get("/quotes")
def intraday_quotes(codes: str = Query(..., description="逗号分隔的股票代码")) -> dict:
    """批量最新快照（分时页顶部的快捷条）。腾讯一次请求可带多只，比逐只快得多。"""
    wanted = [c.strip().upper() for c in str(codes).split(",") if c.strip()][:40]
    session = live_quotes.trading_phase()
    if not wanted:
        return {"quotes": [], "session": session}

    payload, tx_error = live_quotes.tencent_quotes(wanted)
    quotes = []
    missing = []
    for code in wanted:
        symbol = live_quotes.to_ts_code(code).split(".")[0]
        info = payload.get(symbol)
        if not info:
            quotes.append({"stock_code": code, "available": False, "reason": tx_error})
            missing.append(code)
            continue
        prev = info.get("prev_close")
        price = info.get("price")
        quotes.append({
            "stock_code": live_quotes.to_ts_code(code),
            "name": info.get("name"),
            "available": True,
            "source": "tx_quote",
            "price": price,
            "prev_close": prev,
            "change": info.get("change"),
            "change_pct": info.get("change_pct"),
            "high": info.get("high"),
            "low": info.get("low"),
            "open": info.get("open"),
            "amount_yuan": (info.get("amount_wan") or 0) * 10_000 if info.get("amount_wan") else None,
            "volume_shares": int((info.get("volume_lots") or 0) * 100),
            "time": info.get("time"),
        })

    # 腾讯缺的票用东财分时逐个补齐（保持顺序一致）
    if missing:
        def one(code: str) -> dict:
            frame, error = live_quotes.intraday_minute(code, "1")
            if frame is None or frame.empty:
                return {"stock_code": code, "available": False, "reason": error}
            last_day = pd.Timestamp(frame["clock"].iloc[-1]).normalize()
            sub = frame[frame["clock"].dt.normalize() == last_day]
            prev = _prev_close(code, last_day)
            points = _points(sub.rename(columns={"volume": "volume_lots"}), "em")
            return {"stock_code": live_quotes.to_ts_code(code), "available": True, "source": "em",
                    "latest": _summarize(points, prev), "prev_close": _f(prev)}

        for result in _POOL.map(one, missing):
            for i, quote in enumerate(quotes):
                if quote["stock_code"] == live_quotes.to_ts_code(result.get("stock_code") or ""):
                    merged = dict(result)
                    merged["stock_code"] = quote["stock_code"]
                    latest = merged.get("latest") or {}
                    merged.update({
                        "price": latest.get("price"), "change": latest.get("change"),
                        "change_pct": latest.get("change_pct"), "high": latest.get("high"),
                        "low": latest.get("low"), "open": latest.get("open"),
                        "amount_yuan": latest.get("amount_yuan"),
                        "volume_shares": latest.get("volume_shares"),
                        "time": latest.get("time"),
                    })
                    quotes[i] = merged
                    break
    return {"quotes": quotes, "session": session}


@router.get("/coverage/{code}")
def intraday_coverage(code: str) -> dict:
    """该票的分时可用范围：本地 5 分钟归档的日期清单 + 实时源能给出的日期。

    K 线页用它来判断"点这一天有没有分时"：本地归档覆盖哪段、实时源覆盖哪几天，
    点不出来的日子直接在页面上说明原因，而不是发一个注定 404 的请求。
    """
    ts_code = live_quotes.to_ts_code(code)
    store = mbs.intraday_store_coverage(ts_code)
    live_dates: list[str] = []
    live_error = None
    frame, error = live_quotes.intraday_minute(ts_code, "1")
    if frame is None or frame.empty:
        frame, error2 = live_quotes.tencent_kline(ts_code, "m5")
        if frame is None or frame.empty:
            live_error = error or error2
        else:
            live_dates = sorted({pd.Timestamp(c).strftime("%Y-%m-%d") for c in frame["clock"]})
    else:
        live_dates = sorted({pd.Timestamp(c).strftime("%Y-%m-%d") for c in frame["clock"]})
    return {
        "code": ts_code,
        "today": live_quotes.now_cn().strftime("%Y-%m-%d"),
        "store": {k: store[k] for k in ("available", "first_date", "last_date", "n_dates")},
        "store_dates": store["dates"],
        "live": {"available": bool(live_dates), "dates": live_dates, "reason": live_error},
        "session": live_quotes.trading_phase(),
        "notes": ["本地 5 分钟为前复权价（与 K 线同口径），实时源为不复权价，除权日两者会有价差。"],
    }


@router.get("/{code}")
def intraday(
    code: str,
    period: str = Query("1", pattern="^(1|5)$"),
    mode: str = Query("auto", pattern="^(auto|live|store)$"),
    date: str = Query("", description="留空 = 该来源的最新一天"),
) -> dict:
    """单只股票的分时序列（含均价线），供分时图与实时刷新使用。"""
    ts_code = live_quotes.to_ts_code(code)
    name = ""
    basics = mbs.basic_frame()
    if not basics.empty and ts_code in basics.index:
        name = str(basics.loc[ts_code, "name"])

    coverage = mbs.intraday_store_coverage(ts_code)
    store_dates = set(coverage["dates"] or [])
    store_has_date = bool(date) and date in store_dates

    # 见模块 docstring：auto 模式下"目标日本地有全历史"时先本地（前复权，价格与 K 线一致）
    if mode == "store":
        order = ["store"]
    elif mode == "live":
        order = ["live"]
    else:
        order = ["store", "live"] if store_has_date else ["live", "store"]

    source = ""
    points: list[dict] = []
    live_error: str | None = None
    error: str | None = None
    store_align: dict = {}

    for step in order:
        if step == "live":
            frame, live_source, reason = _live_frame(ts_code, period, date)
            live_error = live_error or reason
            if frame is None:
                continue
            if date:
                frame = frame[frame["clock"].dt.strftime("%Y-%m-%d") == date]
            elif live_source in ("em", "tx_kline"):
                # 这两个源会返回多个交易日，默认只画最新一天
                last_day = pd.Timestamp(frame["clock"].iloc[-1]).normalize()
                frame = frame[frame["clock"].dt.normalize() == last_day]
            if frame.empty:
                error = f"{ts_code} 在 {date or '最新交易日'} 没有分时数据"
                continue
            points = _points(frame, live_source)
            source = live_source
        else:
            frame = _store_frame(ts_code, date or None)
            if frame.empty:
                continue
            day_str = str(frame["date"].iloc[-1])
            frame, align_meta = _align_store_to_daily(ts_code, frame, day_str)
            store_align = align_meta
            points = _points(frame, "store")
            source = "store"
        if points:
            break

    if points:
        error = None

    if not points:
        coverage_text = (
            f"本地 5 分钟归档覆盖 {coverage['first_date']} ~ {coverage['last_date']}"
            if coverage["available"] else "本地 5 分钟归档里没有这只票"
        )
        raise HTTPException(status_code=404, detail={
            "message": (f"{error}（{coverage_text}；实时源只覆盖最近几个交易日）"
                        if error else f"{ts_code} 没有可用的分时数据（{coverage_text}）"),
            "live_error": live_error,
            "store_coverage": {k: coverage[k] for k in ("available", "first_date", "last_date", "n_dates")},
            "hint": "三条网络源都不可用且本地分时未同步时会出现；"
                    "本地同步：uv run python scripts/sync_cn_intraday_full.py --refresh",
        })

    day = pd.Timestamp(points[-1]["time"]).normalize()
    prev_close = _prev_close(ts_code, day)
    store_path = mbs.intraday_store_path(ts_code)
    return {
        "code": ts_code,
        "name": name,
        "period": "1" if source in ("tx_minute", "em") and period == "1" else ("5" if source in ("tx_kline", "store") else period),
        "requested_period": period,
        "mode": mode,
        "source": source,
        "source_label": {"tx_minute": "腾讯分时", "em": "东财分时",
                         "tx_kline": "腾讯 5 分钟 K", "store": "本地 5 分钟"}.get(source, source),
        "date": day.strftime("%Y-%m-%d"),
        "prev_close": _f(prev_close),
        "latest": _summarize(points, prev_close),
        "points": points,
        "session": live_quotes.trading_phase(),
        "live": {"available": source in ("tx_minute", "em", "tx_kline"), "reason": live_error},
        "store": {"available": store_path is not None,
                  "path": str(store_path) if store_path else None,
                  "first_date": coverage["first_date"],
                  "last_date": coverage["last_date"],
                  "n_dates": coverage["n_dates"],
                  "has_date": bool(date) and date in store_dates,
                  "align": store_align or None},
        "notes": NOTES,
    }
