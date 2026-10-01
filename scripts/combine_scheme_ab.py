#!/usr/bin/env python
"""「筛池后再排序」 vs 「池内正交化 / 分组中性化 / 软倾斜」 的四方案对照。

用 walk-forward 的样本外模型分（`cn_lightgbm_oos_predictions.csv`，5 折 expanding）当 alpha，
用同一个横截面里算出的启动状态量（dist_from_120d_low / dist_from_60d_high / volume_ratio_20d /
moneyflow z）当暴露，比较四种把 alpha 与暴露结合的方式：

  A model_only      只用模型分（基线）
  B screen_rank     先按启动带+资金流硬筛池，再在池内按模型分排序（用户提的方案一）
  C residual        把模型分对 [dist, flow] 做横截面回归取残差后排序（正交化）
  D group_neutral   在 dist×flow 的分位格子里把模型分去均值后排序（非参数中性化）
  E tilt            软倾斜：z(model) + λ·(z(dist)+z(flow))（现在的门禁思路）

指标：秩 IC、Top-20/50 相对池均值的超额、vs 随机（配对）、换手、扣 25bps；
非重叠子集（每 4 个周度决策日取 1 ≈ 20 交易日）。
输出：output/research/combine_scheme_ab/{detail.csv, metrics.csv, REPORT.md}
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

OUT = ROOT / "output/research/combine_scheme_ab"
OOS = ROOT / "output/oos_predictions/cn_lightgbm_oos_predictions.csv"
KS = (20, 50)
COST_BPS = 25
N_RANDOM = 200
BAND = (0.05, 0.35)          # 启动带（下沿不追未启动、上沿不追已走完）
FLOW_MIN = 0.0               # 资金流确认门槛（z 值）


def build_state(frames: pd.DataFrame) -> pd.DataFrame:
    frames = frames.sort_values(["stock_code", "trade_date"])
    grouped = frames.groupby("stock_code", sort=False)
    frames["low120"] = grouped["low"].transform(lambda s: s.rolling(120, min_periods=60).min())
    frames["high60"] = grouped["high"].transform(lambda s: s.rolling(60, min_periods=30).max())
    frames["vol20"] = grouped["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    frames["dist_from_120d_low"] = frames["close"] / frames["low120"] - 1.0
    frames["dist_from_60d_high"] = frames["close"] / frames["high60"] - 1.0
    frames["volume_ratio_20d"] = frames["volume"] / frames["vol20"]
    return frames[["stock_code", "trade_date", "dist_from_120d_low", "dist_from_60d_high", "volume_ratio_20d"]]


def zscore(series: pd.Series) -> pd.Series:
    return (series - series.mean()) / (series.std() + 1e-12)


def score_residual(frame: pd.DataFrame, model_col: str, exposures: list[str]) -> pd.Series:
    """横截面 OLS 取残差（秩变换后回归，抗厚尾）。"""
    y = frame[model_col].rank(pct=True)
    X = np.column_stack([np.ones(len(frame))] + [frame[c].rank(pct=True) for c in exposures])
    beta, *_ = np.linalg.lstsq(X, y.to_numpy(), rcond=None)
    resid = y.to_numpy() - X @ beta
    return pd.Series(resid, index=frame.index)


def score_group_neutral(frame: pd.DataFrame, model_col: str, exposures: list[str], bins: int = 5) -> pd.Series:
    """在暴露因子的分位格子里把模型分去均值。"""
    keys = []
    for column in exposures:
        keys.append(pd.qcut(frame[column].rank(method="first"), bins, labels=False, duplicates="drop"))
    key_frame = pd.concat(keys, axis=1)
    grouped = frame[model_col].groupby([key_frame[c] for c in key_frame.columns]).transform("mean")
    return frame[model_col] - grouped


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    oos = pd.read_csv(OOS, parse_dates=["trade_date"])
    oos = oos.dropna(subset=["forward_return_20d", "model_score_raw"])
    dates = sorted(oos["trade_date"].unique())
    non_overlap = dates[::4]
    print(f"[oos] 决策日 {len(dates)} 个 → 非重叠子集 {len(non_overlap)} 个 "
          f"({pd.Timestamp(non_overlap[0]).date()}~{pd.Timestamp(non_overlap[-1]).date()})", flush=True)

    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date=(pd.Timestamp(dates[0]) - pd.Timedelta(days=260)).strftime("%Y-%m-%d"),
                                    end_date=pd.Timestamp(dates[-1]).strftime("%Y-%m-%d"),
                                    columns=["stock_code", "trade_date", "high", "low", "close", "volume"])
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    state = build_state(bars)
    print(f"[state] 状态量 {len(state):,} 行（dist120low / dist60high / volratio20）", flush=True)

    money = svc._clean_panel_training_data(
        market="CN", factor_set="alpha_zoo_hk", frequency="daily", adjust="qfq", days=756, label_horizon=20,
        cleaning_version="p0.2.v1", label_mode="forward_return", startup_only=False, label_path_horizon=60,
        preserve_startup_context=False, min_stock_count=50, end_date=None, show_progress=False)[0]
    money["trade_date"] = pd.to_datetime(money["trade_date"])
    flow_col = "moneyflow_net_z_5d_clean"
    money = money[["stock_code", "trade_date", flow_col]].rename(columns={flow_col: "flow_z_5d"})

    frame = oos.merge(state, on=["stock_code", "trade_date"], how="left").merge(
        money, on=["stock_code", "trade_date"], how="left")
    frame = frame[frame["trade_date"].isin(non_overlap)].dropna(subset=["dist_from_120d_low", "volume_ratio_20d"])
    frame["flow_z_5d"] = frame["flow_z_5d"].fillna(0.0)
    print(f"[frame] 可用 {len(frame):,} 行 / {frame['trade_date'].nunique()} 个决策日", flush=True)

    rng = np.random.default_rng(5)
    rows = []
    prev: dict[str, set] = {}   # 每个方案各自记上一期 Top-k，用于算换手
    for date, day in frame.groupby("trade_date"):
        day = day.copy()
        pool_mean = float(day["forward_return_20d"].mean())
        scores = {
            "A_model_only": zscore(day["model_score_raw"]),
            "C_residual": zscore(score_residual(day, "model_score_raw", ["dist_from_120d_low", "flow_z_5d"])),
            "D_group_neutral": zscore(score_group_neutral(day, "model_score_raw", ["dist_from_120d_low", "flow_z_5d"])),
            "E_tilt": zscore(day["model_score_raw"]) + 0.5 * (zscore(day["dist_from_120d_low"]) + zscore(day["flow_z_5d"])),
        }
        in_band = day["dist_from_120d_low"].between(BAND[0], BAND[1])
        pool = day[in_band & (day["flow_z_5d"] >= FLOW_MIN)]
        scores["B_screen_rank"] = day["model_score_raw"].where(day.index.isin(pool.index), np.nan)
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
                last = prev.get(scheme) or set()
                turnover = len(top - last) / k if last else 1.0
                prev[scheme] = top
                rows.append({"trade_date": date, "scheme": scheme, "k": k, "ic": ic, "excess": excess,
                             "vs_random": excess - float(random_means.mean()),
                             "pool_size": int(valid.sum()), "turnover": turnover})
    detail = pd.DataFrame(rows)
    detail.to_csv(OUT / "detail.csv", index=False)
    summary = []
    for (scheme, k), group in detail.groupby(["scheme", "k"]):
        summary.append({"scheme": scheme, "k": k, "periods": len(group),
                        "ic": round(float(group["ic"].mean()), 4),
                        "excess_%": round(float(group["excess"].mean()) * 100, 3),
                        "t": round(float(group["excess"].mean() / group["excess"].std() * np.sqrt(len(group))), 2),
                        "hit": round(float((group["excess"] > 0).mean()), 3),
                        "vs_random_%": round(float(group["vs_random"].mean()) * 100, 3),
                        "vs_random_t": round(float(group["vs_random"].mean() / group["vs_random"].std() * np.sqrt(len(group))), 2)
                        if len(group) > 1 else None,
                        "pool_size": int(group["pool_size"].mean()),
                        "turnover": round(float(group["turnover"].mean()), 3),
                        "net25_%": round(float((group["excess"] - group["turnover"] * 2 * COST_BPS / 10000).mean()) * 100, 3)})
    table = pd.DataFrame(summary).sort_values(["k", "excess_%"], ascending=[True, False])
    table.to_csv(OUT / "metrics.csv", index=False)
    print("\n" + table.to_string(index=False), flush=True)
    (OUT / "REPORT.md").write_text(
        "# 筛池 vs 正交化 vs 分组中性化 vs 软倾斜（walk-forward 样本外）\n\n"
        f"alpha = 5 折 expanding 的样本外 LightGBM 分；暴露 = dist_from_120d_low / flow_z_5d；"
        f"非重叠决策日 {len(non_overlap)} 个；超额相对当日全池均值；对照 = 同日随机 {N_RANDOM} 组；成本 25bps 双边。\n\n"
        + table.to_markdown(index=False) + "\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
