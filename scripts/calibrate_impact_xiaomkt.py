#!/usr/bin/env python3
"""用 5min 分时标定小市值轮动的执行成本与冲击系数 `impact_coef`。

思路（不依赖自有成交回报，全部可从公开数据测量）：
  1. 对每笔 BUY，取当日第一根 5min（09:30-09:35）作为"决策时点"，计算
       participation_N = 委托额 / 接下来 N 根 5min 的成交额      （N=1,3,6,12）
  2. 用"接着 N 根 VWAP 相对 09:35 收盘价的变化"作为**执行成本代理**
       cost_N = sign(side) × (VWAP_next_N / P_0935 - 1)
     —— 这是"分 N 根建仓实际拿到的价格比首根收盘差多少"，不是冲击本身，但同量级可用。
  3. 按资金规模（10 万 / 100 万 / 500 万 / 1000 万）缩放委托额，看参与率与成本如何变化；
  4. 用 cost_N = impact_coef × vol20 × sqrt(participation_N) 过原点回归，估计 impact_coef，
     与引擎里用的中性假设 1.0 对比。

用法：
  uv run python scripts/calibrate_impact_xiaomkt.py --freq 5 --windows 1,3,6,12
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCALES = (1, 5, 20, 100)      # 相对"回测实际订单"的倍数（回测初始 10 万，2025-10 时账户已约 125 万）


def load_intraday(root: Path, freq: str) -> pd.DataFrame:
    files = sorted(str(p) for p in (root / f"frequency={freq}min").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"没有分时数据：{root}/frequency={freq}min")
    frames = [pd.read_parquet(p) for p in files]
    frame = pd.concat(frames, ignore_index=True)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values(["stock_code", "date", "time"])
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="执行成本与 impact_coef 标定")
    parser.add_argument("--trades", type=Path,
                        default=ROOT / "output/results_cn/xiaomkt_rotation_optimized/trades.csv")
    parser.add_argument("--intraday-root", type=Path, default=ROOT / "assets/data/raw/intraday_baostock")
    parser.add_argument("--freq", default="5")
    parser.add_argument("--windows", default="1,3,6,12", help="建仓窗口（5min 根数）")
    parser.add_argument("--vol-window", type=int, default=20, help="日波动估计窗口（交易日）")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "output" / "research")
    args = parser.parse_args()

    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    bars = load_intraday(args.intraday_root, args.freq)
    print(f"[calib] 分时 {len(bars):,} 行 / {bars['stock_code'].nunique()} 只 | {bars['date'].min().date()} ~ {bars['date'].max().date()}", flush=True)

    trades = pd.read_csv(args.trades)
    trades["date"] = pd.to_datetime(trades["date"])
    trades = trades[trades["side"] == "BUY"].copy()
    bars_by_date = {d: g for d, g in bars.groupby("date")}
    daily_close = bars.groupby(["stock_code", "date"])["close"].last().unstack(0)
    daily_ret = daily_close.pct_change()
    vol = daily_ret.rolling(args.vol_window, min_periods=5).std()

    rows = []
    for tr in trades.itertuples():
        day = bars_by_date.get(tr.date)
        if day is None:
            continue
        seq = day[day["stock_code"] == tr.code]
        if len(seq) < max(windows) + 1:
            continue
        first = seq.iloc[0]
        if not np.isfinite(first["close"]) or first["close"] <= 0:
            continue
        rec = {
            "date": tr.date, "code": tr.code, "reason": getattr(tr, "reason", ""),
            "price_daily_open": float(tr.price), "price_0935": float(first["close"]),
        }
        rec["open_vs_0935"] = rec["price_0935"] / rec["price_daily_open"] - 1
        bar_amounts = seq["amount"].to_numpy(dtype=float)
        for n in windows:
            window = seq.iloc[0: n]                      # 从开盘第一根开始铺单
            if len(window) < n:
                continue
            vwap = window["amount"].sum() / max(window["volume"].sum(), 1e-9)
            rec[f"cost_{n}"] = float(vwap / first["open"] - 1)     # 相对开盘价：铺 N 根 vs 直接吃开盘
            rec[f"window_amount_{n}"] = float(window["amount"].sum())
        rec["day_amount"] = float(seq["amount"].sum())
        rows.append(rec)

    if not rows:
        print("[calib] 没有可匹配的交易（分时数据未覆盖交易日？）", flush=True)
        return 1
    frame = pd.DataFrame(rows)
    print(f"[calib] 匹配到 {len(frame)} 笔买入", flush=True)

    # 真实委托额：直接来自回测成交（lots × 100 × 成交价），账户规模从净值曲线取
    scale_lookup = trades.drop_duplicates(subset=["date", "code"]).set_index(["date", "code"])["lots"]
    lots = np.array([scale_lookup.get(k, np.nan) for k in zip(frame["date"], frame["code"])], dtype=float)
    base_amount = frame["price_daily_open"].to_numpy() * 100 * lots
    equity_path = None
    curve_path = ROOT / "output/results_cn/xiaomkt_rotation_optimized/equity.parquet"
    if curve_path.exists():
        equity_path = pd.read_parquet(curve_path)["equity"]

    summary = []
    for scale in SCALES:
        amount = base_amount * scale
        rec = {"scale": scale, "median_order_amount": float(np.nanmedian(amount))}
        for n in windows:
            wcol, ccol = f"window_amount_{n}", f"cost_{n}"
            if wcol not in frame:
                continue
            participation = np.minimum(amount / frame[wcol].to_numpy(), 1.0)
            rec[f"participation_{n}_median"] = float(np.nanmedian(participation))
            rec[f"participation_{n}_p90"] = float(np.nanpercentile(participation, 90))
            rec[f"cost_{n}_median"] = float(np.nanmedian(frame[ccol]))
            rec[f"cost_{n}_mean"] = float(np.nanmean(frame[ccol]))
        rec["participation_day_median"] = float(np.nanmedian(np.minimum(amount / frame["day_amount"].to_numpy(), 1.0)))
        rec["share_over_10pct_bar"] = float(np.mean(np.minimum(amount / frame["window_amount_1"].to_numpy(), 1.0) > 0.10)) if "window_amount_1" in frame else float("nan")
        summary.append(rec)
    summary_frame = pd.DataFrame(summary)

    # ---- 建仓可行性：按每根 bar 参与率上限，模拟"能不能在 N 根内把目标金额吃完" ----
    fill_rows = []
    for scale in SCALES:
        amount = base_amount * scale
        for cap in (0.10, 0.05):
            ratios, used_bars = [], []
            for idx, row in enumerate(frame.itertuples()):     # 与 base_amount 同序（frame = 匹配到的买入）
                day = bars_by_date.get(row.date)
                if day is None:
                    continue
                seq = day[day["stock_code"] == row.code]
                if len(seq) < 2:
                    continue
                seq = seq.iloc[:48]
                target = amount[idx]
                if not np.isfinite(target) or target <= 0:
                    continue
                remaining, filled, used = target, 0.0, 0
                for k, bar_amount in enumerate(seq["amount"].to_numpy(dtype=float)):
                    take = min(remaining, cap * bar_amount)
                    if take <= 0:
                        continue
                    remaining -= take
                    filled += take
                    used = k + 1
                    if remaining <= 0:
                        break
                ratios.append(filled / target)
                used_bars.append(used)
            if ratios:
                fill_rows.append({
                    "scale": scale, "bar_cap": cap,
                    "median_fill_ratio": float(np.median(ratios)),
                    "share_fully_filled": float(np.mean(np.array(ratios) > 0.999)),
                    "median_bars_used": float(np.median([b for b in used_bars if b > 0])) if any(used_bars) else 0.0,
                })
    fill_frame = pd.DataFrame(fill_rows)

    # ---- 过原点回归估计 impact_coef：cost = coef × vol20 × sqrt(participation) ----
    fit_rows = []
    vol_lookup = vol
    for scale in SCALES:
        amount = base_amount * scale
        for n in windows:
            wcol, ccol = f"window_amount_{n}", f"cost_{n}"
            if wcol not in frame:
                continue
            participation = np.minimum(amount / frame[wcol].to_numpy(), 1.0)
            vols = []
            for d, c in zip(frame["date"], frame["code"]):
                try:
                    vols.append(vol_lookup.at[d, c])
                except KeyError:
                    vols.append(np.nan)
            vols = np.array(vols, dtype=float)
            x = vols * np.sqrt(participation)
            y = frame[ccol].to_numpy(dtype=float)
            mask = np.isfinite(x) & np.isfinite(y) & (x > 0)
            if mask.sum() < 20:
                continue
            coef = float((x[mask] * y[mask]).sum() / (x[mask] ** 2).sum())   # 过原点最小二乘
            r2 = float(1 - ((y[mask] - coef * x[mask]) ** 2).sum() / ((y[mask] - y[mask].mean()) ** 2).sum())
            fit_rows.append({"scale": scale, "window": n, "impact_coef": coef, "r2": r2,
                             "median_participation": float(np.median(participation[mask])),
                             "median_cost": float(np.median(y[mask]))})
    fit_frame = pd.DataFrame(fit_rows)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_dir / f"xiaomkt_impact_calib_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_dir / "trades_with_costs.csv", index=False)
    summary_frame.to_csv(out_dir / "by_capital.csv", index=False)
    fit_frame.to_csv(out_dir / "impact_fit.csv", index=False)
    fill_frame.to_csv(out_dir / "fill_feasibility.csv", index=False)

    lines = ["# 小市值轮动：执行成本与 impact_coef 标定", "",
             f"- 分时：{args.freq}min，{bars['stock_code'].nunique()} 只，{bars['date'].min().date()} ~ {bars['date'].max().date()}",
             f"- 匹配买入：{len(frame)} 笔；窗口 = {windows} 根 5min（从开盘第一根开始铺单）",
             f"- 回测实际单笔委托额中位：{np.nanmedian(base_amount):,.0f} 元（初始 10 万，2025-10 时账户已复利到百万级）"
             + (f"；成交日账户净值中位 {float(equity_path.reindex(frame['date']).median()):,.0f} 元" if equity_path is not None else ""),
             "",
             "## 参与率与执行成本（按订单规模倍数）", "",
             "| 订单倍数 | 单笔委托额中位 | 参与率中位(首根) | 参与率 p90(首根) | 参与率中位(6根) | 日参与率中位 | 首根占比>10%的笔数 |",
             "|---|---|---|---|---|---|---|"]
    for r in summary_frame.to_dict("records"):
        lines.append(f"| ×{r['scale']} | {r['median_order_amount']:,.0f} | "
                     f"{r.get('participation_1_median', float('nan')):.2%} | {r.get('participation_1_p90', float('nan')):.2%} | "
                     f"{r.get('participation_6_median', float('nan')):.2%} | {r['participation_day_median']:.2%} | "
                     f"{r['share_over_10pct_bar']:.0%} |")
    lines += ["", "| 订单倍数 | 窗口 | 铺单成本中位（相对开盘价） | 拟合 impact_coef | R² |", "|---|---|---|---|---|"]
    for r in fit_frame.to_dict("records"):
        lines.append(f"| ×{r['scale']} | {r['window']} 根 | {r['median_cost']:.4%} | "
                     f"{r['impact_coef']:.3f} | {r['r2']:.3f} |")
    lines += ["", "## 建仓可行性（当天 48 根 5min 内能否吃完目标金额）", "",
              "| 订单倍数 | 每根参与率上限 | 成交比例中位 | 完全成交笔数占比 | 用掉的 bar 中位 |",
              "|---|---|---|---|---|"]
    for r in fill_frame.to_dict("records"):
        lines.append(f"| ×{r['scale']:.0f} | {r['bar_cap']:.0%} | {r['median_fill_ratio']:.1%} | "
                     f"{r['share_fully_filled']:.0%} | {r['median_bars_used']:.0f} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "by_capital": summary, "impact_fit": fit_rows, "trades_matched": len(frame),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[calib] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
