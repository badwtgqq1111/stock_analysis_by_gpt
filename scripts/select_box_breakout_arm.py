#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""横盘箱体突破回踩手臂（box_arm）：把上一轮"结构型信号"落成**可查的候选策略**。

背景（`output/research/box_breakout/REPORT.md`）：把社区策略"首板+回调+企稳+低吸"换成**多日结构**口径 ——
长期窄幅横盘（60 日振幅 ≤45% 且 60 日无涨停）→ 首板且收盘创 60 日新高（箱体被突破）→ 1~5 日内回调 ≥3%
→ **回踩落在箱体上沿 −3%~+1% 内不破** → 次日开盘买（当日收盘卖）。实测（2016-2026，4,888 笔）：

  * 逐笔毛 +0.352%、胜率 51.3%（**日度聚类后 NW t = +3.66，显著为正**）；
  * 每天满仓口径下双边 31bp 把毛收益吃光（扣费后 +0.042% ≈ 0）；
  * **降换手**（每周只做一次）后 NW t 仍 +2.56 —— 所以本手臂默认 **weekly** 节奏。

这里把它做成既有名单又有**净值曲线**的候选策略（与 first_ban_lowxi / rot_arm 同一套接线）：
  * 名单：`output/results_cn/box_arm_history/<决策日>.csv`（决策日 = 回踩日；供台账/选股日历）
  * 最新：`output/results_cn/box_arm_selection.csv/json`
  * 曲线：`output/research/box_breakout/curve/nav_engine.csv`（T+1 开盘买、当日收盘卖、隔夜空仓、扣 15.5bp/边）

用法：
  uv run python scripts/select_box_breakout_arm.py                     # 默认 weekly、最多 5 只
  uv run python scripts/select_box_breakout_arm.py --cadence daily --holdings 10
  # 2026-10-22 低波拐点变体（不覆盖原 box_arm）：
  uv run python scripts/select_box_breakout_arm.py --arm-id box_squeeze_arm \
      --consolidation-amp 0.25 --max-range-ratio 0.5 --max-pre-amp20 0.10
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import box_breakout_study as box  # noqa: E402
import first_ban_lowxi_study as lowxi  # noqa: E402

OUT = ROOT / "output/results_cn"
HISTORY = OUT / "box_arm_history"
CURVE_DIR = ROOT / "output/research/box_breakout/curve"
COST_ONE_WAY = 15.5 / 1e4


def build_picks(start: str, cadence: str, holdings: int,
                consolidation_amp: float, pullback_min: float, retest_band: float,
                max_range_ratio: float = 1.0, max_pre_amp20: float = 1.0) -> pd.DataFrame:
    """回踩日 → 名单（每周/每日节奏，按箱体振幅升序 = 越窄的横盘越优先）。"""
    bars = box.forward_returns(lowxi.bars_window(start))
    events = box.build_signals(bars, consolidation_amp, pullback_min, retest_band,
                               max_range_ratio=max_range_ratio,
                               max_pre_amp20=max_pre_amp20)
    if events.empty:
        raise SystemExit("没有结构型信号：检查参数与日线数据")
    merged = events.merge(
        bars[["stock_code", "trade_date", "open", "close", "next_open", "next_close",
              "open_next_ret", "hold2", "hold3", "hold5"]],
        left_on=["stock_code", "retest_date"], right_on=["stock_code", "trade_date"], how="left")
    merged = merged.dropna(subset=["next_open", "next_close", "open_next_ret"])
    merged["week"] = pd.to_datetime(merged["retest_date"]).dt.to_period("W")
    if cadence == "weekly":
        merged = merged.sort_values(["week", "retest_date", "consolidated_amp"])
        merged = merged.groupby("week", group_keys=False).head(holdings * 4)
        merged = merged.sort_values(["week", "consolidated_amp"])
        merged = merged.groupby("week", group_keys=False).head(holdings)
    else:
        merged = merged.sort_values(["retest_date", "consolidated_amp"]).groupby(
            "retest_date", group_keys=False).head(holdings)
    merged["target_weight"] = 1.0 / merged.groupby("retest_date")["stock_code"].transform("count")
    return merged.reset_index(drop=True)



def _basic_names() -> dict[str, dict[str, str]]:
    """`cn_stock_basic.parquet` → {ts_code: {name, industry}}（缺表时返回空映射，不阻断）。"""
    path = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    if not path.exists():
        return {"name": {}, "industry": {}}
    frame = pd.read_parquet(path, columns=["ts_code", "name", "industry"]).drop_duplicates("ts_code")
    return {"name": dict(zip(frame["ts_code"].astype(str), frame["name"].fillna("").astype(str))),
            "industry": dict(zip(frame["ts_code"].astype(str), frame["industry"].fillna("").astype(str)))}


def main() -> int:
    ap = argparse.ArgumentParser(description="横盘箱体突破回踩手臂（候选策略：名单 + 净值曲线）")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--cadence", default="weekly", choices=["weekly", "daily"])
    ap.add_argument("--holdings", type=int, default=5)
    ap.add_argument("--arm-id", default="box_arm", help="产物前缀；squeeze 变体用 box_squeeze_arm")
    ap.add_argument("--consolidation-amp", type=float, default=0.45)
    ap.add_argument("--max-range-ratio", type=float, default=1.0,
                    help="突破前20日振幅 / 60日振幅上限；<1 = 压缩箱体")
    ap.add_argument("--max-pre-amp20", type=float, default=1.0,
                    help="突破前20日振幅绝对值上限；0.10 = 低波箱体上下价差不超过 10%%")
    ap.add_argument("--pullback-min", type=float, default=0.03)
    ap.add_argument("--retest-band", type=float, default=0.03)
    ap.add_argument("--skip-sensitivity", action="store_true", help="跳过相邻口径敏感性对照（默认跑）")
    args = ap.parse_args()

    picks = build_picks(args.start, args.cadence, args.holdings, args.consolidation_amp,
                        args.pullback_min, args.retest_band, args.max_range_ratio,
                        args.max_pre_amp20)
    history_dir = OUT / f"{args.arm_id}_history"
    curve_dir = (ROOT / "output/research/box_breakout/curve" if args.arm_id == "box_arm"
                 else ROOT / "output/research/box_breakout" / args.arm_id / "curve")
    print(f"[box-arm] 决策日 {picks['retest_date'].nunique()} 个 / 逐笔 {len(picks)} 笔"
          f"（{args.cadence}，最多 {args.holdings} 只/期）", flush=True)

    # ── 曲线：T+1 开盘买、当日收盘卖、隔夜空仓（与 first_ban_lowxi 同一时序口径）──
    daily = picks.groupby("retest_date").apply(
        lambda g: pd.Series({"ret": g["open_next_ret"].head(args.holdings).mean(),
                             "n": int(len(g))}), include_groups=False).reset_index()
    daily["retest_date"] = pd.to_datetime(daily["retest_date"])
    raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    bench = raw[raw.ts_code == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench.set_index("trade_date")["close"].sort_index()

    # 成交日 = 决策日之后的第一个交易日（T+1）
    calendar = bench.index
    def next_day(day: pd.Timestamp) -> pd.Timestamp | None:
        future = calendar[calendar > day]
        return future[0] if len(future) else None
    daily["buy_date"] = daily["retest_date"].map(next_day)
    daily = daily.dropna(subset=["buy_date"])
    frame = pd.DataFrame(index=calendar[(calendar >= daily["buy_date"].min()) & (calendar <= daily["buy_date"].max())])
    frame["ret"] = daily.set_index("buy_date")["ret"].reindex(frame.index).fillna(0.0)
    frame["n"] = daily.set_index("buy_date")["n"].reindex(frame.index).fillna(0).astype(int)
    net = frame["ret"] - np.where(frame["n"] > 0, 2 * COST_ONE_WAY, 0.0)
    nav = (1 + net).cumprod()
    bnav = bench.reindex(frame.index)
    bnav = bnav / bnav.iloc[0]
    curve_dir.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame({"asof_date": [str(d.date()) for d in frame.index],
                        "nav": nav.to_numpy(), "csi500": bnav.to_numpy(), "n_picks": frame["n"].to_numpy()})
    out.to_csv(curve_dir / "nav_engine.csv", index=False)

    # ── 名单 ──
    history_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for date, group in picks.groupby("retest_date"):
        day = str(pd.Timestamp(date).date())
        table = group.copy()
        table["trade_date"] = day
        table["stock_code"] = table["stock_code"]
        table["rank"] = range(1, len(table) + 1)
        table["in_portfolio"] = True
        # 名字/行业从股票基础表补（2026-10-15：原来这里是空串占位、从没填过 ⇒ 名单里没有名字，
        # 下游（/api/selection、选股台账、K 线自选）只能显示空；补齐后前端与台账都有名字）。
        basic = _basic_names()
        table["name"] = table["stock_code"].map(basic["name"]).fillna("")
        table["industry"] = table["stock_code"].map(basic["industry"]).fillna("")
        table["board"] = "箱体突破回踩"
        table["score"] = -table["consolidated_amp"]           # 箱体越窄分越高
        table["lots_at_capital"] = 0
        table["planned_amount"] = 0
        table[["trade_date", "stock_code", "name", "board", "industry", "score", "rank",
               "in_portfolio", "target_weight", "lots_at_capital", "planned_amount",
               "consolidated_amp", "pre_amp20", "range_ratio", "offset", "level"]].to_csv(
                   history_dir / f"{day}.csv", index=False)
        rows.append(table)
    latest = max(rows, key=lambda t: t["trade_date"].iloc[0]) if rows else None
    if latest is not None:
        latest.to_csv(OUT / f"{args.arm_id}_selection.csv", index=False)

    def stats(series: pd.Series) -> dict:
        x = np.r_[1.0, series.to_numpy(dtype=float)]
        rets = x[1:] / x[:-1] - 1
        years = len(rets) / 252
        peak = np.maximum.accumulate(x)
        return {"total_return": float(x[-1] - 1), "max_drawdown": float((x / peak - 1).min()),
                "annual_return": float(x[-1] ** (1 / years) - 1) if years > 0 and len(rets) > 60 else None,
                "sharpe_rf0": float(rets.mean() / rets.std(ddof=1) * np.sqrt(252)) if rets.std(ddof=1) > 0 else None,
                "n_days": int(len(rets))}

    nw_t = box._nw_t(net.to_numpy(dtype=float), 5)
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cadence": args.cadence, "holdings": args.holdings,
        "params": {"consolidation_amp": args.consolidation_amp, "pullback_min": args.pullback_min,
                   "retest_band": args.retest_band, "max_range_ratio": args.max_range_ratio,
                   "max_pre_amp20": args.max_pre_amp20},
        "window": [str(frame.index[0].date()), str(frame.index[-1].date())],
        "n_decision_days": int(picks["retest_date"].nunique()), "n_trades": int(len(picks)),
        "avg_picks_per_day": float(daily["n"].mean()),
        "trade_mean_gross": float(picks["open_next_ret"].mean()),
        "trade_mean_net": float(picks["open_next_ret"].mean() - 2 * COST_ONE_WAY),
        "metrics": stats(nav), "benchmark": {"code": "000905.SH", "name": "中证500",
                                             "total_return": float(bnav.iloc[-1] - 1)},
        "daily_report_nw_t": float(nw_t),
        "methodology": ("T 日（回踩日）选股 → T+1 开盘买 → T+1 收盘卖、隔夜空仓；"
                        f"{args.cadence} 节奏、每期最多 {args.holdings} 只等权、按箱体振幅升序（越窄越优先）；"
                        f"突破前20日/60日振幅上限={args.max_range_ratio:.2f}，"
                        f"突破前20日振幅绝对值上限={args.max_pre_amp20:.2%}；"
                        "扣 15.5bp/边（含滑点）。"),
        "evidence": (
            "output/research/box_breakout/REPORT.md（原始 60 日振幅 ≤45% 口径：毛 +0.352%、扣费 +0.042%、"
            "日度聚类 NW t=+3.66）"
            if args.arm_id == "box_arm" else
            "output/research/box_breakout/BOX_SQUEEZE.md（压缩箱体：60 日振幅 ≤25%、突破前 20 日振幅 "
            "≤10% 且 ≤50%×60 日振幅；事件层 263 笔毛 +0.89%、扣费 +0.58%、日度聚类 NW t=+3.64）"
        ),
    }
    # ── 参数敏感性（同一信号、不同节奏/宽度）──────────────────────────────
    # 为什么必须跑：这个结构型信号的**逐笔**毛期望显著（t=3.66），但组合形态对节奏/宽度极其敏感
    # —— 实测 weekly×5 累计 +27%、weekly×10 却是 -69%、daily×10 -75%。只报最好的那一格就是自欺。
    sensitivity = []
    if not args.skip_sensitivity:
        for cadence, holdings in (("weekly", 5), ("weekly", 10), ("daily", 5), ("daily", 10)):
            if (cadence, holdings) == (args.cadence, args.holdings):
                continue
            try:
                alt = build_picks(args.start, cadence, holdings, args.consolidation_amp,
                                  args.pullback_min, args.retest_band, args.max_range_ratio,
                                  args.max_pre_amp20)
                daily_alt = alt.groupby("retest_date")["open_next_ret"].mean()
                net_alt = daily_alt - 2 * COST_ONE_WAY
                nav_alt = (1 + net_alt.sort_index()).cumprod()
                sensitivity.append({
                    "cadence": cadence, "holdings": holdings, "n_trades": int(len(alt)),
                    "trade_mean_gross": float(alt["open_next_ret"].mean()),
                    "total_return_simple": float(nav_alt.iloc[-1] - 1) if len(nav_alt) else None,
                })
            except SystemExit:
                continue
    payload["parameter_sensitivity"] = sensitivity
    positive = [f"{r['cadence']}×{r['holdings']}" for r in sensitivity
                if (r.get("total_return_simple") or 0) > 0]
    payload["sensitivity_note"] = (
        "结构型信号对节奏/宽度极敏感：原始 45% 箱体只有 weekly×5 为正，"
        "weekly×10 与 daily×10 都是深度亏损 —— 所以它只能当观察候选，不进生产权重。"
        if args.arm_id == "box_arm" else
        "压缩箱体变体的相邻口径见 parameter_sensitivity；只有样本足够、且相邻节奏不全为负时才考虑晋级。"
        + (f" 本口径相邻正收益格：{'、'.join(positive)}。" if positive else " 本口径相邻格均为负。")
    )
    (curve_dir / "curve.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                                          encoding="utf-8")
    print(f"[box-arm] 曲线 {payload['window'][0]} ~ {payload['window'][1]}："
          f"累计 {payload['metrics']['total_return']*100:+.2f}%（中证500 {payload['benchmark']['total_return']*100:+.2f}%）"
          f" 回撤 {payload['metrics']['max_drawdown']*100:.1f}% NW t {nw_t:+.2f}", flush=True)
    print(f"[box-arm] 逐笔毛 {payload['trade_mean_gross']*100:+.3f}% / 净 {payload['trade_mean_net']*100:+.3f}%"
          f"（avg {payload['avg_picks_per_day']:.1f} 只/期）", flush=True)
    for row in sensitivity:
        print(f"[box-arm] 敏感性 {row['cadence']}×{row['holdings']}: 逐笔毛 {row['trade_mean_gross']*100:+.3f}% "
              f"/ 累计 {row['total_return_simple']*100:+.1f}%", flush=True)
    print(f"[box-arm] 写出 {history_dir.relative_to(ROOT)}/{len(rows)} 个决策日 + "
          f"{args.arm_id}_selection.csv + {(curve_dir / 'nav_engine.csv').relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
