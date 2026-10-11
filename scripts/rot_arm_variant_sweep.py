#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行业轮动手臂：**口径对比**（哪种收益曲线更好）。

用户要求（2026-10-10）："那再试试，看哪种收益曲线更好。"

做法：把同一个"板块层 + 个股层"框架下的若干口径，在**同一批决策日**上各出一份名单，
再用**同一套执行假设**（`run_paper_account`：T+1 开盘成交、佣金/印花税/滑点、整手，起始 45,000）
跑成净值曲线，与**中证500**同窗对比，并做逐期配对检验（vs 基线口径）。

对比的口径维度（都与 `select_rotation_arm.py` 的 `run()` 共用同一段逻辑 `board_choice`/`_assemble`）：
  * 板块层打分：`repo7_linear`（7 价量因子简单等权，现行默认）vs `paper_macro`（论文式：7 价量 + 25 宏观，因子聚类再等权）
  * 大类个数 k：4 / 5
  * 取几个大类：top-1 / top-2
  * 个股腿：`reversal`（板块内买滞涨，现行默认）/ `leader`（板块内买龙头）/ `cap_leader`（市值龙头）

输出：
  output/results_cn/rot_arm_sweep/<tag>/<决策日>.csv    每套口径的名单历史
  output/research/industry_rotation/rot_arm_sweep.json  净值/指标/配对检验
  output/research/industry_rotation/rot_arm_sweep.md    人读对照表
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

import select_rotation_arm as arm  # noqa: E402
import xy_daily_rotation as daily  # noqa: E402
from factor_engine.portfolio.execution_assumptions import load_assumptions  # noqa: E402
from factor_engine.portfolio.paper_account import run_paper_account  # noqa: E402
from scripts.build_strategy_pool_performance import metrics  # noqa: E402

LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
OUT_DIR = ROOT / "output/results_cn/rot_arm_sweep"
RESEARCH = ROOT / "output/research/industry_rotation"
BASELINE_TAG = "base_repo7_k5_top1_reversal"

CONFIGS: list[dict] = [
    # 基线：现行默认（7 价量因子等权 + k=5 + top-1 大类 + 板块内买滞涨）
    {"tag": BASELINE_TAG, "board_kind": "repo7_linear", "k": 5, "top_clusters": 1,
     "stock_mode": "reversal"},
    # 论文式（含 25 个宏观因子、因子聚类再等权）：k=4 / k=5
    {"tag": "macro_k4_top1_reversal", "board_kind": "paper_macro", "k": 4, "top_clusters": 1,
     "stock_mode": "reversal"},
    {"tag": "macro_k5_top1_reversal", "board_kind": "paper_macro", "k": 5, "top_clusters": 1,
     "stock_mode": "reversal"},
    # 大类个数：k=4
    {"tag": "repo7_k4_top1_reversal", "board_kind": "repo7_linear", "k": 4, "top_clusters": 1,
     "stock_mode": "reversal"},
    # 取两个大类（分散）
    {"tag": "repo7_k5_top2_reversal", "board_kind": "repo7_linear", "k": 5, "top_clusters": 2,
     "stock_mode": "reversal"},
    # 个股腿：板块内买龙头 / 市值龙头
    {"tag": "repo7_k5_top1_leader", "board_kind": "repo7_linear", "k": 5, "top_clusters": 1,
     "stock_mode": "leader"},
    {"tag": "repo7_k5_top1_capleader", "board_kind": "repo7_linear", "k": 5, "top_clusters": 1,
     "stock_mode": "cap_leader"},
]


def decision_dates(limit: int = 0, start: str = "") -> list[str]:
    frame = pd.read_parquet(LEDGER)
    dates = sorted(set(frame[frame["source"].isin(["preselected", "notify"])]
                        ["decision_date"].astype(str)))
    if start:
        dates = [d for d in dates if d >= start]
    return dates[-limit:] if limit else dates


def price_bars() -> pd.DataFrame:
    import pyarrow.dataset as ds
    files = [str(p) for year in (2025, 2026)
             for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["stock_code", "trade_date", "open", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    return bars[bars["trade_date"] >= "2025-06-01"]


def benchmark() -> pd.Series:
    raw = pd.read_parquet(BENCH)
    series = raw[raw.ts_code == "000905.SH"].copy()
    series["trade_date"] = pd.to_datetime(series["trade_date"])
    return series.set_index("trade_date")["close"].sort_index()


def period_returns(nav: pd.Series, dates: list[str]) -> pd.Series:
    """按决策日区间切期收益（第 i 期 = 决策日 i → i+1）。"""
    marks = nav.reindex(pd.DatetimeIndex(dates), method="ffill").dropna()
    return pd.Series({str(a.date()): float(b) / float(a_nav) - 1.0
                      for a, a_nav, b in zip(marks.index[:-1], marks.iloc[:-1], marks.iloc[1:])})


def sign_test(diff: pd.Series) -> dict:
    from math import comb
    d = diff.dropna()
    better, worse = int((d > 0).sum()), int((d < 0).sum())
    n = better + worse
    if n == 0:
        return {"n": 0, "better": 0, "worse": 0, "median": None, "p": None}
    p = 2 * sum(comb(n, k) for k in range(0, min(better, worse) + 1)) / (2 ** n)
    return {"n": n, "better": better, "worse": worse, "median": float(d.median()),
            "p": round(min(p, 1.0), 4)}


def paired_t(a: pd.Series, b: pd.Series) -> dict:
    from scipy import stats
    frame = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(frame) < 8 or (frame["a"] - frame["b"]).std(ddof=1) < 1e-12:
        return {"n": int(len(frame)), "t": None, "p": None, "diff_mean": None}
    test = stats.ttest_rel(frame["a"], frame["b"])
    return {"n": int(len(frame)), "t": float(test.statistic), "p": float(test.pvalue),
            "diff_mean": float((frame["a"] - frame["b"]).mean())}


def main() -> int:
    ap = argparse.ArgumentParser(description="行业轮动手臂口径对比（哪种收益曲线更好）")
    ap.add_argument("--limit", type=int, default=0, help="只用最近 N 个决策日（0=全部）")
    ap.add_argument("--start", default="")
    ap.add_argument("--panel-start", default="2016-01-01")
    ap.add_argument("--capital", type=float, default=45_000.0)
    ap.add_argument("--tags", default="", help="逗号分隔，只跑这些 tag")
    args = ap.parse_args()

    dates = decision_dates(args.limit, args.start)
    configs = [c for c in CONFIGS if not args.tags or c["tag"] in args.tags.split(",")]
    print(f"[sweep] 决策日 {len(dates)} 个（{dates[0]} ~ {dates[-1]}）；口径 {len(configs)} 套", flush=True)

    # 行业面板**只加载一次**（这是最贵的一步），每期按 as_of 切片保证 PIT
    latest = pd.Timestamp(dates[-1])
    panels_all = daily.board_daily_panels(args.panel_start, latest.strftime("%Y-%m-%d"))
    macro_all = None
    if any(c["board_kind"] == "paper_macro" for c in configs):
        macro_all = arm.rot.load_macro_panel(panels_all["ret"].index)
    print(f"[sweep] 行业面板 {panels_all['ret'].shape[0]} 交易日 × {panels_all['ret'].shape[1]} 行业"
          f"；宏观 {0 if macro_all is None else macro_all.shape[1]} 指标", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tables: dict[str, list[pd.DataFrame]] = {c["tag"]: [] for c in configs}
    for date in dates:
        try:
            picks = arm.run_variants(date, configs, start=args.panel_start,
                                     panels_all=panels_all, macro_panel=macro_all)
        except SystemExit as exc:
            print(f"[sweep] {date} 跳过：{exc}", flush=True)
            continue
        for tag, payload in picks.items():
            folder = OUT_DIR / tag
            folder.mkdir(parents=True, exist_ok=True)
            payload["table"].to_csv(folder / f"{date}.csv", index=False)
            tables[tag].append(payload["table"])
        print(f"[sweep] {date} ok："
              + " | ".join(f"{tag}={len(p['table'])}" for tag, p in picks.items()), flush=True)

    bench = benchmark()
    bars = price_bars()
    rows, navs = [], {}
    for config in configs:
        tag = config["tag"]
        frames = tables.get(tag) or []
        if not frames:
            continue
        sel = pd.concat(frames, ignore_index=True)
        sel = sel[sel["in_portfolio"]].copy()                      # 只买名单里的持仓（等权）
        sel["trade_date"] = pd.to_datetime(sel["trade_date"])
        sel["target_weight"] = 1.0 / sel.groupby("trade_date")["stock_code"].transform("count")
        result = run_paper_account(sel[["trade_date", "stock_code", "target_weight"]], bars,
                                   initial_capital=args.capital,
                                   strategy_version=f"sweep-{tag}")
        nav = result["nav"].copy()
        nav["asof_date"] = pd.to_datetime(nav["asof_date"])
        nav = nav.set_index("asof_date")["nav"].sort_index()
        nav = nav[nav.index >= sel["trade_date"].min()]
        navs[tag] = nav
        b = bench.reindex(nav.index).dropna()
        nav_aligned = nav.reindex(b.index)
        nav_norm = (nav_aligned / nav_aligned.iloc[0]).tolist()
        bench_norm = (b / b.iloc[0]).tolist()
        rows.append({
            "tag": tag, "config": {k: v for k, v in config.items() if k != "tag"},
            "decision_dates": int(sel["trade_date"].nunique()),
            "start_date": str(nav_aligned.index[0].date()), "end_date": str(nav_aligned.index[-1].date()),
            "nav": nav_norm, "benchmark_nav": bench_norm,
            "dates": [str(d.date()) for d in nav_aligned.index],
            "metrics": metrics(nav_norm), "benchmark_metrics": metrics(bench_norm),
            "excess_return": nav_norm[-1] / bench_norm[-1] - 1,
            "fills": int(len(result["fills"])),
        })
        print(f"[sweep] {tag}: 累计 {(nav_norm[-1]-1)*100:.2f}% 基准 {(bench_norm[-1]-1)*100:.2f}% "
              f"超额 {(nav_norm[-1]/bench_norm[-1]-1)*100:.2f}% 成交 {len(result['fills'])}", flush=True)

    by_tag = {row["tag"]: row for row in rows}
    baseline_periods = None
    if BASELINE_TAG in navs:
        baseline_periods = period_returns(navs[BASELINE_TAG], dates)
    for row in rows:
        tag = row["tag"]
        periods = period_returns(navs[tag], dates)
        bench_periods = period_returns(bench, dates).reindex(periods.index)
        row["vs_csi500"] = sign_test(periods - bench_periods)
        row["paired_t_vs_csi500"] = paired_t(periods, bench_periods)
        if baseline_periods is not None and tag != BASELINE_TAG:
            aligned_a = periods.reindex(baseline_periods.index)
            row["vs_baseline"] = sign_test(aligned_a - baseline_periods)
            row["paired_t_vs_baseline"] = paired_t(aligned_a, baseline_periods)

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decision_dates": dates, "n_decision_dates": len(dates),
        "capital": args.capital, "baseline_tag": BASELINE_TAG,
        "assumptions_version": load_assumptions().get("version"),
        "methodology": ("同一套执行假设（run_paper_account：T+1 开盘成交、佣金/印花税/滑点、整手），"
                        "基准为中证500价格指数（000905.SH）同窗归一到 1；"
                        "逐期配对按决策日区间切分，符号检验为双侧。"),
        "variants": rows,
    }
    (RESEARCH / "rot_arm_sweep.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    write_md(payload, RESEARCH / "rot_arm_sweep.md")
    print(f"[sweep] 写出 {RESEARCH / 'rot_arm_sweep.json'} + rot_arm_sweep.md", flush=True)
    return 0


def write_md(payload: dict, path: Path) -> None:
    def pct(v):
        return "—" if v is None else f"{v*100:.2f}%"
    def num(v, nd=2):
        return "—" if v is None else f"{v:.{nd}f}"
    rows = sorted(payload["variants"], key=lambda r: -(r["metrics"].get("total_return") or -9))
    lines = ["# 行业轮动手臂：口径对比（哪种收益曲线更好）", "",
             f"生成 {payload['generated_at']}｜脚本 `scripts/rot_arm_variant_sweep.py`", "",
             f"决策日 {payload['n_decision_dates']} 个（{payload['decision_dates'][0]} ~ "
             f"{payload['decision_dates'][-1]}）｜起始资金 {payload['capital']:,.0f}｜"
             f"执行假设 {payload['assumptions_version']}", "",
             payload["methodology"], "",
             "| 口径 | 决策日 | 累计收益 | **中证500** | **超额** | 最大回撤 | 夏普 | 年化 | vs 中证500 逐期(更好/更差, p) | vs 基线(更好/更差, p) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        m, bm = row["metrics"], row["benchmark_metrics"]
        vc = row.get("vs_csi500") or {}
        vb = row.get("vs_baseline") or {}
        lines.append(
            f"| `{row['tag']}`<br><span>{json.dumps(row['config'], ensure_ascii=False)}</span> "
            f"| {row['decision_dates']} | {pct(m.get('total_return'))} | {pct(bm.get('total_return'))} "
            f"| **{pct(row['excess_return'])}** | {pct(m.get('max_drawdown'))} | {num(m.get('sharpe_rf0'))} "
            f"| {pct(m.get('annual_return'))} "
            f"| {vc.get('better', '—')}/{vc.get('worse', '—')}, p={num(vc.get('p'), 3)} "
            f"| {('—' if not vb else f'{vb.get(chr(98)+chr(101)+chr(116)+chr(116)+chr(101)+chr(114), 0)}/{vb.get(chr(119)+chr(111)+chr(114)+chr(115)+chr(101), 0)}, p={num(vb.get(chr(112)), 3)}')} |")
    best = rows[0]["tag"] if rows else "—"
    lines += ["", f"**按累计收益排序第一**：`{best}`。",
              "", "> ⚠️ 决策日只有几十个：短样本的累计收益排名**不能**当上线依据，",
              "> 要同时看逐期配对检验（vs 中证500 与 vs 基线）与前后半窗一致性（见 `promotion_check.py` 的闸门口径）。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
