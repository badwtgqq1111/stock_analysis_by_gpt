#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复权因子同步（Tushare relay）—— 数据体检里"未达标"三项之一，实为**开关问题**。

诊断（2026-10-09）
------------------
`data_completeness_report.py` 报 `adj_factor` 未达标，原因是目录
`assets/data/raw/adjustment_factor_snapshots` **根本不存在** —— 而配置里写着
`[relay_data].adjustment_factors_enabled = false`。也就是说：**不是 Tushare 没数据，
是这份数据一直没开**。实测 relay 的 `adj_factor` 接口可用（base 网关，
000001.SZ 2026-10-08 因子 142.0699）。

本脚本独立于主流水线的 relay 阶段（那条路要跑完整 fundamental 链路），
只做一件事：**按股票增量补齐 adj_factor 原始快照**，可断点续跑、可限时，
产物路径与 `data/ingest/service.py` 的布局一致，方便之后主链路直接复用。

用法
----
  uv run python scripts/sync_cn_adj_factor.py --session-seconds 900     # 增量：只补落后的票
  uv run python scripts/sync_cn_adj_factor.py --max-per-run 200         # 本次最多补 200 只
  uv run python scripts/sync_cn_adj_factor.py --status                  # 只看覆盖率，不抓
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "assets/data/raw/adjustment_factor_snapshots/market=CN/source=tushare_relay"
COVERAGE = OUT_DIR / "_coverage.json"
REPORT_DIR = ROOT / "output/pipeline_reports/adj_factor_sync"
DEFAULT_START = "2015-01-01"


def _targets() -> list[str]:
    """要覆盖的股票：优先用基础表（含北交所），退化到分时同步的 codes.txt。"""
    basic = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    if basic.exists():
        frame = pd.read_parquet(basic, columns=["ts_code", "list_status"])
        codes = frame[frame["list_status"].astype(str).str.upper() == "L"]["ts_code"].astype(str)
        if len(codes) > 1000:
            return sorted(codes.unique().tolist())
    codes_file = ROOT / "assets/data/raw/intraday_baostock/_sync/codes.txt"
    if codes_file.exists():
        return sorted({c.strip() for c in codes_file.read_text().split() if c.strip()})
    return []


def _last_date(code: str) -> str | None:
    path = OUT_DIR / f"{code.replace('.', '_')}.parquet"
    if not path.exists():
        return None
    try:
        frame = pd.read_parquet(path, columns=["trade_date"])
        return str(pd.to_datetime(frame["trade_date"]).max().date())
    except Exception:                                          # noqa: BLE001
        return None


def sync_by_date(days: int, report: dict) -> None:
    """按交易日同步**全市场**（一次请求 5,000 行、分页取满）。

    为什么不是逐股：逐股一天要 5,572 次请求（实测 1.25s/只 ≈ 1.9 小时），
    而 `adj_factor(trade_date=...)` 一次就给当天全市场 5,000 行（1.2s）——
    日更必须用按日口径，逐股只用于历史回补。
    """
    from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient
    from backend.services.market_board_service import trade_dates

    client = CNTushareRelayClient()
    dates = [d.strftime("%Y-%m-%d") for d in trade_dates(max(days + 5, 30))][-int(days):]
    report["by_date"] = {"dates": dates, "fetched": [], "failed": []}
    for day in dates:
        target = OUT_DIR / f"date={day.replace('-', '')}" / "adj_factor.parquet"
        if target.exists():
            continue
        rows, offset, errors = [], 0, None
        while True:
            try:
                frame, _ = client.get("adj_factor", trade_date=day.replace("-", ""),
                                      limit=5000, offset=offset)
            except Exception as exc:                           # noqa: BLE001
                errors = f"{type(exc).__name__}: {exc}"[:140]
                break
            if frame is None or frame.empty:
                break
            rows.append(frame)
            if len(frame) < 5000:
                break
            offset += 5000
            if offset >= 12000:                                # 全市场 ~5,600 只，两页足够
                break
        if rows:
            merged = pd.concat(rows, ignore_index=True).drop_duplicates(subset=["ts_code"], keep="last")
            merged["trade_date"] = pd.to_datetime(merged["trade_date"])
            merged = merged.rename(columns={"ts_code": "stock_code", "adj_factor": "adj_factor"})
            target.parent.mkdir(parents=True, exist_ok=True)
            merged.to_parquet(target, index=False)
            report["by_date"]["fetched"].append({"date": day, "rows": int(len(merged))})
        elif errors:
            report["by_date"]["failed"].append({"date": day, "error": errors})


def write_coverage(report: dict) -> dict:
    """给数据体检用的覆盖摘要（避免体检去扫几千个 parquet）。"""
    date_files = sorted(OUT_DIR.glob("date=*/adj_factor.parquet"))
    stock_files = sorted(OUT_DIR.glob("*.parquet"))
    latest, rows = None, 0
    if date_files:
        frame = pd.read_parquet(date_files[-1], columns=["trade_date"])
        latest = str(pd.to_datetime(frame["trade_date"]).max().date())
        rows = int(len(frame))
    coverage = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                "latest_date": latest, "stocks_on_latest": rows,
                "by_date_files": len(date_files), "by_stock_files": len(stock_files)}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    COVERAGE.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    return coverage


def main() -> int:
    parser = argparse.ArgumentParser(description="复权因子增量同步（Tushare relay）")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--max-per-run", type=int, default=200)
    parser.add_argument("--session-seconds", type=float, default=900.0)
    parser.add_argument("--status", action="store_true", help="只看覆盖率")
    parser.add_argument("--mode", default="auto", choices=["auto", "by-date", "by-stock"],
                        help="auto=先按日同步全市场（日更用），再按股票补历史（受 --max-per-run 限制）")
    parser.add_argument("--days", type=int, default=5, help="按日同步往回几天（默认 5 个交易日）")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    targets = _targets()
    if not targets:
        print("[adj-factor] 没有股票清单（缺 cn_stock_basic.parquet）", flush=True)
        return 1
    from backend.services.market_board_service import latest_trade_date
    as_of = latest_trade_date()
    as_of_str = str(pd.Timestamp(as_of).date()) if as_of is not None else str(datetime.now().date())

    stale = [c for c in targets if (_last_date(c) or "") < as_of_str] if not args.force else list(targets)
    covered = len(targets) - len(stale)
    print(f"[adj-factor] 目标 {len(targets)} 只；已到 {as_of_str} 的 {covered} 只；待补 {len(stale)} 只",
          flush=True)
    report: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"), "as_of": as_of_str,
                    "mode": args.mode, "targets": len(targets), "covered_before": covered,
                    "stale": len(stale), "failures": []}
    if args.status:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"{datetime.now():%Y-%m-%d}.json").write_text(json.dumps(
            {"as_of": as_of_str, "targets": len(targets), "covered": covered, "stale": len(stale),
             "status": "status-only"}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    from data.ingest.providers.cn_tushare_relay import CNAdjustmentFactorRelayFetcher

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.mode in ("auto", "by-date"):
        sync_by_date(args.days, report)
        got = report.get("by_date", {}).get("fetched", [])
        print(f"[adj-factor] 按日同步 {len(got)} 个交易日"
              f"（{'、'.join(str(x['date']) + ':' + str(x['rows']) for x in got[-3:]) or '无需更新'}）",
              flush=True)
        if args.mode == "by-date":
            report["coverage"] = write_coverage(report)
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (REPORT_DIR / f"{datetime.now():%Y-%m-%d}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
    todo = stale[: max(int(args.max_per_run), 1)]
    started = time.time()
    deadline = started + args.session_seconds if args.session_seconds else float("inf")
    saved = failed = skipped = 0
    failures: list[dict] = []
    fetcher = CNAdjustmentFactorRelayFetcher("000001.SZ")      # 复用 client，逐只改 ts_code
    for code in todo:
        if time.time() > deadline:
            skipped = len(todo) - saved - failed
            print(f"[adj-factor] 到点停止，剩余 {skipped} 只留给下次", flush=True)
            break
        try:
            fetcher.stock_code = code
            frame = fetcher.fetch(start_date=args.start)
            if frame is None or frame.empty:
                failures.append({"code": code, "error": "空表"})
                failed += 1
                continue
            path = OUT_DIR / f"{code.replace('.', '_')}.parquet"
            old = pd.read_parquet(path) if path.exists() else None
            if old is not None and len(old):
                frame = pd.concat([old, frame], ignore_index=True)
            frame = frame.drop_duplicates(subset=["stock_code", "trade_date"], keep="last") \
                         .sort_values("trade_date")
            frame.to_parquet(path, index=False)
            saved += 1
        except Exception as exc:                               # noqa: BLE001
            failures.append({"code": code, "error": f"{type(exc).__name__}: {exc}"[:140]})
            failed += 1

    elapsed = round(time.time() - started, 1)
    report.update({"todo": len(todo), "saved": saved, "failed": failed, "skipped": skipped,
                   "seconds": elapsed, "dir": str(OUT_DIR.relative_to(ROOT)),
                   "failures": failures[:20]})
    report["coverage"] = write_coverage(report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{datetime.now():%Y-%m-%d}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[adj-factor] 本次补 {saved} 只 / 失败 {failed} 只 / 到点跳过 {skipped} 只（{elapsed}s）"
          f" → {OUT_DIR.relative_to(ROOT)}", flush=True)
    remaining = len(stale) - saved
    print(f"[adj-factor] 剩余待补约 {max(remaining, 0)} 只（每日自动化会继续）", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
