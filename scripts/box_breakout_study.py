#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「长期横盘 → 首板 → 回调 → 箱体突破 → 再回调」 vs 文章「首板+回调+企稳+低吸」：
**同一份数据上比一比，是不是同一个逻辑**。

用户问（2026-10-10）："这个和长期横盘首板，然后回调再箱体突破，再回调 是类似的逻辑呢？"

两套说法其实混了两种"低吸"来源：
  * 文章（盘口型）：**当日**的分歧结构 —— 首板、非一字、炸板回封、次日竞价高开 1~6%、按竞价量比排序；
  * 用户描述（结构型）：**多日**的形态结构 —— 长期横盘箱体 → 首板突破 → 回调 → 再突破/回踩。
本脚本把"结构型"用日线可算的方式定义出来，在同一面板/同一标签下与"盘口型"对照：

定义（全部只用 T 日及之前的数据，向量化）：
  * 长期横盘：过去 60 日振幅 `max(high)/min(low)-1` ≤ 横盘阈值（默认 0.45），且这 60 日内**没有涨停**
    （用面板 `zt_cnt_60d`/`max_boards_20d` 校验）——对应"长期无人炒、箱体窄"；
  * 首板突破：`boards==1 & prev_boards==0` 且 **收盘创 60 日新高**（箱体上沿被突破）；
  * 回调：首板后 1~5 日内出现回落（收盘 < 首板日收盘，且回撤幅度 ≥ 3%）；
  * 回踩再买（箱体上沿不破）：回踩日收盘落在「突破位 −3% ~ +1%」区间内 → **次日开盘买**；
  * 对照：面板里同期的①全部涨停 ②首板 ③首板+非一字+换手板 …（文章漏斗）。

评估：`open_next_ret`（次日开盘买→当日收盘，与文章口径一致）、`fwd1`（T 收盘买→次日收盘）、
`fwd2`、`next_zt`（次日是否涨停）；全部扣双边 31bp 后再看。
输出：output/research/box_breakout/{study.json,REPORT.md}
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

import first_ban_lowxi_study as lowxi  # noqa: E402
from reproduce_xy_industry_rotation import _nw_t  # noqa: E402

OUT = ROOT / "output/research/box_breakout"
COST = 2 * 15.5 / 1e4


def build_signals(bars: pd.DataFrame, consolidation_amp: float = 0.45,
                  pullback_min: float = 0.03, retest_band: float = 0.03) -> pd.DataFrame:
    """在日线上向量化标出「横盘 → 首板突破 → 回调 → 回踩」四步。"""
    bars = bars.sort_values(["stock_code", "trade_date"]).copy()
    # `lowxi.bars_window()` 只给 OHLC + 我们自算的 prev_close —— pct_chg 要自己算
    bars["pct_chg"] = bars["close"] / bars["prev_close"] - 1
    g = bars.groupby("stock_code", sort=False)
    bars["hi60"] = g["high"].transform(lambda s: s.rolling(60, min_periods=40).max().shift(1))
    bars["lo60"] = g["low"].transform(lambda s: s.rolling(60, min_periods=40).min())
    bars["amp60"] = bars["hi60"] / bars["lo60"] - 1
    bars["zt"] = (bars["pct_chg"] >= 0.095).astype(int)     # 近似涨停（主板 10%，创业板/科创另算，见 note）
    bars["zt60"] = g["zt"].transform(lambda s: s.rolling(60, min_periods=40).sum().shift(1))
    bars["prev_zt"] = g["zt"].shift(1).fillna(0)
    bars["board_first"] = (bars["zt"] == 1) & (bars["prev_zt"] == 0)
    bars["breakout"] = bars["close"] > bars["hi60"]
    bars["consolidated"] = (bars["amp60"] <= consolidation_amp) & (bars["zt60"] == 0)
    bars["first_breakout"] = bars["board_first"] & bars["breakout"] & bars["consolidated"]
    bars["level"] = bars["hi60"]                            # 箱体上沿
    bars["pullback"] = (bars["close"] / bars["close"].groupby(bars["stock_code"]).shift(1) - 1 < 0)

    # 首板突破后的 1~5 日：回调且回踩到箱体上沿附近（不破）
    rows = []
    view = bars[["stock_code", "trade_date", "first_breakout", "level", "close", "high", "low",
                 "open", "hi60", "amp60", "consolidated", "board_first", "breakout"]]
    for code, frame in view.groupby("stock_code", sort=False):
        index = frame.index.to_numpy()
        hits = np.where(frame["first_breakout"].to_numpy())[0]
        for position in hits:
            level = frame["level"].iloc[position]
            if not np.isfinite(level) or level <= 0:
                continue
            entry_close = frame["close"].iloc[position]
            for offset in range(1, 6):
                if position + offset >= len(index):
                    break
                row = frame.iloc[position + offset]
                drawdown = row["close"] / entry_close - 1
                in_band = -retest_band <= (row["close"] / level - 1) <= 0.01
                if drawdown <= -pullback_min and in_band:
                    rows.append({"stock_code": code, "breakout_date": frame["trade_date"].iloc[position],
                                 "retest_date": row["trade_date"], "offset": offset,
                                 "level": float(level), "consolidated_amp": float(frame["amp60"].iloc[position])})
                    break
    return pd.DataFrame(rows)


def forward_returns(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.sort_values(["stock_code", "trade_date"]).copy()
    g = bars.groupby("stock_code", sort=False)
    bars["next_open"] = g["open"].shift(-1)
    bars["next_close"] = g["close"].shift(-1)
    bars["fwd1"] = bars["next_close"] / bars["close"] - 1
    bars["fwd2"] = g["close"].shift(-2) / bars["close"] - 1
    bars["open_next_ret"] = bars["next_close"] / bars["next_open"] - 1
    bars["next_zt"] = (g["close"].shift(-1) / bars["close"] - 1 >= 0.095).astype(float)
    # "次日开盘买 → 持有 h 天到收盘"：换手降下来，成本只收一次（h=1 就是文章口径）
    for hold in (2, 3, 5):
        bars[f"hold{hold}"] = g["close"].shift(-hold) / bars["next_open"] - 1
    return bars


def summarize(frame: pd.DataFrame, column: str, name: str) -> dict:
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    if values.empty:
        return {"name": name, "n": 0}
    return {"name": name, "n": int(len(values)), "mean": float(values.mean()),
            "median": float(values.median()), "win": float((values > 0).mean()),
            "net_after_cost": float(values.mean() - COST)}


def main() -> int:
    ap = argparse.ArgumentParser(description="横盘箱体突破回踩 vs 文章首板低吸")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--consolidation-amp", type=float, default=0.45, help="横盘阈值：60 日振幅上限")
    ap.add_argument("--pullback-min", type=float, default=0.03, help="回调幅度下限")
    ap.add_argument("--retest-band", type=float, default=0.03, help="回踩落在突破位下方多少以内算有效")
    args = ap.parse_args()

    bars = forward_returns(lowxi.bars_window(args.start))
    print(f"[box] 日线 {len(bars):,} 行 / {bars.stock_code.nunique()} 只", flush=True)
    events = build_signals(bars, args.consolidation_amp, args.pullback_min, args.retest_band)
    print(f"[box] 结构型信号（横盘→首板突破→回调→回踩）{len(events):,} 个", flush=True)
    merged = events.merge(bars[["stock_code", "trade_date", "fwd1", "fwd2", "open_next_ret", "next_zt",
                                "hold2", "hold3", "hold5"]],
                          left_on=["stock_code", "retest_date"], right_on=["stock_code", "trade_date"],
                          how="left")

    panel = lowxi.add_next_day_features(lowxi.load_panel(args.start), bars)
    marked = lowxi.mark_conditions(panel)
    zt = marked[marked["is_zt"] == 1]
    comparisons = [
        summarize(zt, "open_next_ret", "① 全部涨停样本（面板基线）"),
        summarize(zt[zt["boards"] == 1], "open_next_ret", "② 首板"),
        summarize(zt[(zt["boards"] == 1) & (zt["open_limit"] == 0) & (zt["touched_limit"] == 1)],
                  "open_next_ret", "③ 首板 + 非一字 + 换手板（文章前 3 条）"),
        summarize(merged, "open_next_ret", "④ 结构型：横盘→首板突破→回调→回踩（次日开盘买）"),
    ]
    for label in ("fwd1", "fwd2", "next_zt"):
        comparisons.append(summarize(merged, label, f"④ 结构型 · {label}"))
    # 日度聚类的显著性：同一天会有多个信号（横截面相关），直接对逐笔做 t 检验会把 t 值抬高。
    # 先按"回踩日"聚合成日度组合收益，再做 Newey-West(lag=5)。
    daily_cluster = {}
    if len(merged):
        daily = merged.groupby("retest_date")["open_next_ret"].mean().sort_index()
        values = daily.to_numpy(dtype=float)
        daily_cluster = {
            "n_days": int(len(daily)), "mean_daily": float(np.nanmean(values)),
            "nw_t": float(_nw_t(values, 5)), "win_days": float(np.nanmean(values > 0)),
            "net_after_cost": float(np.nanmean(values) - COST),
            "per_trade_n": int(len(merged)),
            "se_note": "同一回踩日多个持仓已聚合成一条日度收益，t 值不再被横截面相关放大",
        }
    holding_variants, weekly = [], {}
    if len(merged):
        for hold, column in ((1, "open_next_ret"), (2, "hold2"), (3, "hold3"), (5, "hold5")):
            row = summarize(merged, column, f"持有 {hold} 天（次日开盘买，扣一次双边 31bp）")
            daily = merged.groupby("retest_date")[column].mean().sort_index()
            row["nw_t"] = float(_nw_t(daily.to_numpy(dtype=float), 5))
            row["n_days"] = int(len(daily))
            holding_variants.append(row)
        weekly_frame = merged.assign(week=pd.to_datetime(merged["retest_date"]).dt.to_period("W"))
        first_days = weekly_frame.groupby("week")["retest_date"].min()
        picked = merged[merged["retest_date"].isin(set(first_days))]
        for hold, column in ((1, "open_next_ret"), (3, "hold3")):
            daily = picked.groupby("retest_date")[column].mean().sort_index()
            weekly[f"hold{hold}"] = {
                "n_trades": int(len(picked)), "n_days": int(len(daily)),
                "mean": float(np.nanmean(daily.to_numpy(dtype=float))),
                "nw_t": float(_nw_t(daily.to_numpy(dtype=float), 5)),
                "net_after_cost": float(np.nanmean(daily.to_numpy(dtype=float)) - COST),
                "win_days": float(np.nanmean(daily.to_numpy(dtype=float) > 0)),
            }
    by_amp = []
    for amp in (0.30, 0.45, 0.60, 1.00):
        sub = events[events["consolidated_amp"] <= amp]
        row = summarize(merged[merged["consolidated_amp"] <= amp] if "consolidated_amp" in merged else sub,
                        "open_next_ret", f"横盘阈值 60 日振幅 ≤ {amp:.0%}")
        by_amp.append(row)
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": args.start, "params": {"consolidation_amp": args.consolidation_amp,
                                        "pullback_min": args.pullback_min, "retest_band": args.retest_band},
        "n_events": int(len(events)), "n_stocks": int(bars.stock_code.nunique()),
        "comparisons": comparisons, "by_consolidation": by_amp, "daily_cluster": daily_cluster,
        "holding_variants": holding_variants, "weekly_variants": weekly,
        "offset_distribution": events["offset"].value_counts().sort_index().to_dict() if len(events) else {},
        "notes": [
            "涨停判定用 9.5% 近似（未区分创业板/科创板/北交所 20%/30% 限价），结构型样本会略偏主板；",
            "回踩日收盘买/次日开盘买都不需要集合竞价数据 —— 这正是结构型与文章盘口型的关键差别；",
            "评估标签与文章口径一致（open_next_ret = 次日开盘买→当日收盘），扣双边 31bp。",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "study.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(payload, OUT / "REPORT.md")
    if daily_cluster:
        print(f"[box] 日度组合（{daily_cluster['n_days']} 个回踩日 / {daily_cluster['per_trade_n']} 笔）："
              f"日均 {daily_cluster['mean_daily']:+.3%} NW t {daily_cluster['nw_t']:+.2f} "
              f"胜日 {daily_cluster['win_days']:.1%} 扣费后 {daily_cluster['net_after_cost']:+.3%}", flush=True)
    for row in holding_variants:
        if row.get("n"):
            print(f"[box] {row['name']:<34s} n={row['n']:>6,} 均值 {row['mean']:+.3%} "
                  f"胜率 {row['win']:.1%} 扣费后 {row['net_after_cost']:+.3%} NW t {row['nw_t']:+.2f} "
                  f"({row['n_days']} 天)", flush=True)
    for key, row in weekly.items():
        print(f"[box] 每周只做一次 · {key}: {row['n_trades']} 笔 / {row['n_days']} 周 日均 {row['mean']:+.3%} "
              f"扣费后 {row['net_after_cost']:+.3%} NW t {row['nw_t']:+.2f}", flush=True)
    for row in comparisons:
        if row.get("n"):
            print(f"[box] {row['name']:<42s} n={row['n']:>7,} 均值 {row['mean']:+.3%} "
                  f"中位 {row['median']:+.3%} 胜率 {row['win']:.1%} 扣费后 {row['net_after_cost']:+.3%}", flush=True)
    print(f"[box] 写出 {OUT}/study.json + REPORT.md", flush=True)
    return 0


def write_report(payload: dict, path: Path) -> None:
    def pct(v, nd=3):
        return "—" if v is None else f"{v*100:.{nd}f}%"
    lines = ["# 「长期横盘→首板→回调→箱体突破→再回调」 vs 文章「首板+回调+企稳+低吸」", "",
             f"生成 {payload['generated_at']}｜脚本 `scripts/box_breakout_study.py`｜"
             f"样本 {payload['start']} ~ 至今，{payload['n_stocks']} 只股票", "",
             f"结构型信号数：**{payload['n_events']:,}**（横盘振幅阈值 {payload['params']['consolidation_amp']:.0%}、"
             f"回调 ≥{payload['params']['pullback_min']:.0%}、回踩落在突破位 −{payload['params']['retest_band']:.0%}~+1%）", "",
             "## 对照（同标签 `open_next_ret` = 次日开盘买→当日收盘，扣双边 31bp）", "",
             "| 口径 | 样本 | 均值 | 中位 | 胜率 | 扣费后 |", "|---|---|---|---|---|---|"]
    for row in payload["comparisons"]:
        if not row.get("n"):
            continue
        lines.append(f"| {row['name']} | {row['n']:,} | {pct(row['mean'])} | {pct(row['median'])} "
                     f"| {row['win']*100:.1f}% | {pct(row['net_after_cost'])} |")
    cluster = payload.get("daily_cluster") or {}
    if cluster:
        lines += ["", "## 显著性（按回踩日聚合成日度组合，再做 Newey-West）", "",
                  f"* 回踩日 **{cluster['n_days']}** 天 / 逐笔 {cluster['per_trade_n']} 笔；"
                  f"日均收益 **{pct(cluster['mean_daily'])}**、NW t **{cluster['nw_t']:+.2f}**、"
                  f"胜日 {cluster['win_days']*100:.1f}%、扣费后 {pct(cluster['net_after_cost'])}；",
                  f"* {cluster['se_note']}。"]
    if payload.get("holding_variants"):
        lines += ["", "## 换手是这条策略的命门：不同持有期（次日开盘买、扣一次双边 31bp）", "",
                  "| 口径 | 样本 | 均值 | 胜率 | 扣费后 | 日度 NW t | 回踩日 |", "|---|---|---|---|---|---|---|"]
        for row in payload["holding_variants"]:
            if row.get("n"):
                lines.append(f"| {row['name']} | {row['n']:,} | {pct(row['mean'])} | {row['win']*100:.1f}% "
                             f"| {pct(row['net_after_cost'])} | {row['nw_t']:+.2f} | {row.get('n_days')} |")
    if payload.get("weekly_variants"):
        lines += ["", "## 每周只做一次（每周第一个回踩日）", "",
                  "| 口径 | 笔数 | 周数 | 日均 | 扣费后 | 胜周 | NW t |", "|---|---|---|---|---|---|---|"]
        for key, row in payload["weekly_variants"].items():
            lines.append(f"| {key} | {row['n_trades']} | {row['n_days']} | {pct(row['mean'])} "
                         f"| {pct(row['net_after_cost'])} | {row['win_days']*100:.1f}% | {row['nw_t']:+.2f} |")
    lines += ["", "## 结构型在不同横盘阈值下", "", "| 阈值 | 样本 | 均值 | 胜率 | 扣费后 |", "|---|---|---|---|---|"]
    for row in payload["by_consolidation"]:
        if row.get("n"):
            lines.append(f"| {row['name']} | {row['n']:,} | {pct(row['mean'])} | {row['win']*100:.1f}% "
                         f"| {pct(row['net_after_cost'])} |")
    lines += ["", "## 说明", ""] + [f"* {note}" for note in payload["notes"]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
