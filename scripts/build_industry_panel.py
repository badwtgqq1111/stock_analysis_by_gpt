#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行业轮动面板：把个股行情/资金流聚合成"行业 × 交易日"，并给出行业前瞻收益标签。

为什么需要它（2026-10-09）
--------------------------
`/industry` 页面的热度指数只做**描述**。要做"行业热度选股"必须先回答：热度（及其各分项）
能不能预测**行业未来的收益**？这需要一份行业级面板 + 行业级前瞻标签，再用同一套
LLM 因子挖掘（`scripts/llm_factor_mining.py --label ind_*`）去检验。

面板口径：
  * 行业归属 `cn_stock_basic.industry`（110 个行业，非 PIT）；
  * 行业日收益 = 成分股当日涨跌幅的**中位数**（等权均值会被小盘极端值主导）；
  * 资金流 = 成分股净流入合计（tushare 口径，万元）/ 成分股成交额合计（元）→ 无量纲强度；
  * 情绪 = 涨停股票日数、连板家数（涨停判定复用 `build_zt_board_features.limit_flags`）；
  * 标签 = 行业等权收益指数（基于中位数日收益累乘）向前看 1/5/20 个交易日的收益。

用法：
  uv run python scripts/build_industry_panel.py --start 2024-01-01
输出：
  output/research/industry_rotation/industry_panel.parquet
  output/research/industry_rotation/summary.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_zt_board_features as bzb  # noqa: E402

OUT_DIR = ROOT / "output/research/industry_rotation"
MONEYFLOW = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
UNCLASSIFIED = "未分类"


def industry_map() -> pd.Series:
    basic = pd.read_parquet(BASIC, columns=["ts_code", "industry"])
    basic["ts_code"] = basic["ts_code"].astype(str)
    series = basic.drop_duplicates("ts_code").set_index("ts_code")["industry"].astype(str)
    series = series.replace({"nan": "", "None": ""}).fillna("")
    return series.where(series != "", UNCLASSIFIED)


def moneyflow(start: str, source: str = "moneyflow") -> pd.DataFrame:
    import pyarrow.dataset as ds
    dataset = ds.dataset(str(MONEYFLOW), format="parquet")
    table = dataset.to_table(
        columns=["trade_date", "stock_code", f"{source}_net_amount"],
        filter=(ds.field("trade_date") >= pd.Timestamp(start)) & (ds.field("source") == source))
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.rename(columns={f"{source}_net_amount": "net_amount"})


def build(start: str = "2024-01-01", source: str = "moneyflow") -> tuple[pd.DataFrame, dict]:
    print(f"[industry-panel] 读日线（{start} 起）…", flush=True)
    bars = bzb.load_bars(start)
    flags = bzb.limit_flags(bars)
    mapping = industry_map()
    flags["industry"] = flags["stock_code"].map(mapping).fillna(UNCLASSIFIED)

    flow = moneyflow(start, source)
    flags = flags.merge(flow, on=["trade_date", "stock_code"], how="left")

    grouped = flags[flags["industry"] != UNCLASSIFIED].groupby(["trade_date", "industry"])
    panel = pd.DataFrame({
        "n": grouped.size(),
        "ret": grouped["pct_chg"].median(),
        "ret_mean": grouped["pct_chg"].mean(),
        "up_ratio": grouped["pct_chg"].apply(lambda s: float((s > 0).mean())),
        "amount": grouped["amount"].sum(),
        "net_amount": grouped["net_amount"].sum(min_count=1),
        "zt_days": grouped["limit_up"].sum(),
        "boards_ge2": grouped["boards"].apply(lambda s: int((s >= 2).sum())),
        "max_boards": grouped["boards"].max(),
    }).reset_index()

    # 行业 x 日期 的等权收益指数（用于前瞻标签），用中位数日收益累乘
    panel = panel.sort_values(["industry", "trade_date"])
    panel["nav"] = panel.groupby("industry")["ret"].transform(
        lambda s: (1 + s.fillna(0)).cumprod())
    # 等权（均值）口径的行业指数：与中位数口径会背离（少数大牛股把均值拉起来、中位数不动），
    # 两套标签都留着，避免"IC 说反转、回测说动量"这种口径混用导致的误判。
    panel["nav_mean"] = panel.groupby("industry")["ret_mean"].transform(
        lambda s: (1 + s.fillna(0)).cumprod())
    for horizon in (1, 5, 20):
        panel[f"ind_fwd{horizon}"] = panel.groupby("industry")["nav"].transform(
            lambda s: s.shift(-horizon) / s - 1.0)
        panel[f"ind_fwd{horizon}_mean"] = panel.groupby("industry")["nav_mean"].transform(
            lambda s: s.shift(-horizon) / s - 1.0)

    # 情绪/资金/涨跌的历史特征（行业自身时序）
    grouped2 = panel.groupby("industry")
    for window in (5, 20):
        panel[f"ret_{window}d"] = grouped2["nav"].transform(lambda s: s / s.shift(window) - 1.0)
        panel[f"flow_{window}d"] = grouped2["net_amount"].transform(
            lambda s: s.rolling(window, min_periods=1).sum())
        panel[f"zt_{window}d"] = grouped2["zt_days"].transform(
            lambda s: s.rolling(window, min_periods=1).sum())
        panel[f"heat_prev_{window}d"] = grouped2["ret"].transform(
            lambda s: s.rolling(window, min_periods=1).mean())
    panel["flow_intensity"] = (panel["net_amount"] * 1e4) / panel["amount"].replace(0, np.nan)
    panel["zt_density"] = panel["zt_days"] / panel["n"]
    panel["boards_density"] = panel["boards_ge2"] / panel["n"]
    panel["turnover_share"] = panel["amount"] / panel.groupby("trade_date")["amount"].transform("sum")
    panel["ret_rank"] = panel.groupby("trade_date")["ret"].rank(pct=True)
    panel["flow_rank"] = panel.groupby("trade_date")["flow_intensity"].rank(pct=True)
    panel["zt_rank"] = panel.groupby("trade_date")["zt_density"].rank(pct=True)

    # 热度指数（与 /industry 页面同一套权重，行业间横截面 z 加权后取分位）
    weights = {"ret": 0.30, "up_ratio": 0.15, "flow_intensity": 0.25, "zt_density": 0.20}
    composite = pd.Series(0.0, index=panel.index)
    for key, weight in weights.items():
        column = panel[key].astype(float)
        z = column.groupby(panel["trade_date"]).transform(
            lambda s: (s - s.mean()) / s.std(ddof=0) if s.std(ddof=0) and s.std(ddof=0) > 0 else 0.0)
        composite = composite + z.fillna(0.0) * weight
    panel["heat_z"] = composite
    panel["heat"] = panel.groupby("trade_date")["heat_z"].rank(pct=True) * 100
    panel["heat_chg"] = panel.groupby("industry")["heat"].diff()

    # 市场环境（当日全市场口径）
    market = flags.groupby("trade_date").agg(
        mkt_ret=("pct_chg", "median"), mkt_up_ratio=("pct_chg", lambda s: float((s > 0).mean())),
        mkt_zt_cnt=("limit_up", "sum"), mkt_amount=("amount", "sum"),
        mkt_net_amount=("net_amount", "sum"))
    market["mkt_zt_5d"] = market["mkt_zt_cnt"].rolling(5, min_periods=1).mean()
    panel = panel.merge(market.reset_index(), on="trade_date", how="left")

    keep = ["trade_date", "industry", "n", "ret", "ret_mean", "up_ratio", "amount", "net_amount",
            "flow_intensity", "zt_days", "zt_density", "boards_ge2", "boards_density", "max_boards",
            "turnover_share", "ret_rank", "flow_rank", "zt_rank", "heat", "heat_z", "heat_chg",
            "ret_5d", "ret_20d", "flow_5d", "flow_20d", "zt_5d", "zt_20d",
            "heat_prev_5d", "heat_prev_20d", "mkt_ret", "mkt_up_ratio", "mkt_zt_cnt", "mkt_zt_5d",
            "mkt_amount", "mkt_net_amount", "ind_fwd1", "ind_fwd5", "ind_fwd20",
            "ind_fwd1_mean", "ind_fwd5_mean", "ind_fwd20_mean"]
    return panel[keep].sort_values(["trade_date", "industry"]).reset_index(drop=True), {"flow": flow}


def main() -> int:
    parser = argparse.ArgumentParser(description="行业轮动面板构建")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    panel, _ = build(args.start)
    path = out_dir / "industry_panel.parquet"
    panel.to_parquet(path, index=False)
    print(f"[industry-panel] 写出 {path}（{len(panel):,} 行 × {panel.shape[1]} 列）", flush=True)

    lines = [f"# 行业轮动面板（{panel['trade_date'].min().date()} ~ {panel['trade_date'].max().date()}）", ""]
    lines.append(f"- {panel['industry'].nunique()} 个行业 × {panel['trade_date'].nunique()} 个交易日 = {len(panel):,} 行")
    lines.append(f"- 每行业每日平均 {panel['n'].mean():.1f} 只成分股（中位 {panel['n'].median():.0f}）")
    valid = panel.dropna(subset=["ind_fwd5"])
    lines += ["", "## 行业日收益的自相关/预测性基线（月度标签 ind_fwd20）", ""]
    sample = panel.dropna(subset=["ind_fwd20"])
    for label in ["ind_fwd20", "ind_fwd20_mean"]:
        lines.append(f"\n### 标签 {label}")
        for key in ["heat", "ret", "ret_5d", "ret_20d", "flow_intensity", "zt_density", "mkt_zt_cnt"]:
            ics = []
            for _, group in panel.dropna(subset=[label]).groupby("trade_date"):
                if len(group) >= 30:
                    ic = group[key].corr(group[label], method="spearman")
                    if np.isfinite(ic):
                        ics.append(ic)
            if ics:
                series = pd.Series(ics)
                lines.append(f"- `{key}` → {label}：IC 均值 {series.mean():+.4f}，"
                             f"ICIR {series.mean()/series.std():+.3f}，>0 占比 {(series>0).mean():.1%}，"
                             f"{len(series)} 个交易日")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
