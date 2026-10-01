#!/usr/bin/env python
"""低吸腿 A/B：模型分在"低吸子集"内排序 vs 全截面排序（其余口径与 §20 一致）。

臂：
  A model_alpha       全截面按模型分排序（现状）
  B dip_then_model    先按 dip state 过滤（dist∈[0.05,0.30] + 量比≤1.2 + flowz≥0），再按模型分排序
  C dip_by_position   dip 子集内按"离低点最近"排序（检验模型在子集内是否还有增量）
  D dip_random        dip 子集内随机（子集本身的基准，用于区分"子集有效"与"模型有效"）

口径：alpha = 5 折 expanding 的样本外 LightGBM 分；13 个非重叠决策日；
      超额 = Top-k 20 日收益 − 当日全池均值；对照 = 同日随机同规模 ×200；成本 25bps 双边。
输出：output/research/dip_leg_ab/{detail.csv, metrics.csv, REPORT.md}
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

OUT = ROOT / "output/research/dip_leg_ab"
OOS = ROOT / "output/oos_predictions/cn_lightgbm_oos_predictions.csv"
KS = (6, 12, 20)
COST_BPS = 25
N_RANDOM = 200
BAND = (0.05, 0.30)
VOL_MAX = 1.2
FLOW_MIN = 0.0


def main() -> int:
    import tomllib
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    oos = pd.read_csv(OOS, parse_dates=["trade_date"]).dropna(subset=["forward_return_20d", "model_score_raw"])
    dates = sorted(oos["trade_date"].unique())
    non_overlap = dates[::4]
    oos = oos[oos["trade_date"].isin(non_overlap)]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date=(pd.Timestamp(dates[0]) - pd.Timedelta(days=260)).strftime("%Y-%m-%d"),
                                    end_date=pd.Timestamp(dates[-1]).strftime("%Y-%m-%d"),
                                    columns=["stock_code", "trade_date", "high", "low", "close", "volume"])
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values(["stock_code", "trade_date"])
    g = bars.groupby("stock_code", sort=False)
    bars["dist_from_120d_low"] = bars["close"] / g["low"].transform(lambda s: s.rolling(120, min_periods=60).min()) - 1.0
    bars["volume_ratio_20d"] = bars["volume"] / g["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    state = bars[["stock_code", "trade_date", "dist_from_120d_low", "volume_ratio_20d"]]

    money = svc._clean_panel_training_data(
        market="CN", factor_set="alpha_zoo_hk", frequency="daily", adjust="qfq", days=756, label_horizon=20,
        cleaning_version="p0.2.v1", label_mode="forward_return", startup_only=False, label_path_horizon=60,
        preserve_startup_context=False, min_stock_count=50, end_date=None, show_progress=False)[0]
    money["trade_date"] = pd.to_datetime(money["trade_date"])
    money = money[["stock_code", "trade_date", "moneyflow_net_z_5d_clean"]].rename(
        columns={"moneyflow_net_z_5d_clean": "flow_z"})
    frame = oos.merge(state, on=["stock_code", "trade_date"], how="left").merge(money, on=["stock_code", "trade_date"], how="left")
    frame["flow_z"] = frame["flow_z"].fillna(0.0)
    frame = frame.dropna(subset=["dist_from_120d_low", "volume_ratio_20d"])
    pool_means = frame.groupby("trade_date")["forward_return_20d"].mean()
    print(f"[frame] {len(frame):,} 行 / {frame['trade_date'].nunique()} 个决策日", flush=True)

    rng = np.random.default_rng(17)
    rows, prev = [], {}
    for date, day in frame.groupby("trade_date"):
        day = day.copy()
        dip = day["dist_from_120d_low"].between(*BAND) & (day["volume_ratio_20d"] <= VOL_MAX) & (day["flow_z"] >= FLOW_MIN)
        dip_size = int(dip.sum())
        if dip_size < max(KS) + 5:
            continue
        dip_frame = day[dip]
        scores = {
            "A_model_alpha": (day, day["model_score_raw"]),
            "B_dip_then_model": (dip_frame, dip_frame["model_score_raw"]),
            "C_dip_by_position": (dip_frame, -dip_frame["dist_from_120d_low"]),
        }
        for scheme, (sub, score) in scores.items():
            values = sub["forward_return_20d"].to_numpy()
            order = np.argsort(-score.to_numpy())
            codes = sub["stock_code"].to_numpy()
            ic = float(pd.Series(score.to_numpy()).corr(pd.Series(values), method="spearman"))
            for k in KS:
                if len(values) < k:
                    continue
                excess = float(values[order[:k]].mean() - pool_means.loc[date])
                random_means = day["forward_return_20d"].to_numpy()[rng.integers(0, len(day), size=(N_RANDOM, k))].mean(axis=1)
                top = set(codes[order[:k]])
                last = prev.get((scheme, k)) or set()
                turnover = len(top - last) / k if last else 1.0
                prev[(scheme, k)] = top
                rows.append({"trade_date": date, "scheme": scheme, "k": k, "ic": ic, "pool_size": len(values),
                             "dip_subset_size": dip_size, "excess_%": round(excess * 100, 3),
                             "vs_random_%": round((excess - float(random_means.mean())) * 100, 3), "turnover": round(turnover, 3)})
        # D：dip 子集内随机（基准）
        for k in KS:
            if dip_size < k:
                continue
            random_pick = dip_frame["forward_return_20d"].to_numpy()[rng.integers(0, dip_size, size=(N_RANDOM, k))].mean(axis=1)
            rows.append({"trade_date": date, "scheme": "D_dip_random", "k": k, "ic": np.nan, "pool_size": dip_size,
                         "dip_subset_size": dip_size, "excess_%": round((float(random_pick.mean()) - pool_means.loc[date]) * 100, 3),
                         "vs_random_%": round((float(random_pick.mean()) - float(day["forward_return_20d"].to_numpy()[rng.integers(0, len(day), size=(N_RANDOM, k))].mean(axis=1).mean())) * 100, 3),
                         "turnover": np.nan})
    detail = pd.DataFrame(rows)
    detail.to_csv(OUT / "detail.csv", index=False)
    summary = []
    for (scheme, k), group in detail.groupby(["scheme", "k"]):
        ex = group["excess_%"].astype(float)
        turnover = group["turnover"].astype(float).fillna(1.0)
        summary.append({"scheme": scheme, "k": k, "periods": len(group),
                        "mean_excess_%": round(float(ex.mean()), 3), "median_excess_%": round(float(ex.median()), 3),
                        "t": round(float(ex.mean() / ex.std() * np.sqrt(len(ex))), 2) if len(ex) > 1 else None,
                        "hit": round(float((ex > 0).mean()), 3),
                        "ic": round(float(group["ic"].mean()), 4) if group["ic"].notna().any() else None,
                        "pool_size": int(group["pool_size"].mean()),
                        "dip_share": round(float(group["dip_subset_size"].mean()) / float(group["pool_size"].mean()), 3),
                        "net25_%": round(float((ex - turnover * 2 * COST_BPS / 100).mean()), 3)})
    table = pd.DataFrame(summary).sort_values(["k", "mean_excess_%"], ascending=[True, False])
    table.to_csv(OUT / "metrics.csv", index=False)
    print("\n" + table.to_string(index=False), flush=True)
    (OUT / "REPORT.md").write_text(
        "# 低吸腿 A/B（模型分在低吸子集内 vs 全截面）\n\n"
        f"dip state = dist_from_120d_low∈[{BAND[0]},{BAND[1]}] 且 volume_ratio_20d≤{VOL_MAX} 且 flow_z≥{FLOW_MIN}；"
        f"alpha = 5 折 expanding 样本外 LightGBM；非重叠决策日 {frame['trade_date'].nunique()} 个；"
        f"超额相对当日全池均值；对照同日随机 ×{N_RANDOM}；成本 {COST_BPS}bps 双边。\n\n"
        + table.to_markdown(index=False) + "\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
