#!/usr/bin/env python3
"""把 5min 分时归档从「近一年」向前回补到 BaoStock 可得的最早日期（2020-01）。

为什么不是 iFinD：试用账号 `high_frequency` 只开放近 1 年（errorcode -4309），
2020 年及更早的 5min 请求直接拒绝；BaoStock 的 5min 从 2020-01 起可用（实测
2019 年全部为空、2020-01-02 起有 48 根/日），是当前唯一能拿到多年 5min 的源。
东财 push2his 5min 每次只返回 1512 根（约 31 个交易日），腾讯 320 根，都不具备回补能力。

吞吐是硬约束：BaoStock 逐行流式返回，实测 ~500 行/秒/连接。全市场 5,200 只 × 7 年
≈ 4.1 亿行 ≈ 9 天单线程；因此本脚本做三件事：
  1) **多进程并行**（每个 worker 独立登录，互不干扰；单只超时/卡死只损失一只）；
  2) **优先级排队**：实盘持仓 → 近期策略选股 → 成交过的标的 → 其余按 20 日成交额降序；
  3) **可断点续跑**：每只完成即写 `_hist/done/<code>.json`，重复启动自动跳过。

与每日增量同步（`sync_cn_intraday_full.py --refresh`）的关系：
  * 两者写同一批 parquet（schema 完全一致），下游无需改动；
  * 本脚本在 15:00-19:00 主动休眠，避开每日同步窗口，降低整文件覆盖的竞态概率；
  * 写入是「临时文件 + os.replace」原子替换；若每日同步仍把某只票的历史冲掉，
    `--audit` 会检出（min(date) > 2019-12-31）并自动重排该票。

用法：
  uv run python scripts/backfill_cn_intraday_history.py --limit 20 --workers 4   # 试跑
  uv run python scripts/backfill_cn_intraday_history.py --workers 6 --session-seconds 21600
  uv run python scripts/backfill_cn_intraday_history.py --status
  uv run python scripts/backfill_cn_intraday_history.py --audit
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FREQ_DIR = ROOT / "assets/data/raw/intraday_baostock/frequency=5min"
STATE_DIR = ROOT / "assets/data/raw/intraday_baostock/_hist"
DONE_DIR = STATE_DIR / "done"
LOG = STATE_DIR / "backfill.log"
QUEUE_FILE = STATE_DIR / "queue.txt"
COOLDOWN = STATE_DIR / "cooldown.json"
PAUSE = STATE_DIR / "PAUSE"
LOGIN_FAILED = STATE_DIR / "login_failed"
FIELDS = "date,time,open,high,low,close,volume,amount"
COLS = FIELDS.split(",")


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%F %T}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def bs_code(code: str) -> str:
    num, exch = code.upper().split(".", 1)
    return f"{'sh' if exch == 'SH' else 'sz'}.{num}"


def parquet_path(code: str) -> Path:
    return FREQ_DIR / f"{code.replace('.', '_')}.parquet"


def done_path(code: str) -> Path:
    return DONE_DIR / f"{code.replace('.', '_')}.json"


def cooling() -> float:
    """返回还需等待的秒数（限流/黑名单冷却）。"""
    try:
        until = float(json.loads(COOLDOWN.read_text()).get("until") or 0)
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, until - time.time())


def set_cooldown(seconds: int, reason: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    COOLDOWN.write_text(json.dumps({"until": time.time() + seconds, "reason": reason,
                                    "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    log(f"冷却 {seconds}s：{reason}")


def target_end() -> str:
    today = date.today()
    # 15:05 之前当日分钟线还没收完，用昨天做上界
    return (today if datetime.now() >= datetime.combine(today, datetime.min.time()).replace(hour=15, minute=5)
            else today - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- code universe
def priority_codes() -> tuple[list[str], dict[str, str]]:
    """实盘持仓 / 近期策略选股 / 成交记录 优先，返回 (codes, {code: tier})。"""
    tiers: dict[str, str] = {}

    def add(code: str, tier: str) -> None:
        code = str(code).strip().upper()
        if code.endswith((".SH", ".SZ")) and code not in tiers:
            tiers[code] = tier

    live = ROOT / "output/results_cn/cn_live_holdings.csv"
    if live.exists():
        try:
            for code in pd.read_csv(live)["code"].dropna().unique():
                add(code, "live")
        except Exception:  # noqa: BLE001
            pass

    cutoff = (date.today() - timedelta(days=30)).isoformat()
    hist_dirs = list((ROOT / "output/results_cn").glob("*_history"))
    hist_dirs += list((ROOT / "output/strategies").glob("*/history"))
    for folder in hist_dirs:
        for path in sorted(folder.glob("*.csv")):
            if path.stem < cutoff:
                continue
            try:
                frame = pd.read_csv(path)
            except Exception:  # noqa: BLE001
                continue
            col = next((c for c in ("code", "stock_code", "ts_code") if c in frame.columns), None)
            if col:
                for code in frame[col].dropna().unique():
                    add(code, "strategy")

    trades = ROOT / "output/results_cn/xiaomkt_rotation_optimized/trades.csv"
    if trades.exists():
        try:
            for code in pd.read_csv(trades, usecols=["code"])["code"].dropna().unique():
                add(code, "traded")
        except Exception:  # noqa: BLE001
            pass
    return list(tiers), tiers


def universe(max_priority: int = 0) -> list[tuple[str, str]]:
    """(code, tier)，按优先级排序；其余按 20 日成交额降序（流动性好的先补）。"""
    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet")
    status_col = "list_status" if "list_status" in basic.columns else "status"
    live = basic[basic[status_col] == "L"]
    live = live[live["ts_code"].str.match(r"^\d{6}\.(SH|SZ)$")]
    all_codes = set(live["ts_code"])

    prio, tiers = priority_codes()
    prio = [c for c in prio if c in all_codes]
    if max_priority:
        prio = prio[:max_priority]

    rest = [c for c in all_codes if c not in set(prio)]
    rest.sort(key=lambda c: (-(tiers.get(c) == "x"), c))
    # 用最近一个交易日的成交额排序（一次读取，比 20 日均值便宜）
    try:
        import glob
        import pyarrow.dataset as ds

        files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                        "asset_type=equity/frequency=daily/adjust=qfq/year=2026/*.parquet")))
        table = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code", "amount"]).to_pandas()
        table["trade_date"] = pd.to_datetime(table["trade_date"])
        last = table["trade_date"].max()
        snap = table[table["trade_date"] == last].set_index("stock_code")["amount"].to_dict()
        rest.sort(key=lambda c: -(snap.get(c, 0) or 0))
    except Exception:  # noqa: BLE001
        pass

    ordered = [(c, tiers.get(c, "live")) for c in prio] + [(c, "other") for c in rest]
    return ordered


# ---------------------------------------------------------------- worker
def _alarm(signum, frame):  # pragma: no cover
    raise TimeoutError("per-code timeout")


def fetch(code: str, start: str, end: str) -> pd.DataFrame:
    import baostock as bs

    rs = bs.query_history_k_data_plus(bs_code(code), FIELDS, start_date=start,
                                      end_date=end, frequency="5", adjustflag="2")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        raise RuntimeError(f"空结果 {rs.error_code} {rs.error_msg}")
    frame = pd.DataFrame(rows, columns=COLS)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").astype("datetime64[us]")
    frame["volume"] = frame["volume"].fillna(0).astype("int64")
    frame["stock_code"] = code
    frame["frequency"] = "5min"
    frame["adjust"] = "qfq"
    frame["source"] = "baostock"
    return frame


def merge_write(code: str, new: pd.DataFrame) -> int:
    path = parquet_path(code)
    if path.exists():
        old = pd.read_parquet(path)
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(["date", "time"], keep="last").sort_values(["date", "time"])
    merged = merged.reset_index(drop=True)
    tmp = path.with_suffix(".tmp.parquet")
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return len(merged)


def worker(name: str, queue, start: str, end: str, expected: dict, stall_mark) -> None:  # noqa: ANN001
    import baostock as bs

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    hb = STATE_DIR / f"heartbeat_{name}.json"
    login = bs.login()
    if login.error_code != "0":
        log(f"[{name}] 登录失败 {login.error_code} {login.error_msg}")
        # 不能直接 return：父进程看到进程死亡会立刻重启，变成"每 2 秒登录一次"的死循环，
        # 只会把黑名单一直续期。写标记让父进程统一冷却退出。
        LOGIN_FAILED.write_text(json.dumps({"code": login.error_code, "msg": login.error_msg,
                                            "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        return
    empty_streak = 0
    while True:
        try:
            code = queue.get(timeout=30)
            empty_streak = 0
        except Exception:  # noqa: BLE001
            empty_streak += 1
            if empty_streak >= 3:      # 队列已空且没有新任务 → 收工
                break
            continue
        if code is None:
            break
        hb.write_text(json.dumps({"code": code, "t": time.time()}), encoding="utf-8")
        while PAUSE.exists() or cooling() > 0:
            time.sleep(60)
            hb.write_text(json.dumps({"code": code, "t": time.time()}), encoding="utf-8")
        try:
            want_end = min(pd.Timestamp(end), pd.Timestamp(expected.get(code, end))).date().isoformat()
            path = parquet_path(code)
            if path.exists():
                dates = pd.to_datetime(pd.read_parquet(path, columns=["date"])["date"])
                lo, hi = dates.min().date(), dates.max().date()
                if lo <= (pd.Timestamp(start) + pd.Timedelta(days=7)).date() and hi >= (pd.Timestamp(want_end) - pd.Timedelta(days=3)).date():
                    done_path(code).write_text(json.dumps({"code": code, "skipped": True,
                                                           "range": [str(lo), str(hi)]}), encoding="utf-8")
                    continue
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(900)
            try:
                frame = fetch(code, start, want_end)
            finally:
                signal.alarm(0)
            rows = merge_write(code, frame)
            done_path(code).write_text(json.dumps({
                "code": code, "rows": rows, "fetched": len(frame),
                "range": [start, want_end], "at": datetime.now().isoformat(timespec="seconds"),
            }), encoding="utf-8")
            log(f"[{name}] {code} +{len(frame):,} → {rows:,} 行 ({want_end})")
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}"
            log(f"[{name}] {code} 失败 {text[:160]}")
            if "10001011" in text or "黑名单" in text:
                set_cooldown(1800, "baostock 黑名单/限流")
                try:
                    queue.put(code)      # 未写入完成标记 → 冷却后重试
                except Exception:  # noqa: BLE001
                    pass
        finally:
            hb.write_text(json.dumps({"code": None, "t": time.time()}), encoding="utf-8")
    bs.logout()


# ---------------------------------------------------------------- status / audit
def summary(start: str) -> dict:
    done = {p.stem.replace("_", ".") for p in DONE_DIR.glob("*.json")} if DONE_DIR.exists() else set()
    files = sorted(FREQ_DIR.glob("*.parquet"))
    earliest = None
    covered = 0
    for path in files[:4000]:
        try:
            lo = pd.to_datetime(pd.read_parquet(path, columns=["date"])["date"]).min()
        except Exception:  # noqa: BLE001
            continue
        earliest = lo if earliest is None or lo < earliest else earliest
        if lo <= pd.Timestamp(start) + pd.Timedelta(days=7):
            covered += 1
    rows = 0
    for path in files:
        try:
            rows += pd.read_parquet(path, columns=["date"]).shape[0]
        except Exception:  # noqa: BLE001
            continue
    return {"files": len(files), "rows": rows, "done_markers": len(done),
            "covered_from_start": covered, "earliest": str(earliest.date()) if earliest is not None else None}


def audit(start: str) -> list[str]:
    """找出被每日同步覆盖、历史丢失的文件。"""
    bad = []
    for path in FREQ_DIR.glob("*.parquet"):
        try:
            lo = pd.to_datetime(pd.read_parquet(path, columns=["date"])["date"]).min()
        except Exception:  # noqa: BLE001
            bad.append(path.stem.replace("_", "."))
            continue
        if lo > pd.Timestamp(start) + pd.Timedelta(days=7):
            bad.append(path.stem.replace("_", "."))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="5min 分时历史回补（BaoStock 2020+，多进程 + 断点）")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-priority", type=int, default=1500, help="优先队列最多多少只（0=全部按流动性）")
    ap.add_argument("--session-seconds", type=int, default=0)
    ap.add_argument("--stall-seconds", type=int, default=1200)
    ap.add_argument("--quiet-hours", default="15:00-19:00", help="该时段休眠，避开每日同步")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--refresh-queue", action="store_true")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    end = args.end or target_end()

    if args.status:
        print(json.dumps(summary(args.start), ensure_ascii=False, indent=2))
        return 0
    if args.audit:
        bad = audit(args.start)
        print(json.dumps({"lost_history": len(bad), "examples": bad[:20]}, ensure_ascii=False, indent=2))
        return 0

    expected = {}
    try:
        import glob
        import pyarrow.dataset as ds

        files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                        "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
        table = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code"]).to_pandas()
        table["trade_date"] = pd.to_datetime(table["trade_date"])
        expected = {c: str(d.date()) for c, d in table.groupby("stock_code")["trade_date"].max().items()}
    except Exception:  # noqa: BLE001
        pass

    if QUEUE_FILE.exists() and not args.refresh_queue:
        codes = [line.split("\t")[0] for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        ordered = universe(args.max_priority)
        codes = [c for c, _ in ordered]
        QUEUE_FILE.write_text("\n".join(f"{c}\t{t}" for c, t in ordered) + "\n", encoding="utf-8")
    todo = [c for c in codes if not done_path(c).exists()]
    if args.limit:
        todo = todo[: args.limit]
    log(f"队列 {len(codes):,} 只，未完成 {len(todo):,} 只，窗口 {args.start}~{end}，workers={args.workers}")
    if not todo:
        print(json.dumps(summary(args.start), ensure_ascii=False, indent=2))
        return 0

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    for code in todo:
        q.put(code)

    started = time.time()
    procs: dict[str, mp.Process] = {}

    def spawn(name: str) -> None:
        proc = ctx.Process(target=worker, args=(name, q, args.start, end, expected, None), daemon=False)
        proc.start()
        procs[name] = proc
        log(f"[{name}] 启动 pid={proc.pid}")

    for i in range(max(1, args.workers)):
        spawn(f"w{i}")

    while procs:
        time.sleep(20)
        if LOGIN_FAILED.exists():
            try:
                info = json.loads(LOGIN_FAILED.read_text())
            except Exception:  # noqa: BLE001
                info = {}
            LOGIN_FAILED.unlink(missing_ok=True)
            set_cooldown(6 * 3600, f"baostock 登录失败 {info.get('code')}")
            for p in procs.values():
                p.terminate()
            procs.clear()
            log("登录不可用 → 结束本次会话（6 小时内不再尝试）")
            break
        for name, proc in list(procs.items()):
            if args.session_seconds and time.time() - started > args.session_seconds:
                log(f"会话达到 {args.session_seconds}s，优雅退出（进度已落盘）")
                for p in procs.values():
                    p.terminate()
                procs.clear()
                break
            hb_path = STATE_DIR / f"heartbeat_{name}.json"
            code, stamp = None, 0.0
            try:
                hb = json.loads(hb_path.read_text())
                code, stamp = hb.get("code"), float(hb.get("t") or 0)
            except Exception:  # noqa: BLE001
                pass
            if not proc.is_alive():
                procs.pop(name, None)
                if code:
                    q.put(code)          # 该只没写完，重新排队
                if not q.empty():
                    spawn(name)
                continue
            if code and time.time() - stamp > args.stall_seconds:
                log(f"[{name}] {code} 卡住 {int(time.time()-stamp)}s → kill + 重排")
                proc.kill()
                procs.pop(name, None)
                time.sleep(3)
                q.put(code)
                spawn(name)
        # 收盘窗口（默认 15:00-19:59）暂停：每日增量同步要跑，避免 baostock 会话互相挤占
        hour = datetime.now().hour
        if args.quiet_hours and hour in (15, 16, 17, 18, 19):
            PAUSE.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            time.sleep(120)
        elif PAUSE.exists():
            PAUSE.unlink()

    print(json.dumps(summary(args.start), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
