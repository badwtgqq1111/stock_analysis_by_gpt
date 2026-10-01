#!/usr/bin/env python
"""正交化方案网格：选出「最优方案」再接线。

候选（alpha = 5 折 expanding 的样本外 LightGBM 分，13 个非重叠决策日）：
  A_model       只用模型分（现状基线）
  C2            resid(model ~ dist120low + flow_z)
  C4            resid(model ~ dist120low + flow_z + dist60high + volratio)
  C4_ind        resid(model ~ C4 + 行业哑变量)
  C2_state      resid(model ~ dist+flow) 后再叠加状态项 0.5·(z_dist + z_flow)（"中性化 + 信号"两段式）
  C2_state_q    resid(model ~ dist+flow) 后叠加 0.25·z_dist（只加被验证最强的 dist）
指标：秩 IC / Top-20 / Top-50 超额、t、胜率、vs 随机、换手、扣 25bps。
输出：output/research/orthogonalization_variants/{metrics.csv, detail.csv, REPORT.md}
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

OUT = ROOT / "output/research/orthogonalization_variants"
OOS = ROOT / "output/oos_predictions/cn_lightgbm_oos_predictions.csv"
KS = (20, 50)
COST_BPS = 25
N_RANDOM = 200


def zs(series: pd.Series) -> pd.Series:
    return (series - series.mean()) / (series.std() + 1e-12)


def residual(frame: pd.DataFrame, y_col: str, exposures: list[str], dummies: list[str] | None = None) -> pd.Series:
    """秩空间横截面 OLS 取残差；dummies 为 0/1 列名（行业）。"""
    y = frame[y_col].rank(pct=True).to_numpy()
    parts = [np.ones(len(frame))] + [frame[c].rank(pct=True).to_numpy() for c in exposures]
    if dummies:
        for column in dummies:
            parts.append(frame[column].to_numpy(dtype=float))
    X = np.column_stack(parts)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return pd.Series(y - X @ beta, index=frame.index)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    oos = pd.read_csv(OOS, parse_dates=["trade_date"]).dropna(subset=["forward_return_20d", "model_score_raw"])
    dates = sorted(oos["trade_date"].unique())
    non_overlap = dates[::4]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date=(pd.Timestamp(dates[0]) - pd.Timedelta(days=260)).strftime("%Y-%m-%d"),
                                    end_date=pd.Timestamp(dates[-1]).strftime("%Y-%m-%d"),
                                    columns=["stock_code", "trade_date", "high", "low", "close", "volume"])
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values(["stock_code", "trade_date"])
    g = bars.groupby("stock_code", sort=False)
    bars["dist_from_120d_low"] = bars["close"] / g["low"].transform(lambda s: s.rolling(120, min_periods=60).min()) - 1.0
    bars["dist_from_60d_high"] = bars["close"] / g["high"].transform(lambda s: s.rolling(60, min_periods=30).max()) - 1.0
    bars["volume_ratio_20d"] = bars["volume"] / g["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    state = bars[["stock_code", "trade_date", "dist_from_120d_low", "dist_from_60d_high", "volume_ratio_20d"]]

    money = svc._clean_panel_training_data(
        market="CN", factor_set="alpha_zoo_hk", frequency="daily", adjust="qfq", days=756, label_horizon=20,
        cleaning_version="p0.2.v1", label_mode="forward_return", startup_only=False, label_path_horizon=60,
        preserve_startup_context=False, min_stock_count=50, end_date=None, show_progress=False)[0]
    money["trade_date"] = pd.to_datetime(money["trade_date"])
    money = money[["stock_code", "trade_date", "moneyflow_net_z_5d_clean"]].rename(
        columns={"moneyflow_net_z_5d_clean": "flow_z_5d"})
    info = svc.warehouse.read_stock_info(market="CN")
    industry = info[["stock_code", "industry_l1"]].drop_duplicates("stock_code")
    frame = oos.merge(state, on=["stock_code", "trade_date"], how="left").merge(money, on=["stock_code", "trade_date"], how="left")
    frame = frame.merge(industry, on="stock_code", how="left")
    frame = frame[frame["trade_date"].isin(non_overlap)].dropna(subset=["dist_from_120d_low", "volume_ratio_20d", "dist_from_60d_high"])
    frame["flow_z_5d"] = frame["flow_z_5d"].fillna(0.0)
    print(f"[frame] {len(frame):,} 行 / {frame['trade_date'].nunique()} 个非重叠决策日；行业覆盖 "
          f"{frame['industry_l1'].notna().mean():.1%}", flush=True)

    rng = np.random.default_rng(9)
    rows, prev = [], {}
    for date, day in frame.groupby("trade_date"):
        day = day.copy()
        dummies = pd.get_dummies(day["industry_l1"].fillna("NA"), drop_first=True).astype(float)
        dummies.index = day.index
        day = pd.concat([day, dummies], axis=1)
        dummy_cols = list(dummies.columns)
        pool_mean = float(day["forward_return_20d"].mean())
        base = day["model_score_raw"]
        scores = {
            "A_model": zs(base),
            "C2": zs(residual(day, "model_score_raw", ["dist_from_120d_low", "flow_z_5d"])),
            "C4": zs(residual(day, "model_score_raw", ["dist_from_120d_low", "flow_z_5d", "dist_from_60d_high", "volume_ratio_20d"])),
            "C4_ind": zs(residual(day, "model_score_raw", ["dist_from_120d_low", "flow_z_5d", "dist_from_60d_high", "volume_ratio_20d"], dummy_cols)),
        }
        r2 = residual(day, "model_score_raw", ["dist_from_120d_low", "flow_z_5d"])
        scores["C2_state"] = zs(r2) + 0.5 * (zs(day["dist_from_120d_low"]) + zs(day["flow_z_5d"]))
        scores["C2_state_q"] = zs(r2) + 0.25 * zs(day["dist_from_120d_low"])
        for scheme, score in scores.items():
            valid = score.notna()
            if valid.sum() < max(KS) + 20:
                continue
            sub = day[valid].assign(score=score[valid])
            ic = float(sub["score"].corr(sub["forward_return_20d"], method="spearman"))
            order = np.argsort(-sub["score"].to_numpy())
            values = sub["forward_return_20d"].to_numpy()
            codes = sub["stock_code"].to_numpy()
            for k in KS:
                excess = float(values[order[:k]].mean() - pool_mean)
                random_means = day["forward_return_20d"].to_numpy()[rng.integers(0, len(day), size=(N_RANDOM, k))].mean(axis=1)
                top = set(codes[order[:k]])
                last = prev.get((scheme, k)) or set()
                turnover = len(top - last) / k if last else 1.0
                prev[(scheme, k)] = top
                rows.append({"trade_date": date, "scheme": scheme, "k": k, "ic": ic, "excess": excess,
                             "vs_random": excess - float(random_means.mean()), "turnover": turnover})
    detail = pd.DataFrame(rows)
    detail.to_csv(OUT / "detail.csv", index=False)
    summary = []
    for (scheme, k), group in detail.groupby(["scheme", "k"]):
        summary.append({"scheme": scheme, "k": k, "periods": len(group), "ic": round(float(group["ic"].mean()), 4),
                        "excess_%": round(float(group["excess"].mean()) * 100, 3),
                        "t": round(float(group["excess"].mean() / group["excess"].std() * np.sqrt(len(group))), 2),
                        "hit": round(float((group["excess"] > 0).mean()), 3),
                        "vs_random_t": round(float(group["vs_random"].mean() / group["vs_random"].std() * np.sqrt(len(group))), 2),
                        "turnover": round(float(group["turnover"].mean()), 3),
                        "net25_%": round(float((group["excess"] - group["turnover"] * 2 * COST_BPS / 10000).mean()) * 100, 3)})
    table = pd.DataFrame(summary).sort_values(["k", "excess_%"], ascending=[True, False])
    table.to_csv(OUT / "metrics.csv", index=False)
    print("\n" + table.to_string(index=False), flush=True)
    (OUT / "REPORT.md").write_text("# 正交化方案网格\n\n" + table.to_markdown(index=False) + "\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
