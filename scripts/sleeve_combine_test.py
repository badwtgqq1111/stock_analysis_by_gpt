#!/usr/bin/env python3
"""组合口径验收：把多条 sleeve 的预测**组合**起来，看组合 IC/超额是否高于单腿。

P1.24b §8.5 的结论要求：单一弱信号撑不起独立 sleeve，正确路径是**多个低相关弱信号组合**。
本脚本直接检验这个命题：

  1. 读入多个 sleeve 的 OOS 预测（`sleeve_train_*/oos_predictions.parquet`）；
  2. 每个 sleeve 的预测先转成**当日横截面分位**（消除不同 horizon 的量纲差异）；
  3. 组合分 = 等权平均（也可按 ICIR 加权）；
  4. 在统一调仓节奏下比较：单腿 vs 组合的 IC、top-k 毛/净超额、NW t；
  5. 同时输出**两两相关矩阵**（预测分位相关），验证"低相关"是否成立。

用法：
  uv run python scripts/sleeve_combine_test.py \
    --runs 'output/research/sleeve_train_model_valuation_20261006_*/oos_predictions.parquet' ...
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


def load_run(path: str, label: str) -> pd.DataFrame:
    frame = pd.read_parquet(path, columns=["trade_date", "stock_code", "label", "pred", "fwd"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.dropna(subset=["pred", "fwd"])
    frame["rank_pct"] = frame.groupby("trade_date")["pred"].rank(pct=True)
    return frame[["trade_date", "stock_code", "rank_pct", "fwd"]].rename(columns={"rank_pct": label})


def evaluate(merged: pd.DataFrame, score: str, top_k: int, rebalance_every: int, horizon: int, one_way: float) -> dict:
    dates = sorted(merged["trade_date"].unique())
    keep = set(dates[:: max(rebalance_every, 1)])
    sub = merged[merged["trade_date"].isin(keep)]
    ic = sub.groupby("trade_date").apply(
        lambda g: g[score].corr(g["fwd"], method="spearman") if len(g) > 20 else np.nan, include_groups=False).dropna()
    pick_sets = {d: set(g.nlargest(top_k, score)["stock_code"]) for d, g in sub.groupby("trade_date")}
    excess = sub.groupby("trade_date").apply(
        lambda g: float(g.nlargest(top_k, score)["fwd"].mean() - g["fwd"].mean()), include_groups=False)
    turns = []
    plist = [pick_sets[d] for d in sorted(pick_sets)]
    for i in range(1, len(plist)):
        a, b = plist[i - 1], plist[i]
        turns.append((len(a - b) + len(b - a)) / (2 * top_k))
    turnover = float(np.nanmean(turns)) if turns else np.nan
    cost = 2 * one_way * turnover if np.isfinite(turnover) else np.nan
    return {
        "score": score, "top_k": top_k, "days": int(len(excess)), "ic": float(ic.mean()),
        "ic_t": newey_west_t(ic, lag=horizon),
        "gross_excess": float(excess.mean()), "gross_t": newey_west_t(excess, lag=horizon),
        "turnover": turnover, "net_excess": float(excess.mean() - cost) if np.isfinite(cost) else np.nan,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="sleeve 组合口径验收")
    parser.add_argument("--runs", nargs="+", required=True, help="预测文件路径（支持通配）")
    parser.add_argument("--labels", default="", help="逗号分隔的 sleeve 标签（默认取目录名）")
    parser.add_argument("--rebalance-every", type=int, default=20)
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--top-k", default="20,50,100,200")
    parser.add_argument("--one-way-cost", type=float, default=DEFAULT_ONE_WAY)
    parser.add_argument("--out", type=Path, default=ROOT / "output/research")
    args = parser.parse_args()

    paths = []
    for pattern in args.runs:
        paths.extend(sorted(glob.glob(pattern)))
    paths = sorted(set(paths))
    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    if len(labels) != len(paths):
        labels = []
        for p in paths:
            name = Path(p).parent.name
            for key in ("valuation", "quality", "momentum", "reversal"):
                if key in name:
                    labels.append(key)
                    break
            else:
                labels.append(name[-12:])
    print(f"[combine] 载入 {len(paths)} 个 sleeve：{labels}", flush=True)

    merged = None
    for path, label in zip(paths, labels):
        frame = load_run(path, label)
        if merged is None:
            # 统一用第一条腿的 fwd 作为组合评估目标（不同 sleeve 的 horizon 不同，fwd 不能作合并键）
            merged = frame.rename(columns={"fwd": "fwd_eval"})
        else:
            merged = merged.merge(frame.drop(columns=["fwd"]), on=["trade_date", "stock_code"], how="inner")
        print(f"[combine] {label}: {len(frame):,} 行 → 交集 {len(merged):,} 行", flush=True)
    merged = merged.rename(columns={"fwd_eval": "fwd"})
    merged["combo_equal"] = merged[labels].mean(axis=1)
    merged["combo_rankmean"] = merged[labels].rank(pct=True).mean(axis=1)

    corr = merged[labels].corr(method="spearman")
    rows = []
    for score in list(labels) + ["combo_equal", "combo_rankmean"]:
        for k in [int(x) for x in args.top_k.split(",") if x.strip()]:
            rows.append(evaluate(merged, score, k, args.rebalance_every, args.horizon, args.one_way_cost))
    table = pd.DataFrame(rows)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"sleeve_combine_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    table.to_csv(out_dir / "combine_metrics.csv", index=False)
    corr.to_csv(out_dir / "prediction_correlation.csv")
    lines = ["# sleeve 组合口径验收", "",
             f"- 载入：{', '.join(labels)}；交集样本 {len(merged):,} 行 / {merged['trade_date'].nunique()} 个交易日",
             f"- 调仓节奏 {args.rebalance_every} 日；单边成本 {args.one_way_cost:.4%}", "",
             "## 预测分位相关矩阵（Spearman）", "", corr.to_markdown(floatfmt=".3f"), "",
             "## 单腿 vs 组合", "",
             "| 打分 | 宽度 | 天数 | IC | IC t | 毛超额 | NW t | 换手 | **净超额** |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in table.sort_values(["score", "top_k"]).itertuples():
        lines.append(f"| {r.score} | top-{r.top_k} | {r.days} | {r.ic:+.4f} | {r.ic_t:+.2f} | {r.gross_excess:+.3%} | "
                     f"{r.gross_t:+.2f} | {r.turnover:.1%} | **{r.net_excess:+.3%}** |")
    lines += ["", "## 判读", "",
              "- 组合是否优于**最好的单腿**：看 IC t 与净超额；",
              "- 两两相关越低，组合提升越明显（`combo_rankmean` 对相关结构不敏感，作为稳健口径）。"]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "labels": labels, "rows": int(len(merged)), "correlation": corr.to_dict(),
        "metrics": table.to_dict("records"),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[combine] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
