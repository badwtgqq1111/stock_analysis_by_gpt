#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L9 第二步：把**退市股**并为特征面板 v4（量价/市值类特征可算，估值/财务缺失）。

为什么：`P1_25 §11` 量化了幸存者偏差（域内等权 +3.84%/年），但那只修了**被动基准**；
因子级的域/IC 仍只在"在市宇宙"里成立。这一步把退市股并进面板，让**域**（以及任何按市值/量价打分的
逻辑）能看见它们。

能算什么（退市表自带 qfq 收盘、成交量、股本、市值）：
  ret_5/20/120/250、mom_12m_skip_1m、rev_1m、rev_5d、vol_20、mdd_60、ma20_dev、ma60_dev、pos_52w、
  mktcap / log_mktcap / circ_mktcap / circ_ratio / turnover_daily、listed_days（用退市表首条近似）
**算不了什么**（诚实标注）：`pe_ind_pct / pb_ind_pct / pe_pb_combo / ps_ind_pct`（无原始估值）、
`roe_ind_pct/roa_ind_pct/…` 全部财务类、`size_ind_pct`（依赖行业分类）、`rps_20/rps_60`（沿用 v3 口径，
不重算以免两套排名混用）。→ 因此**因子手臂仍然选不到退市股**（打分列为 NaN），只有"域级"逻辑能覆盖它们。

用法:
  uv run python scripts/build_panel_v4_delisted.py
输出:
  assets/data/meta/cn_sleeve_features_long_v4.parquet + .manifest.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "assets/data/meta"
V3 = META / "cn_sleeve_features_long_v3.parquet"
DELISTED = META / "cn_delisted_daily.parquet"
OUT = META / "cn_sleeve_features_long_v4.parquet"
BAD_BOARD = ("688", "300", "301", "8", "4", "920", "689")


def main() -> int:
    v3 = pd.read_parquet(V3)
    v3["trade_date"] = pd.to_datetime(v3["trade_date"])
    schema = list(v3.columns)
    print(f"[v4] v3 面板 {len(v3):,} 行 / {v3.shape[1]} 列", flush=True)

    # 退市表没有 float_share 列（实测列名：close/circ_mv/total_mv/total_share/turnover_rate…），
    # 流通市值直接用 circ_mv（单位万元）。
    dl = pd.read_parquet(DELISTED, columns=["stock_code", "trade_date", "close", "high", "low",
                                            "volume", "total_share", "circ_mv", "total_mv"])
    dl["trade_date"] = pd.to_datetime(dl["trade_date"])
    dl = dl[dl["trade_date"] >= pd.Timestamp("2015-01-01")].sort_values(["stock_code", "trade_date"])
    g = dl.groupby("stock_code", sort=False)

    dl["ret_5"] = g["close"].pct_change(5)
    dl["ret_20"] = g["close"].pct_change(20)
    dl["ret_120"] = g["close"].pct_change(120)
    dl["ret_250"] = g["close"].pct_change(250)
    dl["mom_12m_skip_1m"] = g["close"].pct_change(250) - g["close"].pct_change(20)
    dl["rev_1m"] = -dl["ret_20"]
    dl["rev_5d"] = -g["close"].pct_change(5)
    dl["vol_20"] = g["close"].transform(lambda s: s.pct_change().rolling(20).std())
    dl["mdd_60"] = g["close"].transform(lambda s: (s / s.rolling(60, min_periods=20).max() - 1).rolling(60, min_periods=20).min())
    dl["ma20_dev"] = dl["close"] / g["close"].transform(lambda s: s.rolling(20).mean()) - 1
    dl["ma60_dev"] = dl["close"] / g["close"].transform(lambda s: s.rolling(60).mean()) - 1
    dl["pos_52w"] = g["close"].transform(
        lambda s: (s - s.rolling(252, min_periods=60).min()) /
                  (s.rolling(252, min_periods=60).max() - s.rolling(252, min_periods=60).min()).replace(0, np.nan))
    dl["mktcap"] = dl["close"] * dl["total_share"] * 1e4
    dl["log_mktcap"] = np.log(dl["mktcap"].clip(lower=1.0))
    dl["circ_mktcap"] = dl["circ_mv"] * 1e4
    dl["circ_ratio"] = dl["circ_mktcap"] / dl["mktcap"]
    dl["turnover_daily"] = dl["volume"] / dl["total_share"]
    first = g["trade_date"].transform("min")
    dl["listed_days"] = (dl["trade_date"] - first).dt.days.astype(float)   # 近似（表内首条起算）
    dl["delisted_flag"] = True
    dl["in_panel_v3"] = False

    keep = [c for c in schema if c in dl.columns] + ["delisted_flag", "in_panel_v3"]
    part = dl[keep].copy()
    for col in schema:
        if col not in part.columns:
            part[col] = np.nan
    part = part[schema + ["delisted_flag", "in_panel_v3"]]
    v3 = v3.assign(delisted_flag=False, in_panel_v3=True)

    merged = pd.concat([v3, part], ignore_index=True).sort_values(["trade_date", "stock_code"])
    # 市值分位在**并集**上重算（这是 v4 的关键差异：域现在能看到退市股）
    merged["mktcap_rank"] = merged.groupby("trade_date")["mktcap"].rank(pct=True)
    merged["turnover_rank"] = merged.groupby("trade_date")["turnover_daily"].rank(pct=True)

    code = merged["stock_code"].astype(str)
    board_ok = ~code.str.startswith(BAD_BOARD)
    age_ok = merged["listed_days"] >= 365
    merged["in_domain_v4"] = board_ok & age_ok & (merged["mktcap_rank"] <= 0.4)

    added = int(part.shape[0])
    dom = merged[merged["in_domain_v4"]]
    per_day = dom.groupby("trade_date").agg(n=("stock_code", "size"),
                                            dead=("delisted_flag", "sum"))
    print(f"[v4] 并入退市股 {added:,} 行 / {dl['stock_code'].nunique()} 只；总 {len(merged):,} 行 / {merged.shape[1]} 列", flush=True)
    print(f"[v4] 域内每日平均 {per_day['n'].mean():.0f} 只（其中退市股 {per_day['dead'].mean():.1f} 只，"
          f"占比 {per_day['dead'].sum() / per_day['n'].sum():.2%}）", flush=True)

    merged.to_parquet(OUT, index=False)
    OUT.with_suffix(".manifest.json").write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(merged)), "fields": int(merged.shape[1]),
        "delisted_rows_added": added, "delisted_stocks": int(dl["stock_code"].nunique()),
        "computable_for_delisted": ["ret_*", "mom_12m_skip_1m", "rev_*", "vol_20", "mdd_60",
                                    "ma20_dev", "ma60_dev", "pos_52w", "mktcap", "log_mktcap",
                                    "circ_mktcap", "circ_ratio", "turnover_daily", "listed_days(近似)"],
        "missing_for_delisted": ["pe_ind_pct", "pb_ind_pct", "pe_pb_combo", "size_ind_pct",
                                 "roe/roa/…全部财务类", "rps_20", "rps_60"],
        "domain": {"avg_size": float(per_day["n"].mean()), "avg_delisted": float(per_day["dead"].mean()),
                   "delisted_share": float(per_day["dead"].sum() / per_day["n"].sum())},
        "sources": {"v3": str(V3), "delisted": str(DELISTED)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v4] 写出 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
