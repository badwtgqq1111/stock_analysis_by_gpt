"""A 股大盘指数与全市场成交额。

数据源：
- 指数日线：`assets/data/meta/cn_benchmark_daily.parquet`（Tushare index_daily，
  已补 open/high/low/close/vol/amount；amount_yuan 为元）
- 全 A 成交额：`assets/data/meta/cn_market_turnover.parquet`（clean 日线按日汇总）

这里只做只读展示，不把指数当成可交易股票，也不改变任何选股/回测口径。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from backend.services import live_quotes

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
TURNOVER = ROOT / "assets/data/meta/cn_market_turnover.parquet"
BREADTH = ROOT / "assets/data/derived/cn_market_breadth.parquet"
CONCEPT_RANK = ROOT / "output/results_cn/concept_ranking/latest_all.csv"
CONCEPT_RANK_TOP = ROOT / "output/results_cn/concept_ranking/latest_top.csv"
CONCEPT_COVERAGE = ROOT / "output/results_cn/concept_ranking/coverage.json"
CONCEPT_WATCH = ROOT / "output/results_cn/concept_watchlist"

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




CONCEPT_MEMBERS_IFIND = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
CONCEPT_MEMBERS_LLM = ROOT / "assets/data/derived/cn_concept_members_llm.parquet"
CONCEPT_MEMBERS_CALIBRATED = ROOT / "assets/data/derived/cn_concept_members_scored.parquet"


@lru_cache(maxsize=1)
def _concept_members_all() -> pd.DataFrame:
    if CONCEPT_MEMBERS_CALIBRATED.exists():
        d = pd.read_parquet(CONCEPT_MEMBERS_CALIBRATED)
        return d[["concept", "stock_code", "source"]].drop_duplicates()
    frames = []  # (concept, stock_code, source)
    if ROOT.joinpath("assets/data/meta/cn_board_membership.parquet").exists():
        m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
        m = m[m["taxonomy"] == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
        m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
        m["source"] = "sina"
        frames.append(m[["concept", "stock_code", "source"]])
    if CONCEPT_MEMBERS_IFIND.exists():
        f = pd.read_parquet(CONCEPT_MEMBERS_IFIND)
        f = f[f["snapshot_date"] == f["snapshot_date"].max()][["concept", "stock_code"]]
        f["source"] = "ifind"
        frames.append(f)
    if CONCEPT_MEMBERS_LLM.exists():
        d = pd.read_parquet(CONCEPT_MEMBERS_LLM)
        d = d[~d["concept"].isin(["专精特新", "融资融券", "股权激励"])][["concept", "stock_code"]]
        d["source"] = "llm"
        frames.append(d)
    if not frames:
        return pd.DataFrame(columns=["concept", "stock_code"])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


_QUOTE_CACHE: dict[str, tuple[float, list]] = {}


def _quotes(codes: list[str], ttl: float = 30.0) -> dict[str, dict]:
    """腾讯批量报价（20 只/批），带 30 秒缓存。"""
    import time

    import requests

    key = ",".join(sorted(codes)[:3]) + f"|{len(codes)}"
    hit = _QUOTE_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return {row["stock_code"]: row for row in hit[1]}
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    rows = []
    for i in range(0, len(codes), 20):
        chunk = codes[i:i + 20]
        syms = [("sh" if c.endswith(".SH") else ("bj" if c.endswith(".BJ") else "sz")) + c[:6] for c in chunk]
        try:
            resp = session.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=6)
            resp.encoding = "gbk"
            for line in resp.text.strip().split(";"):
                if "=" not in line:
                    continue
                fields = line.split("=", 1)[1].strip().strip('"').split("~")
                if len(fields) > 38 and fields[3]:
                    code = fields[2] + (".SH" if fields[2].startswith(("6", "9")) else ".SZ")
                    rows.append({"stock_code": code, "name": fields[1], "pct": float(fields[32]),
                                 "amount": float(fields[37]) * 1e4, "turnover": float(fields[38] or 0)})
        except Exception:  # noqa: BLE001
            continue
    _QUOTE_CACHE[key] = (time.time(), rows)
    return {row["stock_code"]: row for row in rows}

@lru_cache(maxsize=4)
def _load_breadth_cached(path_str: str, ver: tuple[int, int]) -> pd.DataFrame:
    """宽广度 + 阿姆士指标（scripts/build_market_breadth.py 生成）。"""
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    return df.sort_values("trade_date")


def _breadth() -> pd.DataFrame:
    if not BREADTH.exists():
        return pd.DataFrame()
    return _load_breadth_cached(str(BREADTH), _version(BREADTH))


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
    for col in ["amount_yuan", "amount_yi", "volume", "n_stocks", "n_priced",
                "up_count", "down_count", "flat_count", "up5_count", "down5_count",
                "median_pct_chg", "up_ratio"]:
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
        breadth = None
        breadth_series: list[dict] = []
        if not turn.empty:
            t = turn[turn["trade_date"] <= asof]
            if not t.empty:
                last = t.iloc[-1]
                turnover_asof = _iso(last["trade_date"])
                total = _f(last.get("amount_yi"))
                n_stocks = int(last["n_stocks"]) if pd.notna(last.get("n_stocks")) else None
                br = _breadth()
                br_row = None
                if not br.empty:
                    sub = br[br["trade_date"] <= asof]
                    if not sub.empty:
                        br_row = sub.iloc[-1]
                breadth = {
                    "n_stocks": n_stocks,
                    "n_priced": (int(last["n_priced"]) if pd.notna(last.get("n_priced")) else None),
                    "up": (int(last["up_count"]) if pd.notna(last.get("up_count")) else None),
                    "down": (int(last["down_count"]) if pd.notna(last.get("down_count")) else None),
                    "flat": (int(last["flat_count"]) if pd.notna(last.get("flat_count")) else None),
                    "up5": (int(last["up5_count"]) if pd.notna(last.get("up5_count")) else None),
                    "down5": (int(last["down5_count"]) if pd.notna(last.get("down5_count")) else None),
                    "median_pct_chg": _f(last.get("median_pct_chg")),
                    "up_ratio": _f(last.get("up_ratio")),
                    "arms": None if br_row is None else _f(br_row.get("arms")),
                    "arms_ma5": None if br_row is None else _f(br_row.get("arms_ma5")),
                    "down_amount_share": None if br_row is None else _f(br_row.get("down_amount_share")),
                    "diverge": None if br_row is None else bool(br_row.get("diverge")),
                    "breadth_drop": None if br_row is None else bool(br_row.get("breadth_drop")),
                }
                turnover_series = [
                    {"date": _iso(x["trade_date"]), "amount_yi": _f(x.get("amount_yi"))}
                    for _, x in t.tail(max(2, int(spark))).iterrows()
                ]
                arms_map = {}
                if not br.empty:
                    arms_map = {pd.Timestamp(k): v for k, v in zip(br["trade_date"], br["arms"])}
                breadth_series = [
                    {
                        "date": _iso(x["trade_date"]),
                        "up": int(x["up_count"]) if pd.notna(x.get("up_count")) else None,
                        "down": int(x["down_count"]) if pd.notna(x.get("down_count")) else None,
                        "flat": int(x["flat_count"]) if pd.notna(x.get("flat_count")) else None,
                        "up_ratio": _f(x.get("up_ratio")),
                        "arms": _f(arms_map.get(pd.Timestamp(x["trade_date"]))),
                    }
                    for _, x in t.tail(max(2, int(spark))).iterrows()
                ]

        return {
            "available": bool(quotes),
            "asof": _iso(asof),
            "turnover_asof": turnover_asof,
            "total_amount_yi": total,
            "total_amount_yuan": None if total is None else total * 1e8,
            "n_stocks": n_stocks,
            "breadth": breadth,
            "breadth_series": breadth_series,
            "turnover_series": turnover_series,
            "indices": quotes,
            "source": {
                "indices": "assets/data/meta/cn_benchmark_daily.parquet（Tushare index_daily）",
                "turnover": "assets/data/meta/cn_market_turnover.parquet（clean 日线汇总）",
                "arms": "assets/data/derived/cn_market_breadth.parquet（scripts/build_market_breadth.py）",
                "turnover_unit": "元；页面换算为亿元",
            },
        }

    def concepts(self, top: int = 20) -> dict:
        """概念排行 + 最新观察池（只读展示，不进选股）。"""
        path = CONCEPT_RANK if CONCEPT_RANK.exists() else CONCEPT_RANK_TOP
        if not path.exists():
            return {"available": False, "reason": "尚未生成概念排行（scripts/concept_ranking.py）", "items": [], "watchlist": []}
        rank = pd.read_csv(path)
        coverage = {}
        if CONCEPT_COVERAGE.exists():
            coverage = json.loads(CONCEPT_COVERAGE.read_text(encoding="utf-8"))
        watch = []
        if CONCEPT_WATCH.exists():
            files = sorted(CONCEPT_WATCH.glob("*.csv"))
            if files:
                latest = files[-1]
                w = pd.read_csv(latest)
                watch = w.head(30).to_dict("records")
                coverage["watchlist_date"] = latest.stem
        cols = [c for c in ("rank", "concept", "family", "is_family_lead", "n", "chg", "breadth",
                            "limit_up", "amount_share", "score") if c in rank.columns]
        items = rank.head(top)[cols].reset_index(drop=True)
        items["rank"] = items.index + 1          # family 去重后重新编号，避免出现 1/3/5 的空号
        return {"available": True, "items": items.to_dict("records"),
                "coverage": coverage, "watchlist": watch}


    def concept_stocks(self, name: str, top: int = 100) -> dict:
        """某个概念的成分股（实时/收盘报价），供热度网格下钻。"""
        members = _concept_members_all()
        sub = members[members["concept"] == name]
        if sub.empty:
            return {"available": False, "reason": f"概念 {name} 没有成分数据", "stocks": []}
        codes = sorted(sub["stock_code"].unique())
        src_map = sub.groupby("stock_code")["source"].agg(lambda s: "+".join(sorted(set(s)))).to_dict()
        quotes = _quotes(codes)
        rows = []
        for code in codes:
            q = quotes.get(code)
            if not q:
                continue
            rows.append({"stock_code": code, "name": q["name"], "pct": round(q["pct"], 2),
                         "amount_yi": round(q["amount"] / 1e8, 2), "turnover": round(q["turnover"], 2),
                         "source": src_map.get(code, "")})
        if not rows:
            return {"available": False, "reason": "行情源暂不可用", "stocks": []}
        df = pd.DataFrame(rows)
        up = float((df["pct"] > 0).mean())
        return {
            "available": True,
            "concept": name,
            "n": int(len(df)),
            "chg": round(float(df["pct"].mean()), 2),
            "breadth": round(up, 4),
            "limit_up": int((df["pct"] >= 9.8).sum()),
            "amount_yi": round(float(df["amount_yi"].sum()), 1),
            "stocks": df.sort_values("pct", ascending=False).head(top).to_dict("records"),
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
