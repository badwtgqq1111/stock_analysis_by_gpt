#!/usr/bin/env python
"""在"低位反弹状态"子集内取模型 Top-k：超额为正则说明"该状态 + 排序"可做，否则状态是死路。

对照：A 全截面 Top-k（现状） / B 低位带内 Top-k / C 反弹确认内 Top-k。
口径：walk-forward 样本外模型分；13 个非重叠决策日；超额 = Top-k 20 日收益 − 当日全池均值；
      vs 随机（同日同规模 ×200）；换手与扣费 25bps。
输出：output/research/rebound_state_topk/{metrics.csv, detail.csv, REPORT.md}
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402
from factor_engine.ml.strategy_labels import build_cn_strategy_labels  # noqa: E402

OUT = ROOT / "output/research/rebound_state_topk"
OOS = ROOT / "output/oos_predictions/cn_lightgbm_oos_predictions.csv"
ARMS = {"A_full": None, "B_startup": "startup_price_eligible", "C_rebound": "bottom_rebound_candidate"}
KS = (6, 20)
COST_BPS = 25
N_RANDOM = 200


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    oos = pd.read_csv(OOS, parse_dates=["trade_date"]).dropna(subset=["forward_return_20d", "model_score_raw"])
    dates = sorted(oos["trade_date"].unique())
    non_overlap = dates[::4]
    oos = oos[oos["trade_date"].isin(non_overlap)]
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date="2024-07-01", end_date="2026-09-30",
                                    columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume"])
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    labels = build_cn_strategy_labels(bars, path_horizon=60, entry_delay=1, exec_horizons=(20,), startup_low_window=120)
    labels["trade_date"] = pd.to_datetime(labels["trade_date"])
    frame = oos.merge(labels[["stock_code", "trade_date", "startup_price_eligible", "bottom_rebound_candidate"]],
                      on=["stock_code", "trade_date"], how="left")
    pool = frame.groupby("trade_date")["forward_return_20d"].mean()
    rng = np.random.default_rng(29)
    rows, prev = [], {}
    for date, day in frame.groupby("trade_date"):
        for arm, state in ARMS.items():
            sub = day if state is None else day[day[state].fillna(False).astype(bool)]
            if len(sub) < max(KS) + 5:
                continue
            order = np.argsort(-sub["model_score_raw"].to_numpy())
            values = sub["forward_return_20d"].to_numpy()
            codes = sub["stock_code"].to_numpy()
            for k in KS:
                if len(values) < k:
                    continue
                excess = float(values[order[:k]].mean() - pool.loc[date])
                random_means = day["forward_return_20d"].to_numpy()[rng.integers(0, len(day), size=(N_RANDOM, k))].mean(axis=1)
                top = set(codes[order[:k]])
                last = prev.get((arm, k)) or set()
                turnover = len(top - last) / k if last else 1.0
                prev[(arm, k)] = top
                rows.append({"trade_date": date, "arm": arm, "k": k, "pool": len(sub),
                             "excess_%": round(excess * 100, 3),
                             "vs_random_%": round((excess - float(random_means.mean())) * 100, 3),
                             "turnover": round(turnover, 3)})
    detail = pd.DataFrame(rows)
    detail.to_csv(OUT / "detail.csv", index=False)
    summary = []
    for (arm, k), group in detail.groupby(["arm", "k"]):
        ex = group["excess_%"].astype(float)
        turnover = group["turnover"].astype(float)
        summary.append({"arm": arm, "k": k, "periods": len(group), "mean_pool": int(group["pool"].mean()),
                        "mean_excess_%": round(float(ex.mean()), 3), "median_excess_%": round(float(ex.median()), 3),
                        "t": round(float(ex.mean() / ex.std() * np.sqrt(len(ex))), 2) if len(ex) > 1 else None,
                        "hit": round(float((ex > 0).mean()), 3),
                        "net25_%": round(float((ex - turnover * 2 * COST_BPS / 100).mean()), 3)})
    table = pd.DataFrame(summary).sort_values(["k", "mean_excess_%"], ascending=[True, False])
    table.to_csv(OUT / "metrics.csv", index=False)
    print(table.to_string(index=False), flush=True)
    (OUT / "REPORT.md").write_text("# 低位反弹状态内的模型 Top-k A/B\n\n" + table.to_markdown(index=False) + "\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
