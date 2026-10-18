#!/usr/bin/env python3
"""P2.74 S1 先验：8 个目标 + 16 维先验（论文口径）。

目标（5/21 日）：累计对数收益、未年化波动、最大回撤、相对等权市场超额。
做法：按年扩展窗口拟合多输出 Ridge（α=20），只用"结果窗口在块开始前已完成"的样本；
输出 8 个标准化预测 + 8 个 log 残差尺度。

用法：
  python3 scripts/p2_74_prior.py --build
  python3 scripts/p2_74_prior.py --evaluate
产物：
  output/research/p2_74_stock_jepa/prior/year=YYYY/part.parquet
  output/research/p2_74_stock_jepa/prior/block_metrics.json
  output/research/p2_74_stock_jepa/prior/g1.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.p2_74_dataset import load_bars, _bar_files  # noqa: E402
from data.ingest.service import is_cn_equity_code  # noqa: E402

FEATURE_DIR = ROOT / "output/research/p2_74_stock_jepa/features"
OUT = ROOT / "output/research/p2_74_stock_jepa/prior"
CONFIG = ROOT / "config/p2_74_stock_jepa.toml"
ALPHA = 20.0
CLIP = 8.0
BLOCKS = list(range(2016, 2027))


def load_features() -> pd.DataFrame:
    files = sorted(FEATURE_DIR.glob("year=*/part.parquet"))
    if not files:
        raise SystemExit("没有特征分片；先跑 scripts/p2_74_dataset.py build")
    frames = [pd.read_parquet(p) for p in files]
    frame = pd.concat(frames, ignore_index=True)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def build_labels() -> pd.DataFrame:
    """逐股票计算 5/21 日前瞻目标（向量化，避免逐样本循环）。"""
    files = _bar_files(2013, 2026)
    import pyarrow.dataset as ds
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars["stock_code"] = bars["stock_code"].astype(str)
    bars = bars.sort_values(["stock_code", "trade_date"])
    bars = bars[bars["stock_code"].map(is_cn_equity_code)]
    # 等权市场日对数收益
    bars["logret"] = np.log(bars["close"]).groupby(bars["stock_code"]).diff()
    ew = bars.groupby("trade_date")["logret"].mean()

    records = []
    for n, (code, sub) in enumerate(bars.groupby("stock_code", sort=False), 1):
        s = sub.set_index("trade_date")["close"]
        lr = np.log(s).diff()
        out = pd.DataFrame(index=s.index)
        out["stock_code"] = code
        out["tgt_end"] = pd.Series(s.index, index=s.index).shift(-21)
        for h in (5, 21):
            out[f"R{h}"] = np.log(s.shift(-h) / s)
            out[f"V{h}"] = lr.shift(-1).rolling(h, min_periods=h).std().shift(-(h - 1))
            hi = s.shift(-1).rolling(h, min_periods=h).max().shift(-(h - 1))
            lo = s.shift(-1).rolling(h, min_periods=h).min().shift(-(h - 1))
            out[f"M{h}"] = np.log(lo / hi)
        records.append(out.reset_index())
        if n % 1000 == 0:
            print(f"[labels] {n} codes", flush=True)
    labels = pd.concat(records, ignore_index=True)
    labels.columns = ["trade_date", "stock_code", "tgt_end", "R5", "V5", "M5", "R21", "V21", "M21"]
    # 市场超额：等权市场同窗口累计对数收益
    for h in (5, 21):
        mkt = ew.shift(-1).rolling(h, min_periods=h).sum().shift(-(h - 1))
        labels[f"E{h}"] = labels[f"R{h}"] - labels["trade_date"].map(mkt)
    return labels


def _standardize_fit(x: np.ndarray, clip: float = CLIP):
    mean = np.nanmean(x, axis=0)
    std = np.nanstd(x, axis=0)
    std = np.where(np.isfinite(std) & (std > 0), std, 1.0)
    z = (x - mean) / std
    z = np.clip(z, -clip, clip)
    z = np.nan_to_num(z, nan=0.0, posinf=clip, neginf=-clip)
    return z, mean, std


def build() -> dict:
    features = load_features()
    labels = build_labels()
    frame = features.merge(labels, on=["trade_date", "stock_code"], how="left")
    feature_cols = [c for c in features.columns if c not in ("trade_date", "stock_code")]
    target_cols = ["R5", "V5", "M5", "E5", "R21", "V21", "M21", "E21"]
    print(f"[merge] rows={len(frame):,} labels={frame[target_cols].notna().all(axis=1).mean():.1%}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    metrics = {"alpha": ALPHA, "features": feature_cols, "targets": target_cols, "blocks": []}
    for year in BLOCKS:
        start = pd.Timestamp(f"{year}-01-01")
        end = pd.Timestamp(f"{year + 1}-01-01")
        train = frame[(frame["tgt_end"] < start) & frame[target_cols].notna().all(axis=1)]
        block = frame[(frame["trade_date"] >= start) & (frame["trade_date"] < end)].copy()
        if len(train) < 10_000 or block.empty:
            continue
        # 每年训练的样本上限 300 万（论文的 thinning 纪律）
        if len(train) > 3_000_000:
            train = train.sample(3_000_000, random_state=year)
        x_train = train[feature_cols].to_numpy(dtype=np.float64)
        x_train, x_mean, x_std = _standardize_fit(x_train)
        y_train = train[target_cols].to_numpy(dtype=np.float64)
        y_mean = y_train.mean(axis=0)
        y_std = y_train.std(axis=0)
        y_std = np.where(np.isfinite(y_std) & (y_std > 0), y_std, 1.0)
        yz = (y_train - y_mean) / y_std
        # 多输出 Ridge 闭式解
        gram = x_train.T @ x_train + ALPHA * np.eye(x_train.shape[1])
        coef = np.linalg.solve(gram, x_train.T @ yz)
        resid = yz - x_train @ coef
        resid_scale = np.sqrt(np.maximum((resid ** 2).mean(axis=0), 1e-8))

        xb = block[feature_cols].to_numpy(dtype=np.float64)
        xbz = np.clip((xb - x_mean) / x_std, -CLIP, CLIP)
        xbz = np.nan_to_num(xbz, nan=0.0, posinf=CLIP, neginf=-CLIP)
        pred = np.clip(xbz @ coef, -CLIP, CLIP)
        log_scale = np.log(np.maximum(resid_scale, 1e-4))
        prior = np.concatenate([pred, np.tile(log_scale, (len(block), 1))], axis=1)
        cols = [f"prior_{i}" for i in range(8)] + [f"logres_{i}" for i in range(8)]
        out = block[["trade_date", "stock_code"]].copy()
        out[cols] = prior.astype("float32")
        shard = OUT / f"year={year}"
        shard.mkdir(parents=True, exist_ok=True)
        out.to_parquet(shard / "part.parquet", index=False)
        metrics["blocks"].append({
            "year": year, "n_train": int(len(train)), "n_block": int(len(block)),
            "x_mean": x_mean.tolist(), "x_std": x_std.tolist(),
            "y_mean": y_mean.tolist(), "y_std": y_std.tolist(),
            "resid_scale": resid_scale.tolist(),
        })
        print(f"[prior] {year} train={len(train):,} block={len(block):,}", flush=True)
    (OUT / "block_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    return metrics


def evaluate() -> dict:
    """G1：S1-only 验证期 RankIC（预测 R21 / E21 vs 实现值）。"""
    files = sorted(FEATURE_DIR.glob("year=*/part.parquet"))
    priors = sorted(OUT.glob("year=*/part.parquet"))
    if not priors:
        raise SystemExit("没有先验产物；先 --build")
    labels = build_labels()
    prior = pd.concat([pd.read_parquet(p) for p in priors], ignore_index=True)
    prior["trade_date"] = pd.to_datetime(prior["trade_date"])
    frame = prior.merge(labels, on=["trade_date", "stock_code"], how="left")
    val = frame[(frame["trade_date"] >= "2022-01-01") & (frame["trade_date"] <= "2023-12-31")]
    out = {}
    for name, pred, target in (("R21", "prior_4", "R21"), ("E21", "prior_7", "E21")):
        ics = []
        for _, day in val.groupby("trade_date"):
            sub = day[[pred, target]].dropna()
            if len(sub) >= 30 and sub[pred].nunique() > 3:
                ics.append(float(sub[pred].corr(sub[target], method="spearman")))
        out[name] = {"mean_rank_ic": float(np.mean(ics)) if ics else None, "n_dates": len(ics)}
        print(name, out[name], flush=True)
    (OUT / "g1.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.74 S1 先验")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args()
    if args.evaluate:
        evaluate()
    else:
        build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
