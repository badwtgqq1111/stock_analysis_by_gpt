#!/usr/bin/env python
"""四个信号 sleeve 的逐年 edge 体检（2022-2026），给出"加预算 / 维持 / 降级"建议。

口径：T+1 开盘进场，收益 = 市场等权相对超额（与 strategy_labels 的 forward_excess_return 一致）；
      事件定义尽量复刻各 sleeve 的 recipe 条件（见下方注释），指标 = 逐年 20 日超额均值/命中率/t。
判定（写在报告里，作为预算调整依据）：
      近两年（2025-2026）20d 超额 > +0.2% 且 t > 1.5  → 可加预算
      -0.2% ~ +0.2% 或 |t| 不显著                → 维持小预算观察
      < -0.2% 或 t < -1.5                        → 降级/关闭
输出：output/research/sleeve_health/{summary.csv, by_year.csv, REPORT.md}
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402

OUT = ROOT / "output/research/sleeve_health"
START = "2022-01-01"
END = "2026-09-30"
HORIZONS = (5, 10, 20)
HORIZON_BY_SLEEVE = {"donchian_breakout": 5, "limit_momentum": 5, "value_reversal": 20,
                     "bottom_momentum": 10, "startup_low_rebound": 20}


def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.sort_values(["stock_code", "trade_date"]).copy()
    g = df.groupby("stock_code", sort=False)
    close, high, low, open_, vol = (df["close"], df["high"], df["low"], df["open"], df["volume"])
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=30).mean())
    df["high20_prev"] = g["high"].transform(lambda s: s.rolling(20, min_periods=10).max().shift(1))
    df["low126"] = g["low"].transform(lambda s: s.rolling(126, min_periods=60).min())
    df["low252"] = g["low"].transform(lambda s: s.rolling(252, min_periods=120).min())
    df["vol20"] = g["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["volume_ratio_20"] = vol / df["vol20"]
    df["return_1d"] = close / g["close"].transform(lambda s: s.shift(1)) - 1.0
    df["return_5d"] = close / g["close"].transform(lambda s: s.shift(5)) - 1.0
    df["return_60d"] = close / g["close"].transform(lambda s: s.shift(60)) - 1.0
    df["return_120d"] = close / g["close"].transform(lambda s: s.shift(120)) - 1.0
    df["return_252d"] = close / g["close"].transform(lambda s: s.shift(252)) - 1.0
    df["drawdown_60d"] = close / g["high"].transform(lambda s: s.rolling(60, min_periods=30).max()) - 1.0
    tr = pd.concat([(high - low), (high - close.groupby(df["stock_code"]).shift(1)).abs(),
                    (low - close.groupby(df["stock_code"]).shift(1)).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.groupby(df["stock_code"]).transform(lambda s: s.rolling(14, min_periods=7).mean())
    df["rebound_atr"] = (close - df["low126"]) / df["atr14"]
    df["relative20"] = df["return_1d"] * 0  # 占位，下面按行业/市场统一用 return_20d 代理
    df["return_20d"] = close / g["close"].transform(lambda s: s.shift(20)) - 1.0
    df["market_trend_20d"] = df.groupby("trade_date")["return_20d"].transform("median")
    # 前向收益（T+1 开盘进场）
    # 价格护栏：存储里存在 open/close = 0 的行（2024 年就有），直接相除会产生 ±inf，并污染
    # 当日的横截面均值（实测把整年 20 日超额算成 -inf）。非正价格一律置 NaN。
    entry = g["open"].transform(lambda s: s.shift(-1))
    entry = entry.where(entry > 0)
    for horizon in HORIZONS:
        exit_close = g["close"].transform(lambda s: s.shift(-(1 + horizon)))
        exit_close = exit_close.where(exit_close > 0)
        fwd = exit_close / entry - 1.0
        fwd = fwd.replace([np.inf, -np.inf], np.nan)
        df[f"fwd_{horizon}d"] = fwd
        df[f"excess_{horizon}d"] = fwd - df.groupby("trade_date")[f"fwd_{horizon}d"].transform("mean")
    return df


def events(df: pd.DataFrame) -> dict[str, pd.Series]:
    """各 sleeve 的事件定义（尽量对齐 recipe 参数）。"""
    return {
        # donchian_pullback / range_breakout：刚突破 20 日通道上沿 + 放量 + 尾部保护
        "donchian_breakout": (
            (df["close"] > df["high20_prev"])
            & (df["close"] / df["high20_prev"] - 1.0 <= 0.05)
            & (df["return_5d"] <= 0.20)
            & (df["volume_ratio_20"] >= 1.30)
        ).fillna(False),
        # momentum_sleeve：严格的"封板"定义——主板 10% 板 [9.5%,10.5%] 或 20% 板 [19.5%,20.5%]，
        # 且该股至少有 60 根历史（排除无涨跌幅限制的新股/次新），并要求市场上行。
        # （初版用 return_1d>=9.5% 太松，把无限制日的暴涨也算进来，把 5 日超额压到 −1.2%。）
        "limit_momentum": (
            ((df["return_1d"].between(0.095, 0.105)) | (df["return_1d"].between(0.195, 0.205)))
            & (df.groupby("stock_code").cumcount() >= 60)
            & (df["market_trend_20d"] >= 0.0)
        ).fillna(False),
        # reversal_sleeve：长期回撤 + 放量修复
        "value_reversal": (
            (df["return_120d"] <= -0.10) & (df["return_252d"] <= -0.08) & (df["volume_ratio_20"] >= 1.20)
        ).fillna(False),
        # bottom_momentum_sleeve：半年低点右侧确认 + 相对强度 + 放量
        "bottom_momentum": (
            (df["rebound_atr"] >= 1.0) & (df["return_20d"] >= 0.0) & (df["volume_ratio_20"] >= 1.10)
        ).fillna(False),
        "startup_low_rebound": (
            (df["close"] / df["low126"] - 1.0).between(0.05, 0.30)
            & (df["return_60d"] <= 0.35)
            & (df["close"] / df["high20_prev"] - 1.0 <= 0.0)
        ).fillna(False),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date=START, end_date=END,
                                    columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume"])
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    print(f"[bars] {len(bars):,} 行 / {bars.stock_code.nunique()} 只 / "
          f"{bars.trade_date.min().date()}~{bars.trade_date.max().date()}", flush=True)
    df = build_features(bars)
    print("[features] 已构造（ma/通道/ATR/量比/前向超额）", flush=True)

    masks = events(df)
    rows, yearly = [], []
    for name, mask in masks.items():
        sub = df[mask]
        per_day = df.assign(_e=mask).groupby("trade_date")["_e"].sum()
        active_days = int((per_day > 0).sum())
        row = {"sleeve": name, "events": int(mask.sum()), "active_days": active_days,
               "events_per_active_day": round(float(per_day[per_day > 0].mean()), 1) if active_days else 0.0}
        for horizon in HORIZONS:
            column = f"excess_{horizon}d"
            series = sub[column].dropna()
            row[f"excess_{horizon}d_%"] = round(float(series.mean()) * 100, 3)
            row[f"hit_{horizon}d"] = round(float((series > 0).mean()), 3)
            if len(series) > 5 and series.std() > 0:
                row[f"t_{horizon}d"] = round(float(series.mean() / series.std() * np.sqrt(len(series)) / np.sqrt(horizon)), 2)
        by_year = (sub.assign(year=sub["trade_date"].dt.year)
                   .groupby("year")
                   .agg(events=("stock_code", "size"),
                        excess_5d=("excess_5d", "mean"), hit_5d=("excess_5d", lambda s: float((s > 0).mean())),
                        excess_20d=("excess_20d", "mean"), hit_20d=("excess_20d", lambda s: float((s > 0).mean())),
                        t_20d=("excess_20d", lambda s: float(s.mean() / s.std() * np.sqrt(len(s)) / np.sqrt(20)) if s.std() else np.nan)))
        by_year["sleeve"] = name
        yearly.append(by_year.reset_index())
        # 近两年判定
        # 按"各 sleeve 设计持有期"判定（limit_momentum/donchian 是 5 日腿，value_reversal 20 日，bottom 10 日）
        designed = HORIZON_BY_SLEEVE.get(name, 20)
        column = f"excess_{designed}d"
        row["designed_horizon_d"] = designed
        recent = sub[sub["trade_date"] >= "2025-01-01"][column].dropna()
        mean_recent = float(recent.mean()) * 100 if len(recent) else float("nan")
        t_recent = (float(recent.mean() / recent.std() * np.sqrt(len(recent)) / np.sqrt(designed))
                    if len(recent) > 5 and recent.std() > 0 else float("nan"))
        if not np.isfinite(mean_recent):
            verdict = "样本不足"
        elif mean_recent > 0.2 and t_recent > 1.5:
            verdict = "可加预算"
        elif mean_recent < -0.2 or t_recent < -1.5:
            verdict = "降级/关闭"
        else:
            verdict = "维持小预算观察"
        row.update({"recent_designed_%": round(mean_recent, 3), "recent_t": round(t_recent, 2), "verdict": verdict})
        rows.append(row)
    summary = pd.DataFrame(rows).sort_values("recent_designed_%", ascending=False)
    summary.to_csv(OUT / "summary.csv", index=False)
    yearly_frame = pd.concat(yearly, ignore_index=True)
    yearly_frame.to_csv(OUT / "by_year.csv", index=False)
    pd.set_option("display.width", 240)
    print("\n" + summary.to_string(index=False), flush=True)
    print("\n逐年 20 日超额（%）:\n" + yearly_frame.pivot_table(index="sleeve", columns="year", values="excess_20d").mul(100).round(3).to_string(), flush=True)
    (OUT / "REPORT.md").write_text(
        "# 信号 sleeve 逐年 edge 体检（2022-2026）\n\n"
        "口径：T+1 开盘进场、市场等权相对超额；事件定义对齐各 recipe 参数。\n"
        "判定：近两年 20d 超额 >+0.2% 且 t>1.5 → 可加预算；< -0.2% 或 t<-1.5 → 降级/关闭；其余维持小预算观察。\n\n"
        + summary.to_markdown(index=False) + "\n\n## 逐年 20 日超额（%）\n\n"
        + yearly_frame.pivot_table(index="sleeve", columns="year", values="excess_20d").mul(100).round(3).to_markdown()
        + "\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
