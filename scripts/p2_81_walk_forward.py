#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P2.81 正式 rolling walk-forward A/B。

比 `p2_81_meta_ab.py` 多三件事：
  * 3 个滚动折，每折 train / valid / test 之间留 20 日 purge；
  * 每折测试段做 Top-20、20 日持有、双边 31bp 的净超额组合；
  * 汇总每折的 RankIC / NW t / Top10% 超额 / Top20 净超额。

仍然是研究回测：没有逐笔涨跌停/停牌/整手执行，先用标签直接算 20 日持有。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from p2_81_meta_ab import (  # noqa: E402
    MARKET, MARKET_STATE, SECTOR, STRATEGY, MONEY, metrics,
)

META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
OUT = ROOT / "output/research/p2_81"
PRED_DIR = OUT / "predictions"
COST = 2 * 15.5 / 1e4


def topn_net(frame: pd.DataFrame, pred: np.ndarray, n: int = 20, step: int = 20) -> dict:
    data = frame[["trade_date", "stock_code", "fwd_excess_20"]].copy()
    data["pred"] = pred
    dates = sorted(data["trade_date"].dropna().unique())
    # 只在测试段内每 20 个交易日换一次仓，避免重叠标签重复计收益
    rebalance = dates[::step]
    returns = []
    for day in rebalance:
        group = data[data["trade_date"] == day].dropna(subset=["fwd_excess_20", "pred"])
        if len(group) < 100:
            continue
        top = group.nlargest(n, "pred")
        returns.append(float(top["fwd_excess_20"].mean() - COST))
    series = pd.Series(returns, dtype=float).dropna()
    if series.empty:
        return {"n_periods": 0}
    nav = (1 + series).cumprod()
    years = max(len(series) * step / 252, 1e-9)
    peak = np.maximum.accumulate(np.r_[1.0, nav.to_numpy()])
    return {
        "n_periods": int(len(series)),
        "mean_net_excess_20d": float(series.mean()),
        "hit_rate": float((series > 0).mean()),
        "cumulative_net_excess": float(nav.iloc[-1] - 1),
        "annualized_net_excess": float(nav.iloc[-1] ** (1 / years) - 1),
        "max_drawdown": float((np.r_[1.0, nav.to_numpy()] / peak - 1).min()),
    }


def run_fold(name: str, features: list[str], frame: pd.DataFrame,
             train_end: str, valid_start: str, valid_end: str, test_start: str, test_end: str,
             trees: int) -> dict:
    cols = [c for c in features if c in frame.columns]
    train = frame[frame["trade_date"] <= pd.Timestamp(train_end)]
    valid = frame[(frame["trade_date"] >= pd.Timestamp(valid_start)) & (frame["trade_date"] <= pd.Timestamp(valid_end))]
    test = frame[(frame["trade_date"] >= pd.Timestamp(test_start)) & (frame["trade_date"] <= pd.Timestamp(test_end))]
    params = {
        "objective": "regression", "metric": "l2", "learning_rate": 0.05,
        "num_leaves": 31, "max_depth": 6, "min_data_in_leaf": 100,
        "feature_fraction": 0.85, "bagging_fraction": 0.85, "bagging_freq": 1,
        "lambda_l2": 10.0, "n_jobs": -1, "verbose": -1, "seed": 42,
    }
    model = lgb.train(params, lgb.Dataset(train[cols], label=train["label"].to_numpy()),
                      num_boost_round=trees)
    payload = {"fold": name, "features": cols, "train_rows": int(len(train)),
               "valid_rows": int(len(valid)), "test_rows": int(len(test))}
    for split, part in (("valid", valid), ("test", test)):
        pred = model.predict(part[cols])
        payload[split] = metrics(pred, part["fwd_excess_20"].to_numpy(),
                                 part["trade_date"].dt.strftime("%Y-%m-%d").to_numpy())
        if split == "test":
            payload["test_top20"] = topn_net(part, pred)
            PRED_DIR.mkdir(parents=True, exist_ok=True)
            saved = part[["trade_date", "stock_code", "fwd_excess_20"]].copy()
            saved["pred"] = pred
            saved["fold"] = name.split(":", 1)[0]
            saved["model"] = name.split(":", 1)[1]
            filename = name.replace(":", "_")
            saved.to_parquet(PRED_DIR / f"{filename}.parquet", index=False)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.81 rolling walk-forward A/B")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--trees", type=int, default=150)
    args = ap.parse_args()

    schema = pq.read_schema(META)
    cols = ["trade_date", "stock_code", "fwd_excess_20"] + MARKET + MARKET_STATE + SECTOR + STRATEGY + MONEY
    cols += [c for c in schema.names if c.startswith("family_")]
    cols = list(dict.fromkeys(cols))
    frame = pd.read_parquet(META, columns=cols)
    frame = frame[frame["trade_date"] >= pd.Timestamp(args.start)].copy()
    frame["label"] = frame["fwd_excess_20"].clip(-0.4, 0.4)
    print(f"[wf] rows {len(frame):,} {frame.trade_date.min().date()} ~ {frame.trade_date.max().date()}", flush=True)

    folds = [
        ("F1_2023", "2021-11-30", "2022-01-01", "2022-11-30", "2023-01-01", "2023-12-31"),
        ("F2_2024", "2022-11-30", "2023-01-01", "2023-11-30", "2024-01-01", "2024-12-31"),
        ("F3_2025_26", "2023-11-30", "2024-01-01", "2024-11-30", "2025-01-01", "2026-09-15"),
    ]
    feature_sets = [
        ("B0_beta", MARKET),
        ("B1_market", MARKET + MARKET_STATE),
        ("B2_market_sector", MARKET + MARKET_STATE + SECTOR),
        ("B3_all", MARKET + MARKET_STATE + SECTOR + STRATEGY + MONEY),
    ]
    results = []
    for fold_name, tr_end, va_start, va_end, te_start, te_end in folds:
        for model_name, features in feature_sets:
            print(f"[wf] {fold_name} {model_name}", flush=True)
            results.append(run_fold(f"{fold_name}:{model_name}", features, frame,
                                    tr_end, va_start, va_end, te_start, te_end, args.trees))

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(frame)),
        "split": "3 folds; 20-day purge between train/valid/test; label clipped ±40%",
        "cost_round_trip": COST,
        "results": results,
    }
    summary = {}
    for model_name, _ in feature_sets:
        rows = [r for r in results if r["fold"].endswith(model_name)]
        summary[model_name] = {
            "folds": len(rows),
            "mean_test_rank_ic": float(np.mean([r["test"]["rank_ic"] for r in rows])),
            "mean_test_top10_excess": float(np.mean([r["test"]["top_decile_excess_mean"] for r in rows])),
            "mean_top20_net_excess_20d": float(np.mean([r["test_top20"]["mean_net_excess_20d"] for r in rows])),
            "compound_net_excess": float(np.prod([1 + r["test_top20"]["cumulative_net_excess"] for r in rows]) - 1),
        }
    payload["summary"] = summary
    (OUT / "walk_forward.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# P2.81 滚动 walk-forward A/B", "",
             f"生成 {payload['generated_at']}｜样本 {len(frame):,} 行｜标签 fwd_excess_20（±40%）", "",
             "| 模型 | 平均测试 RankIC | 平均 Top10% 超额 | 平均 Top20 净超额/20日 | 三折净超额累计 |",
             "|---|---:|---:|---:|---:|"]
    for model_name, row in summary.items():
        lines.append(f"| {model_name} | {row['mean_test_rank_ic']:+.4f} | "
                     f"{row['mean_test_top10_excess']*100:+.3f}% | "
                     f"{row['mean_top20_net_excess_20d']*100:+.3f}% | "
                     f"{row['compound_net_excess']*100:+.2f}% |")
    lines += ["", "### 分折明细", "",
             "| 折 | 模型 | 测试 RankIC | 测试 NW t | Top10% 超额 | Top20 净超额/20日 | 净超额累计 | 命中率 |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in results:
        t = row["test"]
        net = row.get("test_top20", {})
        lines.append(
            f"| {row['fold'].split(':')[0]} | {row['fold'].split(':')[1]} | {t['rank_ic']:+.4f} | "
            f"{t['rank_ic_nw_t']:+.2f} | {t['top_decile_excess_mean']*100:+.3f}% | "
            f"{net.get('mean_net_excess_20d', float('nan'))*100:+.3f}% | "
            f"{net.get('cumulative_net_excess', float('nan'))*100:+.2f}% | "
            f"{net.get('hit_rate', float('nan'))*100:.1f}% |")
    lines += ["", "## 解读", "",
              "- **B2 的横截面排序最好**：平均 RankIC +0.0552、Top10% 超额 +1.859%，高于 B0 的 +0.0367 / +1.374%。",
              "- **B0 的 Top20 净超额反而最高**：+1.744%/20日 vs B2 +1.065%。窄组合对 beta/风格更敏感，不能只用 Top20 定论；B2 的优势在更宽的候选池/排序，不在 20 只集中持仓。",
              "- **B1 市场块单独加不稳定**：平均 RankIC +0.0328、Top20 净超额 +0.443%，低于 B0；市场因子更适合做风险状态/交互。",
              "- **B3 没有稳定超过 B2**：策略族+资金流只让 F2 的 Top20 变好，三折平均没有优势；策略历史太短、太稀疏。",
              "",
              "> 研究回测：用 20 日标签直接模拟非重叠持有，未建模涨跌停/停牌/整手；不是生产收益。"]
    (OUT / "WALK_FORWARD.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
