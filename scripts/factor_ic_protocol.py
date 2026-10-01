#!/usr/bin/env python
"""因子 IC 显著性与有效性协议（准入/淘汰的唯一裁判）。

为什么需要它：1,366 列、单点 IC、样本内 IC 都不足以决定因子去留。本脚本对每一列给出
可复算的四件事，并按门槛产出准入名单：

1. 有效性：月频（≈20 交易日，与非重叠口径一致）横截面**秩 IC** 序列；
   指标 = ic_mean / ic_std / ICIR / Newey-West t（月度、lag=1）/ 正号月占比 / 近 6 月均值。
2. 显著性：对全部列做 Benjamini-Hochberg FDR（q=0.05）与 Harvey-Liu-Zhu 的 t≥3.0 双阈值，
   直接给出"在多重检验下仍然显著"的列（防止数据挖掘）。
3. 稳定性：前半段 vs 后半段 IC 符号一致性（`sign_stable`），以及"近 6 月是否还在同号"。
4. 冗余：抽样日期的横截面秩相关矩阵做聚类（|ρ|≥0.9 归一簇），每簇只保留 |ICIR| 最大的代表，
   避免同一变量重复占权重。

数据质量筛查：覆盖率、常量列、缺失掩码列、估值时效列（age/stale）单独标注。

用法：
  PYTHONPATH=. .venv/bin/python scripts/factor_ic_protocol.py --days 756
输出：output/research/factor_ic_protocol/
  feature_ic_series.csv     月 × 列 的秩 IC
  factor_ic_registry.csv    每列全指标 + 等级 + 簇号 + 是否代表
  factor_ic_admitted.json   准入列清单（可直接喂给特征选择）
  family_significance.csv   族级显著性与通过率
  REPORT.md
"""
from __future__ import annotations

import argparse
import json
import re
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

OUT = ROOT / "output/research/factor_ic_protocol"
UPPER = re.compile(r"^[A-Z]+[0-9]*[A-Z0-9]*$")
QUALITY_TOKENS = ("age_td", "is_stale", "_is_missing", "observation_coverage")


def family(column: str) -> str:
    b = column[:-11] if column.endswith("_is_missing") else (column[:-6] if column.endswith("_clean") else column)
    for pattern, name in ((r"^GTJA\d+", "GTJA191"), (r"^ALPHA101_", "Alpha101"), (r"^TA_", "TA-Lib"),
                          (r"^moneyflow|^flow_second_wave", "moneyflow"),
                          (r"^valuation|^pe_|^pb_|^roe_|^gross_margin|^quality_value|^growth_quality", "valuation"),
                          (r"^financial", "financial"), (r"^liquidity", "liquidity"), (r"^payoff", "payoff"),
                          (r"^vol_", "vol"), (r"^vr_", "vr"), (r"^tr_|^turnover", "tr"), (r"^pv_", "pv"),
                          (r"^academic", "academic")):
        if re.match(pattern, b):
            return name
    return "Alpha158" if UPPER.match(b) else "other"


def month_ic(panel: pd.DataFrame, cols: list[str], label: str, block: int = 120) -> pd.DataFrame:
    """月内逐日横截面秩 IC 的等权平均（12*mean(rc*lc)，等距秩方差 1/12）。"""
    month_key = panel["trade_date"].dt.to_period("M").astype(str)
    result: dict[str, dict[str, float]] = {}
    for month, frame in panel.groupby(month_key):
        if len(frame) < 500:
            continue
        dates = frame["trade_date"].to_numpy()
        lc = pd.Series(frame[label].to_numpy()).groupby(dates).rank(pct=True).to_numpy(dtype="float64") - 0.5
        for start in range(0, len(cols), block):
            chunk = cols[start:start + block]
            raw = frame[chunk].to_numpy(dtype="float64", na_value=np.nan)
            ranks = pd.DataFrame(raw).groupby(dates).rank(pct=True).to_numpy(dtype="float64") - 0.5
            mask = np.isfinite(ranks) & np.isfinite(lc)[:, None]
            prod = np.where(mask, ranks * np.where(mask, lc[:, None], 0.0), 0.0)
            counts = mask.sum(axis=0)
            with np.errstate(invalid="ignore", divide="ignore"):
                ic = 12.0 * prod.sum(axis=0) / np.maximum(counts, 1)
            for column, value in zip(chunk, ic):
                result.setdefault(column, {})[str(month)] = float(value) if np.isfinite(value) else np.nan
    return pd.DataFrame(result).T.sort_index()


def newey_west_t(series: pd.Series, lag: int = 1) -> float:
    """月度 IC 序列的 Newey-West t（标签 20 交易日、月频观测 → 相邻月重叠，lag=1）。"""
    values = series.dropna().to_numpy(dtype=float)
    n = len(values)
    if n < 4:
        return float("nan")
    mean = values.mean()
    demeaned = values - mean
    gamma0 = float((demeaned ** 2).mean())
    var = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        cov = float((demeaned[k:] * demeaned[:-k]).mean())
        var += 2.0 * (1.0 - k / (lag + 1.0)) * cov
    if var <= 1e-18:
        return float("nan")
    return float(mean / np.sqrt(var / n))


def bh_significant(pvalues: pd.Series, q: float = 0.05) -> pd.Series:
    """Benjamini-Hochberg FDR：返回布尔序列（索引与输入一致）。"""
    p = pvalues.dropna()
    if p.empty:
        return pd.Series(False, index=pvalues.index)
    order = np.argsort(p.to_numpy())
    ranked = p.to_numpy()[order]
    m = len(ranked)
    thresholds = q * (np.arange(1, m + 1) / m)
    passed = ranked <= thresholds
    cutoff = np.max(np.where(passed)[0]) + 1 if passed.any() else 0
    accepted = set(p.index.to_numpy()[order[:cutoff]])
    return pd.Series([idx in accepted for idx in pvalues.index], index=pvalues.index)


def redundancy_clusters(panel: pd.DataFrame, cols: list[str], threshold: float = 0.9,
                        n_dates: int = 16, seed: int = 0) -> dict:
    """抽样日期的横截面秩相关矩阵 → 贪心聚类（|ρ|≥threshold 归同簇）。"""
    rng = np.random.default_rng(seed)
    dates = np.array(sorted(panel["trade_date"].unique()))
    pick = dates[rng.choice(len(dates), size=min(n_dates, len(dates)), replace=False)]
    sample = panel[panel["trade_date"].isin(pick)]
    ranks = sample.groupby(sample["trade_date"].to_numpy())[cols].rank(pct=True)
    ranks = ranks.fillna(0.5)
    # 直接算 Z^T Z / n，避免 np.corrcoef 在大矩阵上的额外拷贝（83k×1.4k float32 已 ~450MB）
    matrix_values = ranks.to_numpy(dtype="float32")
    matrix_values -= matrix_values.mean(axis=0, keepdims=True)
    scale = matrix_values.std(axis=0, keepdims=True)
    matrix_values /= np.where(scale > 1e-9, scale, 1.0)
    corr = (matrix_values.T @ matrix_values) / max(len(matrix_values), 1)
    del matrix_values
    return np.nan_to_num(corr.astype("float64"))


def main() -> int:
    import tomllib
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=756)
    ap.add_argument("--fdr-q", type=float, default=0.05)
    ap.add_argument("--cluster-threshold", type=float, default=0.9)
    ap.add_argument("--min-abs-ic", type=float, default=0.02)
    ap.add_argument("--min-abs-icir", type=float, default=0.30)
    ap.add_argument("--hlz-t", type=float, default=3.0)
    ap.add_argument("--oos-split", default=None,
                    help="形如 2026-03：用该月之前的数据做准入，再用该月及之后做样本外验证")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]

    t0 = time.time()
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    panel, features, label = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=int(args.days), label_horizon=int(lg["label_horizon"]),
        cleaning_version=cfg["clean_panel"]["cleaning_version"], label_mode=lg["label_mode"],
        startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=False)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    labeled = panel.loc[panel[label].notna()].copy()
    months = sorted(labeled["trade_date"].dt.to_period("M").astype(str).unique())
    print(f"[panel] {len(labeled):,} 行 / {len(features):,} 列 / {len(months)} 个月 "
          f"({months[0]}~{months[-1]}) | {time.time()-t0:.0f}s", flush=True)

    series = month_ic(labeled, list(features), label)
    series = series.reindex(columns=months)
    series.to_csv(OUT / "feature_ic_series.csv")

    stats = pd.DataFrame({
        "ic_mean": series.mean(axis=1),
        "ic_std": series.std(axis=1),
        "months": series.notna().sum(axis=1),
        "positive_share": (series > 0).sum(axis=1) / series.notna().sum(axis=1).replace(0, np.nan),
        "last6_mean": series[months[-6:]].mean(axis=1),
        "first_half": series[months[: len(months) // 2]].mean(axis=1),
        "second_half": series[months[len(months) // 2:]].mean(axis=1),
    })
    stats["icir"] = stats["ic_mean"] / stats["ic_std"].replace(0, np.nan)
    stats["t_nw"] = [newey_west_t(series.loc[c]) for c in series.index]
    stats["t_naive"] = stats["icir"] * np.sqrt(stats["months"])
    from scipy import stats as sps  # type: ignore
    stats["p_nw"] = 2 * (1 - sps.norm.cdf(stats["t_nw"].abs()))
    stats["fdr_significant"] = bh_significant(stats["p_nw"], q=float(args.fdr_q))
    stats["hlz_significant"] = stats["t_nw"].abs() >= float(args.hlz_t)
    stats["sign_stable"] = np.sign(stats["first_half"]) == np.sign(stats["second_half"])
    stats["recent_same_sign"] = np.sign(stats["last6_mean"]) == np.sign(stats["ic_mean"])
    stats["family"] = [family(c) for c in stats.index]
    stats["coverage"] = [float(pd.to_numeric(labeled[c], errors="coerce").notna().mean()) for c in stats.index]
    stats["quality_column"] = [any(tok in c for tok in QUALITY_TOKENS) for c in stats.index]

    # 冗余聚类：|ρ|≥threshold 归簇，代表 = 该簇内 |ICIR| 最大且非质量列
    cols = [c for c in features if c in stats.index]
    print(f"[cluster] 抽样横截面秩相关 {len(cols)} 列…", flush=True)
    corr = redundancy_clusters(labeled, cols, threshold=float(args.cluster_threshold))
    n = len(cols)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    index_of = {c: i for i, c in enumerate(cols)}
    abs_corr = np.abs(corr)
    for i in range(n):
        row = np.where(abs_corr[i] >= float(args.cluster_threshold))[0]
        for j in row:
            if j > i:
                union(i, int(j))
    cluster_id = {c: find(index_of[c]) for c in cols}
    stats["cluster"] = [cluster_id.get(c, -1) for c in stats.index]
    rank_key = stats["icir"].abs().fillna(0.0)
    representative = {}
    for cluster, group in stats[stats["cluster"] >= 0].groupby("cluster"):
        candidates = group[~group["quality_column"]]
        pool = candidates if not candidates.empty else group
        best = pool["icir"].abs().idxmax() if pool["icir"].notna().any() else pool.index[0]
        representative[cluster] = best
    stats["is_representative"] = [representative.get(cl, None) == c for c, cl in zip(stats.index, stats["cluster"])]
    cluster_sizes = stats[stats["cluster"] >= 0].groupby("cluster").size()
    stats["cluster_size"] = [int(cluster_sizes.get(cl, 0)) for cl in stats["cluster"]]

    def verdict(row) -> str:
        if row["quality_column"]:
            return "质量列(不参与准入)"
        if not np.isfinite(row["ic_mean"]) or row["months"] < 6:
            return "样本不足"
        weak = abs(row["ic_mean"]) < float(args.min_abs_ic) or abs(row["icir"]) < float(args.min_abs_icir)
        if weak:
            return "剔除:强度不足"
        if not bool(row["fdr_significant"]):
            return "剔除:多重检验不显著"
        if not bool(row["sign_stable"]):
            return "剔除:符号不稳"
        if not row["is_representative"]:
            return "剔除:冗余(同簇有更强代表)"
        if abs(row["ic_mean"]) >= 0.04 and abs(row["icir"]) >= 0.5:
            return "准入A"
        return "准入B"

    stats["verdict"] = [verdict(row) for _, row in stats.iterrows()]
    stats.sort_values("icir", key=lambda s: -s.abs()).to_csv(OUT / "factor_ic_registry.csv")

    admitted = stats[stats["verdict"].str.startswith("准入")]
    payload = {
        "generated_at": time.strftime("%F %T"),
        "window_months": [months[0], months[-1]],
        "criteria": {"min_abs_ic": args.min_abs_ic, "min_abs_icir": args.min_abs_icir,
                     "fdr_q": args.fdr_q, "hlz_t": args.hlz_t, "cluster_threshold": args.cluster_threshold},
        "admitted_count": int(len(admitted)),
        "admitted_A": sorted(admitted[admitted["verdict"] == "准入A"].index.tolist()),
        "admitted_B": sorted(admitted[admitted["verdict"] == "准入B"].index.tolist()),
        "by_family": admitted.groupby("family").size().to_dict(),
        "direction": {c: int(np.sign(admitted.loc[c, "ic_mean"])) for c in admitted.index},
    }
    (OUT / "factor_ic_admitted.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 与上一版准入清单对比：显著性/稳定性是"保证"而不是"一次性达标"，
    # 每次复跑都要能回答"谁新进来、谁被淘汰、谁退化"，并落到 factor_ic_delta.json。
    previous_path = OUT / "factor_ic_admitted.json.prev"
    delta = None
    if previous_path.is_file():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            prev_set = set(previous.get("admitted_A", [])) | set(previous.get("admitted_B", []))
            now_set = set(payload["admitted_A"]) | set(payload["admitted_B"])
            dropped = sorted(prev_set - now_set)
            delta = {
                "previous_generated_at": previous.get("generated_at"),
                "windows": {"previous": previous.get("window_months"), "current": payload["window_months"]},
                "admitted_previous": len(prev_set), "admitted_current": len(now_set),
                "newly_admitted": sorted(now_set - prev_set), "newly_rejected": dropped,
                "rejected_reasons": {c: str(stats.loc[c, "verdict"]) for c in dropped if c in stats.index},
            }
            (OUT / "factor_ic_delta.json").write_text(json.dumps(delta, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[delta] 相比上一版：新增 {len(delta['newly_admitted'])} / 淘汰 {len(delta['newly_rejected'])}", flush=True)
        except Exception as exc:  # 上一版不可读不应影响本次结论
            print(f"[delta] 跳过（{type(exc).__name__}: {exc}）", flush=True)
    payload["delta_vs_previous"] = delta
    (OUT / "factor_ic_admitted.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    import shutil as _shutil
    _shutil.copyfile(OUT / "factor_ic_admitted.json", previous_path)

    # 样本外有效性：只在"准入期"选列，再在保留期复核（IC 符号是否保留、强度衰减多少）。
    oos = None
    if args.oos_split:
        split = str(args.oos_split)
        in_months = [m for m in months if m < split]
        out_months = [m for m in months if m >= split]
        if len(in_months) >= 6 and len(out_months) >= 3:
            insample = series[in_months]
            oos_series = series[out_months]
            in_mean = insample.mean(axis=1)
            in_icir = in_mean / insample.std(axis=1).replace(0, np.nan)
            in_t = [newey_west_t(insample.loc[c]) for c in series.index]
            in_p = 2 * (1 - sps.norm.cdf(np.abs(in_t)))
            in_fdr = bh_significant(pd.Series(in_p, index=series.index), q=float(args.fdr_q))
            # 准入只看"准入期"：|IC|/ICIR 门槛 + BH-FDR 显著（不额外要求 HLZ，避免样本更短时过度收缩）
            picked = pd.Series((in_mean.abs() >= float(args.min_abs_ic)) & (in_icir.abs() >= float(args.min_abs_icir)) & in_fdr,
                               index=series.index)
            table = pd.DataFrame({
                "in_ic": in_mean[picked].round(4), "in_icir": in_icir[picked].round(3),
                "out_ic": oos_series.mean(axis=1)[picked].round(4),
                "out_icir": (oos_series.mean(axis=1) / oos_series.std(axis=1).replace(0, np.nan))[picked].round(3),
                "out_t": [newey_west_t(oos_series.loc[c]) for c in series.index[picked]],
                "endured_sign": (np.sign(oos_series.mean(axis=1)[picked]) == np.sign(in_mean[picked])),
            })
            table["endured_strong"] = table["endured_sign"] & (table["out_ic"].abs() >= float(args.min_abs_ic))
            table.to_csv(OUT / "factor_ic_oos_holdout.csv")
            oos = {
                "split": split, "in_months": [in_months[0], in_months[-1]], "out_months": [out_months[0], out_months[-1]],
                "admitted": int(picked.sum()),
                "sign_kept_share": round(float(table["endured_sign"].mean()), 4) if len(table) else None,
                "strong_kept_share": round(float(table["endured_strong"].mean()), 4) if len(table) else None,
                "in_ic_mean": round(float(table["in_ic"].mean()), 5) if len(table) else None,
                "out_ic_mean": round(float(table["out_ic"].mean()), 5) if len(table) else None,
                "out_icir_mean": round(float(table["out_icir"].mean()), 4) if len(table) else None,
                "out_t_median": round(float(np.nanmedian(table["out_t"])), 3) if len(table) else None,
            }
            payload["oos_holdout"] = oos
            (OUT / "factor_ic_admitted.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[OOS] 准入期 {in_months[0]}~{in_months[-1]} 选列 {oos['admitted']} → "
                  f"保留期 {out_months[0]}~{out_months[-1]}: 符号保留 {oos['sign_kept_share']:.1%}、"
                  f"强度保留(|IC|≥{args.min_abs_ic}) {oos['strong_kept_share']:.1%}、"
                  f"in_IC {oos['in_ic_mean']:+.4f} → out_IC {oos['out_ic_mean']:+.4f}（out t 中位 {oos['out_t_median']}）", flush=True)

    fam = stats[~stats["quality_column"]].groupby("family").agg(
        columns=("ic_mean", "size"), ic_abs=("ic_mean", lambda s: s.abs().mean()),
        icir_abs_median=("icir", lambda s: s.abs().median()),
        fdr_rate=("fdr_significant", "mean"), hlz_rate=("hlz_significant", "mean"),
        admitted=("verdict", lambda s: int(s.str.startswith("准入").sum())),
        stable_rate=("sign_stable", "mean")).round(4).sort_values("ic_abs", ascending=False)
    fam.to_csv(OUT / "family_significance.csv")

    total = int((~stats["quality_column"]).sum())
    n_fdr = int(stats["fdr_significant"].sum())
    n_hlz = int(stats["hlz_significant"].sum())
    print(f"\n[显著性] 测试列 {total} | BH-FDR(q={args.fdr_q}) 显著 {n_fdr} | t_NW≥{args.hlz_t}（HLZ 门槛）{n_hlz}")
    print(f"[准入] A 档 {len(payload['admitted_A'])} | B 档 {len(payload['admitted_B'])} | 合计 {payload['admitted_count']}"
          f" | 簇数 {len(cluster_sizes)}")
    print("\n[族级]\n" + fam.to_string(), flush=True)
    top = stats[stats["verdict"].str.startswith("准入")].reindex(
        stats[stats["verdict"].str.startswith("准入")]["ic_mean"].abs().sort_values(ascending=False).index).head(25)
    print("\n[准入列 Top25]\n" + top[["family", "ic_mean", "icir", "t_nw", "p_nw", "fdr_significant",
                                      "positive_share", "last6_mean", "months", "verdict"]].round(4).to_string(), flush=True)

    lines = ["# 因子 IC 显著性与有效性协议结果", "",
             f"窗口 {months[0]}~{months[-1]}（{len(months)} 个月）、全 A、20 交易日前瞻、横截面秩 IC，"
             f"月度观测 + Newey-West(lag=1) t 值 + BH-FDR(q={args.fdr_q})。",
             f"数据：`feature_ic_series.csv` / `factor_ic_registry.csv` / `family_significance.csv`；"
             f"准入清单 `factor_ic_admitted.json`。", "",
             f"- 参与检验列：{total}；BH-FDR 显著：{n_fdr}（{n_fdr/max(total,1):.1%}）；"
             f"Harvey-Liu-Zhu 门槛 t≥{args.hlz_t}：{n_hlz}",
             f"- 去冗余后准入：**A 档 {len(payload['admitted_A'])} 列 / B 档 {len(payload['admitted_B'])} 列**"
             f"（{len(cluster_sizes)} 个相关簇内各取一个代表）", "",
             "## 族级", "", "| 族 | 列数 | \\|IC\\| 均值 | \\|ICIR\\| 中位 | FDR 通过率 | HLZ 通过率 | 准入 | 半段同号率 |",
             "|---|---|---|---|---|---|---|---|"]
    for name, row in fam.iterrows():
        lines.append(f"| {name} | {int(row.columns)} | {row.ic_abs:.4f} | {row.icir_abs_median:.3f} | "
                     f"{row.fdr_rate:.0%} | {row.hlz_rate:.0%} | {int(row.admitted)} | {row.stable_rate:.0%} |")
    lines += ["", "## 准入列 Top25", "", "| 列 | 族 | IC 均值 | ICIR | t_NW | p_NW | FDR | 正号月占比 | 近6月 | 月数 | 等级 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, row in top.iterrows():
        lines.append(f"| {name} | {row.family} | {row.ic_mean:+.4f} | {row.icir:+.2f} | {row.t_nw:+.2f} | "
                     f"{row.p_nw:.4f} | {'✔' if row.fdr_significant else '✘'} | {row.positive_share:.0%} | "
                     f"{row.last6_mean:+.4f} | {int(row.months)} | {row.verdict} |")
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[done] {OUT} | 用时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
