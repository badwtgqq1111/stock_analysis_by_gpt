"""实盘镜像账户（live mirror account）：把**真实券商持仓**接进同一套盯市口径。

为什么单独一个账户，而不是并进 `output/paper_trading/account/`
------------------------------------------------------------
那个账户是「策略从空仓 45,000 按当日名单下单」的**前向**口径，它的净值曲线是
「如果照着名单下单会怎样」。真实持仓是人做的决策 + 已实现亏损，和名单没有因果关系；
把两者塞进同一个账户，曲线会在快照日出现一次无解释的跳变，之后也没法和影子变体
做同窗口配对（仓库自己的对照纪律就废了）。所以这里给实盘单开一个账户：

* **同一套执行假设**（`config/execution_assumptions_cn.json`）—— 只做「同口径可比」，不做合并；
* 快照日之前**不回填**（买入日期未知，回填就是编数据）；曲线从快照日起逐日累积，
  观测 < 30 一律标 `short_sample`；
* 账户级收益用**推算初始本金**做分母：`成本 + 现金 + |已实现亏损|`
  （已实现亏损已经从账户价值里扣掉了，不加回去就会把分母算小、把亏损率说轻）。

三个纯函数：`account_state`（算术）、`mark_to_market`（逐日盯市）、`advice_rows`（建议规则）。
所有 IO 都在 `scripts/build_live_mirror.py` 里，本模块不碰网络也不碰文件。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

import pandas as pd

REQUIRED_POSITION_FIELDS = ("stock_code", "shares", "cost_price", "last_price")
BACKFILL_TOPN = 3  # 最终名单里 rank ≤ 这个档才给「买入」

# 建议规则（写死在这里，产物里逐条带 `rule` 字段，任何人可以拿快照复算）：
#   R0 最终名单 rank ≤ 3           → 买入（模型最高分档，可加到单票上限）
#   R1 在最新生产最终名单里        → 持有（模型仍在持有它）
#   R2 只在候选池里、没进最终名单  → 持有（观察：模型选进来了但没拿到权重）
#   R3 两处都不在 + 20 日动量为负  → 卖出（模型不要了且动能转弱）
#   R4 两处都不在 + 20 日动量非负  → 持有（模型不要了但动能还在，降级观察）
#   R5 没有日线可用                → 持有（数据不足，不给动作）
ADVICE_RULES = {
    "R0": f"在最终名单里且 rank <= {BACKFILL_TOPN} -> 买入",
    "R1": "在最新生产最终名单里 -> 持有",
    "R2": "只在候选池（没拿到权重）-> 持有（观察）",
    "R3": "两处都不在且 20 日动量为负 -> 卖出",
    "R4": "两处都不在但 20 日动量非负 -> 持有（降级观察）",
    "R5": "缺日线数据 -> 持有（数据不足）",
}
MAX_WEIGHT_SINGLE = 0.12  # 与 config/cn_pipeline.toml 的 portfolio_constraints.max_weight_concentrated 同源


def _ranks(picks: Mapping[str, object] | Iterable[str] | None) -> dict[str, int | None]:
    """名单归一化成 `{代码: rank}`：给 mapping 就用它，给纯列表 rank 记 None。"""
    if picks is None:
        return {}
    if isinstance(picks, Mapping):
        out: dict[str, int | None] = {}
        for code, rank in picks.items():
            try:
                out[str(code)] = int(rank)
            except (TypeError, ValueError):
                out[str(code)] = None
        return out
    return {str(c): None for c in picks}


def validate_snapshot(snapshot: dict) -> dict:
    """快照的必要字段校验：缺什么就直说是哪只票缺什么，不要让它变成 nan 静默流下去。"""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot 必须是 dict")
    for field in ("asof", "cash", "positions"):
        if field not in snapshot:
            raise ValueError(f"snapshot 缺字段 {field!r}")
    positions = snapshot["positions"]
    if not isinstance(positions, list) or not positions:
        raise ValueError("snapshot.positions 必须是非空列表")
    for item in positions:
        missing = [f for f in REQUIRED_POSITION_FIELDS if item.get(f) in (None, "")]
        if missing:
            raise ValueError(f"持仓 {item.get('stock_code') or item.get('name') or '?'} 缺字段 {missing}")
        if float(item["shares"]) <= 0:
            raise ValueError(f"持仓 {item['stock_code']} 股数必须为正")
    for item in snapshot.get("cleared_positions", []) or []:
        if item.get("realized_pnl") in (None, ""):
            raise ValueError(f"已清仓 {item.get('stock_code') or item.get('name')} 缺 realized_pnl")
    return snapshot


def account_state(snapshot: dict) -> dict:
    """账户算术：成本 / 市值 / 浮盈 / 已实现 / 推算初始本金 / 累计收益 / 权重。

    券商 App 的「浮动盈亏」把**已清仓的已实现亏损**也算进去（中泰的这一版就是这么显示），
    所以这里分成两块单独报，并和券商口径做一次对账（`reconciliation`）。
    """
    validate_snapshot(snapshot)
    rows = []
    for item in snapshot["positions"]:
        shares = float(item["shares"])
        cost = float(item["cost_price"])
        price = float(item["last_price"])
        rows.append({
            "stock_code": str(item["stock_code"]),
            "name": str(item.get("name") or ""),
            "shares": shares,
            "cost_price": cost,
            "last_price": price,
            "price_source": str(item.get("price_source") or "snapshot"),
            "cost_basis": cost * shares,
            "market_value": price * shares,
        })
    positions = pd.DataFrame(rows)
    positions["pnl_amount"] = positions["market_value"] - positions["cost_basis"]
    positions["pnl_pct"] = positions["pnl_amount"] / positions["cost_basis"]
    market_value = float(positions["market_value"].sum())
    cost_basis = float(positions["cost_basis"].sum())
    positions["weight"] = positions["market_value"] / market_value if market_value else 0.0
    positions = positions.sort_values("market_value", ascending=False).reset_index(drop=True)

    cash = float(snapshot["cash"])
    realized = float(sum(float(x["realized_pnl"]) for x in (snapshot.get("cleared_positions") or [])))
    total_assets = market_value + cash
    # 已实现亏损已经体现在账户价值里：把它加回来才是「一开始投进来多少钱」。
    initial_capital = cost_basis + cash - realized
    cumulative_return = (total_assets / initial_capital - 1.0) if initial_capital else None
    reported = dict(snapshot.get("reported") or {})
    reconciliation = {}
    if reported:
        reconciliation = {
            "reported_total_assets": reported.get("total_assets"),
            "computed_total_assets": round(total_assets, 2),
            "delta_total_assets": (round(total_assets - float(reported["total_assets"]), 2)
                                   if reported.get("total_assets") is not None else None),
            "reported_market_value": reported.get("market_value"),
            "computed_market_value": round(market_value, 2),
            "reported_float_pnl_incl_realized": reported.get("float_pnl"),
            "computed_float_pnl_incl_realized": round(float(positions["pnl_amount"].sum()) + realized, 2),
        }
    return {
        "asof": str(snapshot["asof"]),
        "broker": snapshot.get("broker"),
        "account_masked": snapshot.get("account_masked"),
        "captured_at": snapshot.get("captured_at"),
        "cash": cash,
        "market_value": market_value,
        "cost_basis": cost_basis,
        "total_assets": total_assets,
        "unrealized_pnl": float(positions["pnl_amount"].sum()),
        "realized_pnl": realized,
        "float_pnl_incl_realized": float(positions["pnl_amount"].sum()) + realized,
        "initial_capital_implied": initial_capital,
        "cumulative_return": cumulative_return,
        "n_positions": int(len(positions)),
        "top1_weight": float(positions["weight"].iloc[0]) if len(positions) else None,
        "top3_weight": float(positions["weight"].head(3).sum()) if len(positions) else None,
        "equity_weight": (market_value / total_assets) if total_assets else None,
        "max_weight_single_allowed": MAX_WEIGHT_SINGLE,
        "reconciliation": reconciliation,
        "positions": positions,
    }


def merge_live_prices(snapshot: dict, quotes: dict[str, dict]) -> dict:
    """用实时快照价覆盖快照里的 `last_price`（只在盘中做，收盘后由日线接管）。

    `quotes` 是 `{6位代码: {"price": .., "time": ..}}`（腾讯批量快照的 shape）。
    没拿到价的票**保留快照价**并把 `price_source` 标成 `snapshot`，不填 0、不外推。
    """
    merged = {**snapshot, "positions": [], "price_source": "live" if quotes else "snapshot"}
    for item in snapshot.get("positions", []):
        row = dict(item)
        code = str(row["stock_code"])
        quote = quotes.get(code.split(".")[0]) or {}
        price = quote.get("price")
        if price:
            row["last_price"] = float(price)
            row["price_source"] = f"live:{quote.get('time') or ''}".rstrip(":")
            row["live_price_time"] = quote.get("time")
        else:
            row["price_source"] = "snapshot"
        merged["positions"].append(row)
    return merged


def mark_to_market(snapshot: dict, bars: pd.DataFrame, *, asof: str | None = None) -> pd.DataFrame:
    """从快照日起逐日盯市：`nav = 现金 + Σ 股数 × 收盘价`（收盘价取 ≤ 当日最近可得价）。

    快照日之前不生成行（买入日期未知，回填等于编数据）。停牌/缺 bar 用**最后可得价**，
    和模拟盘的停牌处理一致；一分红就用后复权价会失真，所以本函数只要求 close，口径写在产物 note 里。
    """
    state = account_state(snapshot)
    start = pd.Timestamp(asof or state["asof"]).normalize()
    shares = {str(r.stock_code): float(r.shares) for r in state["positions"].itertuples()}
    frame = bars.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame = frame[frame["trade_date"] >= start]
    frame = frame[frame["stock_code"].astype(str).isin(shares)]
    if frame.empty:
        return pd.DataFrame(columns=["asof_date", "nav", "cash", "market_value", "daily_return",
                                     "cum_return", "drawdown", "priced", "unpriced"])
    wide = (frame.assign(stock_code=frame["stock_code"].astype(str))
            .pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last")
            .sort_index()
            .ffill())
    rows = []
    base = None
    peak = None
    for day, row in wide.iterrows():
        value = 0.0
        priced = 0
        for code, qty in shares.items():
            price = row.get(code)
            if pd.notna(price):
                value += float(price) * qty
                priced += 1
        market_value = value
        nav = market_value + state["cash"]
        base = nav if base is None else base
        peak = nav if peak is None else max(peak, nav)
        rows.append({
            "asof_date": day.strftime("%Y-%m-%d"),
            "nav": round(nav, 4),
            "cash": round(state["cash"], 4),
            "market_value": round(market_value, 4),
            "daily_return": None if not rows else round(nav / rows[-1]["nav"] - 1, 8),
            "cum_return": round(nav / base - 1, 8),
            "drawdown": round(nav / peak - 1, 8),
            "priced": priced,
            "unpriced": len(shares) - priced,
        })
    return pd.DataFrame(rows)


def advice_rows(positions: pd.DataFrame,
                *, selected: Mapping[str, object] | Iterable[str] | None = None,
                preselected: Mapping[str, object] | Iterable[str] | None = None,
                momentum_20d: dict[str, float] | None = None) -> pd.DataFrame:
    """逐票建议：模型名单（R0/R1/R2）+ 20 日动量（R3/R4）合成，缺数据走 R5。

    `selected` / `preselected` 是**最新决策日**的生产最终名单与候选池
    （给 `{代码: rank}` 能识别最高分档；给纯列表就没有 rank 信息）。
    建议只描述「模型怎么看待这只票」，不生成订单；`action` 固定是 买入 / 持有 / 卖出 三档。
    """
    selected_ranks = _ranks(selected)
    preselected_ranks = _ranks(preselected)
    momentum_20d = momentum_20d or {}
    rows = []
    for r in positions.itertuples():
        code = str(r.stock_code)
        mom = momentum_20d.get(code)
        rank = selected_ranks.get(code)
        if code in selected_ranks and rank is not None and rank <= BACKFILL_TOPN:
            action, rule = "买入", "R0"
            why = f"在最新生产最终名单里且 rank={rank}（模型最高分档）"
        elif code in selected_ranks:
            action, rule = "持有", "R1"
            why = "在最新生产最终名单里（模型仍在持有它）" + (f"，rank={rank}" if rank else "")
        elif code in preselected_ranks:
            action, rule, why = "持有", "R2", "在最新候选池里但没拿到权重（观察）"
        elif mom is None:
            action, rule, why = "持有", "R5", "没有 20 日动量数据，不给动作"
        elif mom < 0:
            action, rule, why = "卖出", "R3", f"模型最终名单与候选池都不要，且 20 日动量为负（{mom:+.2%}）"
        else:
            action, rule, why = "持有", "R4", f"模型都不要但 20 日动量非负（{mom:+.2%}），降级观察"
        rows.append({
            "stock_code": code,
            "name": r.name,
            "weight": float(r.weight),
            "weight_vs_cap": float(r.weight) - MAX_WEIGHT_SINGLE,
            "pnl_pct": float(r.pnl_pct),
            "momentum_20d": mom,
            "in_selected": code in selected_ranks,
            "in_preselected": code in preselected_ranks,
            "selected_rank": rank,
            "action": action,
            "rule": rule,
            "reason": why,
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 收益对比与收益日历（2026-10-12 用户要求：日/周/月 对中证500 的收益曲线对比 + 收益日历）
#
# 口径说明（必须写在产物里，否则就是拿一条看着漂亮的线骗自己）：
#   * 真实账户的历史**不可知**（买入日期未知、中途有买卖、有已实现亏损），所以账户曲线只能从快照日
#     向前累积（见 `mark_to_market`）。为了让"日/周/月 vs 中证500"和"收益日历"**现在就可用**，
#     这里另给一条**明确标注的回填口径**：
#         `buy_and_hold_curve` = 「按**当前权重**在起点日买入并持有到最新交易日」的组合净值，
#         权重取自今天的持仓市值占比（固定不漂移），不含现金、不含已实现亏损、不含交易成本。
#     它不是账户业绩，是「这一篮子现在这个结构，过去 N 年跟中证500 比怎样」的对照；
#     真实账户曲线在 `nav.csv`，两条都展示、不混。
# ─────────────────────────────────────────────────────────────────────────────
TRADING_DAYS_PER_YEAR = 252
QUANTILES = (0.25, 0.75)


def buy_and_hold_curve(weights: dict[str, float], bars: pd.DataFrame, *,
                       start, end=None) -> pd.DataFrame:
    """当前权重固定买入持有 → 组合日收益序列（起点归一为 1.0）。

    `weights` 自动归一（只保留在 `bars` 里有行情的票）；缺行情的票用**最后可得价**顺延（与盯市一致）。
    """
    frame = bars.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame["stock_code"] = frame["stock_code"].astype(str)
    frame = frame[frame["stock_code"].isin(weights)]
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "port_ret", "port_nav", "coverage_weight"])
    wide = frame.pivot_table(index="trade_date", columns="stock_code", values="close",
                             aggfunc="last").sort_index().ffill()
    wide = wide[wide.index >= pd.Timestamp(start).normalize()]
    if end is not None:
        wide = wide[wide.index <= pd.Timestamp(end).normalize()]
    usable = [c for c in wide.columns if c in weights and wide[c].notna().any()]
    if not usable or len(wide) < 2:
        return pd.DataFrame(columns=["trade_date", "port_ret", "port_nav", "coverage_weight"])
    total = sum(float(weights[c]) for c in usable)
    share = {c: float(weights[c]) / total for c in usable}
    relative = wide[usable] / wide[usable].iloc[0]
    port = (relative * pd.Series(share)).sum(axis=1)
    out = pd.DataFrame({"trade_date": wide.index, "port_nav": port.to_numpy()})
    out["port_ret"] = out["port_nav"].pct_change().fillna(0.0)
    out["coverage_weight"] = total / max(sum(float(v) for v in weights.values()), 1e-12)
    return out.reset_index(drop=True)


def blend_with_benchmark(port: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    """把组合曲线与基准（中证500 收盘价 Series，索引=trade_date）对齐并各自归一为 1。"""
    if port.empty:
        return pd.DataFrame(columns=["trade_date", "port_nav", "bench_nav", "port_ret", "bench_ret", "excess_ret"])
    series = bench.copy()
    series.index = pd.to_datetime(series.index).normalize()
    aligned = series.reindex(pd.to_datetime(port["trade_date"])).ffill().bfill()
    out = port.copy()
    out["bench_nav"] = (aligned / aligned.iloc[0]).to_numpy()
    out["bench_ret"] = pd.Series(out["bench_nav"]).pct_change().fillna(0.0).to_numpy()
    out["excess_ret"] = out["port_ret"] - out["bench_ret"]
    return out.dropna(subset=["bench_nav"]).reset_index(drop=True)


def resample_curve(curve: pd.DataFrame, freq: str) -> pd.DataFrame:
    """把日频曲线聚合成周频（W-FRI）/月频（月末）：期内收益按**复利**连乘，净值取期末。"""
    if curve.empty:
        return pd.DataFrame(columns=["period_end", "port_nav", "bench_nav", "port_ret", "bench_ret", "excess_ret"])
    frame = curve.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.set_index("trade_date").sort_index()
    rule = "W-FRI" if freq.upper().startswith("W") else "ME"
    grouped = frame.resample(rule)
    rows = []
    for period_end, chunk in grouped:
        if chunk.empty:
            continue
        # 标签用**该期最后一个真实交易日**，不用 resample 的期界 ——
        # `ME` 会把 2026-10-09 那一期标成 2026-10-31（未来日期），页面和统计都会看错。
        actual_end = chunk.index[-1].strftime("%Y-%m-%d")
        rows.append({
            "period_end": actual_end,
            "period_label": period_end.strftime("%Y-%m-%d"),
            "port_nav": float(chunk["port_nav"].iloc[-1]),
            "bench_nav": float(chunk["bench_nav"].iloc[-1]),
            "port_ret": float((1.0 + chunk["port_ret"]).prod() - 1.0),
            "bench_ret": float((1.0 + chunk["bench_ret"]).prod() - 1.0),
            "excess_ret": float((1.0 + chunk["port_ret"]).prod() - (1.0 + chunk["bench_ret"]).prod()),
        })
    return pd.DataFrame(rows)


def curve_stats(curve: pd.DataFrame, *, freq: str = "D") -> dict:
    """一段曲线的体检：累计/年化/回撤/胜率/最好最差/超额 —— 每个粒度都用同一套算法算。

    累计收益用**期收益复利连乘**（不是 `nav末/nav首 − 1`）：周/月频的第一行是该期**期末**净值，
    拿它当起点会把第一期的涨跌漏掉 —— 实测日频 −19.46% 被算成周频 −17.90%（差 1.56pp），
    而且三个粒度的累计本该**完全相等**（同一段行情的不同切法）。
    年化也按同一分母（期数 / 每年期数）。
    """
    if curve.empty:
        return {"periods": 0}
    # 日频用 `trade_date`、周/月频用 `period_end` —— 统一取那个存在的日期列（别假设列名）。
    date_col = "trade_date" if "trade_date" in curve.columns else "period_end"
    port = curve["port_nav"].astype(float)
    bench = curve["bench_nav"].astype(float)
    rets = curve["port_ret"].astype(float)
    bench_rets = curve["bench_ret"].astype(float)
    per_year = {"D": TRADING_DAYS_PER_YEAR, "W": 52, "M": 12}.get(freq.upper()[:1], 52)
    is_daily = freq.upper().startswith("D")
    # 日频第一行的收益是构造出来的 0（没有前收），要跳过；周/月频每一行都是**一期真实收益**。
    tail = rets.iloc[1:] if is_daily else rets
    bench_tail = bench_rets.iloc[1:] if is_daily else bench_rets
    excess_tail = curve["excess_ret"].iloc[1:] if is_daily else curve["excess_ret"]
    periods = int(len(tail))
    nav_port = (1.0 + rets).cumprod()
    nav_bench = (1.0 + bench_rets).cumprod()
    total = float(nav_port.iloc[-1] - 1.0)
    bench_total = float(nav_bench.iloc[-1] - 1.0)
    years = max(periods / per_year, 1e-9)
    ann = float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1 and periods >= 1 else None
    bench_ann = float((1.0 + bench_total) ** (1.0 / years) - 1.0) if bench_total > -1 and periods >= 1 else None
    drawdown = nav_port / nav_port.cummax() - 1.0
    wins = int((tail > bench_tail).sum())
    return {
        "periods": periods,
        "start": str(pd.Timestamp(curve[date_col].iloc[0])).strip()[:10],
        "end": str(pd.Timestamp(curve[date_col].iloc[-1])).strip()[:10],
        "total_return": total,
        "benchmark_total_return": bench_total,
        "excess_return": total - bench_total,
        "annualized_return": ann,
        "benchmark_annualized_return": bench_ann,
        "max_drawdown": float(drawdown.min()),
        "win_rate": (wins / len(tail)) if len(tail) else None,
        "best_period": float(tail.max()) if len(tail) else None,
        "worst_period": float(tail.min()) if len(tail) else None,
        "ann_vol": float(tail.std(ddof=1) * (per_year ** 0.5)) if len(tail) > 1 else None,
        "mean_excess": float(excess_tail.mean()) if periods else None,
    }


def calendar_cells(curve: pd.DataFrame, freq: str = "D") -> list[dict]:
    """收益日历的格子数据：日 → 每个交易日一格；周 → 每周一格；月 → 每月一格。

    每格给 **组合收益 / 中证500 / 超额** 三个数（前端按这三个数上色与标数值）。
    """
    if curve.empty:
        return []
    if freq.upper().startswith("D"):
        rows = curve
        key = lambda row: str(pd.Timestamp(row["trade_date"]).date())  # noqa: E731
    else:
        rows = resample_curve(curve, freq)
        key = lambda row: str(row["period_end"])  # noqa: E731
    cells = []
    for row in rows.to_dict("records"):
        port_ret = float(row.get("port_ret", 0.0) or 0.0)
        bench_ret = float(row.get("bench_ret", 0.0) or 0.0)
        cells.append({"period": key(row), "port_ret": port_ret, "bench_ret": bench_ret,
                      "excess": port_ret - bench_ret})
    return cells


def realtime_view(positions: pd.DataFrame, quotes: dict[str, dict] | None, *, cash: float,
                  cleared_pnl: float, asof: str) -> dict:
    """**盘中实时**重算账户视图：用实时价覆盖持仓价，再走一遍 `account_state` 的同一套算术。

    为什么要复用 `account_state`：账户算术（市值/权重/浮盈/推算初始本金/累计/集中度）只能有**一份**实现
    —— 盘后落盘那份（`positions.csv`/`summary.json`）与页面实时那份如果各写一套，两边迟早对不上。
    所以这里只做"把实时价塞进快照"，剩下的算术完全交给 `account_state`。

    `quotes` 形如 `{6位代码: {"price": .., "time": .., "change_pct": ..}}`；拿不到价的票保留盘后价，
    并把 `price_source` 标成 `disk`（不填 0、不外推）。
    """
    quotes = quotes or {}
    rows = []
    for row in positions.to_dict("records"):
        code = str(row.get("stock_code"))
        quote = quotes.get(code.split(".")[0]) or {}
        price = quote.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        disk_price = float(row.get("last_price") or 0.0)
        rows.append({
            "stock_code": code,
            "name": str(row.get("name") or ""),
            "shares": float(row.get("shares") or 0.0),
            "cost_price": float(row.get("cost_price") or 0.0),
            "last_price": price if price else disk_price,
            "disk_price": disk_price,
            "price_source": (f"live:{quote.get('time')}" if price else "disk"),
            "live_time": quote.get("time"),
            "change_pct": quote.get("change_pct"),
            "prev_close": quote.get("prev_close"),
        })
    snapshot = {"asof": asof, "cash": cash, "positions": rows,
                "cleared_positions": [{"stock_code": "_realized", "realized_pnl": cleared_pnl}]}
    state = account_state(snapshot)
    priced = sum(1 for r in rows if r["price_source"].startswith("live"))
    # 当日参考盈亏 = Σ 股数 ×（现价 − 昨收）—— 与券商 App 首页那个数同一口径（有昨收才算）。
    day_pnl = 0.0
    day_covered = 0.0
    for r in rows:
        prev = r.get("prev_close")
        if prev in (None, ""):
            continue
        day_pnl += (float(r["last_price"]) - float(prev)) * float(r["shares"])
        day_covered += float(r["last_price"]) * float(r["shares"])
    out = state["positions"].copy()
    # 行情侧字段（实时时间/涨跌/昨收/价源）必须从**我们刚拼的那批 rows** 带过来：
    # `account_state` 只认价格与股数，不会把 `live_time` 之类带出来（实测页面上四行全显示"盘后"就是这么来的）。
    quote_cols = {str(r["stock_code"]): {k: r.get(k) for k in
                                        ("price_source", "live_time", "change_pct", "prev_close", "disk_price")}
                  for r in rows}
    for col in ("price_source", "live_time", "change_pct", "prev_close", "disk_price"):
        out[col] = out["stock_code"].map(lambda code, c=col: (quote_cols.get(str(code)) or {}).get(c))
    advice = positions.set_index("stock_code")
    for col in ("action", "rule", "reason", "momentum_20d", "in_selected", "in_preselected",
                "selected_rank"):
        if col in out.columns or col not in advice.columns:
            continue
        out[col] = out["stock_code"].map(advice[col])
    return {
        "asof": asof,
        "cash": state["cash"],
        "market_value": state["market_value"],
        "total_assets": state["total_assets"],
        "cost_basis": state["cost_basis"],
        "unrealized_pnl": state["unrealized_pnl"],
        "realized_pnl": state["realized_pnl"],
        "initial_capital_implied": state["initial_capital_implied"],
        "cumulative_return": state["cumulative_return"],
        "top1_weight": state["top1_weight"],
        "top3_weight": state["top3_weight"],
        "n_positions": state["n_positions"],
        "priced": priced,
        "unpriced": len(rows) - priced,
        "day_pnl": day_pnl if day_covered else None,
        "day_pnl_coverage": (day_covered / state["market_value"]) if state["market_value"] else None,
        "positions": out.where(pd.notna(out), None).to_dict("records"),
    }
