#!/usr/bin/env python3
"""P1.24 Phase 0 v2：从信号层重算每个 sleeve 的**完整候选域**，再算共线性与净额化空间。

与 v1（`sleeve_diagnostics.py`，只看"最终进组合的标的"）的区别：
  * v1 实测所有 sleeve 两两 Jaccard = 0（零重叠）—— 但那只是**组合层**的交集；
  * v2 直接按注册表的 `state` 规则在全市场重算每个 sleeve 的候选域，回答"伪分散到底存不存在"。

做法：把日线 qfq 面板算出 state 规则需要的指标列（列名与注册表规则一致），
再调用 `factor_engine.strategies.state_mask()` —— 与生产同源，不另写一套判定逻辑。

已知近似（报告中会标注）：
  * `flow_z` 用资金流特征 `moneyflow_net_z_20d` 代理（生产用 clean panel 的 flow_z）；
  * `signal_relative20` 用"个股 20 日收益 − 全市场中位"代理（生产是行业相对）；
  * `signal_extension` 用"收盘 / 20 日最高 − 1"代理；
  * `signal_rebound_atr` = (close − 126 日最低) / ATR20。

用法：
  uv run python scripts/sleeve_candidate_diagnostics.py --start 2024-10-01 --freq M
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")


def load_bars(start_year: int = 2024) -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = [str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")]
    files = [f for f in files if any(f"year={y}/" in f for y in range(start_year, 2027))]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount"])
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame[frame["stock_code"].str[:3].isin(MAIN_PREFIX)]
    return frame


def build_indicator_frame(bars: pd.DataFrame) -> dict:
    """把长表转成宽表并算出 state 规则需要的指标。"""
    panels = {}
    for col in ("high", "low", "close", "volume", "amount"):
        panels[col] = bars.pivot_table(index="trade_date", columns="stock_code", values=col, aggfunc="last")
    close, high, low, volume, amount = (panels["close"], panels["high"], panels["low"], panels["volume"], panels["amount"])

    tr = pd.concat([
        (high - low).stack(),
        (high - close.shift()).abs().stack(),
        (low - close.shift()).abs().stack(),
    ], axis=1).max(axis=1).unstack()
    atr20 = tr.rolling(20, min_periods=10).mean()

    indicators = {
        "close": close,
        "dist_from_120d_low": close / low.rolling(120, min_periods=60).min() - 1,
        "dist_from_60d_high": close / high.rolling(60, min_periods=30).max() - 1,
        "volume_ratio_20d": volume / volume.rolling(20, min_periods=10).mean(),
        "signal_runup_5d": close.pct_change(5, fill_method=None),
        "signal_rebound_atr": (close - low.rolling(126, min_periods=60).min()) / atr20,
        "signal_extension": close / high.rolling(20, min_periods=10).max() - 1,
        "ret20": close.pct_change(20, fill_method=None),
        "amount_ma20": amount.rolling(20, min_periods=10).mean(),
    }
    indicators["signal_volume_ratio_20"] = indicators["volume_ratio_20d"]
    universe_ret20 = indicators["ret20"].median(axis=1)
    indicators["signal_relative20"] = indicators["ret20"].sub(universe_ret20, axis=0)
    return indicators


def load_flow_proxy() -> pd.DataFrame | None:
    path = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path, columns=["trade_date", "stock_code", "moneyflow_net_z_20d"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.pivot_table(index="trade_date", columns="stock_code", values="moneyflow_net_z_20d", aggfunc="last")


def rebalance_dates(index: pd.DatetimeIndex, start: str, end: str, freq: str) -> list[pd.Timestamp]:
    window = index[(index >= start) & (index <= end)]
    if freq == "M":
        keys = window.to_period("M")
        return [window[keys == k].max() for k in sorted(set(keys))]
    return list(window[:: int(freq)])


def main() -> int:
    parser = argparse.ArgumentParser(description="P1.24 Phase 0 v2：sleeve 候选域诊断")
    parser.add_argument("--start", default="2024-10-01")
    parser.add_argument("--end", default=str(pd.Timestamp.today().date()))
    parser.add_argument("--freq", default="M", help="M=月末；或整数步长")
    parser.add_argument("--out", type=Path, default=ROOT / "output/research")
    args = parser.parse_args()

    import tomllib

    from factor_engine.strategies import load_strategies, state_mask

    specs = load_strategies(tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb")))
    signal_specs = [s for s in specs.values() if s.kind == "signal" and s.state]
    print(f"[sleeve-v2] 参与诊断的信号腿：{[(s.strategy_id, s.status) for s in signal_specs]}", flush=True)

    bars = load_bars()
    print(f"[sleeve-v2] 日线 {len(bars):,} 行 / {bars['stock_code'].nunique()} 只 / "
          f"{bars['trade_date'].min().date()} ~ {bars['trade_date'].max().date()}", flush=True)
    indicators = build_indicator_frame(bars)
    flow = load_flow_proxy()
    dates = rebalance_dates(indicators["close"].index, args.start, args.end, args.freq)
    print(f"[sleeve-v2] 诊断日 {len(dates)} 个：{dates[0].date()} ~ {dates[-1].date()}", flush=True)

    rows, members_by_date = [], {}
    for date in dates:
        snapshot = {name: frame.loc[date] for name, frame in indicators.items()}
        if flow is not None and date in flow.index:
            snapshot["flow_z"] = flow.loc[date]
        frame = pd.DataFrame(snapshot)
        date_members = {}
        for spec in signal_specs:
            try:
                mask = state_mask(frame, spec, allow_missing=True)
            except Exception as exc:  # 列缺失等
                print(f"[sleeve-v2] {spec.strategy_id} @ {date.date()} 跳过：{type(exc).__name__}: {exc}", flush=True)
                continue
            codes = set(frame.index[mask.fillna(False)])
            date_members[spec.strategy_id] = codes
            sub = frame.loc[list(codes)] if codes else frame.iloc[0:0]
            rows.append({
                "trade_date": str(date.date()), "sleeve": spec.strategy_id, "status": spec.status,
                "candidates": len(codes),
                "median_amount_ma20": float(sub["amount_ma20"].median()) if len(sub) else np.nan,
                "median_volume_ratio_20d": float(sub["volume_ratio_20d"].median()) if len(sub) else np.nan,
            })
        members_by_date[str(date.date())] = date_members
        if len(rows) % 40 == 0:
            print(f"[sleeve-v2] {date.date()} 完成，累计 {len(rows)} 行", flush=True)

    candidates = pd.DataFrame(rows)
    coll_rows, net_rows = [], []
    for date, members in members_by_date.items():
        for a, b in combinations(sorted(members), 2):
            inter, union = members[a] & members[b], members[a] | members[b]
            coll_rows.append({"trade_date": date, "sleeve_a": a, "sleeve_b": b,
                              "jaccard": len(inter) / len(union) if union else np.nan,
                              "overlap": len(inter)})
        counter: dict = {}
        for sleeve, codes in members.items():
            for code in codes:
                counter.setdefault(code, set()).add(sleeve)
        multi = {c: s for c, s in counter.items() if len(s) >= 2}
        total = sum(len(c) for c in members.values())
        net_rows.append({"trade_date": date, "candidates_total": total, "codes_multi_sleeve": len(multi),
                         "multi_share": len(multi) / len(counter) if counter else np.nan})
    collinearity = pd.DataFrame(coll_rows)
    netting = pd.DataFrame(net_rows)
    summary = (collinearity.groupby(["sleeve_a", "sleeve_b"])
               .agg(days=("trade_date", "nunique"), jaccard_mean=("jaccard", "mean"),
                    overlap_mean=("overlap", "mean")).reset_index().sort_values("jaccard_mean", ascending=False)
               if not collinearity.empty else collinearity)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"sleeve_candidate_diagnostics_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(out_dir / "candidates.csv", index=False)
    collinearity.to_csv(out_dir / "candidate_collinearity.csv", index=False)
    summary.to_csv(out_dir / "candidate_collinearity_summary.csv", index=False)
    netting.to_csv(out_dir / "candidate_netting.csv", index=False)

    lines = ["# P1.24 Phase 0 v2：sleeve **候选域** 诊断（信号层重算）", "",
             f"- 诊断日：{len(dates)} 个（{dates[0].date()} ~ {dates[-1].date()}，{args.freq} 频）",
             f"- 参与腿：{', '.join(f'{s.strategy_id}({s.status})' for s in signal_specs)}", "",
             "## 1) 候选域规模（每个 sleeve 每日候选数）", "",
             "| sleeve | 状态 | 诊断日 | 候选数中位 | 候选数最大 | 20日均额中位(万元) |",
             "|---|---|---|---|---|---|"]
    for sleeve, group in candidates.groupby("sleeve"):
        lines.append(f"| {sleeve} | {group['status'].iloc[0]} | {group['trade_date'].nunique()} | "
                     f"{group['candidates'].median():.0f} | {group['candidates'].max()} | "
                     f"{group['median_amount_ma20'].median()/1e4:.0f} |")
    lines += ["", "## 2) 候选域共线性（两两 Jaccard，按均值降序）", "",
              "| sleeve A | sleeve B | 天数 | 平均 Jaccard | 平均重叠标的数 |", "|---|---|---|---|---|"]
    for r in (summary.head(12).itertuples() if not summary.empty else []):
        lines.append(f"| {r.sleeve_a} | {r.sleeve_b} | {r.days} | {r.jaccard_mean:.4f} | {r.overlap_mean:.1f} |")
    if not netting.empty:
        lines += ["", "## 3) 净额化空间（同日多 sleeve 重叠标的）", "",
                  f"- 平均重叠标的数：{netting['codes_multi_sleeve'].mean():.1f}",
                  f"- 重叠标的占候选并集的平均比例：{netting['multi_share'].mean():.2%}",
                  f"- 最多重叠标的的一天：{netting.loc[netting['codes_multi_sleeve'].idxmax(), 'trade_date']}"
                  f"（{int(netting['codes_multi_sleeve'].max())} 只）"]
    lines += ["", "## 4) 与 v1（组合层）的对比 —— 本报告的核心问题", "",
              "- v1（`sleeve_diagnostics.py`，只看最终进组合的标的）：所有 sleeve 两两 **Jaccard = 0**。",
              f"- v2（本报告，看完整候选域）：平均 Jaccard 最高的一对是 "
              f"{(summary.iloc[0]['sleeve_a'] + ' vs ' + summary.iloc[0]['sleeve_b']) if not summary.empty else 'n/a'} = "
              f"{summary.iloc[0]['jaccard_mean']:.4f}（{summary.iloc[0]['overlap_mean']:.1f} 只/日）。",
              "- 判读：若候选域重叠显著高于组合层，说明**组合层零重叠是选择器（PK/约束）造成的，不是策略逻辑本身不重叠** ——",
              "  那么『sleeve 之间反向订单对冲降换手』的收益仍然接近零（最终持仓不重叠），",
              "  但『伪分散』的判断要以候选域为准：候选域重叠高 = 多腿在同一批股票里挑，风格暴露重叠。"]
    lines += ["", "## 5) 口径与近似（必读）", "",
              "1. 指标口径与生产**同源**（直接调用 `factor_engine.strategies.state_mask`），但输入列是本地重算的近似：",
              "   `flow_z` ← `moneyflow_net_z_20d`；`signal_relative20` ← 全市场中位相对（生产为行业相对）；",
              "   `signal_extension` ← 收盘/20 日最高；`signal_rebound_atr` ← (close − 126 日低)/ATR20。",
              "2. 本报告只做 state 过滤，不含 recipe 形态细节与可交易性约束（停牌/涨跌停/成交额门槛），",
              "   因此候选数是**上界**；要与组合层对比，需再过一遍可交易口径。",
              f"3. 输出目录：`{out_dir.relative_to(ROOT)}`"]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "dates": len(dates), "sleeves": [s.strategy_id for s in signal_specs],
        "candidate_median": {k: float(v) for k, v in candidates.groupby("sleeve")["candidates"].median().items()},
        "top_collinear_pair": (summary.iloc[0][["sleeve_a", "sleeve_b", "jaccard_mean"]].to_dict()
                               if not summary.empty else None),
        "netting_multi_share_mean": float(netting["multi_share"].mean()) if not netting.empty else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[sleeve-v2] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
