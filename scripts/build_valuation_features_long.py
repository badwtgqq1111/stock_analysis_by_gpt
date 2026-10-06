#!/usr/bin/env python3
"""构建**长样本**估值特征面板（2014 起），用于给 valuation sleeve 拉长复验样本。

背景：clean panel 只覆盖 2025-03 起（354 个交易日），估值腿虽然通过了门槛，但样本太短。
ClickHouse `valuation_snapshot_meta` 有 2014 起的 pe/pb/ps/market_cap（实测 pe/pb 每年覆盖 95%+），
本脚本把它算成**行业分位**（估值因子的正确用法），落到
`assets/data/meta/cn_valuation_features_long.parquet`。

已知限制（必须一起引用）：
  * `dividend_yield` 在该源为 0/空 → 长面板**不含股息率**（spec 里该列在长样本上被剔除）；
  * 行业分类用 registry 的**当前** industry_l1（非 PIT 历史）→ 属近似；
  * 行业分位按 `trade_date × industry_l1` 计算，行业缺失时退回全市场分位（并打 `industry_missing=1`）。

用法：
  uv run python scripts/build_valuation_features_long.py --start 2014 --end 2026
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "assets/data/meta/cn_valuation_features_long.parquet"


def load_industry() -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = sorted(str(p) for p in (ROOT / "assets/data/meta/stock_info_registry").glob("**/*.parquet"))
    dataset = ds.dataset(files, format="parquet")
    frame = dataset.to_table(columns=["stock_code", "industry_l1"]).to_pandas()
    frame["stock_code"] = frame["stock_code"].astype(str)
    return frame.dropna(subset=["industry_l1"]).drop_duplicates("stock_code")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建长样本估值特征面板")
    parser.add_argument("--start", type=int, default=2014)
    parser.add_argument("--end", type=int, default=2026)
    args = parser.parse_args()

    import clickhouse_connect

    client = clickhouse_connect.get_client(host="localhost", port=8123, username="default",
                                           password="quant2024", database="quant")
    industry = load_industry()
    industry_map = dict(zip(industry["stock_code"], industry["industry_l1"]))

    frames = []
    for year in range(args.start, args.end + 1):
        frame = client.query_df(
            f"""select trade_date, stock_code, pe_ratio, pb_ratio, ps_ratio, market_cap
                from valuation_snapshot_meta
                where market='CN' and asset_type='equity' and market_cap is not null
                  and toYear(trade_date) = {year}"""
        )
        if frame.empty:
            continue
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame["stock_code"] = frame["stock_code"].astype(str)
        frame = frame.drop_duplicates(["trade_date", "stock_code"], keep="last")
        frame["industry_l1"] = frame["stock_code"].map(industry_map)
        frame["industry_missing"] = frame["industry_l1"].isna().astype(int)
        frame["industry_l1"] = frame["industry_l1"].fillna("__ALL__")
        frame["log_market_cap"] = np.log(frame["market_cap"].clip(lower=1e-6))
        for col in ("pe_ratio", "pb_ratio", "ps_ratio"):
            frame.loc[frame[col] <= 0, col] = np.nan          # 负 PE/PB 无经济含义，按缺失处理
        grp = frame.groupby(["trade_date", "industry_l1"])
        for col, name in (("pe_ratio", "pe_ind_pct"), ("pb_ratio", "pb_ind_pct"),
                          ("ps_ratio", "ps_ind_pct"), ("log_market_cap", "size_ind_pct")):
            frame[name] = grp[col].rank(pct=True)
        frames.append(frame[["trade_date", "stock_code", "pe_ind_pct", "pb_ind_pct", "ps_ind_pct",
                             "size_ind_pct", "industry_missing"]])
        print(f"[long-valuation] {year}: {len(frame):,} 行", flush=True)

    merged = pd.concat(frames, ignore_index=True).sort_values(["trade_date", "stock_code"])
    merged["pe_pb_combo"] = merged[["pe_ind_pct", "pb_ind_pct"]].mean(axis=1)
    merged["missing_count"] = merged[["pe_ind_pct", "pb_ind_pct", "ps_ind_pct"]].isna().sum(axis=1)
    merged["trade_date"] = pd.to_datetime(merged["trade_date"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUT, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(merged)), "codes": int(merged["stock_code"].nunique()),
        "window": [str(merged["trade_date"].min().date()), str(merged["trade_date"].max().date())],
        "columns": list(merged.columns),
        "coverage": {c: round(float(merged[c].notna().mean()), 4)
                     for c in ("pe_ind_pct", "pb_ind_pct", "ps_ind_pct", "size_ind_pct")},
        "limitations": ["dividend_yield 在该源为空 → 长面板不含股息率分位",
                        "industry_l1 用 registry 当前值（非 PIT）",
                        "行业缺失时按全市场分位（industry_missing=1）"],
        "out": str(OUT),
    }
    (OUT.parent / "cn_valuation_features_long.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
