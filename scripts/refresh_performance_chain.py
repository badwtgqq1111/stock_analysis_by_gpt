#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「回测绩效」页的数据链刷新到最新交易日（每日自动化的一步）。

链条：`picks.csv`（研究口径的月末选股，按 arm 分组）
      → `replay_daily_execution.py`（逐笔级日频回放，逐日 mark-to-market）
      → `output/research/l3_replay/<arm>_{nav,trades,metrics}` 
      → `portfolio_performance.py` → `output/results_cn/performance/*.json`（前端「回测绩效」页读它）

为什么需要它：绩效页此前**从来不在每日流程里**，窗口一直停在上一次手工跑
（实测 2026-10-10 页面还停在 10-07）。而回放本身是"给定一份 picks，按日推进净值"——
每天重跑一次就能把净值曲线推到最新交易日，成本只有几十秒/arm。

口径说明（诚实记录）：
  * picks 的**月末决策**只到 research 那次跑出来的最后一个月（当前 2026-09），
    所以重跑解决的是"净值/曲线/指标更新到今天"，**不产生新的调仓决策**；
    新的决策月要靠重跑 `scripts/llm_sleeve_oos_backtest.py` 那条研究链（另行排期）。
  * `random5` 的窗口与其它 arm 不同（picks 里含随机基准），页面上已注明。

用法：
  uv run python scripts/refresh_performance_chain.py                 # 自动找最新 picks，全部 arm
  uv run python scripts/refresh_performance_chain.py --arms base_v1  # 只刷一个
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "output/research"
NAV_DIR = ROOT / "output/research/l3_replay"
REPORT_DIR = ROOT / "output/pipeline_reports/performance_chain"
DEFAULT_ARMS = ("base_v1", "llm_new", "combo_50_50", "random5")


def latest_picks() -> Path | None:
    """最新的 sleeve OOS picks（含 arm 列：base_v1 / llm_new / combo_50_50 / random5）。"""
    candidates = sorted(RESEARCH.glob("llm_factor_mining_*/sleeve_oos_v4d/picks.csv"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            import pandas as pd
            head = pd.read_csv(path, nrows=5)
            if "arm" in head.columns:
                return path
        except Exception:                                      # noqa: BLE001
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新回测绩效数据链（回放 → 指标）")
    ap.add_argument("--picks", default="", help="留空=自动取最新 sleeve OOS picks")
    ap.add_argument("--arms", default=",".join(DEFAULT_ARMS))
    ap.add_argument("--capital", type=float, default=45_000.0)
    ap.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args()

    started = time.time()
    picks = Path(args.picks) if args.picks else latest_picks()
    report: dict = {"started_at": datetime.now().isoformat(timespec="seconds"),
                    "picks": str(picks) if picks else None, "arms": {}, "errors": []}
    NAV_DIR.mkdir(parents=True, exist_ok=True)

    if not args.skip_replay:
        if picks is None or not picks.exists():
            report["errors"].append("找不到 picks.csv（先跑 llm_sleeve_oos_backtest 或指定 --picks）")
        else:
            for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
                t0 = time.time()
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/replay_daily_execution.py"),
                     "--picks", str(picks), "--arm", arm, "--out-dir", str(NAV_DIR),
                     "--capital", str(args.capital)],
                    cwd=str(ROOT), capture_output=True, text=True)
                ok = proc.returncode == 0
                report["arms"][arm] = {"ok": ok, "seconds": round(time.time() - t0, 1),
                                       "tail": (proc.stdout or proc.stderr or "").strip().splitlines()[-1:]}
                print(f"[perf-chain] {arm}: {'ok' if ok else 'FAILED'} "
                      f"({report['arms'][arm]['seconds']}s) {report['arms'][arm]['tail']}", flush=True)
                if not ok:
                    report["errors"].append(f"{arm}: {proc.stderr.strip()[:200]}")

    t0 = time.time()
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/portfolio_performance.py")],
                          cwd=str(ROOT), capture_output=True, text=True)
    report["performance"] = {"ok": proc.returncode == 0, "seconds": round(time.time() - t0, 1)}
    if proc.returncode != 0:
        report["errors"].append(f"portfolio_performance: {proc.stderr.strip()[:200]}")
    print(f"[perf-chain] portfolio_performance: {'ok' if proc.returncode == 0 else 'FAILED'}"
          f"（{report['performance']['seconds']}s）", flush=True)

    report["seconds"] = round(time.time() - started, 1)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{datetime.now():%Y-%m-%d}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
