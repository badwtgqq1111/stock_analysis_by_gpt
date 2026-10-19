"""东财实时数据封装（涨停池 / 分时 / 资金流实时排行）。

为什么要单独一层：
  * **网络必须可失败**：本机走 clash 代理（127.0.0.1:7897），实测
    `push2ex.eastmoney.com`（涨停池）与 `push2his.eastmoney.com`（分时）能通，
    而 `push2.eastmoney.com` 的 clist 接口（实时资金流排行 / 全市场快照）经常
    `ProxyError`（代理直接断连）。所以全部包成“尽力而为”：拿到就用，拿不到返回
    `(None, 原因)`，由调用方降级到本地数据 —— 页面不会因为一个接口挂了就整页报错。
  * **实时数据不能久缓存**：涨停池/分时用 60 秒 TTL，120 秒给资金流排行。

统一约定：函数返回 `(payload, error)`，`error` 为 None 表示成功。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from threading import Lock
from time import sleep
from zoneinfo import ZoneInfo

import pandas as pd

CN_TZ = ZoneInfo("Asia/Shanghai")
_CACHE_LOCK = Lock()
_BUILD_LOCKS: dict[tuple, Lock] = {}
_CACHE: dict[tuple, tuple[float, object, str | None]] = {}


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _cached(key: tuple, ttl: float, builder, error_ttl: float = 20.0):
    """失败结果用更短的 TTL 缓存（`error_ttl`）。

    本机代理是**间歇性**断连（同一个域名前一分钟还通、下一分钟 ProxyError），
    所以失败不能跟成功一样缓存 60s：那样用户点一次"刷新"、看到失败，
    接下来一分钟内怎么点都还是那份失败结果。20s 后自动重试一次。
    """
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < (ttl if hit[2] is None else min(ttl, error_ttl)):
            return hit[1], hit[2]
        lock = _BUILD_LOCKS.setdefault(key, Lock())
    with lock:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.time() - hit[0] < (ttl if hit[2] is None else min(ttl, error_ttl)):
                return hit[1], hit[2]
        payload, error = builder()
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), payload, error)
        return payload, error



def cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def to_ts_code(symbol: str) -> str:
    """6 位代码 → 带交易所后缀。

    北交所新代码段必须显式处理：920xxx / 43xxxx / 83xxxx / 87xxxx / 88xxxx。
    只按“6/9 开头=沪”的老规则会把 920xxx 错判成 .SH
    （`scripts/build_selection_ledger.py` 的 `_norm_code` 就是那样，这里补全，其余保持一致）。
    """
    code = str(symbol).strip().upper()
    if "." in code:
        return code
    if code.startswith(("920", "43", "83", "87", "88")):
        return f"{code}.BJ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def six_digit(ts_code: str) -> str:
    return str(ts_code).split(".")[0]


def trading_phase(moment: datetime | None = None) -> dict:
    """当前处于哪个交易时段（前端据此决定“实时”标签与轮询节奏）。"""
    moment = moment or now_cn()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CN_TZ)
    moment = moment.astimezone(CN_TZ)
    minutes = moment.hour * 60 + moment.minute
    weekday = moment.weekday()
    phase = "closed"
    if weekday < 5:
        if 9 * 60 + 15 <= minutes < 9 * 60 + 25:
            phase = "auction"          # 集合竞价
        elif 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
            phase = "am"
        elif 11 * 60 + 30 < minutes < 13 * 60:
            phase = "lunch"
        elif 13 * 60 <= minutes <= 15 * 60:
            phase = "pm"
    return {
        "phase": phase,
        "live": phase in ("am", "pm", "auction"),
        "is_trading_day_guess": weekday < 5,
        "now": moment.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 涨停池（含连板数 / 封板资金 / 首封时间 —— 这些本地日线算不出来）
# ─────────────────────────────────────────────────────────────────────────────
_ZT_COLUMNS = {
    "代码": "symbol", "名称": "name", "涨跌幅": "pct_chg", "最新价": "close",
    "成交额": "amount", "流通市值": "float_mv", "换手率": "turnover",
    "封板资金": "seal_amount", "首次封板时间": "first_seal", "最后封板时间": "last_seal",
    "炸板次数": "open_times", "涨停统计": "stat", "连板数": "boards", "所属行业": "industry",
}


def zt_pool(day: str) -> tuple[dict | None, str | None]:
    """东财涨停池（`ak.stock_zt_pool_em`）。day 形如 2026-10-09 或 20261009。"""
    stamp = str(day).replace("-", "")

    def build():
        frame, error = None, None
        for attempt in range(2):
            try:
                import akshare as ak
                frame = ak.stock_zt_pool_em(date=stamp)
                error = None
                break
            except Exception as exc:                  # noqa: BLE001 —— 网络层什么都可能抛
                error = f"{type(exc).__name__}: {exc}"[:300]
                frame = None
                if attempt == 0:
                    sleep(0.4)
        if frame is None:
            return None, error
        if frame is None or frame.empty:
            return None, f"涨停池 {stamp} 返回空（非交易日或东财未更新）"
        frame = frame.rename(columns=_ZT_COLUMNS)
        keep = [c for c in _ZT_COLUMNS.values() if c in frame.columns]
        frame = frame[keep]
        payload: dict[str, dict] = {}
        for _, row in frame.iterrows():
            code = to_ts_code(row.get("symbol"))
            record = {}
            for key in keep:
                if key == "symbol":
                    continue
                value = row.get(key)
                if isinstance(value, str):
                    record[key] = value
                else:
                    record[key] = None if pd.isna(value) else round(float(value), 4)
            payload[code] = record
        return payload, None

    return _cached(("zt_pool", stamp), 60.0, build)


# ─────────────────────────────────────────────────────────────────────────────
# 分时（当日 1 分钟 / 5 分钟）
# ─────────────────────────────────────────────────────────────────────────────
def intraday_minute(code: str, period: str = "1") -> tuple[pd.DataFrame | None, str | None]:
    """东财分时。period: '1'（1 分钟，含最近几个交易日）或 '5'（5 分钟）。"""
    symbol = six_digit(code)
    period = "5" if str(period) == "5" else "1"

    def build():
        frame, error = None, None
        for attempt in range(2):                      # 代理间歇性断连 → 失败立刻重试一次
            try:
                import akshare as ak
                frame = ak.stock_zh_a_hist_min_em(symbol=symbol, period=period, adjust="")
                error = None
                break
            except Exception as exc:                  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"[:300]
                frame = None
                if attempt == 0:
                    sleep(0.4)
        if frame is None:
            return None, error
        if frame is None or frame.empty:
            return None, f"{symbol} 分时返回空"
        frame = frame.rename(columns={"时间": "clock", "开盘": "open", "收盘": "close",
                                      "最高": "high", "最低": "low", "成交量": "volume",
                                      "成交额": "amount", "均价": "avg"})
        frame["clock"] = pd.to_datetime(frame["clock"], errors="coerce")
        frame = frame.dropna(subset=["clock"]).sort_values("clock")
        return frame, None

    return _cached(("minute", symbol, period), 15.0, build, error_ttl=8.0)


# ─────────────────────────────────────────────────────────────────────────────
# 实时资金流排行（clist 接口，本机代理不稳定 → 只做尽力而为）
# ─────────────────────────────────────────────────────────────────────────────
def fund_flow_rank(indicator: str = "今日") -> tuple[pd.DataFrame | None, str | None]:
    """东财个股资金流排行（实时）。indicator: 今日 / 5日 / 10日。"""
    indicator = indicator if indicator in ("今日", "5日", "10日") else "今日"

    def build():
        try:
            import akshare as ak
            frame = ak.stock_individual_fund_flow_rank(indicator=indicator)
        except Exception as exc:                      # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"[:300]
        if frame is None or frame.empty:
            return None, "资金流排行返回空"
        return frame, None

    return _cached(("flow_rank", indicator), 120.0, build)


def recent_day_candidates(today: datetime | None = None, back: int = 7) -> list[str]:
    """最近若干个自然日（倒序，含今天）——按日取涨停池时要跳过非交易日。"""
    today = (today or now_cn()).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(back + 1)]


# ─────────────────────────────────────────────────────────────────────────────
# 腾讯行情（分时 / 5 分钟 K / 批量快照）
# ─────────────────────────────────────────────────────────────────────────────
# 为什么加这一路（2026-10-09 实测）：
#   东财 `push2his.eastmoney.com` 经本机代理**间歇性断连**（同一分钟内前一次成功、
#   后一次 ProxyError，直连也不通），而腾讯 `web.ifzq.gtimg.cn` / `qt.gtimg.cn`
#   实测稳定（0.2s 返回、含当日盘中数据）。分时/快照是页面主功能，不能只有一条通路。
_TX_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
               "Referer": "https://gu.qq.com/"}


def tx_symbol(code: str) -> str:
    """ts_code → 腾讯代码（sz000001 / sh600519 / bj430047）。"""
    ts = to_ts_code(code)
    symbol, _, exchange = ts.partition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper(), "sz")
    return f"{prefix}{symbol}"


def _tx_get(url: str, params: dict, timeout: float = 12.0, attempts: int = 2):
    import requests
    last = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, params=params, headers=_TX_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response, None
        except Exception as exc:                       # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:300]
            if attempt == 0:
                sleep(0.3)
    return None, last


def tencent_minute(code: str) -> tuple[pd.DataFrame | None, str | None]:
    """腾讯当日分时（`minute/query`）。

    原始行形如 `'0931 11.83 62344 73792269.98'` = 时间 / 最新价 / **累计**成交量(手) / **累计**成交额(元)。
    这里化成逐分钟量价 + 精确均价（累计额 ÷ 累计股数），与东财的"均价"口径一致。
    """
    symbol = tx_symbol(code)

    def build():
        response, error = _tx_get("https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                                  {"code": symbol})
        if response is None:
            return None, error
        try:
            payload = response.json()["data"][symbol]["data"]
            rows = payload["data"]
            day = str(payload.get("date") or "")
        except Exception as exc:                       # noqa: BLE001
            return None, f"腾讯分时解析失败：{type(exc).__name__}: {exc}"[:300]
        if not rows:
            return None, f"{symbol} 腾讯分时为空"
        records = []
        prev_volume = prev_amount = 0.0
        for line in rows:
            parts = str(line).split()
            if len(parts) < 4:
                continue
            clock, price, cum_volume, cum_amount = parts[0], parts[1], parts[2], parts[3]
            try:
                price = float(price)
                cum_volume = float(cum_volume)
                cum_amount = float(cum_amount)
            except ValueError:
                continue
            volume = max(cum_volume - prev_volume, 0.0)
            amount = max(cum_amount - prev_amount, 0.0)
            prev_volume, prev_amount = cum_volume, cum_amount
            stamp = pd.to_datetime(f"{day} {clock[:2]}:{clock[2:4]}", format="%Y%m%d %H:%M",
                                   errors="coerce")
            if pd.isna(stamp):
                continue
            records.append({
                "clock": stamp,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume_lots": volume,
                "amount": amount,
                "avg": (cum_amount / (cum_volume * 100)) if cum_volume > 0 else None,
            })
        if not records:
            return None, f"{symbol} 腾讯分时无有效行"
        return pd.DataFrame(records), None

    return _cached(("tx_minute", symbol), 4.0, build, error_ttl=8.0)


def tencent_kline(code: str, period: str = "m5", count: int = 320) -> tuple[pd.DataFrame | None, str | None]:
    """腾讯 5 分钟 K（`mkline`）。行格式 `[时间, 开, 收, 高, 低, 成交量(手), {}, 均价?]`。"""
    symbol = tx_symbol(code)
    period = period if period in ("m1", "m5", "m15", "m30", "m60") else "m5"

    def build():
        response, error = _tx_get("https://ifzq.gtimg.cn/appstock/app/kline/mkline",
                                  {"param": f"{symbol},{period},,{int(count)}"})
        if response is None:
            return None, error
        try:
            rows = response.json()["data"][symbol][period]
        except Exception as exc:                       # noqa: BLE001
            return None, f"腾讯 {period} 解析失败：{type(exc).__name__}: {exc}"[:300]
        records = []
        for row in rows or []:
            if len(row) < 6:
                continue
            stamp = pd.to_datetime(str(row[0]), format="%Y%m%d%H%M", errors="coerce")
            if pd.isna(stamp):
                continue
            try:
                records.append({
                    "clock": stamp,
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "volume_lots": float(row[5]),
                    "amount": None,
                    "avg": None,      # 腾讯该接口不给成交额 → 均价留空，不猜
                })
            except (TypeError, ValueError):
                continue
        if not records:
            return None, f"{symbol} 腾讯 {period} 为空"
        return pd.DataFrame(records), None

    return _cached(("tx_kline", symbol, period, int(count)), 15.0, build, error_ttl=8.0)


def tencent_quotes(codes: list[str], timeout: float = 12.0, attempts: int = 2) -> tuple[dict[str, dict], str | None]:
    """腾讯批量快照（`qt.gtimg.cn/q=`，一次请求可带多只）。"""
    wanted = [tx_symbol(c) for c in codes if str(c).strip()]
    if not wanted:
        return {}, None
    joined = ",".join(wanted)

    def build():
        response, error = _tx_get(f"https://qt.gtimg.cn/q={joined}", {}, timeout=timeout, attempts=attempts)
        if response is None:
            return None, error
        response.encoding = "gbk"
        payload: dict[str, dict] = {}
        for line in response.text.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            head, _, body = line.partition("=")
            fields = body.strip().strip('"').split("~")
            if len(fields) < 33:
                continue
            symbol = head.split("_")[-1]
            def num(index, nd=4):
                try:
                    return round(float(fields[index]), nd)
                except (TypeError, ValueError, IndexError):
                    return None
            payload[symbol[-6:]] = {
                "name": fields[1],
                "price": num(3, 4),
                "prev_close": num(4, 4),
                "open": num(5, 4),
                "volume_lots": num(6, 0),
                "time": fields[30],
                "change": num(31, 4),
                "change_pct": (num(32, 4) / 100.0) if num(32, 4) is not None else None,
                "high": num(33, 4),
                "low": num(34, 4),
                "amount_wan": num(37, 2),
                "limit_up": num(47, 4),
                "limit_down": num(48, 4),
            }
        return payload, None

    payload, error = _cached(("tx_quotes", joined), 2.0, build, error_ttl=4.0)
    return payload or {}, error
