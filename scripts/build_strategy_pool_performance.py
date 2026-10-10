#!/usr/bin/env python3
"""Comparable gross selection tracking, NOT a broker-account performance report.

Only a consecutive suffix of archived close-time targets is tracked. Gaps break
the track; missing prices abort it. Daily rebalance occurs at next session open.
Cash is retained, weights are not rescaled, candidate rows with zero weight do
not count. Research replay remains separately labelled.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "output/strategy_pool/performance.json"
METHOD = ("名单目标权重毛收益跟踪（非账户净收益）：收盘信号于下一交易日开盘再平衡，"
          "剩余权重留现金；缺名单即断段，缺持仓价格即停止；未计手续费、滑点、涨跌停、"
          "停牌执行约束和整手。中证500价格指数同期归一到1，非全收益指数。")


def metrics(nav: list[float]) -> dict:
    x = np.asarray(nav, dtype=float)
    if len(x) < 2 or not np.isfinite(x).all() or (x <= 0).any():
        return {}
    total = float(x[-1] / x[0] - 1)
    dd = float((x / np.maximum.accumulate(x) - 1).min())
    n = len(x) - 1
    # One-year minimum for annualized risk ratios; tiny samples must not rank strategies.
    ann = float((x[-1] / x[0]) ** (252 / n) - 1) if n >= 252 else None
    returns = x[1:] / x[:-1] - 1
    sd = returns.std(ddof=1) if n > 1 else 0
    return {"total_return": total, "max_drawdown": dd, "annual_return": ann,
            "calmar": ann / abs(dd) if ann is not None and dd < 0 else None,
            "sharpe_rf0": float(returns.mean() / sd * np.sqrt(252)) if n >= 252 and sd > 0 else None}


def snapshots(folder: Path) -> dict[pd.Timestamp, pd.Series]:
    result = {}
    for path in sorted(folder.glob("????-??-??.csv")):
        day = pd.Timestamp(path.stem)
        frame = pd.read_csv(path, dtype={"stock_code": str})
        if frame.empty or "trade_date" not in frame or "target_weight" not in frame:
            continue
        if not pd.to_datetime(frame.trade_date).eq(day).all():
            raise ValueError(f"Snapshot date mismatch: {path.name}")
        if frame.stock_code.duplicated().any():
            raise ValueError(f"Duplicate names: {path.name}")
        weights = pd.to_numeric(frame.target_weight, errors="raise")
        if not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() > 1.000001:
            raise ValueError(f"Invalid weights: {path.name}")
        result[day] = pd.Series(weights.to_numpy(), index=frame.stock_code.to_numpy())
    return result


def consecutive_suffix(targets: dict, dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if not targets:
        return dates[:0]
    available = dates[dates <= max(targets)]
    if available.empty or available[-1] not in targets:
        return dates[:0]
    start = len(available) - 1
    while start > 0 and available[start - 1] in targets:
        start -= 1
    return available[start:]


def track(targets: dict, bench: pd.Series, bars: pd.DataFrame) -> dict:
    dates = pd.DatetimeIndex(bench.index)
    signals = consecutive_suffix(targets, dates)
    base = {"status": "insufficient_history", "reason": "至少需要一份已归档信号及次日行情。",
            "kind": "selection_tracking_gross", "benchmark_code": "000905.SH",
            "benchmark_name": "中证500", "methodology": METHOD, "dates": [], "nav": [],
            "benchmark_nav": [], "metrics": {}, "benchmark_metrics": {},
            "snapshot_count": len(targets), "consecutive_snapshots": len(signals)}
    if len(signals) == 0:
        return base
    # Latest snapshot needs no future bar. Older most-recent signal can accrue one day,
    # but never extend past a day that has no archived follow-up target.
    i = dates.get_loc(signals[-1])
    end = min(i + 1, len(dates) - 1)
    window = dates[dates.get_loc(signals[0]):end + 1]
    if len(window) < 2:
        return {**base, "start_date": str(signals[0].date()),
                "end_date": str(signals[-1].date())}
    prices = bars.set_index(["trade_date", "stock_code"])
    holdings = pd.Series(dtype=float)  # close-valued asset weights, portfolio starts in cash
    nav = [1.0]
    for previous, day in zip(window[:-1], window[1:]):
        target = targets[previous]
        target = target[target > 0]
        codes = sorted(set(target.index) | set(holdings.index))
        if not codes:
            nav.append(nav[-1])
            continue
        try:
            today = prices.loc[[(day, code) for code in codes]]
            if not np.isfinite(today[["open", "close"]]).all().all() or (today[["open", "close"]] <= 0).any().any():
                raise ValueError("invalid open/close")
            today = today.droplevel(0)
            if not holdings.empty:
                prev_close = prices.loc[[(previous, c) for c in holdings.index], "close"].droplevel(0)
                if not np.isfinite(prev_close).all() or (prev_close <= 0).any():
                    raise ValueError("invalid previous close")
                overnight = float((holdings * (today.open.reindex(holdings.index) / prev_close - 1)).sum())
            else:
                overnight = 0.0
            intraday = today.close.reindex(target.index) / today.open.reindex(target.index)
            growth = float(1 - target.sum() + (target * intraday).sum())
            nav.append(nav[-1] * (1 + overnight) * growth)
            holdings = target * intraday / growth
        except (KeyError, ValueError) as exc:
            return {**base, "status": "data_gap", "reason": f"{day.date()} 持仓价格缺失/无效：{exc}"}
    b = bench.reindex(window)
    if not np.isfinite(b).all() or (b <= 0).any():
        return {**base, "status": "data_gap", "reason": "中证500同期行情缺失/无效"}
    bnav = (b / b.iloc[0]).tolist()
    return {**base, "status": "ok" if len(nav) > 252 else "short_sample",
            "reason": "少于252个收益观测，仅展示累计收益和回撤，年化/卡玛/夏普暂不估计。" if len(nav) <= 252 else "",
            "start_date": str(window[0].date()), "end_date": str(window[-1].date()),
            "observations": len(nav) - 1, "dates": [str(d.date()) for d in window],
            "nav": nav, "benchmark_nav": bnav, "metrics": metrics(nav),
            "benchmark_metrics": metrics(bnav), "excess_return": nav[-1] / bnav[-1] - 1}


def replay(path: Path, bench: pd.Series, *, strategy_id: str = "jq_multi_factor_size_roe") -> dict:
    descriptions = {
        "jq_multi_factor_size_roe": "原聚宽低市值+ROE，rank(roe)-rank(market_cap)",
        "jq_roe_roa_diagnostic": "动态诊断候选ROE＋0.25×ROA，rank(roe)+0.25*rank(roa)，未过训练稳健门槛",
    }
    description = descriptions[strategy_id]
    frame = pd.read_csv(path, parse_dates=["trade_date"]).set_index("trade_date")
    frame = frame.sort_index()
    if frame.index.duplicated().any() or not np.isfinite(frame.nav).all() or (frame.nav <= 0).any():
        raise ValueError("Invalid replay NAV")
    expected = bench.loc[frame.index.min():frame.index.max()]
    if not expected.index.equals(frame.index):
        raise ValueError("Replay calendar differs from CSI500 calendar")
    nav = (frame.nav / frame.nav.iloc[0]).tolist()
    bnav = (expected / expected.iloc[0]).tolist()
    return {"status": "ok", "kind": "research_replay", "strategy_id": strategy_id,
            "benchmark_code": "000905.SH",
            "benchmark_name": "中证500", "start_date": str(frame.index[0].date()),
            "end_date": str(frame.index[-1].date()), "dates": [str(d.date()) for d in frame.index],
            "nav": nav, "benchmark_nav": bnav, "metrics": metrics(nav),
            "benchmark_metrics": metrics(bnav), "excess_return": nav[-1] / bnav[-1] - 1,
            "reason": "独立研究回放，不是每日名单跟踪或实盘业绩。",
            "methodology": description + "；15交易日调仓，次日开盘成交，含研究费用模型、分数股；非每日名单跟踪或生产账户。",
            "source": str(path.relative_to(ROOT))}


def build() -> dict:
    config = json.loads((ROOT / "config/strategies.json").read_text())
    raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    bench = raw[raw.ts_code == "000905.SH"].copy()
    bench.trade_date = pd.to_datetime(bench.trade_date)
    bench = bench.set_index("trade_date").close.sort_index()
    all_targets, errors = {}, {}
    for spec in config["strategies"]:
        try:
            all_targets[spec["id"]] = snapshots(ROOT / spec["history_dir"]) if spec.get("history_dir") else {}
        except (ValueError, KeyError) as exc:
            all_targets[spec["id"]] = {}
            errors[spec["id"]] = str(exc)
    codes = sorted({c for targets in all_targets.values() for weights in targets.values()
                    for c, w in weights.items() if w > 0})
    earliest = min((d for targets in all_targets.values() for d in targets), default=bench.index[-1])
    files = [str(p) for year in range(earliest.year, bench.index[-1].year + 1)
             for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "close"],
        filter=ds.field("stock_code").isin(codes)).to_pandas() if codes else pd.DataFrame()
    if not bars.empty:
        bars.trade_date = pd.to_datetime(bars.trade_date)
        bars = bars.drop_duplicates(["trade_date", "stock_code"])
    rows = {}
    for ident, targets in all_targets.items():
        result = track(targets, bench, bars)
        if ident in errors:
            result.update(status="data_gap", reason=errors[ident])
        result["latest_signal_date"] = str(max(targets).date()) if targets else None
        rows[ident] = {"tracking": result}
    replay_sources = {
        'jq_multi_factor_size_roe': ROOT / 'output/research/jq_multi_factor/nav_original_size_roe.csv',
        'jq_roe_roa_diagnostic': ROOT / 'output/research/jq_multi_factor_dynamic/nav_dynamic_diagnostic.csv',
    }
    for ident, path in replay_sources.items():
        if ident in rows and path.exists():
            rows[ident]['research'] = replay(path, bench, strategy_id=ident)
    tracks = [x["tracking"] for x in rows.values()]
    # Explicit shared window, never compare annualized numbers over different spans.
    start = max((x.get("start_date", "9999") for x in tracks), default="9999")
    end = min((x.get("end_date", "0000") for x in tracks), default="0000")
    common = {"status": "insufficient_history", "start_date": start, "end_date": end,
              "reason": "所有策略需有共同的已实现收益日期，才生成同窗比较。"}
    if start < end and all(x["nav"] for x in tracks):
        common["status"] = "ok"
        for entry in rows.values():
            x = entry["tracking"]
            indices = [i for i, d in enumerate(x["dates"]) if start <= d <= end]
            nav = [x["nav"][i] / x["nav"][indices[0]] for i in indices]
            bn = [x["benchmark_nav"][i] / x["benchmark_nav"][indices[0]] for i in indices]
            entry["common"] = {**x, "dates": [x["dates"][i] for i in indices],
                               "start_date": start, "end_date": end, "nav": nav, "benchmark_nav": bn,
                               "metrics": metrics(nav), "benchmark_metrics": metrics(bn),
                               "observations": len(nav)-1, "excess_return": nav[-1]/bn[-1]-1}
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "reference_trade_date": str(bench.index[-1].date()), "common_window": common, "strategies": rows}


if __name__ == "__main__":
    payload = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUT.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    temp.replace(OUT)
    for ident, row in payload["strategies"].items():
        x = row["tracking"]
        print(f"{ident}: {x['status']}, observations={x.get('observations', 0)}")
    print(f"performance -> {OUT.relative_to(ROOT)}")
