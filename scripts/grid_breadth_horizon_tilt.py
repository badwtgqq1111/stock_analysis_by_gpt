#!/usr/bin/env python
"""A(扩宽度) / B(拉久期) / C(倾斜池) 在同一 walk-forward 协议下的网格实验。

统一口径：
  · 每次只用**该再平衡日之前**的月度 IC 做因子准入与权重（无前视）；
  · 再平衡间隔 = 标签 horizon（20/40/60 交易日）→ **窗口非重叠**；
  · 对照 = 同日随机同规模篮子 ×200（配对比较）；成本 = 与上次名单的换手 × 双边费率；
  · 任何一档都要同时报 均值/中位/t/胜率/vs随机/扣费后。

A：Top-k 从全截面取，k ∈ {6,12,20,30}
B：horizon ∈ {20,40,60}
C：先在**模型 Top-50 候选池**内用合成分排序再取 Top-k（用 `cn_lightgbm_oos_predictions.csv` 的
   walk-forward 打分，5 折 expanding，无自证）

输出：output/research/grid_experiments/{grid_breadth_horizon.csv, tilt_pool.csv, REPORT.md}
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402
from factor_ic_protocol import bh_significant, month_ic, newey_west_t  # noqa: E402

OUT = ROOT / "output/research/grid_experiments"
PROTOCOL = ROOT / "output/research/factor_ic_protocol"
KS = (6, 12, 20, 30)
HORIZONS = (20, 40, 60)
COST_BPS = 25
N_RANDOM = 200
MIN_HISTORY_MONTHS = 6


def weights_from_history(series: pd.DataFrame, upto_month: str) -> dict:
    """只用 upto_month 之前的月份做准入 + 权重（direction × |ICIR| 归一）。"""
    hist = [str(m) for m in series.columns if str(m) < upto_month]
    if len(hist) < MIN_HISTORY_MONTHS:
        return {}
    sub = series[hist]
    mean = sub.mean(axis=1)
    icir = mean / sub.std(axis=1).replace(0, np.nan)
    t_nw = pd.Series([newey_west_t(sub.loc[c]) for c in sub.index], index=sub.index)
    from scipy import stats as sps
    p = pd.Series(2 * (1 - sps.norm.cdf(t_nw.abs())), index=sub.index)
    fdr = bh_significant(p, q=0.05)
    half = len(hist) // 2
    stable = np.sign(sub[hist[:half]].mean(axis=1)) == np.sign(sub[hist[half:]].mean(axis=1))
    pick = (mean.abs() >= 0.02) & (icir.abs() >= 0.30) & fdr & stable
    cols = list(pick.index[pick])
    if len(cols) < 5:
        return {}
    raw = np.sign(mean[cols]) * icir[cols].abs()
    return (raw / raw.abs().sum()).to_dict()


def composite(frame: pd.DataFrame, weights: dict) -> pd.Series:
    score = pd.Series(0.0, index=frame.index)
    for column, weight in weights.items():
        if column not in frame.columns:
            continue
        score = score + weight * (frame[column].rank(pct=True) - 0.5).fillna(0.0)
    return score


def metrics_for_basket(values: np.ndarray, order: np.ndarray, k: int, rng) -> tuple[float, float, float]:
    pool = float(values.mean())
    top = float(values[order[:k]].mean())
    excess = top - pool
    random_means = values[rng.integers(0, len(values), size=(N_RANDOM, k))].mean(axis=1)
    return excess, excess - float(random_means.mean()), k


def summarise(detail: pd.DataFrame, label: str) -> dict:
    excess = detail["excess"]
    row = {"config": label, "periods": len(detail),
           "ic": round(float(detail["ic"].mean()), 4) if "ic" in detail else None,
           "mean_excess_%": round(float(excess.mean()) * 100, 3),
           "median_excess_%": round(float(excess.median()) * 100, 3),
           "t": round(float(excess.mean() / excess.std() * np.sqrt(len(excess))), 2) if len(excess) > 1 else None,
           "hit": round(float((excess > 0).mean()), 3),
           "vs_random_%": round(float(detail["vs_random"].mean()) * 100, 3),
           "vs_random_t": round(float(detail["vs_random"].mean() / detail["vs_random"].std() * np.sqrt(len(detail))), 2) if len(detail) > 1 else None,
           "turnover": round(float(detail["turnover"].mean()), 3),
           "net25_%": round(float((excess - detail["turnover"] * 2 * COST_BPS / 10000.0).mean()) * 100, 3)}
    return row


def main() -> int:
    import tomllib
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    rng = np.random.default_rng(23)
    rows, tilt_rows = [], []

    for horizon in HORIZONS:
        t0 = time.time()
        panel, features, label = svc._clean_panel_training_data(
            market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
            days=756, label_horizon=int(horizon), cleaning_version=cfg["clean_panel"]["cleaning_version"],
            label_mode=lg["label_mode"], startup_only=False, label_path_horizon=60, preserve_startup_context=False,
            min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
        panel["trade_date"] = pd.to_datetime(panel["trade_date"])
        labeled = panel.loc[panel[label].notna()].copy()
        cached = OUT / f"ic_series_h{horizon}.csv"
        if cached.is_file():
            series = pd.read_csv(cached, index_col=0)
        else:
            series = month_ic(labeled, list(features), label)
            series.to_csv(cached)
        series.columns = [str(c) for c in series.columns]
        dates = np.array(sorted(labeled["trade_date"].unique()))
        months = pd.Series(pd.to_datetime(dates)).dt.to_period("M").astype(str).to_numpy()
        start = next((i for i, m in enumerate(months) if len([x for x in series.columns if x < m]) >= MIN_HISTORY_MONTHS), 60)
        rebalance_dates = list(dates[start::int(horizon)])
        print(f"[h={horizon}] 面板 {len(labeled):,} 行 | 再平衡 {len(rebalance_dates)} 次 "
              f"({pd.Timestamp(rebalance_dates[0]).date()}~{pd.Timestamp(rebalance_dates[-1]).date()}) | {time.time()-t0:.0f}s", flush=True)

        # A/B：全截面 Top-k
        baskets = {k: {"excess": [], "vs_random": [], "turnover": [], "ic": []} for k in KS}
        prev = {k: set() for k in KS}
        for d in rebalance_dates:
            day = pd.Timestamp(d)
            weights = weights_from_history(series, day.strftime("%Y-%m"))
            if not weights:
                continue
            frame = labeled[labeled["trade_date"] == day]
            if len(frame) < 200:
                continue
            score = composite(frame, weights)
            order = np.argsort(-score.to_numpy())
            values = frame[label].to_numpy()
            codes = frame["stock_code"].to_numpy()
            ic = float(score.corr(frame[label], method="spearman"))
            for k in KS:
                excess, vs_random, _ = metrics_for_basket(values, order, k, rng)
                top = set(codes[order[:k]])
                turnover = len(top - prev[k]) / k if prev[k] else 1.0
                prev[k] = top
                baskets[k]["excess"].append(excess)
                baskets[k]["vs_random"].append(vs_random)
                baskets[k]["turnover"].append(turnover)
                baskets[k]["ic"].append(ic)
        for k in KS:
            detail = pd.DataFrame({"excess": baskets[k]["excess"], "vs_random": baskets[k]["vs_random"],
                                   "turnover": baskets[k]["turnover"], "ic": baskets[k]["ic"]})
            if detail.empty:
                continue
            row = summarise(detail, f"A/B horizon={horizon}d Top{k}")
            row["horizon"] = horizon
            row["k"] = k
            rows.append(row)
            print(f"    Top{k:<3} 超额均值 {row['mean_excess_%']:+.2f}% | t={row['t']} | 胜率 {row['hit']:.0%} | "
                  f"vs随机 {row['vs_random_%']:+.2f}% (t={row['vs_random_t']}) | 换手 {row['turnover']:.0%} | "
                  f"扣25bps {row['net25_%']:+.2f}% | IC {row['ic']:.3f}", flush=True)
        pd.DataFrame(rows).to_csv(OUT / "grid_breadth_horizon.csv", index=False)

    # C：模型 Top-50 候选池内倾斜
    oos = pd.read_csv(ROOT / "output/oos_predictions/cn_lightgbm_oos_predictions.csv", parse_dates=["trade_date"])
    panel20, _, label20 = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=756, label_horizon=20, cleaning_version=cfg["clean_panel"]["cleaning_version"],
        label_mode=lg["label_mode"], startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
    panel20["trade_date"] = pd.to_datetime(panel20["trade_date"])
    series20 = pd.read_csv(OUT / "ic_series_h20.csv", index_col=0)
    series20.columns = [str(c) for c in series20.columns]
    pooled = panel20[panel20["trade_date"].isin(sorted(oos["trade_date"].unique()))]
    for pool_k in (50, 100):
        buckets = {k: {"excess": [], "vs_random": [], "turnover": [], "ic": [], "pool_excess": []} for k in (6, 12, 20)}
        prev = {k: set() for k in (6, 12, 20)}
        oos_sorted = sorted(oos["trade_date"].unique())[::4]  # 每 4 周 ≈ 20 交易日
        for d in oos_sorted:
            day = pd.Timestamp(d)
            weights = weights_from_history(series20, day.strftime("%Y-%m"))
            if not weights:
                continue
            day_oos = oos[oos["trade_date"] == day]
            if len(day_oos) < pool_k + 50:
                continue
            pool = set(day_oos.nlargest(pool_k, "model_score_raw")["stock_code"])
            frame = pooled[(pooled["trade_date"] == day) & (pooled["stock_code"].isin(pool))]
            if len(frame) < 20 or frame[label20].notna().sum() < 20:
                continue
            frame = frame.dropna(subset=[label20])
            score = composite(frame, weights)
            order = np.argsort(-score.to_numpy())
            values = frame[label20].to_numpy()
            codes = frame["stock_code"].to_numpy()
            pool_mean_model = float(day_oos.nlargest(pool_k, "model_score_raw")["forward_return_20d"].mean())
            for k in (6, 12, 20):
                if len(values) < k:
                    continue
                excess = float(values[order[:k]].mean() - day_oos["forward_return_20d"].mean())
                random_means = day_oos["forward_return_20d"].to_numpy()[rng.integers(0, len(day_oos), size=(N_RANDOM, k))].mean(axis=1)
                top = set(codes[order[:k]])
                turnover = len(top - prev[k]) / k if prev[k] else 1.0
                prev[k] = top
                buckets[k]["excess"].append(excess)
                buckets[k]["vs_random"].append(excess - float(random_means.mean()))
                buckets[k]["turnover"].append(turnover)
                buckets[k]["ic"].append(float(score.corr(pd.Series(values), method="spearman")))
                buckets[k]["pool_excess"].append(pool_mean_model - float(day_oos["forward_return_20d"].mean()))
        for k, data in buckets.items():
            if not data["excess"]:
                continue
            detail = pd.DataFrame({"excess": data["excess"], "vs_random": data["vs_random"],
                                   "turnover": data["turnover"], "ic": data["ic"]})
            row = summarise(detail, f"C pool{pool_k} Top{k}")
            row.update({"pool": pool_k, "k": k, "pool_topk_excess_%": round(float(np.mean(data["pool_excess"])) * 100, 3)})
            tilt_rows.append(row)
            print(f"    [C] pool{pool_k} Top{k:<3} 超额 {row['mean_excess_%']:+.2f}% | t={row['t']} | 胜率 {row['hit']:.0%} | "
                  f"vs随机 {row['vs_random_%']:+.2f}% (t={row['vs_random_t']}) | 换手 {row['turnover']:.0%} | "
                  f"(池内模型 Top-{pool_k} 自身超额 {row['pool_topk_excess_%']:+.2f}%)", flush=True)
        pd.DataFrame(tilt_rows).to_csv(OUT / "tilt_pool.csv", index=False)

    grid = pd.DataFrame(rows)
    tilt = pd.DataFrame(tilt_rows)
    (OUT / "REPORT.md").write_text(
        "# A(宽度) / B(久期) / C(倾斜池) 网格实验\n\n"
        "口径：walk-forward 因子准入与权重（只用再平衡日之前的月度 IC）、再平衡间隔=标签 horizon（非重叠）、"
        "对照=同日随机篮子×200、成本=换手×双边 25bps。\n\n## A/B 全截面 Top-k\n\n"
        + grid.to_markdown(index=False) + "\n\n## C 模型候选池内倾斜\n\n" + tilt.to_markdown(index=False) + "\n",
        encoding="utf-8")
    print("\n=== A/B 网格 ===\n" + grid.to_string(index=False), flush=True)
    print("\n=== C 倾斜 ===\n" + tilt.to_string(index=False), flush=True)
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
