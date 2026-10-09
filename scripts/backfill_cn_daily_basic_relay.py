#!/usr/bin/env python3
"""回补 A 股 `daily_basic`（总市值/流通市值/换手/原始估值）—— 走 Tushare 中继。

为什么要补（2026-10-07 实测）：
  * 面板 v2 里 `size_ind_pct`（行业内市值分位）只有 35.7% 覆盖，2018 年只有 31.8%
    —— 对小市值策略来说，"市值"这块基础数据是缺的；
  * 上游 `cn_valuation_features_long.parquet` 来自 ClickHouse `valuation_snapshot_meta`，
    早期每年只有约 1,100 只/日，而中继 `daily_basic` 按**交易日**一次可返回**全市场**（实测 5,000 行、2.5s）。

做法：**按交易日**拉（1 次调用 = 1 个横截面），按月落盘 + 断点续跑；默认先用月末交易日（约 105 天，
几分钟内可用），再补其余交易日。字段取 Tushare 原始 `total_mv/circ_mv/total_share/float_share/
free_share/turnover_rate/turnover_rate_f/volume_ratio/pe/pe_ttm/pb/ps/ps_ttm/dv_ratio/dv_ttm/close`。

用法：
  uv run python scripts/backfill_cn_daily_basic_relay.py --plan            # 只看计划（不拉数据）
  uv run python scripts/backfill_cn_daily_basic_relay.py --month-ends-only --workers 4
  uv run python scripts/backfill_cn_daily_basic_relay.py --workers 4       # 全交易日（默认 2018 起）
  uv run python scripts/backfill_cn_daily_basic_relay.py --status
输出：assets/data/raw/daily_basic_relay/month=YYYY-MM.parquet（按月，可续跑）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "*")

from data.store.scheduler_env import load_scheduler_env  # noqa: E402
from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient  # noqa: E402

OUT = ROOT / "assets/data/raw/daily_basic_relay"
OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
FIELDS = ["ts_code", "trade_date", "close", "turnover_rate", "turnover_rate_f", "volume_ratio",
          "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm",
          "total_share", "float_share", "free_share", "total_mv", "circ_mv"]
_LOCK = threading.Lock()


def trading_days(start: str, end: str) -> list[pd.Timestamp]:
    """用本地 clean ohlcv 的交易日历（避免额外调用 trade_cal）。"""
    import pyarrow.dataset as ds

    files = sorted(str(p) for p in OHLCV.glob("exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet"))
    table = ds.dataset(files, format="parquet").to_table(columns=["trade_date"]).to_pandas()
    days = pd.to_datetime(table["trade_date"]).drop_duplicates().sort_values()
    days = days[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))]
    return list(days)


def fetch_day(client: CNTushareRelayClient, day: pd.Timestamp, retries: int = 3) -> pd.DataFrame:
    stamp = day.strftime("%Y%m%d")
    last = ""
    for attempt in range(1, retries + 1):
        try:
            frame, _gw = client.get("daily_basic", trade_date=stamp, limit=6000)
            if frame.empty:
                return pd.DataFrame(columns=FIELDS)
            keep = [c for c in FIELDS if c in frame.columns]
            out = frame[keep].copy()
            out["trade_date"] = pd.to_datetime(out["trade_date"])
            # 中继偶发把缺失值回成空字符串（实测 volume_ratio=''）→ 全部数值列强制转换，
            # 否则写 parquet 时会 ArrowInvalid（tried to convert '' to double）。
            for col in out.columns:
                if col not in ("ts_code", "trade_date"):
                    out[col] = pd.to_numeric(out[col], errors="coerce")
            return out
        except Exception as exc:                     # 中继偶发 503/超时 → 退避重试
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"{stamp} 拉取失败：{last}")


def month_file(day: pd.Timestamp) -> Path:
    return OUT / f"month={day.strftime('%Y-%m')}.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(description="中继回补 daily_basic（按交易日横截面）")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--month-ends-only", action="store_true", help="只拉每月最后一个交易日（先解锁月度回测）")
    ap.add_argument("--plan", action="store_true", help="只打印计划")
    ap.add_argument("--status", action="store_true", help="只打印落盘进度")
    args = ap.parse_args()

    load_scheduler_env()
    end = args.end or datetime.now().strftime("%Y-%m-%d")
    days = trading_days(args.start, end)
    if args.month_ends_only:
        s = pd.Series(days)
        df = pd.DataFrame({"d": s, "ym": s.dt.to_period("M")})
        days = list(df.groupby("ym")["d"].max())
    print(f"[db] 目标交易日 {len(days)} 天（{days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d}），"
          f"落盘目录 {OUT}", flush=True)
    if args.plan:
        print("[db] 计划已打印（未拉数据）")
        return 0
    if args.status:
        have = sorted(OUT.glob("month=*.parquet")) if OUT.exists() else []
        total = 0
        for f in have:
            total += len(pd.read_parquet(f, columns=["ts_code"]))
        print(f"[db] 已落盘 {len(have)} 个月文件 / {total:,} 行")
        return 0

    # 待办：按月分组；已有该月文件且行数>0 则跳过（续跑）
    buckets: dict[str, list[pd.Timestamp]] = {}
    for d in days:
        buckets.setdefault(d.strftime("%Y-%m"), []).append(d)
    todo = {m: ds_ for m, ds_ in buckets.items() if not month_file(ds_[0]).exists()}
    print(f"[db] 待拉月份 {len(todo)}/{len(buckets)}（已有文件的月份自动跳过）", flush=True)

    client = CNTushareRelayClient()
    fetched = 0
    t0 = time.time()
    for month in sorted(todo):
        frames, errors = [], []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(fetch_day, client, d): d for d in todo[month]}
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    frames.append(fut.result())
                except Exception as exc:
                    errors.append(f"{d:%Y-%m-%d}: {exc}")
        if frames:
            out = pd.concat(frames, ignore_index=True).drop_duplicates(["trade_date", "ts_code"])
            OUT.mkdir(parents=True, exist_ok=True)
            out.to_parquet(month_file(todo[month][0]), index=False)
            fetched += len(todo[month])
        rate = fetched / max(time.time() - t0, 1e-6)
        print(f"[db] {month}: {len(frames)}/{len(todo[month])} 天成功，累计 {fetched} 天 "
              f"({rate:.2f} 天/秒)，错误 {len(errors)}", flush=True)
        if errors:
            (OUT / "errors.jsonl").open("a", encoding="utf-8").write(
                "\n".join(json.dumps({"month": month, "error": e}, ensure_ascii=False) for e in errors) + "\n")
    print(f"[db] 完成：本次 {fetched} 个交易日，用时 {(time.time()-t0)/60:.1f} 分钟；输出 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
