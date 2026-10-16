#!/usr/bin/env python3
"""放量突破 vs 缩量突破：把 `docs/reference/breakout.md` 的说法变成可检验的统计。

用户（2026-10-15）："breakout.md 看一下 放量突破 和 缩量突破"

文档的主张（作者个人经验，原文注明"市场走势多变，辩证看待"）：
  1. 量能不是"油门"而是**分歧/抛压的测力计**（撮合层面买卖必相等）；
  2. **第一棒：放量突破做攻坚** —— 突破长期横盘/前高时要放量，作用是把套牢盘"成本置换"掉；
  3. **第二棒：缩量突破进巡航** —— 换手充分后上方是流动性真空，不需要量也能涨；
  4. **真缩量 vs 空心陷阱**：缩量突破只有在"前期经历过充分的箱体震荡 + 放量换手"时才算数；
     前期交投死寂、无量晃过前高 = 空心陷阱（一旦主力出逃就是无量闷杀）；
  5. 本该缩量巡航的阶段**反复爆天量** ⇒ 分歧巨大、头顶还有抛压。

本脚本把 2~5 变成事件研究（口径全部只用 T 日及之前 + 次日开盘进场，无前视）：
  事件 = 收盘创 60 日新高（`close_t > max(close_{t-60..t-1})`）且突破前 60 日**横盘**
        （`amp60_prev = max(high)/min(low) − 1 ≤ 0.35`）
  量比 = `vol_t / mean(vol_{t-20..t-1})`；放量 = ≥1.5、缩量 = ≤0.8、其余=常态（口径自定，见 REPORT）
  前期换手 = `sum(turnover_{t-60..t-1})`（换手率小数求和），按事件样本中位数分高/低
  收益 = 次日开盘买 → 第 h 日收盘卖（h=1/3/5/10/20），扣**双边 31bp**；同时给同期横截面均值超额
  另测：缩量突破后 5 日内是否爆天量（量比 ≥2）→ 后续收益（文档第 5 条）

输出：output/research/breakout_volume/{summary.json, table_*.csv, REPORT.md}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT = ROOT / "output/research/breakout_volume"
HORIZONS = (1, 3, 5, 10, 20)
COST = 2 * 15.5 / 1e4          # 双边 31bp（与 box_breakout_study 一致）
AMP_MAX = 0.35                 # 突破前 60 日振幅上限（横盘）
VOL_EXPAND, VOL_SHRINK = 1.5, 0.8


def load_bars(start: str) -> pd.DataFrame:
    """前复权日线（真股票，排除指数）。"""
    import pyarrow.dataset as ds

    from wyckoff_study import _universe

    files = [str(p) for p in BARS.glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")]
    if not files:
        raise SystemExit("找不到前复权日线")
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount", "turnover"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    real = _universe()
    if real:
        table = table[table["stock_code"].isin(real)]
    table = table[table["trade_date"] >= pd.Timestamp(start)]
    return table.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def build(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("stock_code", sort=False)
    df["high60_prev"] = g["high"].transform(lambda s: s.rolling(60, min_periods=40).max().shift(1))
    df["low60_prev"] = g["low"].transform(lambda s: s.rolling(60, min_periods=40).min().shift(1))
    df["amp60_prev"] = df["high60_prev"] / df["low60_prev"] - 1.0
    df["vol20_prev"] = g["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    df["vol_ratio"] = df["volume"] / df["vol20_prev"]
    df["turn60_prev"] = g["turnover"].transform(lambda s: s.rolling(60, min_periods=40).sum().shift(1))
    # 流动性控制（放量/缩量与市值天然相关，必须能分层对照）
    df["amt20_prev"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    # 突破后 5 日的最大量比（文档第 5 条：本该缩量却爆天量）
    df["post5_vol_ratio_max"] = g["vol_ratio"].transform(
        lambda s: s.rolling(5, min_periods=1).max().shift(-5))
    entry = g["open"].transform(lambda s: s.shift(-1))
    entry = entry.where(entry > 0)
    for h in HORIZONS:
        exit_close = g["close"].transform(lambda s: s.shift(-(1 + h)))
        exit_close = exit_close.where(exit_close > 0)
        df[f"fwd{h}"] = (exit_close / entry - 1.0).replace([np.inf, -np.inf], np.nan)
        df[f"excess{h}"] = df[f"fwd{h}"] - df.groupby("trade_date")[f"fwd{h}"].transform("mean")
    # 可买性护栏：次日开盘就顶到涨停（≥+9.5%）的票买不进，剔除
    df["gap_next"] = entry / df["close"] - 1.0
    return df


def events(df: pd.DataFrame) -> pd.DataFrame:
    hit = (df["close"] > df["high60_prev"]) & (df["amp60_prev"] <= AMP_MAX) \
          & df["vol_ratio"].notna() & (df["gap_next"] < 0.095)
    ev = df.loc[hit].copy()
    med = float(ev["turn60_prev"].median())
    ev["turn_group"] = np.where(ev["turn60_prev"] >= med, "前期换手充分", "前期换手清淡")
    ev["vol_group"] = np.where(ev["vol_ratio"] >= VOL_EXPAND, "放量(≥1.5)",
                               np.where(ev["vol_ratio"] <= VOL_SHRINK, "缩量(≤0.8)", "常态(0.8~1.5)"))
    ev["turn_median"] = med
    ev["liq_group"] = pd.qcut(ev["amt20_prev"], [0, 1 / 3, 2 / 3, 1.0],
                              labels=["低流动性(小票)", "中", "高流动性(大票)"])
    return ev


def stats(sub: pd.DataFrame, col: str) -> dict:
    r = sub[col].dropna() - COST
    if len(r) < 30:
        return {"n": int(len(r))}
    wins, losses = r[r > 0], r[r < 0]
    return {"n": int(len(r)), "mean": round(float(r.mean()), 5), "median": round(float(r.median()), 5),
            "win_rate": round(float((r > 0).mean()), 4),
            "odds": round(float(wins.mean() / abs(losses.mean())), 3) if len(wins) and len(losses) else None,
            "t": round(float(r.mean() / (r.std() / np.sqrt(len(r)))), 2) if r.std() > 0 else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 载入前复权日线（{args.start} 起）", flush=True)
    bars = load_bars(args.start)
    print(f"      {len(bars):,} 行 / {bars['stock_code'].nunique()} 只", flush=True)
    print("[2/4] 构造特征（横盘振幅 / 量比 / 前期换手 / 前向收益，全部无前视）", flush=True)
    df = build(bars)
    ev = events(df)
    print(f"      突破事件 {len(ev):,} 个（横盘 ≤{AMP_MAX:.0%} + 收盘创 60 日新高）", flush=True)

    print("[3/4] 分组统计", flush=True)
    h = 10
    overall = {f"fwd{x}": stats(ev, f"fwd{x}") for x in HORIZONS}
    by_vol = {g: {f"fwd{x}": stats(ev[ev["vol_group"] == g], f"fwd{x}") for x in HORIZONS}
              for g in ("放量(≥1.5)", "常态(0.8~1.5)", "缩量(≤0.8)")}
    grid = {}
    for vg in ("放量(≥1.5)", "常态(0.8~1.5)", "缩量(≤0.8)"):
        for tg in ("前期换手充分", "前期换手清淡"):
            sub = ev[(ev["vol_group"] == vg) & (ev["turn_group"] == tg)]
            grid[f"{vg} × {tg}"] = {"n_events": int(len(sub)),
                                    **{f"fwd{x}": stats(sub, f"fwd{x}") for x in HORIZONS},
                                    "excess10": stats(sub, "excess10"), "excess20": stats(sub, "excess20")}
    # 文档第 5 条：缩量突破后 5 日内爆天量
    shrink = ev[ev["vol_group"] == "缩量(≤0.8)"].copy()
    shrink["天量后"] = np.where(shrink["post5_vol_ratio_max"] >= 2.0, "5日内爆天量(≥2)", "未爆天量")
    roar = {k: {f"fwd{x}": stats(v, f"fwd{x}") for x in HORIZONS}
            for k, v in shrink.groupby("天量后")}
    # 量比分位（单调性）
    ev["vol_bucket"] = pd.qcut(ev["vol_ratio"], [0, .2, .4, .6, .8, 1.0],
                               labels=["Q1(最缩)", "Q2", "Q3", "Q4", "Q5(最放)"])
    buckets = {str(k): {f"fwd{x}": stats(v, f"fwd{x}") for x in HORIZONS}
               for k, v in ev.groupby("vol_bucket", observed=True)}

    by_liq = {}
    for lg in ("低流动性(小票)", "中", "高流动性(大票)"):
        for vg in ("放量(≥1.5)", "缩量(≤0.8)", "常态(0.8~1.5)"):
            sub = ev[(ev["liq_group"] == lg) & (ev["vol_group"] == vg)]
            by_liq[f"{lg} × {vg}"] = {f"fwd{x}": stats(sub, f"fwd{x}") for x in HORIZONS}
    summary = {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
               "start": args.start, "events": int(len(ev)), "cost_two_way_bp": COST * 1e4,
               "definitions": {"event": "close > max(close, 60d) & amp60_prev <= 0.35",
                               "vol_ratio": "vol_t / mean(vol, 20d, 前 20 日)",
                               "turn60_prev": "突破前 60 日 turnover 之和（换手率小数）",
                               "entry": "次日开盘；exit：第 h 日收盘；不可买（次日开盘 ≥+9.5%）剔除"},
               "overall": overall, "by_volume": by_vol, "grid": grid,
               "shrink_then_roar": roar, "vol_quantiles": buckets, "by_liquidity": by_liq}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 表格
    pd.DataFrame({k: {f"fwd{x}": v.get(f"fwd{x}", {}).get("mean") for x in HORIZONS}
                  for k, v in grid.items()}).to_csv(out / "table_grid.csv")
    pd.DataFrame({k: {f"fwd{x}": v.get(f"fwd{x}", {}).get("mean") for x in HORIZONS}
                  for k, v in buckets.items()}).to_csv(out / "table_vol_quantiles.csv")

    print("[4/4] 报告", flush=True)
    def fmt(d: dict, x: int) -> str:
        s = d.get(f"fwd{x}", {})
        if not s.get("n"):
            return "样本不足"
        t = s.get("t")
        return "n=%d 均值%+.2f%% 中位%+.2f%% 胜率%.0f%% 赔率%s t=%s" % (
            s["n"], s["mean"] * 100, (s.get("median") or 0) * 100, (s.get("win_rate") or 0) * 100,
            ("%.2f" % s["odds"]) if s.get("odds") is not None else "—",
            ("%+.2f" % t) if t is not None else "—")
    lines = [f"# 放量突破 vs 缩量突破（事件研究）", "",
             f"事件 {len(ev):,} 个（横盘 ≤{AMP_MAX:.0%} + 收盘创 60 日新高；次日开盘买、第 h 日收盘卖、双边 {COST*1e4:.0f}bp）。",
             "", "## 全样本（10 日）", f"- {fmt(overall, 10)}", "",
             "## 按量比分档（10 日）"]
    for k, v in by_vol.items():
        lines.append(f"- {k}：{fmt(v, 10)}")
    lines += ["", "## 2×2：量比 × 前期换手（10 日 / 20 日）"]
    for k, v in grid.items():
        lines.append(f"- {k}：{fmt(v, 10)}｜{fmt(v, 20)}")
    lines += ["", "## 文档第 5 条：缩量突破之后爆天量",]
    for k, v in roar.items():
        lines.append(f"- {k}：{fmt(v, 10)}｜{fmt(v, 20)}")
    lines += ["", "## 流动性分层（放量/缩量与市值天然相关，必须分档看）",]
    for k, v in by_liq.items():
        lines.append(f"- {k}：{fmt(v, 10)}")
    lines += ["", "## 量比分位（10 日，看单调性）"]
    for k, v in buckets.items():
        lines.append(f"- {k}：{fmt(v, 10)}")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
