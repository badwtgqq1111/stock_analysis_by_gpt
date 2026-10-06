#!/usr/bin/env python3
"""用 BaoStock 回补 A 股分时数据（5/15/30/60min），面向"近一年 5min 全历史"场景。

为什么是 BaoStock（2026-10-01 实测，见 docs/runbooks/cn-intraday-sources.md）：
  * 腾讯 mkline：单次最多 320 根且无分页 → 5min 只有最近约 7 个交易日；
  * 东财 push2his：首次 1488 根（约 31 个交易日），连续请求后直接被拒（RemoteDisconnected）；
  * 中继 Tushare `stk-mins` / `a-share-mins`：HTTP 400，未接入；
  * BaoStock：5min 近一年 11,520 行/股、60min 2.75 年 2,664 行，历史完整 —— 但**并发会卡死**，
    连续大批量请求也会被限流，所以本脚本设计为**单线程 + 逐股检查点 + 指数退避**。

用法：
  # 只补策略真正交易过的标的（推荐：冲击成本校准只需要这些）
  uv run python scripts/backfill_cn_intraday_baostock.py \\
      --codes-from-trades output/results_cn/xiaomkt_rotation_optimized/trades.csv \\
      --freq 5 --start 2025-10-01

  # 指定代码
  uv run python scripts/backfill_cn_intraday_baostock.py --codes 600519.SH,002193.SZ --freq 5

  # 断点续跑：默认跳过已有检查点的标的，--force 才重拉
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIELDS = "date,time,open,high,low,close,volume,amount"


def to_baostock_code(code: str) -> str:
    code = code.strip().upper()
    if "." not in code:
        raise ValueError(f"非法代码 {code}（应为 600519.SH 形式）")
    num, exch = code.split(".", 1)
    return f"{ 'sh' if exch == 'SH' else 'sz' }.{num}"


def codes_from_trades(path: Path) -> list[str]:
    frame = pd.read_csv(path, usecols=["code", "side"])
    buys = frame[frame["side"] == "BUY"]["code"].dropna().unique().tolist()
    return sorted(buys)


def fetch_one(bs, code: str, freq: str, start: str, end: str, *, retries: int, backoff: float, sleep: float):
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            rs = bs.query_history_k_data_plus(
                to_baostock_code(code), FIELDS,
                start_date=start, end_date=end, frequency=freq, adjustflag="2",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                frame = pd.DataFrame(rows, columns=FIELDS.split(","))
                for col in ("open", "high", "low", "close", "volume", "amount"):
                    frame[col] = pd.to_numeric(frame[col], errors="coerce")
                frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
                frame["stock_code"] = code.upper()
                frame["frequency"] = f"{freq}min"
                frame["adjust"] = "qfq"
                frame["source"] = "baostock"
                return frame, ""
            last_error = f"empty(result_code={rs.error_code}, msg={rs.error_msg})"
        except Exception as exc:  # 网络层异常同样退避重试
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(backoff * attempt)
    time.sleep(sleep)
    return pd.DataFrame(), last_error


class _Timeout(Exception):
    pass


def _with_timeout(seconds: int, func, *args, **kwargs):
    """用 SIGALRM 给单只标的的抓取加硬超时：baostock 被限流时会卡在 socket 上不返回。"""
    if not seconds or seconds <= 0:
        return func(*args, **kwargs)

    def _handler(signum, frame):  # pragma: no cover - 仅用于中断阻塞调用
        raise _Timeout(f"per-code timeout {seconds}s")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(int(seconds))
    try:
        return func(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description="BaoStock 分时回补（单线程 + 检查点）")
    parser.add_argument("--freq", default="5", choices=["5", "15", "30", "60"])
    parser.add_argument("--start", default=(date.today() - timedelta(days=365)).isoformat())
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--codes", default="")
    parser.add_argument("--codes-file", type=Path, default=None,
                        help="每行一个代码（或逗号分隔）的文本文件；与 --codes 取并集")
    parser.add_argument("--codes-from-trades", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "assets/data/raw/intraday_baostock")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-order", action="store_true",
                        help="保持传入顺序（默认排序）；用于「策略交易过的标的优先」")
    parser.add_argument("--per-code-timeout", type=int, default=90,
                        help="单只标的抓取硬超时（秒）；baostock 限流时卡住不返回，靠它跳过并继续")
    parser.add_argument("--max-failures", type=int, default=0,
                        help=">0：连续失败该次数后退出（让外层循环休眠后重试）；0 = 不退出")
    parser.add_argument("--adaptive-sleep", action="store_true",
                        help="连续失败时自动加大 sleep（限流退避），成功后恢复")
    parser.add_argument("--progress-file", type=Path, default=None,
                        help="进度台账 JSON（已完成数、速率、ETA），供长跑任务监控")
    parser.add_argument("--session-seconds", type=int, default=0,
                        help=">0：本次会话最多运行这么多秒后自行退出（macOS 无 timeout，用它在脚本内限时）")
    parser.add_argument("--status", action="store_true",
                        help="只打印当前进度（检查点数 / 目标数 / 速率 / ETA）后退出，不抓取")
    args = parser.parse_args()

    codes: list[str] = []
    if args.codes:
        codes += [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.codes_file and args.codes_file.exists():
        text = args.codes_file.read_text(encoding="utf-8")
        codes += [c.strip() for c in text.replace("\n", ",").split(",") if c.strip()]
    if args.codes_from_trades:
        codes += codes_from_trades(args.codes_from_trades)
    codes = list(dict.fromkeys(codes)) if args.keep_order else sorted(set(codes))
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        print("没有待补标的：用 --codes 或 --codes-from-trades 指定", flush=True)
        return 2

    freq_dir = args.out_dir / f"frequency={args.freq}min"
    freq_dir.mkdir(parents=True, exist_ok=True)
    if args.status:
        have = {p.stem.replace("_", ".") for p in freq_dir.glob("*.parquet")}
        todo = [c for c in codes if c not in have]
        progress = json.loads(args.progress_file.read_text()) if args.progress_file and args.progress_file.exists() else {}
        rate = float(progress.get("rate_per_sec") or 0.0)
        print(json.dumps({
            "target_total": len(codes),
            "done": len(have),
            "remaining": len(todo),
            "rate_per_sec": rate,
            "eta_hours": round(len(todo) / rate / 3600, 2) if rate > 0 else None,
            "next_codes": todo[:8],
        }, ensure_ascii=False, indent=2), flush=True)
        return 0
    print(f"[intraday] BaoStock 回补 {len(codes)} 只 × {args.freq}min × {args.start}~{args.end} -> {freq_dir}", flush=True)

    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        print(f"[intraday] baostock 登录失败：{login.error_code} {login.error_msg}", flush=True)
        return 1

    ok, skipped, failed = 0, 0, []
    consecutive_failures = 0
    sleep_now = args.sleep
    started = time.time()
    try:
        for idx, code in enumerate(codes, start=1):
            if args.session_seconds and (time.time() - started) > args.session_seconds:
                print(f"[intraday] 会话达到 {args.session_seconds}s 上限，正常退出（下次续跑）", flush=True)
                break
            path = freq_dir / f"{code.replace('.', '_')}.parquet"
            if path.exists() and not args.force:
                skipped += 1
                continue
            try:
                frame, error = _with_timeout(
                    args.per_code_timeout, fetch_one, bs, code, args.freq, args.start, args.end,
                    retries=args.retries, backoff=args.backoff, sleep=sleep_now,
                )
            except _Timeout as exc:
                frame, error = pd.DataFrame(), str(exc)
            if frame.empty:
                failed.append({"code": code, "error": error})
                consecutive_failures += 1
                print(f"[intraday] {idx}/{len(codes)} {code} 失败：{error}", flush=True)
                if args.adaptive_sleep:
                    sleep_now = min(sleep_now * 1.5 + 0.5, 30.0)
                if args.max_failures and consecutive_failures >= args.max_failures:
                    print(f"[intraday] 连续失败 {consecutive_failures} 次，退出本次会话（外层可休眠后续跑）", flush=True)
                    break
                continue
            consecutive_failures = 0
            if args.adaptive_sleep:
                sleep_now = args.sleep
            frame.to_parquet(path, index=False)
            ok += 1
            if idx % 10 == 0 or idx == len(codes):
                print(f"[intraday] {idx}/{len(codes)} 完成（成功 {ok} / 跳过 {skipped} / 失败 {len(failed)}）", flush=True)
            if args.progress_file:
                done_total = len(list(freq_dir.glob("*.parquet")))
                elapsed = max(time.time() - started, 1e-9)
                rate = ok / elapsed if ok else 0.0
                args.progress_file.parent.mkdir(parents=True, exist_ok=True)
                args.progress_file.write_text(json.dumps({
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "frequency": f"{args.freq}min",
                    "target_total": len(codes) + skipped,
                    "done_total": done_total,
                    "session_ok": ok, "session_skipped": skipped, "session_failed": len(failed),
                    "rate_per_sec": round(rate, 4),
                    "eta_hours": round((len(codes) - idx) / rate / 3600, 2) if rate > 0 else None,
                    "sleep_now": round(sleep_now, 2),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            time.sleep(sleep_now)
    finally:
        bs.logout()

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "frequency": f"{args.freq}min",
        "window": [args.start, args.end],
        "requested": len(codes),
        "succeeded": ok,
        "skipped_existing": skipped,
        "failed": failed,
        "out_dir": str(freq_dir),
    }
    (freq_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
