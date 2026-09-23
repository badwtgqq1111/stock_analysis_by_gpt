#!/usr/bin/env python3
"""月内换股回测：评估当前选股结果与 30% 月收益目标的距离。

选股 CSV 只包含决策日横截面，本脚本读取本地 qfq 日线，按相邻决策日持有，
同时输出实际权重、归一化 60%/100% gross，以及只用于上限参考的事后 oracle。
默认使用决策日收盘到下一决策日收盘的区间；不会把下一期之后的数据回填到上一期。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import re

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService


LIST_FIELDS = (
    "st_dropped", "missing_name", "illiquid_dropped", "missing_amount",
    "volume_breakout_dropped", "volume_breakout_unconfirmed",
)


def _selection_files(root: Path) -> list[Path]:
    files = []
    for path in root.glob("replay_*/cn_ensemble_selected.csv"):
        # 只取标准日期回放；rich/small/实验目录会与同一天重复计权。
        if not re.fullmatch(r"replay_\d{8}", path.parent.name):
            continue
        try:
            frame = pd.read_csv(path, usecols=["trade_date"])
            if not frame.empty:
                files.append(path)
        except (OSError, ValueError, pd.errors.EmptyDataError):
            continue
    return sorted(files, key=lambda p: pd.Timestamp(pd.read_csv(p, usecols=["trade_date"]).iloc[0, 0]))


def _load_selections(root: Path, start: str | None, end: str | None) -> pd.DataFrame:
    rows = []
    for path in _selection_files(root):
        frame = pd.read_csv(path)
        if frame.empty or "trade_date" not in frame or "stock_code" not in frame:
            continue
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
        frame["stock_code"] = frame["stock_code"].astype(str)
        frame = frame.dropna(subset=["trade_date"])
        if start:
            frame = frame.loc[frame["trade_date"] >= pd.Timestamp(start).normalize()]
        if end:
            frame = frame.loc[frame["trade_date"] <= pd.Timestamp(end).normalize()]
        if not frame.empty:
            frame["source_file"] = str(path)
            rows.append(frame)
    if not rows:
        raise ValueError("没有找到可用的 replay_*/cn_ensemble_selected.csv")
    merged = pd.concat(rows, ignore_index=True, sort=False)
    # 同一决策日只保留最新文件，避免 rich/small 等实验目录重复计入。
    merged = merged.sort_values(["trade_date", "source_file"])
    merged = merged.groupby(["trade_date", "stock_code"], as_index=False).tail(1)
    return merged.reset_index(drop=True)


def _weight_column(frame: pd.DataFrame) -> str:
    for column in ("trade_weight", "target_weight"):
        if column in frame.columns and pd.to_numeric(frame[column], errors="coerce").fillna(0).abs().sum() > 0:
            return column
    return "target_weight"


def _portfolio_period_returns(
    selections: pd.DataFrame, prices: pd.DataFrame, target_gross: float | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    weight_col = _weight_column(selections)
    selections = selections.copy()
    selections[weight_col] = pd.to_numeric(selections.get(weight_col, 0.0), errors="coerce").fillna(0.0)
    selections = selections.loc[selections[weight_col] > 0].copy()
    dates = sorted(selections["trade_date"].dropna().unique())
    prices = prices.copy()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce").dt.normalize()
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    prices = prices.dropna(subset=["stock_code", "trade_date", "close"]).sort_values(["stock_code", "trade_date"])
    close = prices.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    period_rows = []
    daily_rows = []
    for index, decision_date in enumerate(dates):
        next_date = dates[index + 1] if index + 1 < len(dates) else close.index.max()
        if pd.isna(next_date) or next_date <= decision_date:
            continue
        holding = selections.loc[selections["trade_date"].eq(decision_date), ["stock_code", weight_col]].copy()
        weights = holding.set_index("stock_code")[weight_col].astype(float)
        gross = float(weights.sum())
        if target_gross is not None and gross > 0:
            weights = weights * float(target_gross) / gross
            gross = float(weights.sum())
        window = close.loc[(close.index >= decision_date) & (close.index <= next_date), weights.index.intersection(close.columns)]
        if len(window) < 2 or window.empty:
            continue
        # 收盘到收盘；每个持仓缺失价格的日收益按 0 处理，且记录覆盖率。
        returns = window.pct_change().iloc[1:]
        weighted = returns.mul(weights, axis=1)
        daily = weighted.sum(axis=1, min_count=1).fillna(0.0)
        coverage = returns.notna().mul(weights, axis=1).sum(axis=1).div(max(gross, 1e-12))
        for day, value in daily.items():
            daily_rows.append({
                "trade_date": day, "decision_date": decision_date,
                "daily_return": float(value), "gross": gross,
                "coverage": float(coverage.loc[day]),
            })
        period_rows.append({
            "decision_date": decision_date, "next_decision_date": next_date,
            "gross": gross, "holdings": int(len(weights)),
            "period_return": float((1.0 + daily).prod() - 1.0),
            "min_coverage": float(coverage.min()) if len(coverage) else 0.0,
            "source_file": str(holding.index.name or ""),
        })
    return pd.DataFrame(period_rows), pd.DataFrame(daily_rows)


def _oracle_periods(selections: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """计算入选池内 Top-1 与等权 Top-6 的事后上限（不可部署）。"""
    prices = prices.copy()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce").dt.normalize()
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    close = prices.dropna(subset=["stock_code", "trade_date", "close"]).pivot_table(
        index="trade_date", columns="stock_code", values="close", aggfunc="last"
    ).sort_index()
    dates = sorted(selections["trade_date"].dropna().unique())
    rows = []
    for index, decision_date in enumerate(dates):
        next_date = dates[index + 1] if index + 1 < len(dates) else close.index.max()
        names = selections.loc[selections["trade_date"].eq(decision_date), "stock_code"].astype(str).unique()
        window = close.loc[(close.index >= decision_date) & (close.index <= next_date), [c for c in names if c in close.columns]]
        if len(window) < 2 or window.shape[1] == 0:
            continue
        realized = window.iloc[-1].div(window.iloc[0]).sub(1.0).dropna().sort_values(ascending=False)
        if realized.empty:
            continue
        rows.append({
            "decision_date": decision_date, "next_decision_date": next_date,
            "oracle_top1": float(realized.iloc[0]),
            "oracle_top6": float(realized.head(min(6, len(realized))).mean()),
            "oracle_pool_size": int(len(realized)),
        })
    return pd.DataFrame(rows)


def _summary(monthly: pd.DataFrame, daily: pd.DataFrame, label: str) -> dict:
    values = pd.to_numeric(monthly.get("monthly_return", pd.Series(dtype=float)), errors="coerce").dropna()
    nav = (1.0 + pd.to_numeric(daily.get("daily_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)).cumprod()
    drawdown = float((nav / nav.cummax() - 1.0).min()) if len(nav) else 0.0
    return {
        "strategy": label, "months": int(len(values)),
        "mean_monthly_return": float(values.mean()) if len(values) else 0.0,
        "median_monthly_return": float(values.median()) if len(values) else 0.0,
        "max_monthly_return": float(values.max()) if len(values) else 0.0,
        "min_monthly_return": float(values.min()) if len(values) else 0.0,
        "hit_30pct_months": int((values >= 0.30).sum()),
        "max_drawdown": drawdown,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=Path("output/results_cn"))
    parser.add_argument("--base-dir", type=Path, default=Path("assets/data"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/verification/monthly_target_20260923"))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    selections = _load_selections(args.results_dir, args.start, args.end)
    codes = sorted(selections["stock_code"].astype(str).unique())
    service = MarketDataService(base_dir=str(args.base_dir), data_source="akshare")
    prices = service.warehouse.read_ohlcv(
        market="CN", asset_type="equity", frequency="daily", adjust="qfq",
        stock_code=codes, start_date=str(selections["trade_date"].min().date()),
        end_date=str((pd.Timestamp(selections["trade_date"].max()) + pd.Timedelta(days=40)).date()),
        columns=["stock_code", "trade_date", "close"],
    )
    if prices is None or prices.empty:
        raise ValueError("本地 qfq 日线为空，无法计算回测")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    strategy_frames = {}
    for label, gross in (("actual", None), ("gross_60", 0.60), ("gross_100", 1.00)):
        periods, daily = _portfolio_period_returns(selections, prices, gross)
        if periods.empty:
            continue
        daily["strategy"] = label
        daily["month"] = pd.to_datetime(daily["trade_date"]).dt.to_period("M").astype(str)
        monthly = daily.groupby("month", as_index=False)["daily_return"].apply(lambda s: (1.0 + s).prod() - 1.0).rename(columns={"daily_return": "monthly_return"})
        strategy_frames[label] = {"periods": periods, "daily": daily, "monthly": monthly}

    all_daily = pd.concat([item["daily"] for item in strategy_frames.values()], ignore_index=True)
    all_periods = pd.concat([item["periods"].assign(strategy=label) for label, item in strategy_frames.items()], ignore_index=True)
    all_monthly = pd.concat([item["monthly"].assign(strategy=label) for label, item in strategy_frames.items()], ignore_index=True)
    oracle = _oracle_periods(selections, prices)
    summaries = []
    for label, item in strategy_frames.items():
        summaries.append(_summary(item["monthly"], item["daily"], label))
    for label, column in (("oracle_top1", "oracle_top1"), ("oracle_top6", "oracle_top6")):
        if not oracle.empty:
            fake_monthly = oracle.assign(month=pd.to_datetime(oracle["decision_date"]).dt.to_period("M").astype(str)).groupby("month", as_index=False)[column].apply(lambda s: (1.0 + s).prod() - 1.0).rename(columns={column: "monthly_return"})
            summaries.append(_summary(fake_monthly, pd.DataFrame({"daily_return": []}), label))

    all_periods.to_csv(args.output_dir / "period_returns.csv", index=False)
    all_daily.to_csv(args.output_dir / "daily_returns.csv", index=False)
    all_monthly.to_csv(args.output_dir / "monthly_returns.csv", index=False)
    oracle.to_csv(args.output_dir / "oracle_upper_bound.csv", index=False)
    (args.output_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    lines = ["# 月内换股目标回测", "", f"- 决策日期: {len(sorted(selections['trade_date'].unique()))}", f"- 股票数: {len(codes)}", f"- 行情范围: {prices['trade_date'].min().date()} ~ {prices['trade_date'].max().date()}", "", "| 策略 | 月数 | 平均月收益 | 中位数 | 最大月收益 | 30%达标月数 | 最大回撤 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for item in summaries:
        lines.append(f"| {item['strategy']} | {item['months']} | {item['mean_monthly_return']:.2%} | {item['median_monthly_return']:.2%} | {item['max_monthly_return']:.2%} | {item['hit_30pct_months']} | {item['max_drawdown']:.2%} |")
    lines.extend(["", "说明：actual/ gross_60/ gross_100 是可执行权重情景；oracle_top1/oracle_top6 使用持有期结束后才知道的赢家，仅作事后上限，不代表可部署收益。", ""])
    (args.output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
