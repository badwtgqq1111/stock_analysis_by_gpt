#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拼装 P2.81 元数据集（Phase 4）。

输入：
  * 个股日线（只取 close，用于算未来 20 日标签）
  * 市场因子 / 个股 beta（Phase 1）
  * PIT 板块因子 / 个股相对板块（Phase 2）
  * 策略族聚合特征（Phase 3）

标签：
  fwd_ret_20      = close(t+20) / close(t) - 1
  fwd_excess_20   = fwd_ret_20 - 中证全指同窗口收益
  fwd_positive_20 = fwd_excess_20 > 0

输出：
  assets/data/derived/cn_meta_dataset.parquet
  assets/data/derived/cn_meta_dataset.manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
MARKET = ROOT / "assets/data/derived/cn_market_factors.parquet"
BETA = ROOT / "assets/data/derived/cn_stock_market_beta.parquet"
SECTOR = ROOT / "assets/data/derived/cn_stock_sector_features.parquet"
FAMILY = ROOT / "assets/data/derived/cn_strategy_family_features.parquet"
IFIND_MONTHLY = ROOT / "assets/data/derived/cn_ifind_moneyflow_monthly_features.parquet"
IFIND_DAILY = ROOT / "assets/data/derived/cn_ifind_moneyflow_daily_features.parquet"
CONCEPT_FACTORS = ROOT / "assets/data/derived/cn_concept_factors.parquet"
MONEYFLOW = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
DELISTED = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
OUT = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_meta_dataset.manifest.json"


def _bar_files(start: str, end: str) -> list[str]:
    years = range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1)
    files = []
    for year in years:
        files.extend(str(p) for p in OHLCV.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet"))
    return sorted(set(files))


def load_close(start: str, end: str) -> pd.DataFrame:
    frame = ds.dataset(_bar_files(start, end), format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "stock_code", "close"])
    frame = frame[(frame["trade_date"] >= pd.Timestamp(start)) & (frame["trade_date"] <= pd.Timestamp(end))]
    universe = set(pd.read_parquet(BASIC, columns=["ts_code"])["ts_code"].astype(str))
    universe |= set(pd.read_parquet(DELISTED, columns=["stock_code"])["stock_code"].astype(str))
    frame = frame[frame["stock_code"].isin(universe)]
    frame = frame[frame["close"] > 0].drop_duplicates(["trade_date", "stock_code"], keep="last")
    return frame.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="拼装 P2.81 元数据集")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-12-31")
    args = ap.parse_args()
    started = datetime.now()
    print(f"[meta] 日线 {args.start} ~ {args.end}", flush=True)
    bars = load_close(args.start, args.end)
    bars["fwd_ret_20"] = bars.groupby("stock_code", sort=False)["close"].shift(-20) / bars["close"] - 1
    market = pd.read_parquet(MARKET)
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    market["mkt_fwd_20"] = market["mkt_close"].shift(-20) / market["mkt_close"] - 1
    labels = bars[["trade_date", "stock_code", "close", "fwd_ret_20"]].merge(
        market[["trade_date", "mkt_fwd_20"]], on="trade_date", how="left")
    labels["fwd_excess_20"] = labels["fwd_ret_20"] - labels["mkt_fwd_20"]
    labels["fwd_positive_20"] = (labels["fwd_excess_20"] > 0).astype("int8")
    labels = labels.drop(columns=["close"])

    print("[meta] merge market/beta", flush=True)
    out = labels.merge(
        market[["trade_date", "mkt_ret_5", "mkt_ret_20", "mkt_ret_60",
                "mkt_vol_20", "mkt_vol_60", "mkt_dd_60",
                "mkt_breadth_above_ma20", "mkt_breadth_up",
                "mkt_turnover_z", "mkt_limitup_ratio"]],
        on="trade_date", how="left")
    beta_cols = ["trade_date", "stock_code", "beta_60", "beta_120", "beta_250",
                 "idio_vol_60", "downside_beta_120",
                 "beta_60_x_mkt_ret_20", "beta_60_x_mkt_vol_20"]
    out = out.merge(pd.read_parquet(BETA, columns=beta_cols), on=["trade_date", "stock_code"], how="left")

    print("[meta] merge sector", flush=True)
    sector_cols = ["trade_date", "stock_code", "industry_l2",
                   "sector_ret_20", "sector_rev_20", "sector_dispersion_20",
                   "sector_crowding", "sector_turnover_z", "sector_limitup_density",
                   "stock_vs_sector_ret_20"]
    out = out.merge(pd.read_parquet(SECTOR, columns=sector_cols),
                    on=["trade_date", "stock_code"], how="left")

    print("[meta] merge strategy family", flush=True)
    family = pd.read_parquet(FAMILY)
    family_cols = ["trade_date", "stock_code", "n_selected", "n_families", "best_rank",
                   "mean_strategy_score", "max_strategy_score", "consensus_score"] + [
        c for c in family.columns if c.startswith("family_")]
    out = out.merge(family[family_cols], on=["trade_date", "stock_code"], how="left")
    out["n_selected"] = out["n_selected"].fillna(0).astype("int16")
    out["n_families"] = out["n_families"].fillna(0).astype("int16")
    out["best_rank"] = out["best_rank"].fillna(999).astype("float32")
    fill_cols = ["mean_strategy_score", "max_strategy_score", "consensus_score"] + [
        c for c in out.columns if c.startswith("family_")]
    out[fill_cols] = out[fill_cols].fillna(0.0)
    out["is_strategy_candidate"] = (out["n_selected"] > 0).astype("int8")

    # 本地日频资金流（2024-08 起）：作为短期资金流特征；更早的长期历史用 iFinD 月度
    if MONEYFLOW.exists():
        print("[meta] merge local daily moneyflow", flush=True)
        mf = pd.read_parquet(
            MONEYFLOW,
            columns=["trade_date", "stock_code", "source",
                     "moneyflow_net_z_5d", "moneyflow_positive_ratio_10d", "moneyflow_net_20d"],
        )
        mf = mf[mf["source"] == "moneyflow"].copy()
        mf["trade_date"] = pd.to_datetime(mf["trade_date"]).dt.normalize()
        mf = mf.rename(columns={
            "moneyflow_net_z_5d": "mf_daily_net_z_5d",
            "moneyflow_positive_ratio_10d": "mf_daily_positive_ratio_10d",
            "moneyflow_net_20d": "mf_daily_net_20d",
        }).drop(columns=["source"])
        out = out.merge(mf, on=["trade_date", "stock_code"], how="left")
    else:
        for col in ("mf_daily_net_z_5d", "mf_daily_positive_ratio_10d", "mf_daily_net_20d"):
            out[col] = np.nan

    # iFinD 月度资金流：按 period_end 做 as-of，只使用已经结束的月份
    if IFIND_MONTHLY.exists():
        print("[meta] merge iFinD monthly moneyflow", flush=True)
        monthly = pd.read_parquet(
            IFIND_MONTHLY,
            columns=["period_end", "stock_code", "mf_monthly_ratio", "mf_monthly_z",
                     "mf_monthly_rank", "mf_3m_ratio"],
        )
        monthly["period_end"] = pd.to_datetime(monthly["period_end"]).dt.normalize()
        monthly = monthly.sort_values(["period_end", "stock_code"])
        out = out.sort_values(["trade_date", "stock_code"])
        out = pd.merge_asof(
            out, monthly, left_on="trade_date", right_on="period_end",
            by="stock_code", direction="backward", allow_exact_matches=True)
        out = out.rename(columns={"period_end": "mf_period_end"})
        # 月度数据只到 2024-12；2025 之后不能用 2024-12 的值假装是当月值。
        # 允许 45 天滞后（覆盖次月前几个交易日），超过一律置 NaN。
        stale = out["trade_date"] > out["mf_period_end"] + pd.Timedelta(days=45)
        for col in ("mf_monthly_ratio", "mf_monthly_z", "mf_monthly_rank", "mf_3m_ratio"):
            out.loc[stale, col] = np.nan
        out.loc[stale, "mf_period_end"] = pd.NaT
    else:
        for col in ("mf_monthly_ratio", "mf_monthly_z", "mf_monthly_rank", "mf_3m_ratio"):
            out[col] = np.nan
        out["mf_period_end"] = pd.NaT

    if IFIND_DAILY.exists():
        print("[meta] merge iFinD daily moneyflow", flush=True)
        daily_mf = pd.read_parquet(
            IFIND_DAILY,
            columns=["trade_date", "stock_code", "mf_ifind_daily_ratio", "mf_ifind_daily_z",
                     "mf_ifind_daily_rank", "mf_ifind_5d_ratio", "mf_ifind_20d_ratio"],
        )
        daily_mf["trade_date"] = pd.to_datetime(daily_mf["trade_date"]).dt.normalize()
        out = out.merge(daily_mf, on=["trade_date", "stock_code"], how="left")
    else:
        for col in ("mf_ifind_daily_ratio", "mf_ifind_daily_z", "mf_ifind_daily_rank",
                    "mf_ifind_5d_ratio", "mf_ifind_20d_ratio"):
            out[col] = np.nan

    if CONCEPT_FACTORS.exists():
        print("[meta] merge concept factors", flush=True)
        cf = pd.read_parquet(CONCEPT_FACTORS, columns=["trade_date", "stock_code", "concept_heat",
                                                       "concept_rise", "concept_breadth", "concept_ret5",
                                                       "concept_rank", "n_concepts"])
        out = out.merge(cf, on=["trade_date", "stock_code"], how="left")
        print(f"[meta] concept factors 覆盖 {out['concept_heat'].notna().mean():.1%}", flush=True)

    # 压缩并写出
    float_cols = [c for c in out.columns if c not in
                  ("trade_date", "stock_code", "industry_l2", "n_selected", "n_families",
                   "is_strategy_candidate", "fwd_positive_20", "mf_period_end")]
    for col in float_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float32")
    out = out.dropna(subset=["fwd_excess_20"]).sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "dates": int(out["trade_date"].nunique()),
        "stocks": int(out["stock_code"].nunique()),
        "start": str(out["trade_date"].min().date()),
        "end": str(out["trade_date"].max().date()),
        "label": {
            "fwd_ret_20": "close(t+20)/close(t)-1",
            "fwd_excess_20": "fwd_ret_20 - 000985.CSI 同窗口收益",
            "fwd_positive_20": "fwd_excess_20 > 0",
        },
        "columns": list(out.columns),
        "strategy_candidate_rows": int((out["is_strategy_candidate"] == 1).sum()),
        "path": str(OUT.relative_to(ROOT)),
        "notes": [
            "标签是 T 收盘到 T+20 收盘；执行回测仍需 T+1 可成交口径",
            "策略族特征只覆盖已有归档的日期；非候选日填 0，不伪造策略意见",
            "beta/sector 因子均为 T 日及以前数据；标签在 T+20 才成熟",
            "本地日频 moneyflow 2024-08 起；iFinD 日频 2024-01~08；iFinD 月度 2016-2024，2025 后为空",
        ],
        "seconds": round((datetime.now() - started).total_seconds(), 1),
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("rows", "dates", "stocks", "start", "end",
                                               "strategy_candidate_rows", "seconds")},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
