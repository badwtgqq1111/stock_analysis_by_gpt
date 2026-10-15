#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主力资金流追踪（moneyflow_arm）：名单 + 净值曲线 + 参数网格（候选策略）。

口径与结论见 `scripts/moneyflow_study.py` 与 `output/research/moneyflow/REPORT.md`。
默认参数（z_5d=1.0 / 最多 5 只 / 持有 10 日 / 跌破入场价 −8% 止损）选的是**参数高原的中心**，
不是网格里最好的那一格（z=1.5/pos=5/hold=10 的 +226.6pp 是孤立高点，不作为默认）。

产物：
  * 名单：`output/results_cn/moneyflow_arm_history/<信号日>.csv`
  * 最新：`output/results_cn/moneyflow_arm_selection.csv` / `.json`
  * 曲线：`output/research/moneyflow/curve/nav_engine.csv`（trade_date/asof_date/nav/csi500）
  * 报告：`output/research/moneyflow/REPORT.md`（含 27 格参数网格与分年）
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

import moneyflow_study as ms  # noqa: E402
import wyckoff_study as ws    # noqa: E402

OUT = ROOT / "output/results_cn"
RESEARCH_ROOT = ROOT / "output/research"
# 两个变体各自一套产物（同一条流水线，便于横向对比）：
#   moneyflow = 基线：只用资金流（主力净流入 z + 持续性 + 形态）
#   mainforce = 主力量化（第八十三轮）：资金流 + 筹码**成本偏离**参与名额排序
VARIANTS = {
    "moneyflow": {"id": "moneyflow_arm", "name": "主力资金流追踪",
                  "research": RESEARCH_ROOT / "moneyflow", "kwargs": {}},
    "mainforce": {"id": "mainforce_arm", "name": "主力追踪（资金流＋筹码成本偏离）",
                  "research": RESEARCH_ROOT / "mainforce", "kwargs": {"chip_rank": True}},
}
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
Z_CAP_NOTE = "moneyflow_net_z_5d 在数据里被硬截断在 ±1.79（实测），阈值 >1.79 恒为 0 信号"
CHIP_NOTE = ("筹码（tushare cyq_perf）只有 2024-01-02 起的数据；IC 检验显示 chip_price_vs_cost 显著为负"
             "（现价越高于平均成本越弱）、chip_concentration 显著为负（**越集中越弱**）"
             "—— 所以排序用「成本偏离小」优先")
VARIANT_TABLE = (  # 本轮 A/B 实测（同窗口同参数，只改筹码怎么用）
    [{"variant": "A 资金流基线", "chip": "不用", "total_return": 1.741, "max_drawdown": -0.400,
      "sharpe": 0.95, "calmar": 1.11, "note": "现行 moneyflow_arm"},
     {"variant": "F 资金流＋筹码成本偏离排序", "chip": "参与排序（各半）", "total_return": 1.413,
      "max_drawdown": -0.146, "sharpe": 1.48, "calmar": 2.59, "note": "本轮登记的 mainforce_arm"},
     {"variant": "C 硬过滤现价/成本 ≤15%", "chip": "硬过滤", "total_return": 1.192,
      "max_drawdown": -0.202, "sharpe": 1.34, "calmar": 1.64, "note": "只砍风险，收益掉更多"},
     {"variant": "反例：要求筹码集中（宽度小）", "chip": "反向用", "total_return": 0.199,
      "max_drawdown": -0.236, "sharpe": 0.27, "calmar": 0.29,
      "note": "与 IC 结论一致：越集中越弱，按老口号用会把曲线做没"}])


def _bench() -> pd.Series:
    raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet",
                          columns=["ts_code", "trade_date", "close"])
    raw = raw[raw.ts_code == "000905.SH"].copy()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    return raw.set_index("trade_date")["close"].sort_index()


def _basic() -> pd.DataFrame:
    if not BASIC.exists():
        return pd.DataFrame(columns=["ts_code", "name", "industry"])
    cols = [c for c in ("ts_code", "name", "industry") if c in pd.read_parquet(BASIC).columns]
    return pd.read_parquet(BASIC, columns=cols).drop_duplicates("ts_code")


def build_picks(signals: pd.DataFrame, positions: int) -> pd.DataFrame:
    basic = _basic()
    picks = signals.merge(basic, left_on="stock_code", right_on="ts_code", how="left")
    picks = picks.drop(columns=[c for c in ("ts_code",) if c in picks.columns])
    picks = picks.sort_values(["trade_date", "cause_width"])
    picks["rank"] = picks.groupby("trade_date").cumcount() + 1
    picks["target_weight"] = np.where(picks["rank"] <= positions, 1.0 / positions, 0.0)
    picks["score"] = picks["moneyflow_net_z_5d"].round(4)
    picks["stop"] = (picks["close"] * 0.92).round(3)
    return picks


def grid(bars, mf, bench, base: dict) -> list[dict]:
    rows = []
    for z in (1.0, 1.5):
        for pos in (3, 5, 10):
            for hold in (5, 10, 20):
                sig = ms.build_signals(bars, mf, z_5d=z)
                r = ms.replay(sig, bars, bench, max_positions=pos, max_hold=hold)
                m = ws.metrics(r["nav"])
                bm = ws.metrics(r["benchmark_nav"])
                rows.append({"z_5d": z, "max_positions": pos, "max_hold": hold,
                             "n_trades": int(len(r["trades"])),
                             "total_return": round(m["total_return"], 4),
                             "max_drawdown": round(m["max_drawdown"], 4),
                             "sharpe": round(m["sharpe"], 3) if m.get("sharpe") is not None else None,
                             "excess_vs_csi500": round(m["total_return"] - bm["total_return"], 4)})
    return rows


def year_table(nav, bnav) -> list[dict]:
    out = []
    for year in sorted({d.year for d in nav.index}):
        s, b = nav[nav.index.year == year], bnav[bnav.index.year == year]
        if len(s) < 10:
            continue
        out.append({"year": int(year), "strategy": round(float(s.iloc[-1] / s.iloc[0] - 1), 4),
                    "csi500": round(float(b.iloc[-1] / b.iloc[0] - 1), 4),
                    "excess": round(float(s.iloc[-1] / s.iloc[0] - b.iloc[-1] / b.iloc[0]), 4)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="主力资金流追踪（候选策略：名单 + 净值曲线）")
    ap.add_argument("--bars-start", default="2024-06-01")
    ap.add_argument("--max-picks", type=int, default=10)
    ap.add_argument("--skip-grid", action="store_true")
    ap.add_argument("--variant", default="moneyflow", choices=sorted(VARIANTS))
    args = ap.parse_args()

    variant = VARIANTS[args.variant]
    ident = variant["id"]
    HISTORY = OUT / f"{ident}_history"
    RESEARCH = variant["research"]
    CURVE_DIR = RESEARCH / "curve"
    print(f"[moneyflow] 变体 {args.variant}（id={ident}，筹码用法={variant['kwargs'] or '不用'}）")

    bars, mf = ms.load(args.bars_start)
    bench = _bench()
    base = {k: ms.DEFAULTS[k] for k in ("z_5d", "pos_ratio_10d", "ret20_min", "ret20_max",
                                        "max_positions", "max_hold", "stop_pct", "min_amount_20d")}
    signals = ms.build_signals(bars, mf, **variant['kwargs'])
    if signals.empty:
        print("[moneyflow] 没有信号")
        return 0
    result = ms.replay(signals, bars, bench, **variant['kwargs'])
    nav, bnav, trades = result["nav"], result["benchmark_nav"], result["trades"]
    # 曲线从**第一个信号日**起算：资金流特征表从 2024-08-23 才有数据，
    # 若不裁掉前面 7 个月的"空仓平线"，年化会被摊薄、窗口也会写成 2024-01（口径不实）。
    start = signals["trade_date"].min()
    nav, bnav = nav[nav.index >= start], bnav[bnav.index >= start]
    nav, bnav = nav / nav.iloc[0], bnav / bnav.iloc[0]
    stats, bench_stats = ws.metrics(nav), ws.metrics(bnav)
    picks = build_picks(signals, base["max_positions"])

    HISTORY.mkdir(parents=True, exist_ok=True)
    written = 0
    for day, group in picks.groupby("trade_date"):
        stamp = str(pd.Timestamp(day).date())
        rows = group[group["rank"] <= args.max_picks].copy()
        rows["decision_date"] = stamp
        cols = ["rank", "stock_code", "name", "industry", "score", "target_weight", "stop",
                "close", "moneyflow_net_z_5d", "moneyflow_positive_ratio_10d", "ret20", "decision_date"]
        rows[[c for c in cols if c in rows.columns]].to_csv(HISTORY / f"{stamp}.csv", index=False)
        written += 1
    latest_day = str(picks["trade_date"].max().date())
    latest = picks[picks["trade_date"] == picks["trade_date"].max()].head(args.max_picks)
    latest_out = latest[["rank", "stock_code", "name", "industry", "score", "target_weight", "stop",
                         "close", "moneyflow_net_z_5d", "moneyflow_positive_ratio_10d", "ret20"]].copy()
    latest_out.insert(0, "trade_date", latest_day)
    latest_out.to_csv(OUT / f"{ident}_selection.csv", index=False)

    # 曲线对齐中证500 日历（策略池 replay() 会逐日校验）
    calendar = bench.loc[(bench.index >= nav.index[0]) & (bench.index <= nav.index[-1])].index
    nav_al = nav.reindex(calendar).ffill()
    bnav_al = bench.reindex(calendar)
    bnav_al = bnav_al / bnav_al.iloc[0]
    open_days = pd.Series(0, index=calendar, dtype=int)
    for _, tr in trades.iterrows():
        open_days.loc[(calendar >= tr["entry"]) & (calendar < tr["exit"] + pd.Timedelta(days=1))] += 1
    CURVE_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"trade_date": calendar.strftime("%Y-%m-%d"), "asof_date": calendar.strftime("%Y-%m-%d"),
                  "nav": nav_al.to_numpy(), "csi500": bnav_al.to_numpy(),
                  "n_positions": open_days.to_numpy()}).to_csv(CURVE_DIR / "nav_engine.csv", index=False)

    grid_rows = [] if args.skip_grid else grid(bars, mf, bench, base)
    positive = [g for g in grid_rows if g["excess_vs_csi500"] > 0]
    payload = {
        "strategy": ident,
        "variant": args.variant,
        "variant_comparison": VARIANT_TABLE, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": [str(nav_al.index[0].date()), str(nav_al.index[-1].date())],
        "params": base, "nf": "主力净流入（三源资金流特征表 cn_moneyflow_features.parquet）",
        "metrics": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in stats.items()},
        "benchmark": {"code": "000905.SH", "name": "中证500",
                      **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in bench_stats.items()}},
        "excess_total": round(stats["total_return"] - bench_stats["total_return"], 6),
        "n_signals": int(len(signals)), "n_trades": int(len(trades)),
        "trade_stats": ({"mean_ret": round(float(trades.ret.mean()), 5),
                         "win_rate": round(float((trades.ret > 0).mean()), 4),
                         "mean_days": round(float(trades.days.mean()), 2),
                         "exit_reasons": trades.reason.value_counts().to_dict()} if len(trades) else {}),
        "yearly": year_table(nav_al, bnav_al),
        "parameter_grid": grid_rows,
        "grid_summary": {"n_cells": len(grid_rows), "n_positive": len(positive),
                         "median_excess": (round(float(np.median([g["excess_vs_csi500"] for g in grid_rows])), 4)
                                           if grid_rows else None),
                         "best": (max(grid_rows, key=lambda g: g["excess_vs_csi500"]) if grid_rows else None),
                         "note": "默认参数取**高原中心**（z=1.0/hold=10 的三个 pos 与 z=1.5/pos=5/hold=10 全为正超额），"
                                 "不取网格最好的那一格"},
        "methodology": ("主力净流入 z 分数（5 日）≥阈值 + 10 日里 ≥6 天净流入 + 近 20 日涨幅在 [-10%, +8%] + "
                        f"收盘 > MA20 + 20 日均额 ≥2000 万；次日开盘买、持有 {base['max_hold']} 日或"
                        f"跌破入场价 -{base['stop_pct'] * 100:.0f}% 止损、双边 15.5bp"),
        "caveats": [Z_CAP_NOTE, CHIP_NOTE,
                    "资金流特征表只覆盖 2024-08-23 起 ⇒ 样本 ~2.2 年（比其它策略短得多）",
                    "不含 ST/停牌/涨跌停过滤，且最大回撤一度到 −40%（比中证500 同期 −21% 更深）",
                    "参数网格里 81% 的格子跑赢中证500，但最好的那格（+226.6pp）是孤立高点 ⇒ 默认取高原中心"],
        "data": {"moneyflow": str(ms.MF.relative_to(ROOT)), "bars_start": args.bars_start},
    }
    (OUT / f"{ident}_selection.json").write_text(
        json.dumps({**payload, "picks": latest_out.to_dict("records")}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (CURVE_DIR / "curve.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# {variant['name']}（{ident}）· 回放报告", "",
             f"窗口 **{payload['window'][0]} → {payload['window'][1]}**（前复权日线 + 三源资金流特征，次日开盘成交，双边 15.5bp）", "",
             "## 结论", "",
             f"* 累计 **{stats['total_return'] * 100:+.1f}%** vs 中证500 **{bench_stats['total_return'] * 100:+.1f}%**"
             f" ⇒ 超额 **{payload['excess_total'] * 100:+.1f}pp**（{payload['n_trades']} 笔，"
             f"逐笔 {payload['trade_stats'].get('mean_ret', 0) * 100:+.2f}%，胜率 {payload['trade_stats'].get('win_rate', 0) * 100:.1f}%）",
             f"* 最大回撤 {stats['max_drawdown'] * 100:.1f}%（中证500 {bench_stats['max_drawdown'] * 100:.1f}%）"
             f"｜年化 {stats['annual_return'] * 100:+.1f}%｜夏普 {stats.get('sharpe')}｜卡玛 {stats.get('calmar')}", "",
             "## 分年", "", "| 年 | 策略 | 中证500 | 超额 |", "|---|---|---|---|"]
    for row in payload["yearly"]:
        lines.append(f"| {row['year']} | {row['strategy'] * 100:+.1f}% | {row['csi500'] * 100:+.1f}% | {row['excess'] * 100:+.1f}pp |")
    if grid_rows:
        lines += ["", "## 参数网格（z 阈值 × 每日名额 × 持有天数）", "",
                  f"**{len(positive)}/{len(grid_rows)} 个格子跑赢中证500**，中位超额 "
                  f"{payload['grid_summary']['median_excess'] * 100:+.1f}pp；默认参数取高原中心，不取最高格。", "",
                  "| z_5d | 名额 | 持有 | 交易 | 累计 | 回撤 | 夏普 | 超额 vs 中证500 |", "|---|---|---|---|---|---|---|---|"]
        for g in grid_rows:
            mark = " ⬅默认" if (g["z_5d"] == base["z_5d"] and g["max_positions"] == base["max_positions"]
                                and g["max_hold"] == base["max_hold"]) else ""
            lines.append(f"| {g['z_5d']} | {g['max_positions']} | {g['max_hold']} | {g['n_trades']} | "
                         f"{g['total_return'] * 100:+.1f}% | {g['max_drawdown'] * 100:.1f}% | {g['sharpe']} | "
                         f"{g['excess_vs_csi500'] * 100:+.1f}pp{mark} |")
    lines += ["", "## 筹码用法对照（同窗口同参数，只改筹码怎么用）", "",
              "| 变体 | 筹码用法 | 累计 | 回撤 | 夏普 | 卡玛 | 说明 |", "|---|---|---|---|---|---|---|"]
    for row in VARIANT_TABLE:
        lines.append(f"| {row['variant']} | {row['chip']} | {row['total_return'] * 100:+.1f}% | "
                     f"{row['max_drawdown'] * 100:.1f}% | {row['sharpe']} | {row['calmar']} | {row['note']} |")
    lines += ["", "## 口径", "", payload["methodology"], "",
              "退出原因：" + json.dumps(payload["trade_stats"].get("exit_reasons", {}), ensure_ascii=False), "",
              "## 已知边界", ""] + [f"* {c}" for c in payload["caveats"]]
    RESEARCH.mkdir(parents=True, exist_ok=True)
    (RESEARCH / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[moneyflow] 窗口 {payload['window'][0]}~{payload['window'][1]}｜信号 {len(signals)}｜成交 {len(trades)} 笔")
    print(f"[moneyflow] 累计 {stats['total_return'] * 100:+.1f}% vs 中证500 {bench_stats['total_return'] * 100:+.1f}% "
          f"⇒ 超额 {payload['excess_total'] * 100:+.1f}pp｜回撤 {stats['max_drawdown'] * 100:.1f}%｜夏普 {stats.get('sharpe')}")
    if grid_rows:
        print(f"[moneyflow] 网格 {len(positive)}/{len(grid_rows)} 格为正超额｜中位 {payload['grid_summary']['median_excess'] * 100:+.1f}pp")
    print(f"[moneyflow] 最新决策日 {latest_day}：{len(latest_out)} 只｜写出 {written} 个决策日 + selection + curve + REPORT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
