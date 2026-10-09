#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ST 过滤的 PIT 化审计：把「按当前名称」与「按 as-of 当时名称」两个口径放在同一把尺子上量。

回答三个问题
------------
1. **量级**：在策略域（主板 + 上市满 1 年 + 市值分位 ≤ 40%）里，两个口径每个月各剔多少只？差异有多大？
2. **方向**：差异来自哪些票？把域内每月所有票分成四桶，看各自的 20 日前向收益：
   - `D_clean`          两口径都不剔（正常票）
   - `B_only_pit`       仅 PIT 剔（**当时**就是 ST/退）→ 现行「当前名称」口径**漏放**的票
   - `A_only_current`   仅当前剔（**后来**才变 ST/退）→ 现行口径靠"未来信息"多剔的票（前视）
   - `C_both`           两口径都剔
3. **后果**：把两个口径分别接进 L3 回测域，端到端差多少？（由
   `scripts/llm_sleeve_oos_backtest.py --st-filter none|pit|current` 产出，本脚本只印提示）

用法
----
  uv run python scripts/st_pit_audit.py                       # 写 evidence 目录
  uv run python scripts/st_pit_audit.py --out-dir <dir>

产物
----
  <out>/st_pit_vs_current_by_month.csv   每月两口径剔除只数 / 重合 / 覆盖率
  <out>/st_bucket_fwd20.csv              四桶 + ST/退拆分的 fwd20 分布
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.universe_pit import is_risk_name, load_namechange, risk_asof  # noqa: E402

PANEL = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
BAD_BOARD = ("688", "300", "301", "8", "4", "920", "689")
SIZE_MAX = 0.40


def load_domain_panel() -> pd.DataFrame:
    panel = pd.read_parquet(
        PANEL, columns=["stock_code", "trade_date", "listed_days", "mktcap_rank"]
    )
    panel["stock_code"] = panel["stock_code"].astype(str)
    return panel


def attach_fwd20(panel: pd.DataFrame) -> pd.DataFrame:
    """20 日前向收益（与 llm_factor_mining.load_panel 同一算法：close.shift(-21)/close.shift(-1)-1）。"""
    import pyarrow.dataset as ds

    files = sorted(
        str(p) for p in BARS.glob("exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")
    )
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]
    ).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    close = bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    fwd = (close.shift(-21) / close.shift(-1) - 1.0).stack().rename("fwd20").reset_index()
    return panel.merge(fwd, on=["trade_date", "stock_code"], how="left")


def main() -> int:
    ap = argparse.ArgumentParser(description="ST 过滤 PIT 化审计")
    ap.add_argument("--out-dir", default=str(ROOT.parent / "artifacts/st_pit_namechange/evidence"))
    ap.add_argument("--size-max", type=float, default=SIZE_MAX)
    ap.add_argument("--anchor", default="start", choices=["start", "ann"])
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nc = load_namechange()
    if len(nc) == 0:
        raise SystemExit("缺 assets/data/meta/cn_stock_namechange.parquet → 先跑 scripts/backfill_cn_namechange.py")
    cur = dict(
        zip(
            pd.read_parquet(BASIC, columns=["ts_code"])["ts_code"].astype(str),
            pd.read_parquet(BASIC, columns=["name"])["name"].astype(str),
        )
    )

    panel = attach_fwd20(load_domain_panel())
    last_days = panel.groupby(panel["trade_date"].dt.to_period("M"))["trade_date"].max().sort_values()

    monthly, buckets = [], []
    for day in last_days:
        snap = panel[panel["trade_date"] == day]
        codes = snap["stock_code"]
        dom = (
            (~codes.str.startswith(BAD_BOARD))
            & (snap["listed_days"].fillna(0) >= 365)
            & (snap["mktcap_rank"] <= args.size_max).fillna(False)
        )
        d = snap[dom].copy()
        if len(d) == 0:
            continue
        res = risk_asof(nc, list(pd.unique(d["stock_code"])), day, anchor=args.anchor).reindex(d["stock_code"])
        res.index = d.index
        pit_risk = res["is_risk"].fillna(False).to_numpy()
        cur_risk = d["stock_code"].map(lambda c: is_risk_name(cur.get(c, ""))).to_numpy()
        d["pit_st"], d["pit_delist"] = res["is_st"].fillna(False), res["is_delist"].fillna(False)
        d["cur_st"] = [("ST" in cur.get(c, "").upper()) for c in d["stock_code"]]
        d["cur_delist"] = ["退" in cur.get(c, "") for c in d["stock_code"]]
        d["_bucket"] = np.select(
            [pit_risk & ~cur_risk, ~pit_risk & cur_risk, pit_risk & cur_risk],
            ["B_only_pit", "A_only_current", "C_both"], default="D_clean",
        )
        monthly.append(
            {
                "day": day, "domain": int(len(d)), "pit": int(pit_risk.sum()), "current": int(cur_risk.sum()),
                "both": int((pit_risk & cur_risk).sum()),
                "only_pit": int((pit_risk & ~cur_risk).sum()),
                "only_current": int((~pit_risk & cur_risk).sum()),
                "pit_coverage": round(float(res["covered"].fillna(False).mean()), 4),
            }
        )
        buckets.append(d[["_bucket", "fwd20", "pit_st", "pit_delist", "cur_st", "cur_delist"]])

    mdf = pd.DataFrame(monthly)
    mdf.to_csv(out_dir / "st_pit_vs_current_by_month.csv", index=False)
    allr = pd.concat(buckets)

    # ---- 区间表自身的覆盖率缺口（决定 PIT 判定能信到什么程度）----
    real = panel[panel["listed_days"] >= 0]  # 排除 v4 面板的"未上市占位行"（listed_days<0、市值全空）
    gaps = []
    for day in last_days:
        codes = [
            c for c in pd.unique(real.loc[real["trade_date"] == day, "stock_code"])
            if not (c.endswith(".SH") and c.startswith("000"))  # 上证指数序列本就无更名记录
        ]
        if not codes:
            continue
        r = risk_asof(nc, list(codes), day, anchor=args.anchor)
        r["cur"] = [is_risk_name(cur.get(c, "")) for c in r.index]
        gap = ~r["covered"]
        gaps.append(
            {
                "day": day, "n": len(r), "covered": int(r["covered"].sum()),
                "gap": int(gap.sum()), "gap_and_current_st": int((gap & r["cur"]).sum()),
            }
        )
    gdf = pd.DataFrame(gaps)
    gdf.to_csv(out_dir / "namechange_coverage_gap.csv", index=False)

    def stat(mask, label):
        s = allr.loc[mask, "fwd20"].dropna()
        return {
            "bucket": label, "code_months": int(mask.sum()), "n": int(len(s)),
            "per_month": round(float(mask.sum()) / max(len(mdf), 1), 1),
            "mean": s.mean(), "median": s.median(), "win": (s > 0).mean(),
            "p10": s.quantile(0.10), "p90": s.quantile(0.90),
        }

    cur_risk = allr["cur_st"] | allr["cur_delist"]
    pit_risk = allr["pit_st"] | allr["pit_delist"]
    bdf = pd.DataFrame(
        [
            stat(~pit_risk & ~cur_risk, "D_clean 两口径都不剔"),
            stat(pit_risk & ~cur_risk, "B_only_pit 仅 PIT 剔（当时已 ST/退）"),
            stat(~pit_risk & cur_risk, "A_only_current 仅当前剔（后来才 ST/退，含前视）"),
            stat(pit_risk & cur_risk, "C_both 两口径都剔"),
            stat(allr["pit_st"] & ~allr["pit_delist"], "  B1 当时仅 ST"),
            stat(allr["pit_delist"], "  B2 当时含退市整理期"),
        ]
    )
    bdf.to_csv(out_dir / "st_bucket_fwd20.csv", index=False)

    print(f"[audit] 月末 {len(mdf)} 个（{mdf.day.min():%Y-%m-%d} → {mdf.day.max():%Y-%m-%d}）｜"
          f"域内 PIT 均剔 {mdf.pit.mean():.1f} 只 vs 当前名称 {mdf.current.mean():.1f} 只")
    print(f"[audit] 有差异的月末 {int(((mdf.only_pit > 0) | (mdf.only_current > 0)).sum())}/{len(mdf)}"
          f"｜仅 PIT 剔合计 {mdf.only_pit.sum()}｜仅当前剔合计 {mdf.only_current.sum()}")
    print(bdf.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    if len(gdf):
        print(f"[audit] 区间表覆盖缺口（仅真实上市观测）：covered=False "
              f"{gdf.gap.sum():,}/{gdf.n.sum():,}（{gdf.gap.sum()/gdf.n.sum():.2%}）；"
              f"其中当前名称是 ST/退（可能漏判）{gdf.gap_and_current_st.sum():,}"
              f"（{gdf.gap_and_current_st.sum()/gdf.n.sum():.3%}）")
    print(f"[audit] → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
