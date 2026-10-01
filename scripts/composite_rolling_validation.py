#!/usr/bin/env python
"""准入因子等权合成的滚动非重叠验收（能不能上生产，用这个判）。

与 `top_book_conversion_ab.py` 的单窗口结论不同，这里每个再平衡日只允许使用
**该日之前**的月度 IC 做准入与权重（walk-forward，无前视），并且：
  - 再平衡间隔 = 20 个交易日 = 标签 horizon → **窗口非重叠**；
  - 对照 = 同日同规模随机篮子（200 组）配对比较；
  - 成本 = 与前一次 Top-20 名单的换手比例 × 双边成本（15/25/50bps 三档）。

输出：output/research/composite_validation/{detail.csv, metrics.csv, REPORT.md}
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
from factor_ic_protocol import bh_significant, newey_west_t  # noqa: E402

OUT = ROOT / "output/research/composite_validation"
PROTOCOL = ROOT / "output/research/factor_ic_protocol"
COSTS_BPS = (15, 25, 50)
N_RANDOM = 200
TOP_K = 20


def main() -> int:
    import tomllib
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    panel, features, label = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=756, label_horizon=int(lg["label_horizon"]), cleaning_version=cfg["clean_panel"]["cleaning_version"],
        label_mode=lg["label_mode"], startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    labeled = panel.loc[panel[label].notna()].copy()
    series = pd.read_csv(PROTOCOL / "feature_ic_series.csv", index_col=0)
    all_months = [str(m) for m in series.columns]
    dates = np.array(sorted(labeled["trade_date"].unique()))
    # 再平衡日：从第 13 个月起，每 20 个交易日一个（非重叠）
    months_of_dates = pd.Series(pd.to_datetime(dates)).dt.to_period("M").astype(str).to_numpy()
    min_history_months = 6  # 用满 12 个月只剩 7 个非重叠点；6 个月 → 12 个点，代价是准入样本更短
    start_idx = next((i for i, m in enumerate(months_of_dates)
                      if len([x for x in all_months if x < m]) >= min_history_months), 60)
    rebalance_dates = list(dates[start_idx::20])
    print(f"[plan] 再平衡日 {len(rebalance_dates)} 个：{pd.Timestamp(rebalance_dates[0]).date()} ~ "
          f"{pd.Timestamp(rebalance_dates[-1]).date()}（间隔 20 交易日 = 非重叠）", flush=True)

    rng = np.random.default_rng(11)
    rows, prev_top = [], set()
    for d in rebalance_dates:
        day = pd.Timestamp(d)
        hist = [m for m in all_months if m < day.strftime("%Y-%m")]
        if len(hist) < min_history_months:
            continue
        sub = series[hist]
        mean = sub.mean(axis=1)
        icir = mean / sub.std(axis=1).replace(0, np.nan)
        from scipy import stats as sps
        t_nw = pd.Series([newey_west_t(sub.loc[c]) for c in sub.index], index=sub.index)
        p = pd.Series(2 * (1 - sps.norm.cdf(t_nw.abs())), index=sub.index)
        fdr = bh_significant(p, q=0.05)
        first, second = hist[: len(hist) // 2], hist[len(hist) // 2:]
        stable = np.sign(sub[first].mean(axis=1)) == np.sign(sub[second].mean(axis=1))
        pick = (mean.abs() >= 0.02) & (icir.abs() >= 0.30) & fdr & stable
        cols = [c for c in pick.index[pick] if c in labeled.columns]
        if len(cols) < 5:
            continue
        weights = (np.sign(mean[cols]) * icir[cols].abs())
        weights = (weights / weights.abs().sum()).to_dict()
        day_frame = labeled[labeled["trade_date"] == day]
        if len(day_frame) < 200:
            continue
        score = pd.Series(0.0, index=day_frame.index)
        for column, weight in weights.items():
            score = score + weight * (day_frame[column].rank(pct=True) - 0.5).fillna(0.0)
        order = np.argsort(-score.to_numpy())
        values = day_frame[label].to_numpy()
        pool_mean = float(values.mean())
        top_values = values[order[:TOP_K]]
        excess = float(top_values.mean() - pool_mean)
        random_means = values[rng.integers(0, len(values), size=(N_RANDOM, TOP_K))].mean(axis=1)
        top_codes = set(day_frame["stock_code"].to_numpy()[order[:TOP_K]])
        turnover = len(top_codes - prev_top) / TOP_K if prev_top else 1.0
        prev_top = top_codes
        # 十分位单调性：按 score 分 10 档，档内 label 均值与档序号的 Spearman
        bucket = pd.qcut(pd.Series(score.to_numpy()), 10, labels=False, duplicates="drop")
        decile_means = pd.Series(values).groupby(bucket.to_numpy()).mean()
        decile_spearman = (float(pd.Series(decile_means.index).corr(pd.Series(decile_means.to_numpy()), method="spearman"))
                           if len(decile_means) >= 5 else np.nan)
        rows.append({"trade_date": day, "n_admitted": len(cols), "ic_pool": float(score.corr(day_frame[label], method="spearman")),
                     "top20_return": float(top_values.mean()), "pool_return": pool_mean,
                     "excess": excess, "random_mean": float(random_means.mean()),
                     "vs_random": excess - float(random_means.mean()),
                     "vs_random_p": float((random_means >= top_values.mean()).mean()),
                     "turnover": turnover, "decile_spearman": decile_spearman})
    detail = pd.DataFrame(rows)
    detail.to_csv(OUT / "detail.csv", index=False)
    summary = {"rebalances": len(detail),
               "mean_excess_%": round(float(detail.excess.mean()) * 100, 3),
               "median_excess_%": round(float(detail.excess.median()) * 100, 3),
               "t_excess": round(float(detail.excess.mean() / detail.excess.std() * np.sqrt(len(detail))), 2),
               "hit_positive": round(float((detail.excess > 0).mean()), 3),
               "vs_random_%": round(float(detail.vs_random.mean()) * 100, 3),
               "vs_random_t": round(float(detail.vs_random.mean() / detail.vs_random.std() * np.sqrt(len(detail))), 2),
               "beat_random_share": round(float((detail.vs_random > 0).mean()), 3),
               "mean_turnover": round(float(detail.turnover.mean()), 3),
               "mean_admitted": round(float(detail.n_admitted.mean()), 1)}
    for bps in COSTS_BPS:
        cost = detail.turnover * (2 * bps / 10000.0)
        net = detail.excess - cost
        summary[f"net_{bps}bps_%"] = round(float(net.mean()) * 100, 3)
        summary[f"net_{bps}bps_t"] = round(float(net.mean() / net.std() * np.sqrt(len(net))), 2)
    pd.DataFrame([summary]).to_csv(OUT / "metrics.csv", index=False)
    print("\n" + pd.DataFrame([summary]).T.rename(columns={0: "value"}).to_string(), flush=True)
    (OUT / "REPORT.md").write_text(
        "# 准入因子等权合成：滚动非重叠 + 扣费 + vs 随机 验收\n\n"
        f"再平衡 {len(detail)} 次（每 20 交易日一次，非重叠）；每次只用该日之前的月度 IC 做准入与权重"
        "（walk-forward，无前视）；对照 = 同日随机 20 组 ×200；成本 = 与前次 Top-20 的换手比例 × 双边费率。\n\n"
        + pd.DataFrame([summary]).T.rename(columns={0: "value"}).to_markdown() + "\n\n## 逐次明细\n\n"
        + detail.round(4).to_markdown(index=False) + "\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
