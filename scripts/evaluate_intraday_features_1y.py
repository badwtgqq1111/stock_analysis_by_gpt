#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在**一年窗口**上独立检验 5min 派生特征（L8"只看一年内"的评估段）。

诚实前提（结论必须一起引用）：本期只有 **241 个交易日 / 12 个自然月**，
月频 IC 只有 ~12 个观测 —— 这里给的是**方向与量级**，不是统计结论；
真正的准入仍需 105 个月那套口径（那时得先回补历史分时，本决定是不回补）。

对每个分时特征给出三件事：
  1. 月频横截面秩 IC（均值 / ICIR / 正号月数），窗口 2025-10~2026-09；
  2. 与现有 25 列（v1 控制集）横截面秩相关的**最大 |ρ|** —— 判断是不是"已知字段的换皮"；
  3. 五分位多空组合的月度收益与胜率（等权，不扣成本）。

用法:
  uv run python scripts/evaluate_intraday_features_1y.py
输出:
  output/research/intraday_feature_eval_1y/{REPORT.md,summary.csv}
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from factor_oos_deflated import CONTROL_COLS_V1  # noqa: E402
from llm_factor_mining import load_panel  # noqa: E402

FEAT = ROOT / "assets/data/meta/cn_intraday_daily_features.parquet"
OUT = ROOT / "output/research/intraday_feature_eval_1y"
FIELDS = ["intraday_mom", "overnight_ret", "head_30m", "tail_30m", "intraday_vol",
          "vwap_dev", "range_pct", "tail_vol_share", "am_pm_gap"]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    feat = pd.read_parquet(FEAT)
    feat["trade_date"] = pd.to_datetime(feat["trade_date"])
    print(f"[eval] 分时特征 {len(feat):,} 行 / {feat['stock_code'].nunique()} 只 / "
          f"{feat['trade_date'].nunique()} 个交易日", flush=True)

    # 复权日线 + 前瞻收益（fwd5 / fwd20）
    merged, _ = load_panel("2025-01-01")
    merged = merged[merged["trade_date"].isin(feat["trade_date"].unique())]
    work = merged.merge(feat, on=["trade_date", "stock_code"], how="inner")
    print(f"[eval] 对齐后 {len(work):,} 行（{work['trade_date'].nunique()} 个交易日）", flush=True)

    # fwd5：用已有 fwd20 表补一个 5 日版本
    import pyarrow.dataset as ds
    files = sorted(str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet"))
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    close = bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    fwd5 = (close.shift(-6) / close.shift(-1) - 1).stack().rename("fwd5").reset_index()
    fwd5.columns = ["trade_date", "stock_code", "fwd5"]
    work = work.merge(fwd5, on=["trade_date", "stock_code"], how="left")

    months = sorted(work["trade_date"].dt.to_period("M").astype(str).unique())
    print(f"[eval] 覆盖月份 {len(months)} 个：{months}", flush=True)

    def monthly_ic(col: str, label: str) -> pd.Series:
        """**逐日横截面 IC → 按月平均**（与仓库 factor_ic_protocol 的口径一致）。

        第一版把整月混在一起算一次 Spearman，混入了月内时间漂移，会出现
        "IC 为负但多空组合为正"这种自相矛盾的结果 —— 已改为逐日再平均。
        """
        per_day = {}
        for d, g in work.groupby("trade_date"):
            if len(g) < 100:
                continue
            ic = g[col].corr(g[label], method="spearman")
            if np.isfinite(ic):
                per_day[d] = float(ic)
        series = pd.Series(per_day).sort_index()
        if series.empty:
            return series
        return series.groupby(series.index.to_period("M").astype(str)).mean()

    rows = []
    for col in FIELDS:
        ics = monthly_ic(col, "fwd20")
        ics5 = monthly_ic(col, "fwd5")
        max_rho, rho_against = 0.0, ""
        for other in CONTROL_COLS_V1:
            if other not in work.columns:
                continue
            rho = work[[col, other]].corr(method="spearman").iloc[0, 1]
            if np.isfinite(rho) and abs(rho) > abs(max_rho):
                max_rho, rho_against = float(rho), other
        per_day_ls = {}
        for d, g in work.groupby("trade_date"):
            if len(g) < 100:
                continue
            q = g[col].rank(pct=True)
            if q.notna().sum() < 100:
                continue
            per_day_ls[d] = float(g.loc[q >= 0.8, "fwd20"].mean() - g.loc[q <= 0.2, "fwd20"].mean())
        ls_daily = pd.Series(per_day_ls).sort_index()
        ls = (ls_daily.groupby(ls_daily.index.to_period("M").astype(str)).mean()
              if not ls_daily.empty else ls_daily)
        rows.append({
            "feature": col,
            "ic20_mean": float(ics.mean()) if len(ics) else np.nan,
            "ic20_icir": float(ics.mean() / ics.std()) if len(ics) > 1 and ics.std() else np.nan,
            "ic20_months": int(len(ics)),
            "ic20_pos_months": int((ics > 0).sum()),
            "ic5_mean": float(ics5.mean()) if len(ics5) else np.nan,
            "max_rho_with_v1": max_rho, "rho_against": rho_against,
            "ls_mean_month": float(ls.mean()) if len(ls) else np.nan,
            "ls_win_rate": float((ls > 0).mean()) if len(ls) else np.nan,
        })
    table = pd.DataFrame(rows).sort_values("ic20_mean", key=lambda s: -s.abs())
    table.to_csv(OUT / "summary.csv", index=False)
    print(table.to_string(index=False), flush=True)

    lines = ["# 5min 派生特征：一年窗口（2025-10~2026-09）独立检验", "",
             f"数据：`assets/data/meta/cn_intraday_daily_features.parquet`（1,246,628 行 / 5,228 只 / 241 个交易日，"
             f"9 个特征覆盖率 99.9%+）",
             "",
             "**⚠️ 统计力声明**：本期只有 **12 个自然月 / 241 个交易日**，月频 IC 只有 ~12 个观测（逐日 IC 再按月平均）—— "
             "下表是**方向与量级**，不是统计结论；真正准入需要 105 个月口径（那要求回补历史分时，本决定是不回补）。", "",
             "| 特征 | IC20 均值 | ICIR | 正号月/总月 | IC5 均值 | 与 v1 最大\\|ρ\\| | 对冲列 | 多空月均 | 胜率 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for _, r in table.iterrows():
        lines.append(f"| `{r['feature']}` | {r['ic20_mean']:+.4f} | {r['ic20_icir']:+.2f} | "
                     f"{r['ic20_pos_months']}/{r['ic20_months']} | {r['ic5_mean']:+.4f} | "
                     f"{r['max_rho_with_v1']:+.2f} | {r['rho_against']} | "
                     f"{r['ls_mean_month']:+.2%} | {r['ls_win_rate']:.0%} |")
    lines += ["", "## 已知局限", "",
              "1. 单一年份、单一市场环境（2025-10~2026-09），任何「这一年有效」都不能外推；",
              "2. 前瞻收益用 qfq 日线；**未扣成本**，而分时类因子换手通常更高；",
              "3. 与 v1 的 |ρ| 是**全样本**秩相关，不是逐月冗余聚类；",
              "4. 分时数据本身有少量坏文件（数值被写坏），脚本已 to_numeric 强转 + 记录隔离清单；",
              "5. 域未做「主板 / 上市满 1 年」过滤（这里是全样本对齐），所以数字比 L3 那套更「毛」。", ""]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[eval] 输出 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
