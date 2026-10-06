#!/usr/bin/env python3
"""W2：域内随机基线（sleeve 上线第一道门）。

设计规格 §7 要求："域内 OOS rank IC / top-k 收益必须超过**域内随机基线**"。
本脚本给出这个基线：

  * 在每个决策日，对**可交易域**（有 bar、非涨跌停、成交额 ≥ 下限、上市 ≥1 年、非 ST 代理）随机抽 k 只，
    重复 R 次，得到"无技能"的 top-k 前向收益分布（horizon = 各 sleeve 的标签期）；
  * 同时给出：域内全体（等权）前向收益、以及**真实持仓**（若提供 replay 记录）的前向收益，
    三者对照即可判断"这条腿有没有技能"；
  * 输出百分比分布，便于直接做显著性判断（真实持仓落在随机分布的哪个分位）。

用法：
  uv run python scripts/sleeve_random_baseline.py --start 2025-09-01 --top-k 5 --draws 500
  uv run python scripts/sleeve_random_baseline.py --horizons 5,10,20      # 多 horizon
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")


def load_panel() -> dict:
    import pyarrow.dataset as ds

    files = [str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=202[4-6]/*.parquet")]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount"])
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame[frame["stock_code"].str[:3].isin(MAIN_PREFIX)]
    panels = {col: frame.pivot_table(index="trade_date", columns="stock_code", values=col, aggfunc="last")
              for col in ("open", "high", "low", "close", "amount")}
    return panels


def tradable_mask(panels: dict, i: int, min_amount: float, prev_close: pd.Series) -> pd.Series:
    open_ = panels["open"].iloc[i]
    high = panels["high"].iloc[i]
    low = panels["low"].iloc[i]
    amount = panels["amount"].iloc[i]
    finite = np.isfinite(open_) & np.isfinite(panels["close"].iloc[i])
    liquid = amount >= min_amount
    not_limit_up = ~((high == low) & (open_ >= prev_close * 1.095))
    not_limit_down = ~((high == low) & (open_ <= prev_close * 0.905))
    return finite & liquid & not_limit_up & not_limit_down


def main() -> int:
    parser = argparse.ArgumentParser(description="W2 域内随机基线")
    parser.add_argument("--start", default="2025-09-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--draws", type=int, default=500)
    parser.add_argument("--horizons", default="20", help="逗号分隔，如 5,10,20")
    parser.add_argument("--min-amount", type=float, default=2e7, help="成交额下限（元）")
    parser.add_argument("--picks", type=Path, default=None,
                        help="真实持仓 CSV（含 replay_date, stock_code, sleeve），用于对照")
    parser.add_argument("--out", type=Path, default=ROOT / "output/research")
    args = parser.parse_args()

    panels = load_panel()
    dates = panels["close"].index
    rebalance = [d for d in dates if d >= pd.Timestamp(args.start) and (not args.end or d <= pd.Timestamp(args.end))]
    # 决策日取 replay 目录里的日期（与真实持仓对齐）
    replay_dates = sorted({d.name.replace("replay_", "") for d in (ROOT / "output/results_cn").glob("replay_*")
                           if re.fullmatch(r"replay_\d{8}", d.name)})
    decision_dates = [pd.Timestamp(d) for d in replay_dates
                      if pd.Timestamp(d) >= pd.Timestamp(args.start) and pd.Timestamp(d) in set(dates)]
    if not decision_dates:
        decision_dates = rebalance[::20]
    rng = np.random.default_rng(20261006)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]

    rows = []
    for day in decision_dates:
        i = dates.get_loc(day)
        if i + max(horizons) + 2 >= len(dates):
            continue
        prev_close = panels["close"].iloc[i - 1]
        mask = tradable_mask(panels, i + 1, args.min_amount, prev_close)      # 次日开盘进场
        codes = panels["close"].columns[mask.to_numpy()]
        if len(codes) < args.top_k * 3:
            continue
        entry = panels["open"].iloc[i + 1][codes]
        for h in horizons:
            exit_px = panels["close"].iloc[min(i + 1 + h, len(dates) - 1)][codes]
            fwd = (exit_px / entry - 1).replace([np.inf, -np.inf], np.nan).dropna()
            if len(fwd) < args.top_k * 3:
                continue
            universe_mean = float(fwd.mean())
            draws = [float(rng.choice(fwd.to_numpy(), size=args.top_k, replace=False).mean())
                     for _ in range(args.draws)]
            draws = np.array(draws)
            rows.append({
                "trade_date": str(day.date()), "horizon": h, "universe_n": int(len(fwd)),
                "universe_mean": universe_mean,
                "random_topk_mean": float(draws.mean()),
                "random_p05": float(np.percentile(draws, 5)),
                "random_p50": float(np.percentile(draws, 50)),
                "random_p95": float(np.percentile(draws, 95)),
            })
        if len(rows) % 40 == 0:
            print(f"[w2] {day.date()} 完成（累计 {len(rows)} 行）", flush=True)

    baseline = pd.DataFrame(rows)
    if baseline.empty:
        print("[w2] 没有可用决策日")
        return 1

    summary = (baseline.groupby("horizon")
               .agg(days=("trade_date", "nunique"), universe_n=("universe_n", "median"),
                    universe_mean=("universe_mean", "mean"), random_mean=("random_topk_mean", "mean"),
                    random_p05=("random_p05", "mean"), random_p95=("random_p95", "mean"))
               .reset_index())

    # 真实持仓对照（如有）：用**同一 horizon 重算**持仓的前向收益，避免与基线口径不一致
    picks_table = None
    if args.picks and args.picks.exists():
        picks = pd.read_csv(args.picks)
        picks = picks[picks["sleeve"].astype(str).str.len() > 0]
        picks["trade_date"] = pd.to_datetime(picks["replay_date"].astype(str))
        close, open_ = panels["close"], panels["open"]
        recs = []
        for row in picks.itertuples():
            if row.trade_date not in dates or row.stock_code not in close.columns:
                continue
            i = dates.get_loc(row.trade_date)
            if i + 1 >= len(dates):
                continue
            entry = open_.iloc[i + 1].get(row.stock_code, np.nan)
            if not np.isfinite(entry) or entry <= 0:
                continue
            for h in horizons:
                exit_px = close.iloc[min(i + 1 + h, len(dates) - 1)].get(row.stock_code, np.nan)
                if np.isfinite(exit_px):
                    recs.append({"trade_date": str(row.trade_date.date()), "sleeve": row.sleeve,
                                 "horizon": h, "ret": float(exit_px / entry - 1)})
        pick_fwd = pd.DataFrame(recs)
        merged = (pick_fwd.groupby(["trade_date", "sleeve", "horizon"])["ret"].mean().reset_index()
                  .merge(baseline, on=["trade_date", "horizon"], how="inner"))
        picks_table = (merged.groupby(["sleeve", "horizon"])
                       .agg(days=("trade_date", "nunique"), pick_ret=("ret", "mean"),
                            random_mean=("random_topk_mean", "mean"), random_p95=("random_p95", "mean"),
                            universe_mean=("universe_mean", "mean"))
                       .reset_index())
        picks_table["excess_vs_universe"] = picks_table["pick_ret"] - picks_table["universe_mean"]
        picks_table["beats_random_mean"] = picks_table["pick_ret"] > picks_table["random_mean"]
        picks_table["beats_random_p95"] = picks_table["pick_ret"] > picks_table["random_p95"]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"sleeve_random_baseline_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline.to_csv(out_dir / "baseline_by_date.csv", index=False)
    summary.to_csv(out_dir / "baseline_summary.csv", index=False)
    if picks_table is not None:
        picks_table.to_csv(out_dir / "picks_vs_baseline.csv", index=False)

    lines = ["# W2 域内随机基线（无技能对照）", "",
             f"- 决策日：{len(decision_dates)} 个（{decision_dates[0].date()} ~ {decision_dates[-1].date()}）；"
             f"每次抽 {args.top_k} 只 × {args.draws} 次；成交额下限 {args.min_amount/1e4:.0f} 万", "",
             "| horizon | 天数 | 可交易域中位只数 | 域内等权收益 | 随机 top-k 均值 | 随机 p05 | 随机 p95 |",
             "|---|---|---|---|---|---|---|"]
    for r in summary.itertuples():
        lines.append(f"| {r.horizon}d | {r.days} | {r.universe_n:.0f} | {r.universe_mean:.2%} | "
                     f"{r.random_mean:.2%} | {r.random_p05:.2%} | {r.random_p95:.2%} |")
    if picks_table is not None:
        lines += ["", "## 真实持仓 vs 随机基线（同一 horizon 重算）", "",
                  "| sleeve | horizon | 天数 | 真实持仓收益 | 随机均值 | 随机 p95 | 超出域内等权 | 过随机均值 | 过 p95 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in picks_table.itertuples():
            lines.append(f"| {r.sleeve} | {r.horizon}d | {r.days} | {r.pick_ret:.2%} | {r.random_mean:.2%} | "
                         f"{r.random_p95:.2%} | {r.excess_vs_universe:+.2%} | "
                         f"{'✅' if r.beats_random_mean else '❌'} | {'✅' if r.beats_random_p95 else '❌'} |")
    lines += ["", "## 判读规则（写入设计规格 §7）", "",
              "- 真实持仓收益必须**同时**满足：> 随机 top-k 均值，且 > 随机 p95（即超过 95% 的无技能抽样）；",
              "- 否则该 sleeve 不具备可上线技能，保持 research/off（或重做标签）。",
              f"- 输出目录：`{out_dir.relative_to(ROOT)}`"]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "decision_dates": len(decision_dates), "top_k": args.top_k, "draws": args.draws,
        "summary": summary.to_dict("records"),
        "picks": picks_table.to_dict("records") if picks_table is not None else None,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[w2] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
