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


LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
LEDGER_METHOD = ("台账（selection_ledger.parquet）名单口径毛收益：决策日收盘信号 → 次日开盘建仓，"
                 "持有到下一个决策日的次日开盘（期间按收盘盯市）；未计手续费/滑点/涨跌停/整手。"
                 "`preselected` = 候选池等权 top-10（与 top_n=10 对齐）；`notify` = PK 后名单的目标权重。"
                 "中证500价格指数同期归一到1。")


def ledger_targets(source: str, top_n: int = 10) -> dict[pd.Timestamp, pd.Series]:
    """从台账取某个来源的逐决策日名单 → {决策日: 权重 Series}。

    为什么不用 `snapshots()`：台账里的决策日是**稀疏**的（月频/事件频，2025-09 起 16 个），
    而 `track()` 要求连续交易日都有归档名单 —— 稀疏名单在 `track()` 里会立刻断段，
    这正是 rot_arm 当初"只有 8 个观测"的原因。所以这里走"持有到下一个决策日"的复利口径。
    """
    if not LEDGER.exists():
        return {}
    frame = pd.read_parquet(LEDGER)
    frame = frame[frame["source"].astype(str) == source].copy()
    if frame.empty:
        return {}
    frame["decision_date"] = pd.to_datetime(frame["decision_date"])
    frame = frame.dropna(subset=["decision_date"])
    targets: dict[pd.Timestamp, pd.Series] = {}
    for day, group in frame.groupby("decision_date"):
        group = group.copy()
        weights = pd.to_numeric(group.get("weight"), errors="coerce")
        if weights.notna().any() and float(weights.fillna(0).sum()) > 0:
            picked = group.assign(_w=weights.fillna(0))
            picked = picked[picked["_w"] > 0]
            series = pd.Series(picked["_w"].to_numpy(), index=picked["stock_code"].astype(str).to_numpy())
        else:
            # 候选池没有权重：按 rank 取前 top_n 只**等权**
            ordered = group.sort_values("rank") if "rank" in group else group
            picked = ordered.head(top_n)
            series = pd.Series(1.0 / max(len(picked), 1), index=picked["stock_code"].astype(str).to_numpy())
        if len(series):
            targets[day] = series / series.sum() if series.sum() > 0 else series
    return targets


def ledger_curve(targets: dict[pd.Timestamp, pd.Series], bench: pd.Series, bars: pd.DataFrame,
                 kind: str) -> dict:
    """稀疏决策日名单 → 净值曲线：决策日次日开盘调仓，持有到下一个决策日的次日开盘。

    净值算法（**分段持有、逐段链接**，踩过的坑写在下面）：
      * 每个持有段用"建仓日开盘价"作基准，段内净值 = Σ wᵢ·(closeᵢ/openᵢ) + 现金权重；
      * 段与段之间**按段末净值链接**。
    第一版写成"每天把 Σw·(close/entry) 累乘到净值上"，结果把累计涨幅每天复利一次，
    实测 preselected 曲线跑出 +17,878% 的离谱数字 —— 累乘只能用于**日收益**，不能用于"入场以来收益"。
    """
    if not targets or bars.empty:
        return {"status": "data_gap", "reason": "台账或行情缺失"}
    days = sorted(targets)
    index = bench.index[(bench.index >= days[0]) & (bench.index <= bench.index[-1])]
    if len(index) < 5:
        return {"status": "data_gap", "reason": "共同窗口不足 5 个交易日"}
    opens = bars.pivot_table(index="trade_date", columns="stock_code", values="open", aggfunc="last")
    closes = bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last")

    dates: list[pd.Timestamp] = []
    nav: list[float] = []
    chain = 1.0                      # 段末净值（上一段链接进来的基准）
    plan = sorted(targets.items(), key=lambda item: item[0])
    cursor = 0
    period = None                    # {"weights": Series, "entry_open": Series}
    for position, day in enumerate(index):
        if position == 0:
            dates.append(day); nav.append(chain)
            continue
        prev_day = index[position - 1]
        # 到达"下一个决策日的次日"时换仓（用当日开盘价建仓）
        while cursor < len(plan) and plan[cursor][0] < day:
            weights = plan[cursor][1]
            row = opens.loc[day] if day in opens.index else None
            if row is not None:
                prices = row.reindex(weights.index)
                if prices.notna().all() and (prices > 0).all() and float(weights.sum()) > 0:
                    # 换仓：把**上一段的末值**接到新段上（段内以建仓开盘价为基准，段间按净值链接）
                    if nav:
                        chain = nav[-1]
                    period = {"weights": weights, "entry_open": prices}
            cursor += 1
        if period is None:
            dates.append(day); nav.append(chain)
            continue
        row = closes.loc[day] if day in closes.index else None
        prices = row.reindex(period["weights"].index) if row is not None else None
        if prices is None or not (prices.notna().all() and (prices > 0).all()):
            dates.append(day); nav.append(chain if not nav else nav[-1])   # 缺价顺延，不臆造
            continue
        invested = float((period["weights"].to_numpy()
                          * (prices.to_numpy() / period["entry_open"].to_numpy())).sum())
        cash = max(0.0, 1.0 - float(period["weights"].sum()))
        level = chain * (invested + cash)
        dates.append(day); nav.append(level)

    # 段末链接：下一段要以本段末净值为基准 —— 上面的 chain 只在段内使用，
    # 这里把每段的末值写回，用于下一次换仓（保持简单：整条曲线一次算完）
    window = bench.reindex(pd.DatetimeIndex(dates)).ffill()
    bnav = (window / window.iloc[0]).tolist() if len(window) else []
    result = {"status": "ok", "kind": kind, "strategy_id": "cn_ensemble",
              "benchmark_code": "000905.SH", "benchmark_name": "中证500",
              "start_date": str(pd.DatetimeIndex(dates)[0].date()),
              "end_date": str(pd.DatetimeIndex(dates)[-1].date()),
              "dates": [str(d.date()) for d in pd.DatetimeIndex(dates)], "nav": nav, "benchmark_nav": bnav,
              "metrics": metrics(nav), "benchmark_metrics": metrics(bnav),
              "observations": len(targets), "snapshot_count": len(targets),
              "latest_signal_date": str(max(targets).date()), "methodology": LEDGER_METHOD}
    if result["metrics"] and result["benchmark_metrics"]:
        result["excess_return"] = result["metrics"]["total_return"] - result["benchmark_metrics"]["total_return"]
    return result


def build() -> dict:
    config = json.loads((ROOT / "config/strategies.json").read_text())
    raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    bench = raw[raw.ts_code == "000905.SH"].copy()
    bench.trade_date = pd.to_datetime(bench.trade_date)
    bench = bench.set_index("trade_date").close.sort_index()
    all_targets, errors, no_tracking = {}, {}, set()
    for spec in config["strategies"]:
        try:
            if spec.get("no_tracking"):
                # 策略语义与"持有到下一个决策日"不同（如首板低吸是**次日开盘买、当日日内卖**），
                # 用名单跟踪口径会算出方向相反的曲线 —— 直接不算 tracking，曲线由 curve_source 给。
                all_targets[spec["id"]] = {}
                no_tracking.add(spec["id"])
            else:
                all_targets[spec["id"]] = snapshots(ROOT / spec["history_dir"]) if spec.get("history_dir") else {}
        except (ValueError, KeyError) as exc:
            all_targets[spec["id"]] = {}
            errors[spec["id"]] = str(exc)
    # 模型臂（P2.45-A）：台账里的 preselected / notify 是**稀疏决策日**名单，
    # 单独走 ledger_curve（持有到下一个决策日），与其它策略的 tracking 并列展示。
    # 接线来自声明层 `model_arms`（以前这里硬编码 ("preselected",10)/("notify",10)）：
    # 新增一条"模型臂"策略只要在 config/strategies.json 写 model_arms 即可。
    model_arms: dict[str, dict[str, dict]] = {}
    for _spec in config["strategies"]:
        _plan = _spec.get("model_arms") or {}
        if not _plan:
            continue
        _by_source = {}
        for _source in _plan.get("ledger_sources") or []:
            _targets = ledger_targets(_source, top_n=int(_plan.get("top_n") or 10))
            if _targets:
                _by_source[_source] = _targets
        model_arms[_spec["id"]] = _by_source
    codes = sorted({c for targets in list(all_targets.values()) +
                    [arm for arms in model_arms.values() for arm in arms.values()]
                    for weights in targets.values() for c, w in weights.items() if w > 0})
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
        if ident in no_tracking:
            result.update(status="insufficient_history",
                          reason="该策略的持仓语义不是「持有到下一个决策日」，不做名单跟踪"
                                 "（曲线见 curve_source 给出的原始口径逐笔模拟）。")
        rows[ident] = {"tracking": result}
    # ── 模型臂曲线：候选池（上游）与 PK 后名单（下游）各一条，同窗口、同基准 ────────
    for _ident, _sources in model_arms.items():
        if _ident not in rows or not _sources:
            continue
        _arms = {_source: ledger_curve(_targets, bench, bars, kind=f"ledger_tracking_gross:{_source}")
                 for _source, _targets in _sources.items()}
        rows[_ident]["model_arms"] = _arms
        # 上游（候选池）放进 `research` 槽位：与 jq/rot_arm 的曲线同一个展示位置，
        # 页面无需改结构就能看到"模型臂 vs 中证500"；下游对照在 model_arms 里（报告给两条）。
        _upstream = _arms.get("preselected") or _arms.get("notify")
        if _upstream and _upstream.get("status") == "ok" and "research" not in rows[_ident]:
            rows[_ident]["research"] = {**_upstream, "note": "模型臂上游（候选池等权 top-10）"}

    # ── 独立回放曲线（curve.replay）：策略原始口径的 nav.csv，接线写在 config 里 ──────
    for _spec in config["strategies"]:
        _rel = (_spec.get("curve") or {}).get("replay", {}).get("nav_csv")
        if not _rel or _spec["id"] not in rows:
            continue
        _path = ROOT / str(_rel)
        if _path.exists():
            rows[_spec["id"]]["research"] = replay(_path, bench, strategy_id=_spec["id"])
    # ── 模拟盘（影子组合）净值 → 长窗口对照曲线 ────────────────────────────────
    # 为什么需要：`tracking` 要求**连续交易日**都有归档名单（见 METHOD），而按决策日归档的策略
    # （rot_arm 这类，决策日稀疏到每周/每月）只能拿到"最近几个连续交易日"的样本
    # —— 实测 rot_arm 只有 8 个观测，用户看到的就是"没有中证500 对比曲线"。
    # 影子组合是同一套执行假设（T+1 开盘、佣金/印花税/滑点、整手）的模拟账户，
    # 历史可回填到 2025-09，正好用来做**多期**的 中证500 对照；口径与 `tracking` 明确区分。
    # 接线来自声明层 `curve.shadow`（以前这里硬编码三条）：
    # 新增策略只要在 config 里写 curve.shadow.nav_csv / history_csv。
    shadow_sources = [(spec["id"],
                       (spec.get("curve") or {}).get("shadow", {}).get("nav_csv", ""),
                       (spec.get("curve") or {}).get("shadow", {}).get("history_csv", ""))
                      for spec in config["strategies"] if (spec.get("curve") or {}).get("shadow")]
    for ident, nav_rel, hist_rel in shadow_sources:
        nav_path, hist_path = ROOT / nav_rel, ROOT / hist_rel
        if ident not in rows or not nav_path.exists():
            continue
        frame = pd.read_csv(nav_path, parse_dates=["asof_date"]).set_index("asof_date").sort_index()
        if "nav" not in frame.columns or frame.empty:
            continue
        start = frame.index[0]
        if hist_path.exists():
            hist = pd.read_csv(hist_path, parse_dates=["trade_date"])
            if not hist.empty:
                start = max(start, hist["trade_date"].min())      # 从**首个决策日**起（之前是空仓平线）
        frame = frame[frame.index >= start]
        window = bench.reindex(frame.index).dropna()
        frame = frame.reindex(window.index)
        if len(frame) < 2 or not (frame["nav"] > 0).all():
            continue
        nav = (frame["nav"] / frame["nav"].iloc[0]).tolist()
        bnav = (window / window.iloc[0]).tolist()
        rows[ident]["research"] = {
            "status": "ok", "kind": "shadow_account_net", "strategy_id": ident,
            "benchmark_code": "000905.SH", "benchmark_name": "中证500",
            "start_date": str(frame.index[0].date()), "end_date": str(frame.index[-1].date()),
            "dates": [str(d.date()) for d in frame.index], "nav": nav, "benchmark_nav": bnav,
            "metrics": metrics(nav), "benchmark_metrics": metrics(bnav),
            "observations": len(nav) - 1, "excess_return": nav[-1] / bnav[-1] - 1,
            "reason": ("模拟盘净值（统一执行假设：T+1 开盘成交、含佣金/印花税/滑点、整手），"
                       "不是名单毛收益跟踪、也不是实盘业绩。"),
            "methodology": ("影子组合 `run_shadow_variants.py` 的日度净值（起始资金 45,000），"
                            "中证500价格指数同期归一到 1；口径见 output/paper_trading/shadow/summary.json。"),
            "source": nav_rel,
        }
    # ── curve_source：策略**原始口径**的逐笔模拟曲线（政策略语义与模拟盘引擎不同时用）──
    for spec in config["strategies"]:
        source = spec.get("curve_source") or {}
        ident = spec["id"]
        path = ROOT / str(source.get("nav_csv", "")) if source.get("nav_csv") else None
        if ident not in rows or path is None or not path.exists():
            continue
        frame = pd.read_csv(path)
        if "asof_date" not in frame.columns or "nav" not in frame.columns:
            continue
        frame["asof_date"] = pd.to_datetime(frame["asof_date"])
        frame = frame.set_index("asof_date")["nav"].sort_index()
        window = bench.reindex(frame.index).dropna()
        frame = frame.reindex(window.index)
        if len(frame) < 2 or not (frame > 0).all():
            continue
        nav = (frame / frame.iloc[0]).tolist()
        bnav = (window / window.iloc[0]).tolist()
        rows[ident]["research"] = {
            "status": "ok" if len(nav) > 252 else "short_sample", "kind": str(source.get("kind", "strategy_rule_net")),
            "strategy_id": ident, "benchmark_code": "000905.SH", "benchmark_name": "中证500",
            "start_date": str(frame.index[0].date()), "end_date": str(frame.index[-1].date()),
            "dates": [str(d.date()) for d in frame.index], "nav": nav, "benchmark_nav": bnav,
            "metrics": metrics(nav), "benchmark_metrics": metrics(bnav),
            "observations": len(nav) - 1, "excess_return": nav[-1] / bnav[-1] - 1,
            "reason": str(source.get("reason", "策略原始口径的逐笔模拟（不是模拟盘账户）。")),
            "methodology": str(source.get("methodology", "")), "source": str(source.get("nav_csv")),
        }

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
