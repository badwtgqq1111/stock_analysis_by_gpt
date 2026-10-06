#!/usr/bin/env python3
"""补 A 股财务数据（PIT，带 ann_date）—— 质量/估值 sleeve 的特征底座。

背景（2026-10-06 实测）：clean panel 里 `financial_roe` 只有 19% 覆盖、`financial_roa` 为 0%，
`valuation_fcf_yield` 为 0%；原因是 `pipeline.financial_metrics_enabled = false`（配置默认关闭），
而现有实现走 BaoStock（串行、且当前被限流）。

本脚本改走 **Tushare 中继的 `fina-indicator`**（实测 HTTP 200，108 字段，含 `ann_date` → PIT 可用），
按"子进程硬超时 + 逐股检查点 + 会话限时"的加固模式回补，供质量/估值 sleeve 与 FCF 计算使用。

用法：
  uv run python scripts/backfill_cn_financials.py --build-codes      # 生成清单（入库）
  uv run python scripts/backfill_cn_financials.py --status          # 进度
  uv run python scripts/backfill_cn_financials.py --one-code 600519.SH   # 抓单只（供监督器调用）
  uv run python scripts/backfill_cn_financials.py --session-seconds 3600 # 监督器模式
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "*")
# 非交互式调度（launchd/cron）拿不到交互式 shell 里的中继密钥：
# 与 run_cn_pipeline.py 一致，从 config/scheduler_env.sh 补齐缺失变量，否则
# `CNTushareRelayClient` 会直接抛 "relay requires CN_INDUSTRY_RELAY_BASE_KEY or TUSHARE_RELAY_KEY"。
try:
    from data.store.scheduler_env import load_scheduler_env

    _FILLED = load_scheduler_env()
except Exception:  # 环境文件缺失不应阻塞脚本
    _FILLED = {}

OUT_DIR = ROOT / "assets/data/raw/financials_relay/fina_indicator"
STATE_DIR = ROOT / "assets/data/raw/financials_relay/_sync"
MERGED = ROOT / "assets/data/meta/cn_financial_indicators.parquet"
UV = Path.home() / ".local/bin/uv"

# 只保留 sleeve 需要 + PIT 关键字段（避免 108 列全存）
KEEP = ["ts_code", "ann_date", "end_date", "update_flag",
        # 质量
        "roe", "roe_waa", "roa", "roa2", "grossprofit_margin", "netprofit_margin",
        "debt_to_assets", "current_ratio", "quick_ratio", "eps", "bps",
        # 成长
        "or_yoy", "netprofit_yoy", "dt_netprofit_yoy", "op_yoy", "ebt_yoy", "assets_yoy", "eqt_yoy",
        # 现金流（FCF 代理）
        "ocfps", "cfps", "ocf_yoy"]


def log(path: Path, message: str) -> None:
    line = f"[{datetime.now().strftime('%F %T')}] {message}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def build_codes(out: Path) -> int:
    frame = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet")
    col = "list_status" if "list_status" in frame.columns else "status"
    live = frame[frame[col] == "L"]
    live = live[live["ts_code"].str.match(r"^\d{6}\.(SH|SZ)$")]
    codes = sorted(set(live["ts_code"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(codes) + "\n", encoding="utf-8")
    return len(codes)


def fetch_one(code: str) -> pd.DataFrame:
    from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient

    client = CNTushareRelayClient()
    frame, _gateway = client.get("fina-indicator", ts_code=code, limit=200)
    if frame is None or frame.empty:
        return pd.DataFrame()
    keep = [c for c in KEEP if c in frame.columns]
    frame = frame[keep].copy()
    for col in keep:
        if col not in ("ts_code", "ann_date", "end_date", "update_flag"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.sort_values(["ann_date", "end_date"])
    return frame


def status(codes_path: Path, progress_path: Path) -> dict:
    have = {p.stem.replace("_", ".") for p in OUT_DIR.glob("*.parquet")}
    target = [c for c in codes_path.read_text(encoding="utf-8").split() if c.strip()] if codes_path.exists() else []
    remaining = [c for c in target if c not in have]
    ledger = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    rate = float(ledger.get("rate_per_sec") or 0)
    return {"target_total": len(target), "done": len(have), "remaining": len(remaining),
            "rate_per_sec": rate, "eta_hours": round(len(remaining) / rate / 3600, 2) if rate > 0 else None,
            "last_ledger": ledger.get("updated_at"), "next_codes": remaining[:5]}


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股财务数据回补（Tushare 中继 fina-indicator）")
    parser.add_argument("--codes-file", type=Path, default=STATE_DIR / "codes.txt")
    parser.add_argument("--log", type=Path, default=STATE_DIR / "sync.log")
    parser.add_argument("--progress-file", type=Path, default=STATE_DIR / "progress.json")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--batch-timeout", type=int, default=120)
    parser.add_argument("--session-seconds", type=int, default=3600)
    parser.add_argument("--sleep", type=float, default=0.3)
    parser.add_argument("--max-failures-per-code", type=int, default=3)
    parser.add_argument("--build-codes", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--one-code", default="")
    parser.add_argument("--codes", default="", help="逗号分隔的批量抓取（子进程内一次抓多只，省启动开销）")
    parser.add_argument("--merge", action="store_true", help="把逐股检查点合并成一个 parquet")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.build_codes:
        print(json.dumps({"built": str(args.codes_file), "codes": build_codes(args.codes_file)}, ensure_ascii=False))
        return 0
    if args.status:
        print(json.dumps(status(args.codes_file, args.progress_file), ensure_ascii=False, indent=2))
        return 0
    if args.one_code:
        args.codes = args.one_code
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        done, failed = [], []
        for code in codes:
            path = OUT_DIR / f"{code.replace('.', '_')}.parquet"
            if path.exists():
                done.append(code)
                continue
            ok = False
            for attempt in range(1, 4):
                try:
                    frame = fetch_one(code)
                    if not frame.empty:
                        frame.to_parquet(path, index=False)
                        done.append(code)
                        ok = True
                        break
                except Exception:
                    time.sleep(1.5 * attempt)
            if not ok:
                failed.append(code)
            time.sleep(args.sleep)
        print(json.dumps({"done": done, "failed": failed}, ensure_ascii=False))
        return 0
    if args.merge:
        frames = [pd.read_parquet(p) for p in sorted(OUT_DIR.glob("*.parquet"))]
        if not frames:
            print("没有检查点可合并")
            return 1
        merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "ann_date", "end_date"])
        merged["ann_date"] = pd.to_datetime(merged["ann_date"], format="%Y%m%d", errors="coerce")
        merged["end_date"] = pd.to_datetime(merged["end_date"], format="%Y%m%d", errors="coerce")
        merged = merged.dropna(subset=["ann_date"]).sort_values(["ts_code", "ann_date"])
        MERGED.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(MERGED, index=False)
        manifest = {"generated_at": datetime.now().isoformat(timespec="seconds"), "rows": int(len(merged)),
                    "codes": int(merged["ts_code"].nunique()),
                    "window": [str(merged["ann_date"].min().date()), str(merged["ann_date"].max().date())],
                    "columns": list(merged.columns), "out": str(MERGED)}
        (MERGED.parent / "cn_financial_indicators.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    # ---- 监督器模式：批量子进程 + 硬超时 ----
    if not args.codes_file.exists():
        build_codes(args.codes_file)
    target = [c for c in args.codes_file.read_text(encoding="utf-8").split() if c.strip()]
    quarantine_path = STATE_DIR / "quarantine.json"
    quarantine = json.loads(quarantine_path.read_text()) if quarantine_path.exists() else {}
    session_start = time.time()
    processed = 0
    log(args.log, f"=== 财务回补监督器启动 目标 {len(target)} 只 ===")
    while True:
        have = {p.stem.replace("_", ".") for p in OUT_DIR.glob("*.parquet")}
        pending = [c for c in target if c not in have and quarantine.get(c, 0) < args.max_failures_per_code]
        if not pending:
            log(args.log, f"全部完成或有配额隔离 done={len(have)}")
            break
        if time.time() - session_start > args.session_seconds:
            log(args.log, f"会话达到 {args.session_seconds}s，正常退出")
            break
        batch = pending[: args.batch_size]
        timed_out = False
        try:
            proc = subprocess.run([str(UV), "run", "python", str(ROOT / "scripts/backfill_cn_financials.py"),
                                   "--codes", ",".join(batch)], cwd=ROOT, timeout=args.batch_timeout,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            output = (proc.stdout or "").strip().splitlines()
            if output:
                log(args.log, f"批 {batch[0]}..{batch[-1]} → {output[-1][:180]}")
        except subprocess.TimeoutExpired:
            log(args.log, f"批次超时：{batch}")
            timed_out = True
            for code in batch:
                quarantine[code] = quarantine.get(code, 0) + 1
        processed += len(batch)
        have_now = {p.stem.replace("_", ".") for p in OUT_DIR.glob("*.parquet")}
        if not timed_out:
            for code in batch:
                if code not in have_now:
                    quarantine[code] = quarantine.get(code, 0) + 1
        quarantine_path.write_text(json.dumps(quarantine, ensure_ascii=False, indent=2), encoding="utf-8")
        done = len(have_now)
        rate = processed / max(time.time() - session_start, 1e-9)
        args.progress_file.write_text(json.dumps({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "dataset": "fina_indicator", "done_total": done, "target_total": len(target),
            "session_processed": processed, "rate_per_sec": round(rate, 4),
            "eta_hours": round((len(target) - done) / rate / 3600, 2) if rate > 0 else None,
            "quarantined": sum(1 for v in quarantine.values() if v >= args.max_failures_per_code),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if processed % 50 < args.batch_size:
            log(args.log, f"进度 {done}/{len(target)} | 本会话 {processed} | {rate:.3f} 只/秒")
    log(args.log, f"=== 监督器退出 done={len(list(OUT_DIR.glob('*.parquet')))} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
