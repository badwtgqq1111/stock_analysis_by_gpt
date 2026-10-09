#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""归档每日最终选股名单，并汇总成"模拟盘可回放"的历史文件。

为什么需要它（2026-10-09 发现的 bug）
------------------------------------
`output/results_cn/cn_ensemble_selected.csv` **每天被覆盖**，历史只留最后一天。
而模拟盘（`run_cn_paper_account`）需要的是**一串决策日**：

    run_paper_account(selections, bars)   # selections 需要 trade_date + stock_code + target_weight
    # 它把每个 trade_date 当成一次目标权重再平衡，用"之后第一个可得 bar"以 T+1 成交

调用方喂的是单日文件 ⇒ 那个日期永远是最后一根 bar ⇒ `next_date=None` ⇒
只挂 `pending` 单、**永不成交**。实测 `output/paper_trading/account/`：
nav 496 行恒为 45,000、positions/fills 空表、orders 4 条 pending。

这和 `arm_latest_selection.csv` 是同一类 bug（每天覆盖、无历史）。本脚本做两件事：
1. 把当天的最终名单归档到 `selection_history/<trade_date>.csv`（同日重跑覆盖当天那份）；
2. 回填历史来源（`feishu_<date>.json`、`cn_ensemble_selected_<YYYYMMDD>.csv`），
   汇总成 `paper_selection_history.csv`（列：trade_date, stock_code, target_weight）。

用法:
  uv run python scripts/archive_cn_selection.py             # 归档当天 + 回填 + 汇总
  uv run python scripts/archive_cn_selection.py --dry-run
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "output/results_cn"
HISTORY_DIR = RESULTS / "selection_history"
HISTORY_CSV = RESULTS / "paper_selection_history.csv"
CURRENT = RESULTS / "cn_ensemble_selected.csv"


def _norm(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    """统一成 trade_date / stock_code / target_weight 三列。"""
    if frame.empty or "stock_code" not in frame.columns:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["stock_code"] = frame["stock_code"].astype(str)
    weight = frame["target_weight"] if "target_weight" in frame.columns else None
    if weight is None:
        out["target_weight"] = 1.0 / max(len(frame), 1)
    else:
        out["target_weight"] = pd.to_numeric(weight, errors="coerce").fillna(0.0)
    out = out.dropna(subset=["trade_date"])
    # 权重来源：真实权重 vs 等权兜底（从归因产物捞回的那些只有代码）
    out["weight_source"] = (frame["weight_source"].astype(str).to_numpy()
                            if "weight_source" in frame.columns else "actual")
    out["source"] = source
    return out


def archive_today(dry_run: bool = False) -> dict:
    """当天的最终名单 → selection_history/<trade_date>.csv"""
    if not CURRENT.exists():
        return {"archived": None, "reason": f"缺 {CURRENT.relative_to(ROOT)}"}
    frame = pd.read_csv(CURRENT)
    norm = _norm(frame, "cn_ensemble_selected")
    if norm.empty:
        return {"archived": None, "reason": "当天名单为空或缺 trade_date"}
    date = norm["trade_date"].mode().iloc[0]
    path = HISTORY_DIR / f"{date}.csv"
    if not dry_run:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
    return {"archived": str(path.relative_to(ROOT)), "trade_date": date, "rows": int(len(frame))}


def backfill(dry_run: bool = False) -> list[str]:
    """从历史来源回填缺失的决策日。只补**不存在的日期**，不覆盖归档。"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in HISTORY_DIR.glob("*.csv")}
    filled: list[str] = []

    # ① 旧版带日期的最终名单归档：cn_ensemble_selected_<YYYYMMDD>.csv
    for path in sorted(RESULTS.glob("cn_ensemble_selected_*.csv")):
        stamp = path.stem.rsplit("_", 1)[-1]
        if len(stamp) != 8 or not stamp.isdigit():
            continue
        date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        if date in existing:
            continue
        frame = pd.read_csv(path)
        if not dry_run:
            frame.to_csv(HISTORY_DIR / f"{date}.csv", index=False)
        existing.add(date)
        filled.append(date)

    # ② 选股归因产物：attribution/<date>/pick_attribution.csv 里有 in_selection 标记，
    #    可以捞回**当时进了最终名单的代码**。注意：它的 target_weight 全是 0（那是中间量），
    #    所以权重要用**等权兜底**，并在 manifest 里标明 weight_source=equal_weight_fallback，
    #    免得和"真实权重"的日子混在一起看不出来。
    for day_dir in sorted((ROOT / "output/results_cn/attribution").glob("2*-*-*")):
        date = day_dir.name
        if date in existing:
            continue
        csv_path = day_dir / "pick_attribution.csv"
        if not csv_path.exists():
            continue
        try:
            frame = pd.read_csv(csv_path)
        except Exception:
            continue
        if "in_selection" not in frame.columns or "stock_code" not in frame.columns:
            continue
        picked = frame[frame["in_selection"].astype(str).str.lower().isin(["true", "1", "1.0"])]
        if picked.empty:
            continue
        equal = 1.0 / len(picked)
        out = pd.DataFrame({
            "trade_date": date,
            "stock_code": picked["stock_code"].astype(str).to_numpy(),
            "target_weight": equal,
            "weight_source": "equal_weight_fallback",
        })
        if not dry_run:
            out.to_csv(HISTORY_DIR / f"{date}.csv", index=False)
        existing.add(date)
        filled.append(f"{date}(attribution/等权)")

    # ③ 通知快照 feishu_<date>.json（含 target_weight，就是当天实际下单的名单）
    for path in sorted(RESULTS.glob("feishu_*.json")):
        date = path.stem.replace("feishu_", "")
        if date in existing:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("rows") or []
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        frame["trade_date"] = payload.get("trade_date") or date
        if not dry_run:
            frame.to_csv(HISTORY_DIR / f"{date}.csv", index=False)
        existing.add(date)
        filled.append(date)
    return filled


def summarize(dry_run: bool = False) -> dict:
    """selection_history/*.csv → paper_selection_history.csv"""
    frames = []
    for path in sorted(HISTORY_DIR.glob("*.csv")):
        try:
            frames.append(_norm(pd.read_csv(path), path.stem))
        except Exception:
            continue
    if not frames:
        return {"dates": 0, "rows": 0}
    merged = pd.concat(frames, ignore_index=True)
    merged = (merged[merged["target_weight"] > 0]
              .drop_duplicates(["trade_date", "stock_code"], keep="last")
              .sort_values(["trade_date", "stock_code"]))
    if not dry_run:
        merged.to_csv(HISTORY_CSV, index=False)
    return {"dates": int(merged["trade_date"].nunique()), "rows": int(len(merged)),
            "window": [merged["trade_date"].min(), merged["trade_date"].max()],
            "out": str(HISTORY_CSV.relative_to(ROOT))}


def main() -> int:
    ap = argparse.ArgumentParser(description="归档最终选股名单 + 汇总模拟盘历史")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    a = archive_today(args.dry_run)
    f = backfill(args.dry_run)
    s = summarize(args.dry_run)
    print(json.dumps({
        "archived": a, "backfilled_dates": f, "history": s,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
