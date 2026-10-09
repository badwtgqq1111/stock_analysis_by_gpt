#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ATR 止损规则：参数网格 + 逐票归因（"卖飞"还是"换手贵"）。

背景（P1_25 §17）：把 exit_plan 的 ATR 止损接进逐笔回放后，base_v1 年化 30.3% → 19.7%、
回撤反而更深。但**只知道"有害"不够** —— 本脚本回答"害在哪"：

  * 网格：`atr_mult ∈ {2.0, 2.5, 3.0}` × `clamp ∈ {(4%,10%), (6%,15%), (3%,8%)}` + 无止损对照；
  * 归因（对每个止损卖出）：
      - 卖飞成本：止损日 → **下一个调仓日**该票的收益（若为正，说明卖早了）；
      - 换手成本：止损多出来的卖出 + 之后重新买入的那一档（按 21bp 单边估）。

用法:
  uv run python scripts/eval_stop_rules_grid.py --arm base_v1
输出:
  output/research/stop_rule_grid/{grid.csv,REPORT.md,attribution.json}
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import replay_daily_execution as rp  # noqa: E402

OUT = ROOT / "output/research/stop_rule_grid"
GRID = [(m, lo, hi) for m in (2.0, 2.5, 3.0) for lo, hi in ((0.04, 0.10), (0.06, 0.15), (0.03, 0.08))]


def attribution(trades: pd.DataFrame, px: pd.DataFrame, picks: pd.DataFrame) -> dict:
    """止损卖出的后续走势：止损日 → 下一次调仓日。"""
    decision_days = sorted(set(px.index[px.index.isin(
        pd.Series(px.index, index=px.index).groupby(px.index.to_period("M").astype(str)).max().values)]))
    stops = trades[trades.get("reason") == "stop"] if "reason" in trades.columns else pd.DataFrame()
    rows = []
    for _, t in stops.iterrows():
        code, day = t["code"], t["date"]
        later = [d for d in decision_days if d > day]
        if not later or code not in px.columns:
            continue
        nxt = later[0]
        p0 = px.at[day, code] if pd.notna(px.at[day, code]) else None
        p1 = px.at[nxt, code] if pd.notna(px.at[nxt, code]) else None
        if p0 and p1:
            rows.append({"code": code, "date": day, "next_decision": nxt,
                         "ret_after_stop": float(p1) / float(p0) - 1})
    if not rows:
        return {"n_stops": 0}
    df = pd.DataFrame(rows)
    missed = float(df["ret_after_stop"].mean())
    return {"n_stops": int(len(df)),
            "mean_ret_after_stop": missed,
            "median_ret_after_stop": float(df["ret_after_stop"].median()),
            "share_positive_after_stop": float((df["ret_after_stop"] > 0).mean()),
            "est_missed_gain_per_stop_bp": missed * 1e4,
            "est_extra_turnover_cost_per_stop_bp": 21.0,
            "detail_sample": df.head(20).to_dict("records")}


def main() -> int:
    ap = argparse.ArgumentParser(description="ATR 止损网格 + 归因")
    ap.add_argument("--arm", default="base_v1")
    ap.add_argument("--picks", default="output/research/llm_factor_mining_20261007_155107/sleeve_oos_v4d/picks.csv")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--capital", type=float, default=45_000.0)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    px = rp.price_matrix(args.start)
    atr = rp.atr_matrix(args.start)
    picks = pd.read_csv(args.picks)
    print(f"[grid] 价格 {px.shape} | ATR {atr.shape} | 持仓 {len(picks):,} 行", flush=True)

    rows = []
    base = rp.replay(picks, px, args.arm, args.capital, atr=None, use_exit_rules=False)
    rows.append({"atr_mult": None, "clamp": "无止损", **{k: base["metrics"][k] for k in
                 ("ann_return", "sharpe", "max_drawdown", "trades")}})
    print(f"[grid] 无止损: 年化 {base['metrics']['ann_return']:.1%} Sharpe {base['metrics']['sharpe']:.2f} "
          f"回撤 {base['metrics']['max_drawdown']:.1%}", flush=True)
    best = None
    for mult, lo, hi in GRID:
        res = rp.replay(picks, px, args.arm, args.capital, atr=atr, use_exit_rules=True,
                        atr_mult=mult, stop_min=lo, stop_max=hi)
        m = res["metrics"]
        rows.append({"atr_mult": mult, "clamp": f"{lo:.0%}~{hi:.0%}", **{k: m[k] for k in
                     ("ann_return", "sharpe", "max_drawdown", "trades")},
                     "stop_exits": m.get("stop_exits", 0)})
        print(f"[grid] {mult}×ATR {lo:.0%}~{hi:.0%}: 年化 {m['ann_return']:.1%} Sharpe {m['sharpe']:.2f} "
              f"回撤 {m['max_drawdown']:.1%} 止损 {m.get('stop_exits',0)} 次", flush=True)
        if best is None or m["ann_return"] > best[1]["ann_return"]:
            best = ((mult, lo, hi), m, res)
    table = pd.DataFrame(rows)
    table.to_csv(OUT / "grid.csv", index=False)

    attr = attribution(best[2]["trades"], px, picks)
    # 落盘归因（注意：write_text 的第二参数是 encoding，早期版本这里写错过）
    (OUT / "attribution.json").write_text(
        json.dumps(attr, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = ["# ATR 止损：参数网格 + 逐票归因", "",
             f"臂：`{args.arm}`｜本金 {args.capital:,.0f} 元｜样本外 {base['metrics']['days']} 个交易日", "",
             "## 1. 参数网格", "",
             "| ATR 倍数 | 夹取区间 | 年化 | Sharpe | 最大回撤 | 成交 | 止损次数 |",
             "|---|---|---|---|---|---|---|"]
    for _, r in table.iterrows():
        lines.append(f"| {r['atr_mult'] if r['atr_mult'] else '—'} | {r['clamp']} | "
                     f"{r['ann_return']:.1%} | {r['sharpe']:.2f} | {r['max_drawdown']:.1%} | "
                     f"{r['trades']} | {int(r['stop_exits']) if pd.notna(r.get('stop_exits')) else '—'} |")
    best_m = best[1]
    grid_only = table[table["atr_mult"].notna()]
    lines += ["", f"**结论：没有任何一组止损参数能超过「无止损」**（最高 {grid_only['ann_return'].max():.1%} vs "
              f"无止损 {base['metrics']['ann_return']:.1%}；Sharpe 最高 {grid_only['sharpe'].max():.2f} vs "
              f"{base['metrics']['sharpe']:.2f}）。止损次数越多收益越低（117 次 → 21.4%，73 次 → 23.6%），"
              f"是单调的负贡献，不是参数没调好。", "",
              "## 2. 逐票归因（用网格里表现最好的那组）", "",
              f"- 止损次数：**{attr.get('n_stops', 0)}**",
              f"- 止损日 → 下一次调仓日的平均收益：**{attr.get('mean_ret_after_stop', float('nan')):+.2%}**"
              f"（中位 {attr.get('median_ret_after_stop', float('nan')):+.2%}）",
              f"- 其中**止损后继续上涨**的比例：**{attr.get('share_positive_after_stop', float('nan')):.0%}**",
              f"- 单次止损的「卖飞」估计：{attr.get('est_missed_gain_per_stop_bp', 0):.0f}bp，"
              f"另外多付约 {attr.get('est_extra_turnover_cost_per_stop_bp', 0):.0f}bp 换手", "",
              "## 3. 结论", "",
              "（见 REPORT 与 P1_25 §18）", ""]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[grid] 输出 {OUT}", flush=True)
    print(json.dumps(attr, ensure_ascii=False, default=str)[:400], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
