#!/usr/bin/env python3
"""sleeve 宽度扫描 + 换手/成本重算（P1.24b §8.4 修订后的统一重判工具）。

对每个 sleeve 的 OOS 预测，计算：
  * 不同持仓宽度（top-k）的**毛超额**（相对当日可交易域等权）与 Newey–West t；
  * **换手**：相邻决策日 top-k 集合的变化比例（对称差 / 2k）；
  * **净超额** = 毛超额 − 换手 × 单边成本 × 2（按双边成本计）。

用途：把"top-5 时代"对每条腿的判决（W2/W5/W6）在宽度 50 下**重算一遍**——
`slots=1~2` 的历史约束等于强制 top-2，在 IC≈0.05 的信号上会被噪声吞没，
必须换宽度后再判死活。

用法：
  uv run python scripts/sleeve_width_sweep.py --predictions 'output/research/sleeve_train_*/oos_predictions.parquet'
  uv run python scripts/sleeve_width_sweep.py --widths 5,20,50,100,200 --one-way-cost 0.0011
"""

from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

# 单边成本口径（与 xiaomkt 引擎一致）：佣金万 2.5 + 滑点万 3 = 0.055%；卖出再加印花税千 1 → 0.155%
# 买入卖出平均单边 ≈ 0.105%；这里默认给"单边 0.11%"，双边 0.22%。
DEFAULT_ONE_WAY = 0.0011


def newey_west_t(x: pd.Series, lag: int) -> float:
    v = np.asarray(x, dtype=float)
    v = v[np.isfinite(v)]
    n = len(v)
    if n < 10:
        return float("nan")
    mean = v.mean()
    dm = v - mean
    var = float((dm ** 2).sum() / n)
    for k in range(1, min(lag, n - 1) + 1):
        w = 1 - k / (lag + 1)
        var += 2 * w * float((dm[k:] * dm[:-k]).sum() / n)
    return float(mean / np.sqrt(max(var, 1e-12) / n))


def sweep(pred: pd.DataFrame, widths: list[int], one_way: float, horizon: int = 20,
          rebalance_every: int = 20) -> pd.DataFrame:
    pred = pred.dropna(subset=["pred", "fwd"]).copy()
    pred["trade_date"] = pd.to_datetime(pred["trade_date"])
    # 只在**调仓日**上评估：标签是 horizon 日前向收益，自然调仓节奏 = horizon 个交易日一次。
    # （此前脚本按逐日算换手，得到 70~80% 的"每日换手"，把成本拖累算大了十几倍。）
    all_dates = sorted(pred["trade_date"].unique())
    rebalance_dates = set(all_dates[:: max(int(rebalance_every), 1)])
    pred = pred[pred["trade_date"].isin(rebalance_dates)]
    pred = pred.set_index("trade_date").sort_index().reset_index()
    rows = []
    prev_sets: dict[int, set] = {}
    for k in widths:
        picks = pred.groupby("trade_date").apply(lambda g: set(g.nlargest(k, "pred")["stock_code"]), include_groups=False)
        excess = pred.groupby("trade_date").apply(
            lambda g: float(g.nlargest(k, "pred")["fwd"].mean() - g["fwd"].mean()), include_groups=False)
        turnovers = []
        dates = list(picks.index)
        for i in range(1, len(dates)):
            a, b = picks.iloc[i - 1], picks.iloc[i]
            union = len(a | b)
            turns = (len(a - b) + len(b - a)) / (2 * k) if k else np.nan
            turnovers.append(turns)
        turnover = float(np.nanmean(turnovers)) if turnovers else float("nan")
        # 一个持有期内可能发生多次调仓；这里按"每决策日调一次"的换手直接记成本
        cost = 2 * one_way * turnover if np.isfinite(turnover) else np.nan
        rows.append({
            "top_k": k,
            "days": int(excess.notna().sum()),
            "gross_excess_mean": float(excess.mean()),
            "gross_t": newey_west_t(excess, lag=horizon),
            "turnover_per_rebalance": turnover,
            "cost_drag": cost,
            "net_excess_mean": float(excess.mean() - cost) if np.isfinite(cost) else np.nan,
            "excess_std": float(excess.std()),
        })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="sleeve 宽度扫描 + 换手成本")
    parser.add_argument("--predictions", default=str(ROOT / "output/research/sleeve_train_*/oos_predictions.parquet"))
    parser.add_argument("--widths", default="5,20,50,100,200")
    parser.add_argument("--one-way-cost", type=float, default=DEFAULT_ONE_WAY)
    parser.add_argument("--rebalance-every", type=int, default=20,
                        help="调仓节奏（交易日）；默认 20 = 与 20 日标签对齐。60 日标签的 sleeve 建议传 60")
    parser.add_argument("--out", type=Path, default=ROOT / "output/research")
    args = parser.parse_args()

    widths = [int(x) for x in args.widths.split(",") if x.strip()]
    files = sorted(glob.glob(args.predictions))
    if not files:
        print(f"没有匹配的预测文件：{args.predictions}")
        return 1

    all_rows = []
    for path in files:
        pred = pd.read_parquet(path, columns=["trade_date", "stock_code", "pred", "fwd"])
        tag = Path(path).parent.name
        table = sweep(pred, widths, args.one_way_cost, rebalance_every=args.rebalance_every)
        table.insert(0, "run", tag)
        all_rows.append(table)
        best = table.loc[table["net_excess_mean"].idxmax()]
        print(f"[{tag}] 样本 {table['days'].iloc[0]} 日 | 最优宽度 top-{int(best['top_k'])}: "
              f"毛 {best['gross_excess_mean']:+.3%} / 净 {best['net_excess_mean']:+.3%} / t {best['gross_t']:+.2f}",
              flush=True)
    merged = pd.concat(all_rows, ignore_index=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"sleeve_width_sweep_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_dir / "width_sweep.csv", index=False)
    lines = ["# sleeve 宽度扫描 + 换手成本重算", "",
             f"- 单边成本 {args.one_way_cost:.4%}（佣金万 2.5 + 滑点万 3；卖出另加印花税千 1）",
             f"- 预测文件 {len(files)} 个", "",
             "| run | 宽度 | 天数 | 毛超额(20d) | NW t | 每次调仓换手 | 成本拖累 | **净超额** |",
             "|---|---|---|---|---|---|---|---|"]
    for r in merged.sort_values(["run", "top_k"]).itertuples():
        lines.append(f"| {r.run} | top-{r.top_k} | {r.days} | {r.gross_excess_mean:+.3%} | {r.gross_t:+.2f} | "
                     f"{r.turnover_per_rebalance:.1%} | {r.cost_drag:.3%} | **{r.net_excess_mean:+.3%}** |")
    lines += ["", "## 判读", "",
              "- **净超额**才是可与门槛对照的量：`净超额 > 0` 且 NW t ≥ 1.5 才算该宽度可用；",
              "- 宽度越大换手越低（top-5 几乎全换、top-100 变化很小），成本拖累随宽度下降；",
              "- 若某腿只在宽度 5 为正、宽度 ≥50 转负，说明是噪声不是 alpha（反之亦然）。"]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "one_way_cost": args.one_way_cost, "widths": widths,
        "runs": merged.to_dict("records"),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[width] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
