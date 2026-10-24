#!/usr/bin/env python3
"""补齐 5min 分时的「近端缺口」（新浪 1970 根 / 腾讯 320 根），不依赖 baostock 登录。

背景（2026-10-23）：
  * BaoStock 是唯一能给多年 5min 的源，但并发会话会把 IP 拉黑（`10001011 黑名单用户`）；
    一旦拉黑，每日增量整批失败，于是「总有一部分没同步」。
  * 新浪 `getKLineData` 的 `datalen` 实测上限 **1970 根 = 41 个交易日**，且**带成交额**（元），
    0.05s/只 —— 足够覆盖「昨天/上周没补上」的所有近端缺口。
  * 腾讯 mkline 320 根，无成交额（只能估算），当新浪失败时的二级兜底。

口径：
  * 新浪 volume 单位=股、amount=元，与 BaoStock 归档一致；
  * 腾讯 volume=手（×100），amount 用 close×volume 估算，source 标 `_est`；
  * 深度回补（`backfill_cn_intraday_history.py`）会用 BaoStock 精确值覆盖同一 (date,time)。

用法：
  uv run python scripts/fill_intraday_gap_recent.py --status
  uv run python scripts/fill_intraday_gap_recent.py --limit 700 --workers 6
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
FREQ_DIR = ROOT / "assets/data/raw/intraday_baostock/frequency=5min"
LOG = ROOT / "assets/data/raw/intraday_baostock/_sync/recent_fill.log"
SINA = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
TENCENT = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
SINA_BARS = 1970
TX_BARS = 320
CALENDAR: list[pd.Timestamp] = []


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%F %T}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    return s


def sym(code: str) -> str:
    num, exch = code.upper().split(".", 1)
    return f"{'sh' if exch == 'SH' else 'sz'}{num}"


def trade_calendar() -> list[pd.Timestamp]:
    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet", columns=["ts_code", "trade_date"])
    bench = bench[bench["ts_code"] == "000985.CSI"]
    return sorted(pd.to_datetime(bench["trade_date"]).dt.normalize().unique())


def daily_calendar() -> dict[str, list[pd.Timestamp]]:
    """每只票「真正有日线」的交易日集合。

    用它定义"缺哪些天"，而不是拿全市场日历硬套：停牌日没有日线，也不该有分时，
    否则停牌股会被误判成"中间有洞"（实测 002667.SZ 09-30~10-12 停牌）。
    """
    import glob
    import pyarrow.dataset as ds

    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    table = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    table = table.dropna(subset=["trade_date", "stock_code"]).drop_duplicates()
    return {str(c): sorted(v) for c, v in table.groupby("stock_code")["trade_date"]}


def stale_codes(expected: dict[str, list[pd.Timestamp]]) -> list[tuple[str, pd.Timestamp]]:
    out = []
    for path in FREQ_DIR.glob("*.parquet"):
        code = path.stem.replace("_", ".")
        want = expected.get(code)
        if not want:
            continue
        want = want[-1]
        try:
            last = pd.to_datetime(pd.read_parquet(path, columns=["date"])["date"]).max()
        except Exception:  # noqa: BLE001
            last = pd.Timestamp("1990-01-01")
        if last < want:
            out.append((code, last))
    return sorted(out, key=lambda x: x[1])


def fetch_sina(s: requests.Session, code: str) -> pd.DataFrame:
    r = s.get(SINA, params={"symbol": sym(code), "scale": "5", "ma": "no", "datalen": str(SINA_BARS)}, timeout=25)
    text = r.text.strip()
    if not text or text == "null":
        return pd.DataFrame()
    payload = json.loads(text)
    if not payload:
        return pd.DataFrame()
    frame = pd.DataFrame(payload)
    stamp = pd.to_datetime(frame["day"])
    out = pd.DataFrame({
        "date": stamp.dt.normalize().astype("datetime64[us]"),
        "time": stamp.dt.strftime("%Y%m%d%H%M") + "00000",
        "open": pd.to_numeric(frame["open"], errors="coerce"),
        "high": pd.to_numeric(frame["high"], errors="coerce"),
        "low": pd.to_numeric(frame["low"], errors="coerce"),
        "close": pd.to_numeric(frame["close"], errors="coerce"),
        "volume": pd.to_numeric(frame["volume"], errors="coerce").fillna(0).astype("int64"),
        "amount": pd.to_numeric(frame["amount"], errors="coerce"),
    })
    out["stock_code"] = code
    out["frequency"] = "5min"
    out["adjust"] = "qfq"
    out["source"] = "sina_kline_5min"
    return out


def fetch_tencent(s: requests.Session, code: str) -> pd.DataFrame:
    r = s.get(TENCENT, params={"param": f"{sym(code)},m5,,{TX_BARS}"}, timeout=20)
    payload = r.json().get("data", {}).get(sym(code), {}).get("m5") or []
    if not payload:
        return pd.DataFrame()
    rows = [(i[0], float(i[1]), float(i[3]), float(i[4]), float(i[2]), float(i[5])) for i in payload]
    frame = pd.DataFrame(rows, columns=["stamp", "open", "high", "low", "close", "lots"])
    out = pd.DataFrame({
        "date": pd.to_datetime(frame["stamp"].str[:8], format="%Y%m%d").astype("datetime64[us]"),
        "time": frame["stamp"].str[:12] + "00000",
        "open": frame["open"], "high": frame["high"], "low": frame["low"], "close": frame["close"],
        "volume": (frame["lots"] * 100).round().astype("int64"),
    })
    out["amount"] = out["close"] * out["volume"]
    out["stock_code"] = code
    out["frequency"] = "5min"
    out["adjust"] = "qfq"
    out["source"] = "tencent_mkline_5min_est"
    return out


def merge(path: Path, new: pd.DataFrame) -> int:
    if path.exists():
        old = pd.read_parquet(path)
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(["date", "time"], keep="last").sort_values(["date", "time"]).reset_index(drop=True)
    tmp = path.with_suffix(".tmp.parquet")
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return len(merged)


def run_one(s: requests.Session, code: str, last: pd.Timestamp, source: str,
            daily: list[pd.Timestamp]) -> tuple[str, int]:
    frame = pd.DataFrame()
    if source in ("auto", "sina"):
        frame = fetch_sina(s, code)
    if frame.empty and source in ("auto", "tencent"):
        frame = fetch_tencent(s, code)
    if frame.empty:
        return "empty", 0
    frame = frame[frame["date"] > last]
    if frame.empty:
        return "nocase", 0
    # 今天 15:05 之前的当根是半天数据，不写进归档（避免 max(date)=今天 但只有 24 根）
    now = datetime.now()
    if (now.hour, now.minute) < (15, 5):
        frame = frame[frame["date"] < pd.Timestamp(now.date())]
        if frame.empty:
            return "nocase", 0
    # 该票应有的交易日（> last），必须被本次窗口完整覆盖；停牌日不在 daily 里，不会误判
    need = [d for d in daily if d > last]
    have = set(frame["date"])
    missing = [d for d in need if d not in have and frame["date"].min() <= d <= frame["date"].max()]
    if missing:
        return f"hole(missing={len(missing)}d from {missing[0].date()})", 0
    rows = merge(FREQ_DIR / f"{code.replace('.', '_')}.parquet", frame)
    return "ok", rows


def main() -> int:
    global CALENDAR
    ap = argparse.ArgumentParser(description="新浪/腾讯补 5min 近端缺口")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source", default="auto", choices=["auto", "sina", "tencent"])
    ap.add_argument("--max-gap-days", type=int, default=40)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    CALENDAR = trade_calendar()
    daily_by_code = daily_calendar()
    expected = {c: v for c, v in daily_by_code.items()}
    stale = stale_codes(expected)
    if args.status:
        print(json.dumps({"stale": len(stale), "oldest": [[c, str(d.date())] for c, d in stale[:3]],
                          "newest": [[c, str(d.date())] for c, d in stale[-3:]]}, ensure_ascii=False, indent=2))
        return 0
    if not stale:
        log("无缺口")
        return 0

    def gap_td(code: str, last: pd.Timestamp) -> int:
        return sum(1 for d in expected[code] if d > last)

    todo = [(c, d) for c, d in stale if gap_td(c, d) <= args.max_gap_days]
    skipped = len(stale) - len(todo)
    if args.limit:
        todo = todo[: args.limit]
    log(f"缺口 {len(stale)} 只；窗口内 {len(todo)} 只（跳过 {skipped} 只，需 baostock 深度回补）；source={args.source}")
    if not todo:
        return 0

    s = session()
    ok, fail = 0, []
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_one, s, code, last, args.source, expected[code]): code for code, last in todo}
        for idx, fut in enumerate(as_completed(futures), start=1):
            code = futures[fut]
            try:
                status, rows = fut.result()
            except Exception as exc:  # noqa: BLE001
                status, rows = f"{type(exc).__name__}: {str(exc)[:100]}", 0
            if status == "ok":
                ok += 1
            elif status != "nocase":
                fail.append((code, status))
            if idx % 50 == 0 or idx == len(todo):
                log(f"{idx}/{len(todo)} 修复 {ok} 失败 {len(fail)} 用时 {time.time()-started:.0f}s")
    log(f"完成：修复 {ok}，失败 {len(fail)}，跳过 {skipped}")
    if fail[:5]:
        log(f"失败样例：{fail[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
