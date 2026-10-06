#!/usr/bin/env python3
"""P1.24 Phase 1 前置：sleeve 级收益序列 + 夏普/卡玛**明确归因**。

回答两个问题：
  1) 组合的夏普、卡玛比分别由哪个 sleeve 贡献？
  2) 若去掉某个 sleeve，夏普/卡玛会变好还是变差（留一法）？

口径：
  * 输入 `output/results_cn/replay_*/cn_ensemble_selected.csv`（含 sleeve 标签）；
  * 进场 = 决策日次日开盘，出场 = 下一个决策日开盘（与仓库 T+1 开盘口径一致）；
  * 每条记录的收益按 `target_weight` 加权；sleeve 自身的选股能力另按"腿内归一权重"单独统计。

归因定义（写死，避免事后换口径）：
  * 收益贡献_RC_i   = mean(w_i,t × r_i,t)               （sleeve 对组合期均收益的贡献，单位：每期）
  * 风险贡献_RK_i   = w_i · (Σ w)_i / σ_p               （用 sleeve 收益协方差矩阵，Σ 为各腿权重向量）
  * 夏普贡献_SH_i   = RC_i / σ_p                        （组合夏普可近似拆成各腿之和）
  * 留一 ΔSharpe_i  = Sharpe(组合) − Sharpe(去掉 i)     （最直观的"这条腿值不值"）
  * 卡玛：卡玛比不可加，故用 ① 留一 ΔCalmar_i；② 组合最大回撤窗口内各腿的累计贡献

用法：
  uv run python scripts/sleeve_metric_attribution.py
  uv run python scripts/sleeve_metric_attribution.py --start 2025-09-08 --weight raw|normalized
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PERIODS_PER_YEAR = 12.0  # 回放节奏约每月一次；下面的年化都用它（报告里会标注）


def load_bars() -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = [str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=202[4-6]/*.parquet")]
    table = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code", "open", "close"])
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def load_picks(root: Path) -> pd.DataFrame:
    rows = []
    for day_dir in sorted(root.glob("replay_*")):
        if not re.fullmatch(r"replay_\d{8}", day_dir.name):     # 排除 replay_20260915_rich 等变体目录
            continue
        path = day_dir / "cn_ensemble_selected.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        if frame.empty:
            continue
        frame["replay_date"] = day_dir.name.replace("replay_", "")
        if "target_weight" not in frame.columns:
            frame["target_weight"] = 0.0
        for col in ("selection_channel", "selection_sleeve"):
            if col not in frame.columns:
                frame[col] = ""
        rows.append(frame[["replay_date", "stock_code", "target_weight", "selection_channel", "selection_sleeve"]])
    if not rows:
        raise FileNotFoundError("没有可用的 replay_*/cn_ensemble_selected.csv")
    picks = pd.concat(rows, ignore_index=True)
    picks["sleeve"] = picks["selection_sleeve"].fillna("").astype(str).str.strip()
    picks.loc[(picks["sleeve"] == "") & (picks["selection_channel"] == "model"), "sleeve"] = "model_alpha"
    picks.loc[picks["sleeve"] == "", "sleeve"] = "untagged"
    picks["target_weight"] = pd.to_numeric(picks["target_weight"], errors="coerce").fillna(0.0)
    picks["replay_date"] = picks["replay_date"].astype(str)
    return picks


def main() -> int:
    parser = argparse.ArgumentParser(description="sleeve 级夏普/卡玛归因")
    parser.add_argument("--root", type=Path, default=ROOT / "output/results_cn")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--out", type=Path, default=ROOT / "output/research")
    args = parser.parse_args()

    picks = load_picks(args.root)
    bars = load_bars()
    open_wide = bars.pivot_table(index="trade_date", columns="stock_code", values="open", aggfunc="last")
    calendar = open_wide.index

    dates = sorted({d for d in picks["replay_date"].unique() if d >= args.start.replace("-", "")})
    date_index = [pd.Timestamp(d) for d in dates]
    if len(date_index) < 4:
        print("[attr] 回放日不足 4 个，无法归因")
        return 1
    print(f"[attr] 决策日 {len(date_index)} 个：{date_index[0].date()} ~ {date_index[-1].date()}", flush=True)

    # 每个决策日的进场/出场价：进场 = 决策日次日开盘；出场 = 下一决策日次日开盘（最后一个用最新收盘）
    def next_session(day: pd.Timestamp) -> pd.Timestamp | None:
        later = calendar[calendar > day]
        return later[0] if len(later) else None

    horizon = {}
    for i, day in enumerate(date_index):
        entry = next_session(day)
        exit_day = next_session(date_index[i + 1]) if i + 1 < len(date_index) else None
        if entry is None:
            continue
        if exit_day is None:
            exit_day = calendar[-1]
        horizon[dates[i]] = (entry, exit_day)

    records = []
    for replay, group in picks.groupby("replay_date"):
        if replay not in horizon:
            continue
        entry, exit_day = horizon[replay]
        total_w = float(group["target_weight"].sum()) or np.nan
        for row in group.itertuples():
            if row.stock_code not in open_wide.columns:
                continue
            p0, p1 = open_wide.at[entry, row.stock_code], open_wide.at[exit_day, row.stock_code]
            if not (np.isfinite(p0) and np.isfinite(p1) and p0 > 0):
                continue
            sleeve_w = group.loc[group["sleeve"] == row.sleeve, "target_weight"].sum()
            records.append({
                "replay_date": replay, "sleeve": row.sleeve, "stock_code": row.stock_code,
                "ret": float(p1 / p0 - 1), "weight": float(row.target_weight),
                "weight_in_sleeve": float(row.target_weight / sleeve_w) if sleeve_w > 0 else np.nan,
                "portfolio_weight_share": float(row.target_weight / total_w) if np.isfinite(total_w) else np.nan,
            })
    detail = pd.DataFrame(records)
    if detail.empty:
        print("[attr] 没有可匹配的价格")
        return 1
    print(f"[attr] 匹配 {len(detail)} 条持仓记录 / {detail['sleeve'].nunique()} 个 sleeve", flush=True)

    # ---- sleeve 与组合的期度收益序列 ----
    def series_by_sleeve(key: str) -> pd.DataFrame:
        frames = {}
        for sleeve, group in detail.groupby("sleeve"):
            per_date = group.groupby("replay_date").apply(
                lambda g: float((g["ret"] * g[key]).sum() / g[key].sum()) if g[key].sum() > 0 else np.nan,
                include_groups=False)
            frames[sleeve] = per_date
        return pd.DataFrame(frames).sort_index()

    sleeve_rets = series_by_sleeve("weight_in_sleeve")          # 腿内归一：衡量选股能力
    weights = (detail.groupby(["replay_date", "sleeve"])["weight"].sum().unstack(fill_value=0.0))
    weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)   # 组合权重占比
    portfolio_ret = (sleeve_rets * weights.reindex(sleeve_rets.index).fillna(0.0)).sum(axis=1)

    def metrics(series: pd.Series) -> dict:
        series = series.dropna()
        if len(series) < 3:
            return {}
        curve = (1 + series).cumprod()
        years = len(series) / PERIODS_PER_YEAR
        cagr = curve.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
        vol = series.std() * np.sqrt(PERIODS_PER_YEAR)
        sharpe = (series.mean() * PERIODS_PER_YEAR) / vol if vol > 0 else np.nan
        mdd = float((curve / curve.cummax() - 1).min())
        calmar = cagr / abs(mdd) if mdd < 0 else np.nan
        return {"periods": len(series), "total_return": float(curve.iloc[-1] - 1), "cagr": float(cagr),
                "vol": float(vol), "sharpe": float(sharpe), "max_drawdown": mdd, "calmar": float(calmar)}

    port_metrics = metrics(portfolio_ret)
    table = []
    for sleeve in sleeve_rets.columns:
        row = {"sleeve": sleeve, **metrics(sleeve_rets[sleeve])}
        # 归因
        w = weights[sleeve].reindex(sleeve_rets.index).fillna(0.0) if sleeve in weights else pd.Series(0.0, index=sleeve_rets.index)
        contrib = (w * sleeve_rets[sleeve].fillna(0.0))
        row["return_contribution_per_period"] = float(contrib.mean())
        cov = sleeve_rets.fillna(0.0).cov() * PERIODS_PER_YEAR
        w_mean = weights.reindex(sleeve_rets.index).fillna(0.0).mean()
        if sleeve in cov.index:
            sigma_p = np.sqrt(float(w_mean @ cov.loc[cov.index, cov.index] @ w_mean))
            row["risk_contribution_share"] = float(w_mean[sleeve] * (cov.loc[sleeve] @ w_mean) / (sigma_p ** 2)) if sigma_p > 0 else np.nan
            row["sharpe_contribution"] = float(row["return_contribution_per_period"] * PERIODS_PER_YEAR / sigma_p) if sigma_p > 0 else np.nan
            without = sleeve_rets.drop(columns=[sleeve]).fillna(0.0)
            w2 = weights.drop(columns=[sleeve]).reindex(sleeve_rets.index).fillna(0.0)
            w2 = w2.div(w2.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
            loo = metrics((without * w2).sum(axis=1))
            row["sharpe_leave_one_out"] = loo.get("sharpe")
            row["delta_sharpe_if_removed"] = (port_metrics.get("sharpe") or np.nan) - (loo.get("sharpe") or np.nan)
            row["calmar_leave_one_out"] = loo.get("calmar")
            row["delta_calmar_if_removed"] = (port_metrics.get("calmar") or np.nan) - (loo.get("calmar") or np.nan)
        table.append(row)
    attribution = pd.DataFrame(table).sort_values("return_contribution_per_period", ascending=False)

    # ---- 组合最大回撤窗口归因 ----
    curve = (1 + portfolio_ret.dropna()).cumprod()
    dd = curve / curve.cummax() - 1
    trough = dd.idxmin()
    peak = curve.loc[:trough].idxmax()
    window = sleeve_rets.loc[(sleeve_rets.index >= peak) & (sleeve_rets.index <= trough)]
    mdd_rows = []
    for sleeve in window.columns:
        w = weights[sleeve].reindex(window.index).fillna(0.0)
        mdd_rows.append({"sleeve": sleeve,
                         "window_return_contribution": float((w * window[sleeve].fillna(0.0)).sum()),
                         "window_own_return": float(((1 + window[sleeve].fillna(0.0)).prod() - 1))})
    mdd_table = pd.DataFrame(mdd_rows).sort_values("window_return_contribution")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"sleeve_metric_attribution_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    detail.to_csv(out_dir / "holdings_with_returns.csv", index=False)
    sleeve_rets.to_csv(out_dir / "sleeve_period_returns.csv")
    weights.to_csv(out_dir / "portfolio_weights.csv")
    attribution.to_csv(out_dir / "attribution.csv", index=False)
    mdd_table.to_csv(out_dir / "max_drawdown_window.csv", index=False)

    lines = ["# sleeve 级夏普 / 卡玛归因", "",
             f"- 决策日 {len(date_index)} 个（{date_index[0].date()} ~ {date_index[-1].date()}），"
             f"匹配持仓 {len(detail)} 条，年化基数 = {PERIODS_PER_YEAR:.0f} 期/年", "",
             "## 组合指标", "",
             "| 指标 | 值 |", "|---|---|",
             f"| 期间数 | {port_metrics.get('periods')} |",
             f"| 累计收益 | {port_metrics.get('total_return', float('nan')):.2%} |",
             f"| 年化（按 {PERIODS_PER_YEAR:.0f} 期） | {port_metrics.get('cagr', float('nan')):.2%} |",
             f"| 年化波动 | {port_metrics.get('vol', float('nan')):.2%} |",
             f"| **夏普** | **{port_metrics.get('sharpe', float('nan')):.2f}** |",
             f"| 最大回撤 | {port_metrics.get('max_drawdown', float('nan')):.2%} |",
             f"| **卡玛** | **{port_metrics.get('calmar', float('nan')):.2f}** |", "",
             "## 逐 sleeve 归因", "",
             "| sleeve | 期数 | 自身累计收益 | 自身夏普 | 收益贡献/期 | 风险贡献占比 | 夏普贡献 | 去掉后夏普 | ΔSharpe | 去掉后卡玛 | ΔCalmar |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in attribution.itertuples():
        lines.append(
            f"| {r.sleeve} | {getattr(r, 'periods', '')} | {getattr(r, 'total_return', float('nan')):.2%} | "
            f"{getattr(r, 'sharpe', float('nan')):.2f} | {r.return_contribution_per_period:.3%} | "
            f"{getattr(r, 'risk_contribution_share', float('nan')):.1%} | {getattr(r, 'sharpe_contribution', float('nan')):.2f} | "
            f"{(getattr(r, 'sharpe_leave_one_out', None) or float('nan')):.2f} | "
            f"{(getattr(r, 'delta_sharpe_if_removed', None) or float('nan')):+.2f} | "
            f"{(getattr(r, 'calmar_leave_one_out', None) or float('nan')):.2f} | "
            f"{(getattr(r, 'delta_calmar_if_removed', None) or float('nan')):+.2f} |")
    def _d(x) -> str:
        return pd.Timestamp(x).strftime("%Y-%m-%d") if not isinstance(x, str) else x

    lines += ["", f"## 组合最大回撤窗口（{_d(peak)} → {_d(trough)}，{dd.min():.2%}）归因", "",
              "| sleeve | 窗口内组合收益贡献 | 窗口内自身收益 |", "|---|---|---|"]
    for r in mdd_table.itertuples():
        lines.append(f"| {r.sleeve} | {r.window_return_contribution:+.2%} | {r.window_own_return:+.2%} |")
    lines += ["", "## 口径与局限", "",
              "1. 决策日来自 `output/results_cn/replay_*`（非等间隔，约每月一次），年化基数取 12 期/年；",
              "   样本期短（<20 期），夏普/卡玛的置信区间很宽，**只用于相对比较，不作为绝对预期**。",
              "2. 进场 = 决策日次日开盘，出场 = 下一决策日次日开盘；不含佣金/印花税/滑点。",
              "3. 卡玛比不可加，故用留一法与回撤窗口归因两项替代；夏普用『收益贡献/组合波动』近似拆分 + 留一法交叉验证。",
              f"4. 输出目录：`{out_dir.relative_to(ROOT)}`"]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "dates": len(date_index), "portfolio": port_metrics,
        "attribution": attribution.to_dict("records"), "periods_per_year": PERIODS_PER_YEAR,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[attr] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
