#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P2.81 候选池 shadow：B2 排序 + 行业上限 + 流动性过滤。

输入：
  output/research/p2_81/predictions/*.parquet（walk-forward 保存的测试预测）
  assets/data/derived/cn_stock_sector_features.parquet（PIT 行业）
  assets/data/clean/ohlcv/...（成交额，算 20 日均额）
  output/research/zt_board/zt_board_panel.parquet（涨停，可选剔除）

组合：
  * 每 20 个交易日换仓；
  * TopN 等权，最多 2 只/申万 L1；
  * 可选 20 日均额 ≥ 20M；
  * 双边 31bp；
  * 收益用 fwd_excess_20 标签模拟，不是逐笔成交。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRED = ROOT / "output/research/p2_81/predictions"
SECTOR = ROOT / "assets/data/derived/cn_stock_sector_features.parquet"
OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
ZT = ROOT / "output/research/zt_board/zt_board_panel.parquet"
META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
OUT = ROOT / "output/research/p2_81"
COST = 2 * 15.5 / 1e4


def load_predictions(model: str) -> pd.DataFrame:
    frames = []
    for path in sorted(PRED.glob(f"*_{model}.parquet")):
        frame = pd.read_parquet(path)
        frame["fold"] = path.stem.replace(f"_{model}", "")
        frames.append(frame)
    if not frames:
        raise SystemExit(f"没有 {model} 预测；先跑 scripts/p2_81_walk_forward.py")
    return pd.concat(frames, ignore_index=True)


def load_context() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sector = pd.read_parquet(SECTOR, columns=["trade_date", "stock_code", "industry_l1"])
    sector["trade_date"] = pd.to_datetime(sector["trade_date"]).dt.normalize()
    sector = sector.drop_duplicates(["trade_date", "stock_code"], keep="last")

    files = [str(p) for year in range(2018, 2027) for p in OHLCV.glob(
        f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce").dt.normalize()
    bars["amount"] = pd.to_numeric(bars["amount"], errors="coerce")
    bars = bars.dropna(subset=["trade_date", "stock_code", "amount"]).sort_values(["stock_code", "trade_date"])
    bars["amount_ma20"] = bars.groupby("stock_code", sort=False)["amount"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    liquidity = bars[["trade_date", "stock_code", "amount_ma20"]]

    zt = pd.read_parquet(ZT, columns=["trade_date", "stock_code", "is_zt"])
    zt["trade_date"] = pd.to_datetime(zt["trade_date"]).dt.normalize()
    return sector, liquidity, zt


def pick_candidates(group: pd.DataFrame, n: int, max_per_industry: int | None,
                    min_amount: float, exclude_zt: bool, min_mktcap: float = 0.0) -> pd.DataFrame:
    group = group.dropna(subset=["pred", "fwd_excess_20"]).copy()
    if min_amount > 0:
        group = group[group["amount_ma20"].fillna(0) >= min_amount]
    if min_mktcap > 0 and "mktcap" in group.columns:
        group = group[group["mktcap"].fillna(0) >= min_mktcap]
    if exclude_zt:
        group = group[group["is_zt"].fillna(0) == 0]
    group = group.sort_values("pred", ascending=False)
    if max_per_industry is None:
        return group.head(n)
    picked, counts = [], {}
    for _, row in group.iterrows():
        industry = str(row.get("industry_l1") or "UNKNOWN")
        if industry in ("", "nan", "None", "UNKNOWN"):
            continue
        if counts.get(industry, 0) >= max_per_industry:
            continue
        picked.append(row)
        counts[industry] = counts.get(industry, 0) + 1
        if len(picked) >= n:
            break
    return pd.DataFrame(picked)


def run_variant(name: str, pred: pd.DataFrame, sector: pd.DataFrame, liquidity: pd.DataFrame,
                zt: pd.DataFrame, market: pd.DataFrame, n: int, max_ind: int | None,
                min_amount: float, exclude_zt: bool, min_mktcap: float = 0.0) -> dict:
    data = pred.merge(sector, on=["trade_date", "stock_code"], how="left")
    data = data.merge(liquidity, on=["trade_date", "stock_code"], how="left")
    data = data.merge(zt, on=["trade_date", "stock_code"], how="left")
    periods = []
    for fold, block in data.groupby("fold", sort=False):
        dates = sorted(block["trade_date"].unique())
        for day in dates[::20]:
            group = block[block["trade_date"] == day]
            picked = pick_candidates(group, n, max_ind, min_amount, exclude_zt, min_mktcap)
            if len(picked) < max(5, n // 4):
                continue
            excess = float(picked["fwd_excess_20"].mean() - COST)
            mkt = float(market.loc[market["trade_date"] == day, "mkt_fwd_20"].iloc[0]) \
                if (market["trade_date"] == day).any() else 0.0
            periods.append({"fold": fold, "trade_date": day, "n": len(picked),
                            "excess": excess, "absolute": excess + mkt, "market": mkt})
    frame = pd.DataFrame(periods)
    if frame.empty:
        return {"name": name, "n_periods": 0}
    excess_nav = (1 + frame["excess"]).cumprod()
    abs_nav = (1 + frame["absolute"]).cumprod()
    market_nav = (1 + frame["market"]).cumprod()
    peak = np.maximum.accumulate(np.r_[1.0, excess_nav.to_numpy()])
    years = max(len(frame) * 20 / 252, 1e-9)
    by_fold = frame.groupby("fold")["excess"].mean().to_dict()
    return {
        "name": name, "n_periods": int(len(frame)),
        "avg_holdings": float(frame["n"].mean()),
        "mean_excess_20d": float(frame["excess"].mean()),
        "hit_rate": float((frame["excess"] > 0).mean()),
        "cumulative_excess": float(excess_nav.iloc[-1] - 1),
        "annualized_excess": float(excess_nav.iloc[-1] ** (1 / years) - 1),
        "max_drawdown_excess": float((np.r_[1.0, excess_nav.to_numpy()] / peak - 1).min()),
        "cumulative_absolute": float(abs_nav.iloc[-1] - 1),
        "cumulative_market": float(market_nav.iloc[-1] - 1),
        "by_fold_mean_excess": {str(k): float(v) for k, v in by_fold.items()},
        "periods": [{**row, "trade_date": str(row["trade_date"].date())} for row in frame.to_dict("records")],
    }


def main() -> int:
    sector, liquidity, zt = load_context()
    market = pd.read_parquet(META, columns=["trade_date", "mkt_fwd_20"]).drop_duplicates("trade_date")
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    b0 = load_predictions("B0_beta")
    b2 = load_predictions("B2_market_sector")
    b3 = load_predictions("B3_all")
    ensemble = b0[["trade_date", "stock_code", "fwd_excess_20", "pred", "fold"]].merge(
        b2[["trade_date", "stock_code", "pred"]], on=["trade_date", "stock_code"],
        suffixes=("_b0", "_b2"), how="inner")
    ensemble["pred"] = (
        ensemble.groupby("trade_date")["pred_b0"].rank(pct=True)
        + ensemble.groupby("trade_date")["pred_b2"].rank(pct=True)
    ) / 2.0
    ensemble = ensemble[["trade_date", "stock_code", "fwd_excess_20", "pred", "fold"]]
    variants = [
        ("B0_top20", b0, 20, None, 0, False),
        ("B2_top20", b2, 20, None, 0, False),
        ("B2_top50", b2, 50, None, 0, False),
        ("B2_top20_ind2", b2, 20, 2, 0, False),
        ("B2_top50_ind2", b2, 50, 2, 0, False),
        ("B2_top20_ind2_liq20m", b2, 20, 2, 20_000_000, True),
        ("B2_top50_ind2_liq20m", b2, 50, 2, 20_000_000, True),
        ("B3_top50_ind2_liq20m", b3, 50, 2, 20_000_000, True),
        ("B0+B2_top20_ind2_liq20m", ensemble, 20, 2, 20_000_000, True),
        ("B0+B2_top50_ind2_liq20m", ensemble, 50, 2, 20_000_000, True),
    ]
    results = [run_variant(name, pred, sector, liquidity, zt, market, n, ind, liq, ex)
               for name, pred, n, ind, liq, ex in variants]
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "cost_round_trip": COST, "rebalance": "every 20 trading days",
               "results": results}
    (OUT / "shadow.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    curve_rows = []
    for row in results:
        for period in row.get("periods", []):
            curve_rows.append({"variant": row["name"], **period})
    pd.DataFrame(curve_rows).to_csv(OUT / "shadow_curve.csv", index=False)
    lines = ["# P2.81 候选池 shadow（B2 排序）", "",
             f"生成 {payload['generated_at']}｜20 日换仓｜双边 31bp｜研究回测，不是实盘", "",
             "| 组合 | 期数 | 平均持仓 | 20日净超额 | 命中率 | 累计超额 | 年化超额 | 超额回撤 | 累计绝对 | 同期中证500 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in results:
        lines.append(
            f"| {row['name']} | {row['n_periods']} | {row['avg_holdings']:.1f} | "
            f"{row['mean_excess_20d']*100:+.3f}% | {row['hit_rate']*100:.1f}% | "
            f"{row['cumulative_excess']*100:+.2f}% | {row['annualized_excess']*100:+.2f}% | "
            f"{row['max_drawdown_excess']*100:.1f}% | {row['cumulative_absolute']*100:+.2f}% | "
            f"{row['cumulative_market']*100:+.2f}% |")
    lines += ["", "### 分折平均净超额（20日）", "",
              "| 组合 | F1 2023 | F2 2024 | F3 2025-26 |", "|---|---:|---:|---:|"]
    for row in results:
        folds = row.get("by_fold_mean_excess", {})
        lines.append(f"| {row['name']} | " + " | ".join(
            f"{folds.get(k, float('nan'))*100:+.2f}%" for k in ("F1_2023", "F2_2024", "F3_2025_26")) + " |")
    lines += ["", "> 研究回测：用 20 日标签模拟，未逐笔建模涨跌停/停牌/整手；不是生产收益。"]
    (OUT / "SHADOW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
