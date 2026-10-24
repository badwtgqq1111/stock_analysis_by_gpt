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
  uv run python scripts/sync_cn_intraday_full.py --refresh --limit 50   # 每日增量：只补最新交易日

`--refresh`（2026-10-08 新增，接进每日自动化）
--------------------------------------------
不加 `--refresh` 时，"完成"的判据是**文件存在** —— 这是为一次性历史回补设计的。
后果是：只要 5,228 个 parquet 都在了，监督器每次启动都会立刻 `done=5228` 退出，
**永远不会去补新的一天**（实测 2026-10-08：数据停在 09-30，而 KeepAlive 每 30 秒空转一次）。

`--refresh` 把判据换成"文件的最后日期 >= 最新交易日"，并且只抓
`最后日期+1 → 今天` 这一小段（每只约 48 行），用 `--merge` 合并回原文件，
不覆盖历史。这样它才能当每日任务跑。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "assets/data/raw/intraday_baostock/_sync"
DEFAULT_CODES = STATE_DIR / "codes.txt"
DEFAULT_LOG = STATE_DIR / "sync.log"
DEFAULT_PROGRESS = STATE_DIR / "progress.json"
DEFAULT_QUARANTINE = STATE_DIR / "quarantine.json"
COOLDOWN = STATE_DIR / "cooldown.json"
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


def full_universe(freq_dir: Path) -> list:
    """刷新模式的目标集：全部在市 A 股 ∪ 已有分时文件（含已退市）。

    2026-10-23 修：原来 --refresh 只遍历 `_sync/codes.txt`，而那份清单是**首次回补时
    还没文件的标的**。结果是「文件已存在」的 695 只在清单之外，永远不会被刷新 ——
    实测 684 只停在 2026-09-30，`--status` 却报 remaining=0，正好对上
    「每天总有一部分同步不成功、要手动补」的现象。
    """
    codes = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
    try:
        basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet")
        col = "list_status" if "list_status" in basic.columns else "status"
        live = basic[basic[col] == "L"]
        codes |= set(live[live["ts_code"].str.match(r"^\d{6}\.(SH|SZ)$")]["ts_code"])
    except Exception:  # noqa: BLE001
        pass
    return sorted(codes)


def status(freq: str, codes_path: Path, progress_path: Path) -> dict:
    freq_dir = ROOT / f"assets/data/raw/intraday_baostock/frequency={freq}min"
    have = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
    target = [c for c in codes_path.read_text(encoding="utf-8").split() if c.strip()] if codes_path.exists() else []
    remaining = [c for c in target if c not in have]
    ledger = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    rate = float(ledger.get("rate_per_sec") or 0)
    # 真实进度：不只看文件在不在，还要看最后日期是否落后于该股自己的最后一条日线
    stale = []
    try:
        expected = latest_daily_by_code()
        for code in sorted(have):
            want = expected.get(code)
            if want is None:
                continue
            try:
                last = pd.to_datetime(pd.read_parquet(freq_dir / f"{code.replace('.', '_')}.parquet",
                                                      columns=["date"])["date"]).max().date()
            except Exception:  # noqa: BLE001
                stale.append(code)
                continue
            if last < want:
                stale.append(code)
    except Exception:  # noqa: BLE001
        pass
    return {
        "target_total": len(target),
        "done": len(have),
        "remaining": len(remaining),
        "stale": len(stale),
        "stale_examples": stale[:5],
        "rate_per_sec": rate,
        "eta_hours": round(len(remaining) / rate / 3600, 2) if rate > 0 else None,
        "last_ledger": ledger.get("updated_at"),
        "next_codes": remaining[:5],
    }


def acquire_lock(lock_dir: Path, stale_seconds: int = 3 * 3600) -> bool:
    """单实例锁。LaunchAgent 的 StartInterval 触发与 run_daily_production.sh 里的 intraday 步骤
    可能同时到点 —— 两个实例抓同一批标的纯属浪费，还可能在 merge 时互相覆盖。

    持有者进程不存活、或锁超过 stale_seconds ⇒ 判定为陈旧锁并接管（与 run_daily_production.sh 同款策略）。
    """
    try:
        lock_dir.mkdir(parents=True)
        (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
        return True
    except FileExistsError:
        pid_text = (lock_dir / "pid").read_text(encoding="utf-8").strip() if (lock_dir / "pid").exists() else ""
        alive = False
        if pid_text.isdigit():
            try:
                os.kill(int(pid_text), 0)
                alive = True
            except OSError:
                alive = False
        age = time.time() - lock_dir.stat().st_mtime
        if alive and age <= stale_seconds:
            return False
        shutil.rmtree(lock_dir, ignore_errors=True)
        try:
            lock_dir.mkdir(parents=True)
            (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
            return True
        except FileExistsError:
            return False


def latest_trade_date() -> "object":
    """最新交易日：直接读日线（qfq）里最大的 trade_date。

    不引入交易日历依赖 —— 日线本身就是"最近一次成功同步到哪天"的最可靠来源。
    """
    import glob
    import pyarrow.dataset as ds

    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    if not files:
        return None
    table = ds.dataset(files, format="parquet").to_table(columns=["trade_date"]).to_pandas()
    return pd.to_datetime(table["trade_date"]).max().date()


def resolve_target_date(daily_max, now) -> "object":
    """把"目标交易日"从"日线最大日期"升级为**日历规则**（2026-10-12 修）。

    原来只读日线的最大 `trade_date` 当作"最新交易日"。后果（F6/F9 同款坑）：
    **日线当天还没同步时，当日的分时永远不会被补** —— 实测今天 12:10 的那次 refresh 判据是
    `最新交易日=2026-10-09`，于是今天（10-12）根本没进目标。

    规则：若"今天"是工作日且已过 **15:05**（baostock 的当日分钟线收盘后才可得），
    就把今天纳入目标；否则沿用日线最大日期。节假日会多试一次（返回空），不影响正确性。
    """
    if daily_max is not None and hasattr(daily_max, "date"):
        pass
    today = now.date() if hasattr(now, "date") else now
    if today.weekday() < 5 and (now.hour, now.minute) >= (15, 5):
        return today if (daily_max is None or today > daily_max) else daily_max
    return daily_max


def refresh_pending(freq_dir: Path, codes: list, latest, expected_by_code=None) -> list:
    """返回 [(code, last_date_or_None)]，只含"没有文件"或"最后日期落后于最新交易日"的标的。

    只读 parquet 的 `date` 列（不解码 OHLCV），5,000+ 只大约几秒到几十秒。
    """
    pending = []
    if expected_by_code is None:
        expected_by_code = latest_daily_by_code() if latest is not None else {}
    for code in codes:
        path = freq_dir / f"{code.replace('.', '_')}.parquet"
        # 停牌/退市股没有当日日线：它的"应有最后日期"是最后一条日线，不是全市场最新交易日。
        # 不修这个，会把 002717.SZ 这类停牌票永远报成 pending，逼用户每天手动重跑。
        expected = expected_by_code.get(code, latest)
        if expected is None:
            continue
        if not path.exists():
            pending.append((code, None))
            continue
        try:
            dates = pd.read_parquet(path, columns=["date"])["date"]
            last = pd.to_datetime(dates).max().date()
        except Exception:
            pending.append((code, None))
            continue
        if last < expected:
            pending.append((code, last))
    return pending


def latest_daily_by_code():
    """每只股票最后一条日线日期；停牌股用它判断分时是否已到自己的最后交易日。"""
    import glob
    import pyarrow.dataset as ds

    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    if not files:
        return {}
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"], errors="coerce")
    table = table.dropna(subset=["trade_date", "stock_code"])
    last = table.groupby("stock_code")["trade_date"].max()
    return {str(code): value.date() for code, value in last.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="全 A 股分时同步监督器")
    parser.add_argument("--freq", default="5")
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--codes-file", type=Path, default=DEFAULT_CODES)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--progress-file", type=Path, default=DEFAULT_PROGRESS)
    parser.add_argument("--quarantine-file", type=Path, default=DEFAULT_QUARANTINE)
    # 2026-10-18：batch=5 时每批 ~55s（uv 启动 + baostock 登录占大头），实测 20 只/15s；
    # 提到 40 后单进程只登录一次，吞吐约 1 只/秒，808 只待补可在 ~15 分钟完成。
    parser.add_argument("--batch-size", type=int, default=40, help="每批交给一个子进程的标的数")
    parser.add_argument("--batch-timeout", type=int, default=180, help="每批硬超时（秒），到点 SIGKILL")
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--session-seconds", type=int, default=3600)
    parser.add_argument("--max-failures-per-code", type=int, default=3)
    parser.add_argument("--build-codes", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--refresh", action="store_true",
                        help="每日增量模式：只补「文件最后日期 < 最新交易日」的标的，且只抓缺失窗口")
    parser.add_argument("--latest-trade-date", default="",
                        help="最新交易日（留空=从日线数据推断）；仅 --refresh 用")
    parser.add_argument("--limit", type=int, default=0,
                        help=">0：本次最多处理这么多只（调试/限时用）")
    parser.add_argument("--reset-quarantine", action="store_true",
                        help="清空隔离计数（历史瞬时失败不该永久挡住这些票）")
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
    if not acquire_lock(STATE_DIR / ".lock"):
        print("[intraday] 已有实例在跑，本次直接退出（避免两个实例抓同一批）", flush=True)
        return 0
    if not args.codes_file.exists():
        build_codes(args.freq, args.codes_file)
    target = [c for c in args.codes_file.read_text(encoding="utf-8").split() if c.strip()]
    if args.refresh:
        target = full_universe(freq_dir)      # 刷新必须覆盖"全部已有文件"，不能只看待建清单
    quarantine = json.loads(args.quarantine_file.read_text()) if args.quarantine_file.exists() else {}
    if args.reset_quarantine:
        quarantine = {}
        args.quarantine_file.write_text("{}", encoding="utf-8")
        log(args.log, "=== 已清空 quarantine 计数，所有失败票重新进入队列 ===")
    session_start = time.time()
    latest = None
    if args.refresh:
        latest = (datetime.fromisoformat(args.latest_trade_date).date() if args.latest_trade_date
                  else resolve_target_date(latest_trade_date(), datetime.now()))
        expected_by_code = latest_daily_by_code()
        log(args.log, f"=== 刷新模式启动 目标 {len(target)} 只 freq={args.freq}min 最新交易日={latest} "
                      f"batch={args.batch_size} ===")
    else:
        expected_by_code = {}
        log(args.log, f"=== 监督器启动 目标 {len(target)} 只 freq={args.freq}min batch={args.batch_size} ===")

    processed = 0
    while True:
        have = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
        pending_count = 0
        if args.refresh:
            pending = [(c, last) for c, last in refresh_pending(freq_dir, target, latest, expected_by_code)
                       if quarantine.get(c, 0) < args.max_failures_per_code]
            pending_count = len(pending)
            if args.limit:
                pending = pending[: args.limit]
        else:
            pending = [(c, None) for c in target
                       if c not in have and quarantine.get(c, 0) < args.max_failures_per_code]
            pending_count = len(pending)
        if not pending:
            log(args.log, f"全部完成或有配额隔离：done={len(have)} quarantine={sum(1 for v in quarantine.values() if v >= args.max_failures_per_code)}")
            break
        if time.time() - session_start > args.session_seconds:
            log(args.log, f"会话达到 {args.session_seconds}s，正常退出（由 LaunchAgent 重新拉起）")
            break

        batch = pending[: args.batch_size]
        # 刷新模式下每只票的起始日不同（= 自己最后一条的下一天），按 start 分组后各起一个子进程；
        # 历史回补模式则只有一组（args.start）。正常情况下同一批的 start 完全相同，所以就是 1 个子进程。
        # 2026-10-18：按"各自最后日期+1"分组会退化成大量 1-2 只的小批次
        # （每批一次 uv 启动 + baostock 登录，实测 0.66 只/秒）。改成整批统一从
        # latest-10 天拉取 + --merge：重叠几天由 merge 去重，批次始终保持 batch-size。
        if args.refresh and latest is not None:
            start = (latest - timedelta(days=10)).isoformat()
            groups: dict[str, list[str]] = {start: [c for c, _ in batch]}
        else:
            groups = {args.start: [c for c, _ in batch]}
        codes_batch = [c for v in groups.values() for c in v]
        batch_start = time.time()
        timed_out = False
        blacklisted = False
        for start, group_codes in groups.items():
            command = [
                str(UV), "run", "python", str(ROOT / "scripts/backfill_cn_intraday_baostock.py"),
                "--codes", ",".join(group_codes), "--keep-order", "--freq", args.freq, "--start", start,
                "--sleep", str(args.sleep), "--per-code-timeout", "30",
            ]
            if args.refresh:
                command.append("--merge")
            try:
                proc = subprocess.run(command, cwd=ROOT, timeout=args.batch_timeout,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                # 原实现把子进程输出整个丢掉 ⇒ 刷新模式下"全军覆没"和"全部成功"在日志里长得一模一样。
                # 这里把失败行捞出来写进日志，至少能看出这一批为什么没进展。
                if args.refresh and proc.stdout:
                    # 用带冒号的"失败："匹配逐只失败行；否则会把成功摘要
                    # "5/5 完成（成功 5 / 跳过 0 / 失败 0）" 也当成失败（实测误报）。
                    bad = [ln for ln in proc.stdout.splitlines()
                           if "失败：" in ln or "错误：" in ln or "Traceback" in ln]
                    for ln in bad[:5]:
                        log(args.log, f"    {ln.strip()[:200]}")
                    if bad:
                        log(args.log, f"  批次 {len(group_codes)} 只有 {len(bad)} 行失败输出")
                    # 黑名单/限流：这是账号级拒绝，不是这些票的问题 —— 不能计入 quarantine，
                    # 否则一天就把几十只票"永久"隔离掉。
                    if "10001011" in (proc.stdout or "") or "黑名单" in (proc.stdout or ""):
                        blacklisted = True
                        COOLDOWN.write_text(json.dumps({
                            "until": time.time() + 6 * 3600, "reason": "baostock 10001011",
                            "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
                        log(args.log, "baostock 返回 10001011（黑名单/限流）→ 中止本次会话，6 小时内不再尝试")
            except subprocess.TimeoutExpired:
                timed_out = True
                for code in group_codes:
                    quarantine[code] = quarantine.get(code, 0) + 1
        if blacklisted:
            break
        duration = time.time() - batch_start
        processed += len(codes_batch)

        have_now = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
        if args.refresh and codes_batch:
            # 刷新模式的成败只能靠"复核最后日期"来判断：文件本来就在，不能用"文件不存在=失败"。
            # 不这么做的话，"抓取全部失败"和"全部成功"在日志上无法区分（本次就踩到了：
            # 17:10 那轮到底补上没有，只能事后逐只读 parquet 才确认）。
            still = {c for c, _ in refresh_pending(freq_dir, codes_batch, latest, expected_by_code)}
            updated = len(codes_batch) - len(still)
            log(args.log, f"批次复核：{updated}/{len(codes_batch)} 已更新到 {latest}（未更新 {len(still)}）")
            for code in still:
                quarantine[code] = quarantine.get(code, 0) + 1
        elif not timed_out:
            for code in codes_batch:
                if code not in have_now:
                    quarantine[code] = quarantine.get(code, 0) + 1
        args.quarantine_file.write_text(json.dumps(quarantine, ensure_ascii=False, indent=2), encoding="utf-8")

        # 进度只统计**目标票**里已经更新到最新交易日的数量。
        # 旧实现用 len(have)（全目录文件数），会出现 done=5228 > target=4533、ETA 为负。
        done = max(0, len(target) - pending_count)
        elapsed = max(time.time() - session_start, 1e-9)
        rate = processed / elapsed
        args.progress_file.write_text(json.dumps({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "frequency": f"{args.freq}min",
            "done_total": done,
            "target_total": len(target),
            "session_processed": processed,
            "rate_per_sec": round(rate, 4),
            "pending_total": pending_count,
            "eta_hours": round(pending_count / rate / 3600, 2) if rate > 0 else None,
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
    shutil.rmtree(STATE_DIR / ".lock", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
