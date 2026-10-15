#!/usr/bin/env python3
"""当日数据就绪闸门（P2.55）：18:00 起跑时，先确认"今天的日线已经落地"，否则别往下跑。

为什么需要（2026-10-12 用户要求把每日自动化提前到 18:00）：收盘（15:00）后中继/数据源通常要
17:00~18:30 才把当日日线铺开，且会偶发 429/503。**如果 18:00 就开跑而当天日线还没到位**，
流水线里那些"按最新交易日取数"的步骤会安静地拿到**昨天**的数据 —— 最后 `web-freshness` 也会"全绿"
（因为它比的也是昨天），于是你以为今天出了新名单，其实没有。这就是本闸门要挡的东西。

做法（不自己抓数据，只**等** + **判**）：
  1. 今天不是交易日（或还没到 --min-hour）⇒ 直接放行（exit 0，`skip`）；
  2. **先问数据源**：日线的上游是腾讯（`data_source="tencent"`，见 `config/cn_pipeline.toml`
     的 `[daily_bars]`），用一次约 1 秒的探针问"今天有没有 bar"；本地日线（基准指数
     `cn_benchmark_daily.parquet` 与 clean 日线取最大日期）作为第二判据；两者任一成立即就绪；
  3. 若 < 今天：每 --sleep-seconds 秒重查一次，最多 --wait-minutes 分钟（默认 5 分钟 × 9 次 = 45 分钟）；
  4. 超时仍未就绪 ⇒ **exit 1**（让上游的 run_data_step/FAILED 逻辑照常发邮件），并把状态写进
     `output/data_quality/daily_ready_gate.json`，供人工与后续审计核对。

用法：
    uv run python scripts/daily_ready_gate.py --print           # 实际用于每日流程
    uv run python scripts/daily_ready_gate.py --check           # 只看一眼，不等

为什么要有"问数据源"这一路（2026-10-14 修）：本地日线是**流水线自己**写的 —— clean 日线由
`daily_bars` 阶段写、基准指数由紧随其后的 `benchmark` 数据步骤写，而这两步都跑在闸门**之后**
⇒ 只按"本地最大日期 ≥ 今天"判，闸门就是**自指的死锁**：只要漏跑一天（或闸门第一次上线），
本地就永远追不上今天，18:30 / 20:00 / 21:30 三档会全部在闸门处中止，自动化再也起不来。
实测 2026-10-12 20:00 那次之后本地一直停在 10-12（`daily_ready_gate.json` 的 clean_latest），
所以修完护栏 bug 也**必须**补这一路，否则今晚照样卡死。
探针可关：`READY_GATE_UPSTREAM=0`（退回纯本地判据）；探针标的：`READY_GATE_PROBE_SYMBOL`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
CLEAN = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "output/data_quality/daily_ready_gate.json"
PROBE_SYMBOL = os.environ.get("READY_GATE_PROBE_SYMBOL", "000001.SZ")


def is_trading_weekday(day) -> bool:
    return day.weekday() < 5


def bench_latest_date() -> str | None:
    if not BENCH.exists():
        return None
    try:
        frame = pd.read_parquet(BENCH, columns=["ts_code", "trade_date"])
    except Exception:
        return None
    series = pd.to_datetime(frame[frame["ts_code"] == "000905.SH"]["trade_date"]).dropna()
    return str(series.max().date()) if not series.empty else None


def clean_latest_date() -> str | None:
    """clean 日线的最大日期（只读 trade_date 列，几百毫秒）。"""
    import pyarrow.dataset as ds

    files = [str(p) for p in CLEAN.glob("exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")]
    if not files:
        return None
    try:
        table = ds.dataset(files, format="parquet").to_table(columns=["trade_date"])
    except Exception:
        return None
    series = pd.to_datetime(table.to_pandas()["trade_date"]).dropna()
    return str(series.max().date()) if not series.empty else None


def upstream_has_today(today) -> bool | None:
    """问日线数据源（腾讯，与 `[daily_bars].data_source` 同源）：今天有没有 bar。

    单票单日、约 1 秒。取不到（网络/接口抽风）返回 None ⇒ 退回本地判据，绝不因此误判"就绪"。
    """
    if os.environ.get("READY_GATE_UPSTREAM", "1") != "1":
        return None
    try:
        import akshare as ak
    except Exception:
        return None
    symbol = ("sh" if PROBE_SYMBOL.endswith(".SH") else "sz") + PROBE_SYMBOL.split(".")[0]
    stamp = today.strftime("%Y%m%d")
    try:
        frame = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=stamp, end_date=stamp,
                                      adjust="", timeout=6)
    except Exception:
        return None
    if frame is None or len(frame) == 0:
        return False
    try:
        return str(pd.to_datetime(frame["date"]).max().date()) >= today.isoformat()
    except Exception:
        return None


def evaluate(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    today = now.date()
    bench, clean = bench_latest_date(), clean_latest_date()
    latest = max([d for d in (bench, clean) if d] or [""]) if (bench or clean) else None
    trading = is_trading_weekday(today)
    local_ready = bool(latest) and latest >= today.isoformat()
    # 本地已就绪就不打网络；否则问一次数据源（上游有今天 == 本地落后只是因为还没跑）
    upstream = None if local_ready else upstream_has_today(today)
    ready = local_ready or upstream is True
    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "is_trading_weekday": trading,
        "bench_latest": bench,
        "clean_latest": clean,
        "latest": latest,
        "upstream_has_today": upstream,
        "judged_by": "local" if local_ready else ("upstream" if upstream is True else "none"),
        "ready": ready,
        # 非交易日一律 skip：这一天本来就不该跑，"本地有没有数据"与就绪性无关
        "verdict": "skip" if not trading else ("ready" if ready else "not_ready"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="当日数据就绪闸门")
    ap.add_argument("--wait-minutes", type=int, default=45)
    ap.add_argument("--sleep-seconds", type=int, default=300)
    ap.add_argument("--min-hour", type=int, default=15, help="在此之前不检查（收盘前不算落后）")
    ap.add_argument("--check", action="store_true", help="只看一眼，不等待")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    deadline = time.monotonic() + max(args.wait_minutes, 0) * 60
    attempts = 0
    payload = evaluate()
    while True:
        attempts += 1
        payload = evaluate()
        if payload["ready"] or not payload["is_trading_weekday"]:
            break
        if datetime.now().time() < dtime(hour=args.min_hour):
            payload["verdict"] = "skip_before_close"
            break
        if args.check or time.monotonic() >= deadline:
            break
        if args.print:
            print(f"[ready-gate] 本地最新 {payload['latest'] or '—'}、上游今天={payload['upstream_has_today']}"
                  f" < 今天 {payload['today']}；等 {args.sleep_seconds}s 再看一次"
                  f"（第 {attempts} 次，最多 {args.wait_minutes} 分钟）")
        time.sleep(args.sleep_seconds)

    payload["attempts"] = attempts
    payload["waited_minutes"] = round(args.wait_minutes if attempts > 1 else 0, 1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    exit_code = 0
    if payload["is_trading_weekday"] and not payload["ready"] and datetime.now().time() >= dtime(hour=args.min_hour):
        exit_code = 1
        if args.print:
            print(f"[ready-gate] ❌ 当日日线仍未就绪：本地最新 {payload['latest'] or '—'} < 今天 {payload['today']}"
                  f"、上游探针={payload['upstream_has_today']}"
                  f"（等了 {attempts} 次）—— 继续跑会安静地拿到昨天的数据，所以这里**中断**。"
                  f"稍后由 20:00 / 21:30 的重试触发；或手动：bash scripts/run_daily_production.sh --force")
    elif args.print:
        reason = "已就绪" if payload["ready"] else ("非交易日" if not payload["is_trading_weekday"] else "收盘前不判")
        print(f"[ready-gate] ✅ {reason}（本地最新 {payload['latest'] or '—'}；今天 {payload['today']}；"
              f"判据={payload['judged_by']}、上游今天={payload['upstream_has_today']}）-> {OUT.relative_to(ROOT)}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
