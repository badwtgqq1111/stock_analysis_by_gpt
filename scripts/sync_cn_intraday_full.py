#!/usr/bin/env python3
"""全 A 股分时同步「监督器」（对 baostock 卡死免疫）。

为什么需要监督器（2026-10-01 事故记录）：
  * 第一版用 SIGALRM 给单只标的加 45s 硬超时，但 baostock 阻塞在 socket 读时**信号打不断**，
    结果一只标的把整个会话挂了 4 天 10 小时（进度停在 695/5224）；
  * 代码清单/日志/台账原先放在 /tmp，被系统清理后外层循环直接失效。

本监督器把这两点都修掉：
  1) **子进程级硬超时**：每批 N 只交给一个子进程，`subprocess.run(timeout=...)` 到点直接 SIGKILL，
     卡死最多损失一批（默认 5 只 ≈ 60 秒），不会拖垮整轮；
  2) **状态入库**：代码清单、日志、台账全部落在 `assets/data/raw/intraday_baostock/_sync/`，
     不依赖 /tmp；逐股检查点仍然生效，中断/重启零损失；
  3) **失败隔离**：某只连续超时 3 次即进 quarantine 列表，不阻塞其余 5,000+ 只；
  4) **会话限时**：默认跑满 1 小时自行退出（配合 LaunchAgent 重新拉起，天然清理泄漏状态）。

用法：
  uv run python scripts/sync_cn_intraday_full.py --build-codes        # 生成/刷新代码清单
  uv run python scripts/sync_cn_intraday_full.py --status             # 查看进度
  uv run python scripts/sync_cn_intraday_full.py --session-seconds 3600
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "assets/data/raw/intraday_baostock/_sync"
DEFAULT_CODES = STATE_DIR / "codes.txt"
DEFAULT_LOG = STATE_DIR / "sync.log"
DEFAULT_PROGRESS = STATE_DIR / "progress.json"
DEFAULT_QUARANTINE = STATE_DIR / "quarantine.json"
UV = Path.home() / ".local/bin/uv"


def log(path: Path, message: str) -> None:
    stamp = datetime.now().strftime("%F %T")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def build_codes(freq: str, out: Path) -> int:
    """全 A 股（沪+深在市）代码清单，策略交易过的标的排最前。"""
    basic = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    frame = pd.read_parquet(basic)
    col = "list_status" if "list_status" in frame.columns else "status"
    live = frame[frame[col] == "L"]
    live = live[live["ts_code"].str.match(r"^\d{6}\.(SH|SZ)$")]     # 北交所 BaoStock 不提供
    codes = sorted(set(live["ts_code"]))

    done = {p.name.replace("_", ".")[:-8] for p in (ROOT / f"assets/data/raw/intraday_baostock/frequency={freq}min").glob("*.parquet")}
    trades = ROOT / "output/results_cn/xiaomkt_rotation_optimized/trades.csv"
    traded = []
    if trades.exists():
        traded = [c for c in pd.unique(pd.read_csv(trades)["code"]) if c in set(codes)]
    ordered = [c for c in traded if c not in done] + [c for c in codes if c not in set(traded) and c not in done]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(ordered) + "\n", encoding="utf-8")
    return len(ordered)


def status(freq: str, codes_path: Path, progress_path: Path) -> dict:
    freq_dir = ROOT / f"assets/data/raw/intraday_baostock/frequency={freq}min"
    have = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
    target = [c for c in codes_path.read_text(encoding="utf-8").split() if c.strip()] if codes_path.exists() else []
    remaining = [c for c in target if c not in have]
    ledger = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    rate = float(ledger.get("rate_per_sec") or 0)
    return {
        "target_total": len(target) + len(have & set(target)),
        "done": len(have),
        "remaining": len(remaining),
        "rate_per_sec": rate,
        "eta_hours": round(len(remaining) / rate / 3600, 2) if rate > 0 else None,
        "last_ledger": ledger.get("updated_at"),
        "next_codes": remaining[:5],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="全 A 股分时同步监督器")
    parser.add_argument("--freq", default="5")
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--codes-file", type=Path, default=DEFAULT_CODES)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--progress-file", type=Path, default=DEFAULT_PROGRESS)
    parser.add_argument("--quarantine-file", type=Path, default=DEFAULT_QUARANTINE)
    parser.add_argument("--batch-size", type=int, default=5, help="每批交给一个子进程的标的数")
    parser.add_argument("--batch-timeout", type=int, default=180, help="每批硬超时（秒），到点 SIGKILL")
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--session-seconds", type=int, default=3600)
    parser.add_argument("--max-failures-per-code", type=int, default=3)
    parser.add_argument("--build-codes", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.build_codes:
        count = build_codes(args.freq, args.codes_file)
        print(json.dumps({"built": str(args.codes_file), "pending": count}, ensure_ascii=False, indent=2))
        return 0
    if args.status:
        print(json.dumps(status(args.freq, args.codes_file, args.progress_file), ensure_ascii=False, indent=2))
        return 0

    freq_dir = ROOT / f"assets/data/raw/intraday_baostock/frequency={args.freq}min"
    freq_dir.mkdir(parents=True, exist_ok=True)
    if not args.codes_file.exists():
        build_codes(args.freq, args.codes_file)
    target = [c for c in args.codes_file.read_text(encoding="utf-8").split() if c.strip()]
    quarantine = json.loads(args.quarantine_file.read_text()) if args.quarantine_file.exists() else {}
    session_start = time.time()
    log(args.log, f"=== 监督器启动 目标 {len(target)} 只 freq={args.freq}min batch={args.batch_size} ===")

    processed = 0
    while True:
        have = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
        pending = [c for c in target if c not in have and quarantine.get(c, 0) < args.max_failures_per_code]
        if not pending:
            log(args.log, f"全部完成或有配额隔离：done={len(have)} quarantine={sum(1 for v in quarantine.values() if v >= args.max_failures_per_code)}")
            break
        if time.time() - session_start > args.session_seconds:
            log(args.log, f"会话达到 {args.session_seconds}s，正常退出（由 LaunchAgent 重新拉起）")
            break

        batch = pending[: args.batch_size]
        command = [
            str(UV), "run", "python", str(ROOT / "scripts/backfill_cn_intraday_baostock.py"),
            "--codes", ",".join(batch), "--keep-order", "--freq", args.freq, "--start", args.start,
            "--sleep", str(args.sleep), "--per-code-timeout", "30",
        ]
        batch_start = time.time()
        timed_out = False
        try:
            subprocess.run(command, cwd=ROOT, timeout=args.batch_timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except subprocess.TimeoutExpired:
            timed_out = True
            for code in batch:
                quarantine[code] = quarantine.get(code, 0) + 1
        duration = time.time() - batch_start
        processed += len(batch)

        have_now = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
        if not timed_out:
            for code in batch:
                if code not in have_now:
                    quarantine[code] = quarantine.get(code, 0) + 1
        args.quarantine_file.write_text(json.dumps(quarantine, ensure_ascii=False, indent=2), encoding="utf-8")

        done = len(have_now)
        elapsed = max(time.time() - session_start, 1e-9)
        rate = processed / elapsed
        args.progress_file.write_text(json.dumps({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "frequency": f"{args.freq}min",
            "done_total": done,
            "target_total": len(target),
            "session_processed": processed,
            "rate_per_sec": round(rate, 4),
            "eta_hours": round((len(target) - done) / rate / 3600, 2) if rate > 0 else None,
            "last_batch_seconds": round(duration, 1),
            "last_batch_timed_out": timed_out,
            "quarantined": sum(1 for v in quarantine.values() if v >= args.max_failures_per_code),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        if timed_out:
            log(args.log, f"批次超时（{duration:.0f}s）→ 已隔离 {len(batch)} 只，当前 done={done}/{len(target)}")
            time.sleep(10)
        elif processed % 50 < args.batch_size:
            log(args.log, f"进度 {done}/{len(target)} | 本会话 {processed} 只 | {rate:.3f} 只/秒 | "
                          f"ETA {((len(target) - done) / rate / 3600 if rate > 0 else 0):.1f}h")

    log(args.log, f"=== 监督器退出 done={len({p.name for p in freq_dir.glob('*.parquet')})} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
