"""A 股大盘指数与全市场成交额。

数据源：
- 指数日线：`assets/data/meta/cn_benchmark_daily.parquet`（Tushare index_daily，
  已补 open/high/low/close/vol/amount；amount_yuan 为元）
- 全 A 成交额：`assets/data/meta/cn_market_turnover.parquet`（clean 日线按日汇总）

这里只做只读展示，不把指数当成可交易股票，也不改变任何选股/回测口径。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from backend.services import live_quotes

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
TURNOVER = ROOT / "assets/data/meta/cn_market_turnover.parquet"

INDEX_ORDER = [
    "000001.SH", "399001.SZ", "399006.SZ", "000300.SH",
    "000905.SH", "000985.CSI", "000688.SH", "000016.SH",
]
INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000985.CSI": "中证全指",
    "000688.SH": "科创50",
    "000016.SH": "上证50",
}
CORE_CODES = ["000001.SH", "399001.SZ", "399006.SZ", "000905.SH"]


def _version(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=4)
def _load_benchmark_cached(path_str: str, ver: tuple[int, int]) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["ts_code"] = df["ts_code"].astype(str)
    for col in ["open", "high", "low", "close", "pre_close", "change",
                "pct_chg", "vol", "amount", "amount_yuan"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            df[col] = df["close"]
    if "amount_yuan" not in df.columns:
        df["amount_yuan"] = df["amount"] * 1000.0 if "amount" in df.columns else np.nan
    if "vol" not in df.columns:
        df["vol"] = np.nan
    if "pct_chg" not in df.columns:
        df["pct_chg"] = np.nan
    df["benchmark_name"] = df.get("benchmark_name", df["ts_code"].map(INDEX_NAMES))
    df["benchmark_name"] = df["benchmark_name"].fillna(df["ts_code"].map(INDEX_NAMES))
    df = df.dropna(subset=["trade_date", "close"])
    df = df[df["close"] > 0].sort_values(["ts_code", "trade_date"])
    return df.drop_duplicates(["ts_code", "trade_date"], keep="last").reset_index(drop=True)


@lru_cache(maxsize=4)
def _load_turnover_cached(path_str: str, ver: tuple[int, int]) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["amount_yuan", "amount_yi", "volume", "n_stocks"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount_yi" not in df.columns and "amount_yuan" in df.columns:
        df["amount_yi"] = df["amount_yuan"] / 1e8
    return df.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)


def _benchmark() -> pd.DataFrame:
    if not BENCHMARK.exists():
        return pd.DataFrame()
    return _load_benchmark_cached(str(BENCHMARK), _version(BENCHMARK))


def _turnover() -> pd.DataFrame:
    if not TURNOVER.exists():
        return pd.DataFrame()
    return _load_turnover_cached(str(TURNOVER), _version(TURNOVER))


def _iso(value) -> str | None:
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _f(value) -> float | None:
    try:
        out = float(value)
        return out if np.isfinite(out) else None
    except (TypeError, ValueError):
        return None


class MarketService:
    def overview(self, spark: int = 20) -> dict:
        bench = _benchmark()
        if bench.empty:
            return {"available": False, "reason": "缺少 cn_benchmark_daily.parquet",
                    "indices": [], "total_amount_yi": None}
        asof = bench["trade_date"].max()
        quotes = []
        for code in INDEX_ORDER:
            g = bench[bench["ts_code"] == code]
            if g.empty:
                continue
            g = g[g["trade_date"] <= asof]
            if g.empty:
                continue
            row = g.iloc[-1]
            tail = g.tail(max(2, int(spark)))
            quotes.append({
                "code": code,
                "name": INDEX_NAMES.get(code) or str(row.get("benchmark_name") or code),
                "date": _iso(row["trade_date"]),
                "open": _f(row.get("open")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "close": _f(row.get("close")),
                "pct_chg": _f(row.get("pct_chg")),
                "amount_yi": (None if _f(row.get("amount_yuan")) is None
                              else _f(row.get("amount_yuan")) / 1e8),
                "vol": _f(row.get("vol")),
                "spark": [
                    {"date": _iso(x["trade_date"]), "close": _f(x["close"])}
                    for _, x in tail.iterrows()
                ],
            })

        turn = _turnover()
        total = None
        n_stocks = None
        turnover_asof = None
        turnover_series: list[dict] = []
        if not turn.empty:
            t = turn[turn["trade_date"] <= asof]
            if not t.empty:
                last = t.iloc[-1]
                turnover_asof = _iso(last["trade_date"])
                total = _f(last.get("amount_yi"))
                n_stocks = int(last["n_stocks"]) if pd.notna(last.get("n_stocks")) else None
                turnover_series = [
                    {"date": _iso(x["trade_date"]), "amount_yi": _f(x.get("amount_yi"))}
                    for _, x in t.tail(max(2, int(spark))).iterrows()
                ]

        return {
            "available": bool(quotes),
            "asof": _iso(asof),
            "turnover_asof": turnover_asof,
            "total_amount_yi": total,
            "total_amount_yuan": None if total is None else total * 1e8,
            "n_stocks": n_stocks,
            "turnover_series": turnover_series,
            "indices": quotes,
            "source": {
                "indices": "assets/data/meta/cn_benchmark_daily.parquet（Tushare index_daily）",
                "turnover": "assets/data/meta/cn_market_turnover.parquet（clean 日线汇总）",
                "turnover_unit": "元；页面换算为亿元",
            },
        }

    def turnover(self, days: int = 365) -> dict:
        """每日全 A 成交额序列（亿元）+ 20 日均值。"""
        turn = _turnover()
        if turn.empty:
            return {"available": False, "reason": "缺少 cn_market_turnover.parquet",
                    "series": [], "stats": {}}
        g = turn.copy()
        if "amount_yi" not in g.columns:
            g["amount_yi"] = g["amount_yuan"] / 1e8
        g = g.dropna(subset=["trade_date", "amount_yi"]).sort_values("trade_date")
        if days and days > 0:
            g = g.tail(int(days))
        g["ma20"] = g["amount_yi"].rolling(20, min_periods=1).mean()
        series = [
            {"date": _iso(r["trade_date"]),
             "amount_yi": _f(r["amount_yi"]),
             "ma20": _f(r["ma20"])}
            for _, r in g.iterrows()
        ]
        latest = series[-1] if series else {}
        amounts = [x["amount_yi"] for x in series if x["amount_yi"] is not None]
        return {
            "available": bool(series),
            "asof": latest.get("date"),
            "series": series,
            "stats": {
                "latest": latest.get("amount_yi"),
                "ma20": latest.get("ma20"),
                "mean": (sum(amounts) / len(amounts)) if amounts else None,
                "max": max(amounts) if amounts else None,
                "min": min(amounts) if amounts else None,
                "n": len(series),
            },
            "unit": "亿元",
            "source": "assets/data/meta/cn_market_turnover.parquet",
        }

    def index_ohlcv(self, code: str, days: int = 365) -> dict:
        code = str(code).strip().upper()
        bench = _benchmark()
        if bench.empty:
            raise ValueError("缺少指数日线数据")
        g = bench[bench["ts_code"] == code].copy()
        if g.empty:
            raise ValueError(f"没有指数 {code} 的日线")
        g = g.sort_values("trade_date").tail(max(1, int(days))).copy()
        close = g["close"].astype(float)
        prev_close = g["pre_close"] if "pre_close" in g.columns else close.shift(1)
        pct = g["pct_chg"].astype(float) if "pct_chg" in g.columns else close.pct_change() * 100
        pct = pct.where(pct.notna(), close.pct_change() * 100)
        amplitude = (g["high"] - g["low"]) / prev_close.replace(0, np.nan) * 100
        for n in (5, 10, 20, 60):
            g[f"ma{n}"] = close.rolling(n, min_periods=n).mean()
        g["change_pct"] = pct
        g["amplitude"] = amplitude
        records = []
        for _, row in g.iterrows():
            amount_yuan = _f(row.get("amount_yuan"))
            records.append({
                "time": _iso(row["trade_date"]),
                "open": _f(row.get("open")) or _f(row["close"]),
                "high": _f(row.get("high")) or _f(row["close"]),
                "low": _f(row.get("low")) or _f(row["close"]),
                "close": _f(row["close"]),
                "volume": _f(row.get("vol")) or 0.0,
                "amount_yuan": amount_yuan,
                "ma5": _f(row.get("ma5")),
                "ma10": _f(row.get("ma10")),
                "ma20": _f(row.get("ma20")),
                "ma60": _f(row.get("ma60")),
                "vol_ma5": None,
                "vol_ma20": None,
                "change_pct": _f(row.get("change_pct")),
                "amplitude": _f(row.get("amplitude")),
                "buy_signal": False,
                "sell_signal": False,
            })
        latest = records[-1] if records else {}
        name = INDEX_NAMES.get(code) or str(g.iloc[-1].get("benchmark_name") or code)
        return {
            "code": code,
            "name": name,
            "data": records,
            "signal_stats": None,
            "chips": None,
            "stage": None,
            "profile": None,
            "latest": {
                "close": latest.get("close") or 0.0,
                "change_pct": latest.get("change_pct") or 0.0,
                "total_bars": len(records),
            },
            "amount_unit": "yuan",
        }

    def index_intraday(self, code: str) -> dict:
        """指数当日分时（腾讯 minute/query，盘中约 8 秒缓存）。"""
        code = str(code).strip().upper()
        name = INDEX_NAMES.get(code) or code
        frame, error = live_quotes.tencent_minute(code)
        if frame is None or frame.empty:
            return {
                "available": False, "code": code, "name": name,
                "date": live_quotes.now_cn().strftime("%Y-%m-%d"),
                "prev_close": None, "points": [], "latest": {},
                "source": "tx_minute", "reason": error or "腾讯指数分时为空",
                "session": live_quotes.trading_phase(),
            }
        frame = frame.sort_values("clock").reset_index(drop=True)
        day = pd.Timestamp(frame["clock"].iloc[-1]).normalize()
        bench = _benchmark()
        prev_close = None
        if not bench.empty:
            g = bench[bench["ts_code"] == code].sort_values("trade_date")
            before = g[g["trade_date"] < day]
            if not before.empty:
                prev_close = _f(before.iloc[-1]["close"])
            elif not g.empty:
                prev_close = _f(g.iloc[-1]["close"])
        points = []
        for _, row in frame.iterrows():
            stamp = pd.Timestamp(row["clock"])
            points.append({
                "time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "clock": stamp.strftime("%H:%M"),
                "price": _f(row.get("close")),
                "avg": _f(row.get("avg")),
                "open": _f(row.get("open")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "volume_shares": int((row.get("volume_lots") or 0) * 100),
                "amount_yuan": _f(row.get("amount")),
            })
        last = points[-1]
        price = last["price"]
        change = None if price is None or prev_close is None else price - prev_close
        change_pct = None if change is None or not prev_close else change / prev_close * 100
        return {
            "available": True,
            "code": code,
            "name": name,
            "date": day.strftime("%Y-%m-%d"),
            "period": "1",
            "requested_period": "1",
            "mode": "live",
            "source": "tx_minute",
            "source_label": "腾讯指数分时",
            "prev_close": prev_close,
            "points": points,
            "latest": {
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "avg": last["avg"],
                "high": max((p["high"] or p["price"] or 0) for p in points),
                "low": min((p["low"] or p["price"] or 0) for p in points),
                "open": points[0]["open"],
                "volume_shares": sum(p["volume_shares"] for p in points),
                "amount_yuan": sum(p["amount_yuan"] or 0 for p in points),
                "points": len(points),
                "time": last["time"],
            },
            "session": live_quotes.trading_phase(),
            "live": {"available": True, "reason": None},
            "store": {"available": False},
            "notes": ["指数分时来自腾讯 minute/query；盘中约 8 秒缓存。",
                      "指数成交额/成交量按源提供，仅用于分时展示。"],
        }
