#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""持仓卖出规则（退出/减仓）评估。

设计依据来自本仓库 2024-01 ~ 2026-09 全市场 3.42M 股票日的持仓状态研究
（市场中性超额收益，流动性门槛 20 日中位成交额 ≥ 5000 万）：

    状态                         +5 日     +20 日   分年度 +20 日
    20 日涨幅 < -15%             +0.64%    +1.87%   2024 +2.15 / 2025 +2.01 / 2026 +1.58
    回撤 60 日高点 < -40%         +0.83%    +2.45%   2025 +3.22 / 2026 +3.01 / 2024 -0.27
    Donchian 位置 < 0（破下沿）     +0.15%    +0.66%   三年为正但较弱
    Donchian 位置 > 0.8（贴上沿）   -0.23%    -0.68%   2024 -1.80 / 2025 -0.40 / 2026 +0.10
    20 日涨幅 > +15%             -0.18%    -0.53%   2024 -1.66 / 2025 -0.74 / 2026 +0.39

结论：本样本期是反转市，**固定百分比止损与"跌破均线卖出"会系统性卖在期望收益最高的
状态上**，因此默认不启用收益型止损；卖出侧只保留三类规则：

1. 风险型（硬）：ST / 流动性枯竭 / 停牌 / 模型排名跌出下限；
2. 兑现型（数据支持，但效应在衰减）：贴近通道上沿或 20 日大涨 → 减仓而非清仓；
3. 结构型：单只权重超上限 → 减到上限。

`stop_loss_pct` 保留为可选参数但默认为 0（关闭），并在报告里显式标注它在本样本中无效。
"""

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExitRules:
    """卖出规则配置。收益型止损默认关闭，风险型与结构型规则默认开启。"""

    # 风险型（硬约束）
    exclude_st: bool = True
    min_median_amount_20d: float = 50_000_000.0
    max_suspend_sessions: int = 5
    min_model_percentile: float = 30.0
    # 兑现型（减仓）
    take_profit_pos: float = 0.80
    take_profit_ret20: float = 0.15
    take_profit_reduce_ratio: float = 0.34
    # 结构型
    max_weight: float = 0.35
    lot_size: int = 100  # A 股按手交易，减仓数量向下取整到手
    # 可选的收益型止损（本样本中未获得支持，默认关闭）
    stop_loss_pct: float = 0.0
    # ATR 缩放止损（P1.17 §0.19：触发率 ~19% 即可把 5 日 std 压 28%，优于固定百分比）
    stop_loss_atr_multiple: float = 0.0
    stop_loss_min_pct: float = 0.04
    stop_loss_max_pct: float = 0.10
    # 记录用
    notes: tuple = field(default_factory=lambda: (
        "stop_loss_pct 默认 0：样本期 20 日跌幅 > 15% 的标的未来 20 日超额 +1.6~2.2%，止损会卖在最优点",
        "stop_loss_atr_multiple 默认 0：开启后按 k×ATR14（夹取 min/max）设止损，15 周 A/B 里以 19% 触发率压住尾部",
        "take_profit 的负期望在 2026 年已衰减到接近 0，减仓比例保持保守",
    ))


def _f(value, default=np.nan):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if np.isfinite(result) else float(default)


def evaluate_exit_plan(
    holdings: pd.DataFrame,
    market_state: pd.DataFrame,
    *,
    rules: ExitRules | None = None,
    cash: float = 0.0,
) -> pd.DataFrame:
    """Return a per-position action plan.

    ``holdings`` requires ``stock_code`` and ``shares``; ``cost_price`` is
    optional.  ``cash`` is the uninvested balance: position weights are measured
    against total equity (positions + cash), which is the denominator a risk
    limit should use, and the invested-only weight is reported alongside.  ``market_state`` requires ``stock_code`` and ``close`` plus any of
    the diagnostic columns produced by the service (ma20, ma60, high60,
    return_20d, donchian_pos, median_amount_20, model_percentile, name,
    sessions_since_last_bar).
    """
    cfg = rules or ExitRules()
    if holdings is None or holdings.empty:
        return pd.DataFrame(columns=["stock_code", "action"])
    positions = holdings.copy()
    positions["stock_code"] = positions["stock_code"].astype(str)
    if "shares" not in positions.columns:
        positions["shares"] = 0.0
    positions["shares"] = pd.to_numeric(positions["shares"], errors="coerce").fillna(0.0)
    state = market_state.copy() if market_state is not None else pd.DataFrame(columns=["stock_code"])
    state["stock_code"] = state["stock_code"].astype(str)
    merged = positions.merge(state, on="stock_code", how="left", suffixes=("", "_mkt"))

    close = pd.to_numeric(merged.get("close"), errors="coerce")
    shares = merged["shares"]
    market_value = close * shares
    invested_value = float(market_value.sum())
    equity_value = invested_value + max(0.0, _f(cash, 0.0))
    cost_price = pd.to_numeric(merged.get("cost_price"), errors="coerce")
    pnl_pct = np.where(cost_price.notna() & (cost_price > 0), close / cost_price - 1.0, np.nan)

    rows = []
    for index, row in merged.iterrows():
        code = str(row["stock_code"])
        value = _f(market_value.iloc[index], 0.0)
        weight = value / equity_value if equity_value > 0 else np.nan
        invested_weight = value / invested_value if invested_value > 0 else np.nan
        name = str(row.get("name") or "")
        close_price = _f(row.get("close"))
        return_20d = _f(row.get("return_20d"))
        pos = _f(row.get("donchian_pos"))
        amount = _f(row.get("median_amount_20"))
        model_pct = _f(row.get("model_percentile"))
        suspend = _f(row.get("sessions_since_last_bar"), 0.0)
        loss = _f(pnl_pct[index] if index < len(pnl_pct) else np.nan)
        atr_pct = _f(row.get("atr_pct_14"))
        effective_stop = abs(float(cfg.stop_loss_pct or 0.0))
        stop_basis = "fixed" if effective_stop > 0 else ""
        if float(cfg.stop_loss_atr_multiple or 0.0) > 0 and np.isfinite(atr_pct) and atr_pct > 0:
            effective_stop = float(np.clip(
                float(cfg.stop_loss_atr_multiple) * atr_pct,
                float(cfg.stop_loss_min_pct or 0.0), float(cfg.stop_loss_max_pct or 1.0),
            ))
            stop_basis = f"ATR×{cfg.stop_loss_atr_multiple:g}"

        reasons = []
        action = "HOLD"
        reduce_ratio = 0.0

        # 1) risk rules
        if cfg.exclude_st and "ST" in name.upper():
            action, reasons = "EXIT", reasons + [f"ST 特别处理（{name}）"]
        elif not np.isfinite(close_price):
            action, reasons = "EXIT", reasons + ["缺少最新行情（疑似停牌）"]
        elif suspend > cfg.max_suspend_sessions:
            action, reasons = "EXIT", reasons + [f"停牌 {int(suspend)} 个交易日"]
        elif np.isfinite(amount) and amount < cfg.min_median_amount_20d:
            action, reasons = "EXIT", reasons + [f"流动性枯竭 20 日中位成交额 {amount:,.0f} < {cfg.min_median_amount_20d:,.0f}"]
        elif np.isfinite(model_pct) and model_pct < cfg.min_model_percentile:
            action, reasons = "EXIT", reasons + [f"模型排名跌出下限 {model_pct:.0f} < {cfg.min_model_percentile:.0f} 分位"]

        # 2) optional return-based stop (off by default)
        if action == "HOLD" and effective_stop > 0 and np.isfinite(loss) and loss <= -effective_stop:
            action, reasons = "EXIT", reasons + [f"触发止损（{stop_basis} = {effective_stop:.1%}）{loss:.1%}"]

        # 3) profit taking (reduce, do not exit)
        if action == "HOLD":
            if np.isfinite(pos) and pos >= cfg.take_profit_pos:
                action = "REDUCE"
                reduce_ratio = max(reduce_ratio, cfg.take_profit_reduce_ratio)
                reasons.append(f"贴近通道上沿 pos={pos:.2f} ≥ {cfg.take_profit_pos:.2f}（该状态 20 日超额 -0.68%）")
            if np.isfinite(return_20d) and return_20d >= cfg.take_profit_ret20:
                action = "REDUCE"
                reduce_ratio = max(reduce_ratio, cfg.take_profit_reduce_ratio)
                reasons.append(f"20 日涨幅 {return_20d:+.1%} ≥ {cfg.take_profit_ret20:.0%}（该状态 20 日超额 -0.53%）")

        # 4) structural cap: target the cap directly and round to tradeable lots
        target_shares = None
        if np.isfinite(weight) and weight > cfg.max_weight and equity_value > 0 and close_price > 0:
            lot = max(1, int(cfg.lot_size))
            cap_value = cfg.max_weight * equity_value
            capped = np.floor((cap_value / close_price) / lot) * lot
            target_shares = float(min(capped, shares.iloc[index]))
            sell_qty = max(0.0, float(shares.iloc[index]) - target_shares)
            if sell_qty > 0:
                if action == "HOLD":
                    action = "REDUCE"
                reduce_ratio = max(reduce_ratio, sell_qty / max(float(shares.iloc[index]), 1.0))
                reasons.append(
                    f"单只权重 {weight:.1%} > 上限 {cfg.max_weight:.0%}"
                    f"（按总资产 {equity_value:,.0f} 元计，目标 ≤ {capped:.0f} 股）"
                )

        if action == "HOLD" and not reasons:
            reasons = ["无卖出信号：处于样本期期望为正的状态（深度回撤/破通道下沿）"]

        rows.append({
            "stock_code": code, "name": name, "shares": float(shares.iloc[index]),
            "cost_price": _f(cost_price.iloc[index]) if np.isfinite(_f(cost_price.iloc[index])) else np.nan,
            "close": close_price, "market_value": round(value, 2),
            "weight": round(float(weight), 4) if np.isfinite(weight) else np.nan,
            "invested_weight": round(float(invested_weight), 4) if np.isfinite(invested_weight) else np.nan,
            "pnl_pct": round(float(loss), 4) if np.isfinite(loss) else np.nan,
            "atr_pct_14": round(float(atr_pct), 4) if np.isfinite(atr_pct) else np.nan,
            "effective_stop": round(float(effective_stop), 4) if effective_stop > 0 else 0.0,
            "action": action,
            "reduce_ratio": round(float(reduce_ratio), 4),
            "suggested_shares_to_sell": _sell_quantity(action, float(shares.iloc[index]), float(reduce_ratio), cfg.lot_size),
            "suggested_lots_to_sell": _sell_quantity(action, float(shares.iloc[index]), float(reduce_ratio), cfg.lot_size) // max(1, int(cfg.lot_size)),
            "model_percentile": round(model_pct, 1) if np.isfinite(model_pct) else np.nan,
            "return_20d": round(return_20d, 4) if np.isfinite(return_20d) else np.nan,
            "drawdown_60d": round(_f(row.get("drawdown_60d")), 4) if np.isfinite(_f(row.get("drawdown_60d"))) else np.nan,
            "donchian_pos": round(pos, 3) if np.isfinite(pos) else np.nan,
            "median_amount_20": amount if np.isfinite(amount) else np.nan,
            "reasons": "; ".join(reasons),
        })
    plan = pd.DataFrame(rows)
    plan.attrs["rules"] = asdict(cfg)
    plan.attrs["total_market_value"] = round(invested_value, 2)
    plan.attrs["cash"] = round(max(0.0, _f(cash, 0.0)), 2)
    plan.attrs["total_equity"] = round(equity_value, 2)
    return plan.sort_values(["action", "weight"], ascending=[True, False]).reset_index(drop=True)


def _sell_quantity(action: str, shares: float, ratio: float, lot_size: int) -> int:
    """Tradeable sell quantity: whole lots for trims, everything for exits."""
    if shares <= 0 or action == "HOLD":
        return 0
    if action == "EXIT":
        return int(shares)  # an exit may dispose of an odd lot
    lot = max(1, int(lot_size))
    if ratio <= 0:
        return 0
    quantity = int(np.floor(shares * ratio / lot) * lot)
    if quantity == 0 and shares * ratio >= lot * 0.5:
        quantity = int(min(lot, shares))
    return quantity


def render_exit_plan_markdown(plan: pd.DataFrame, *, as_of=None) -> str:
    """Render the plan as a Markdown report."""
    rules = plan.attrs.get("rules") or {}
    lines = ["# CN 持仓卖出规则报告", ""]
    if as_of is not None:
        lines.append(f"- 评估日期：{as_of}")
    lines.append(f"- 持仓市值：{plan.attrs.get('total_market_value', 0):,.0f} 元 ｜ 现金：{plan.attrs.get('cash', 0):,.0f} 元 ｜ 总资产：{plan.attrs.get('total_equity', 0):,.0f} 元")
    lines.append("- 规则：风险型（ST/流动性/停牌/模型排名下限）+ 兑现型（贴上沿/20日大涨 → 减仓）+ 结构型（单只权重上限）")
    lines.append(f"- 委托单位：{int(rules.get('lot_size', 100))} 股/手，减仓数量按手向下取整（卖出也需为整手）")
    atr_multiple = float(rules.get("stop_loss_atr_multiple") or 0.0)
    fixed_stop = rules.get("stop_loss_pct")
    if atr_multiple > 0:
        # The ATR stop is the live rule; the fixed-percentage one is kept off.
        # Reporting only ``stop_loss_pct`` made a plan whose names were armed with
        # ``effective_stop`` look like it had no return-based stop at all.
        lines.append(
            f"- 收益型止损：固定百分比关闭（样本期该规则无效）；"
            f"ATR 缩放开启 = {atr_multiple:g} × ATR14%，夹取 "
            f"{float(rules.get('stop_loss_min_pct') or 0.0):.1%}~{float(rules.get('stop_loss_max_pct') or 1.0):.1%}"
            "，逐票生效止损位见 `effective_stop` 列"
        )
    else:
        lines.append(f"- 收益型止损：{'关闭（样本期该规则无效）' if not fixed_stop else fixed_stop}")
    lines.append("")
    lines.append("| 代码 | 名称 | 持股 | 成本 | 现价 | 市值 | 权重(总资产) | 权重(持仓内) | 盈亏 | 模型分位 | 20日 | 回撤60日 | 通道位置 | 操作 | 建议卖出股数 | 理由 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|")
    for _, row in plan.iterrows():
        lines.append(
            "| {code} | {name} | {shares:.0f} | {cost} | {close:.2f} | {value:,.0f} | {weight:.1%} | {iweight:.1%} | {pnl} | {mp} | {r20} | {dd} | {pos} | **{action}** | {sell} | {reasons} |".format(
                code=row["stock_code"], name=row.get("name", ""), shares=row["shares"],
                cost=f"{row['cost_price']:.2f}" if np.isfinite(_f(row.get("cost_price"))) else "-",
                close=_f(row.get("close"), 0.0), value=_f(row.get("market_value"), 0.0),
                weight=_f(row.get("weight"), 0.0), iweight=_f(row.get("invested_weight"), 0.0),
                pnl=f"{row['pnl_pct']:.1%}" if np.isfinite(_f(row.get("pnl_pct"))) else "-",
                mp=f"{row['model_percentile']:.0f}" if np.isfinite(_f(row.get("model_percentile"))) else "-",
                r20=f"{row['return_20d']:+.1%}" if np.isfinite(_f(row.get("return_20d"))) else "-",
                dd=f"{row['drawdown_60d']:+.1%}" if np.isfinite(_f(row.get("drawdown_60d"))) else "-",
                pos=f"{row['donchian_pos']:.2f}" if np.isfinite(_f(row.get("donchian_pos"))) else "-",
                action=row["action"], sell=f'{int(row["suggested_shares_to_sell"])} 股（{int(row.get("suggested_lots_to_sell", 0))} 手）' if int(row["suggested_shares_to_sell"]) else "0",
                reasons=str(row["reasons"]),
            )
        )
    if rules.get("notes"):
        lines.append("")
        lines.append("## 规则依据与已知局限")
        for note in rules["notes"]:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"

def plan_lot_orders(target_weights: dict, prices: dict, equity: float, *, lot_size: int = 100) -> pd.DataFrame:
    """Convert target weights into executable integer-lot orders.

    A-share orders must be whole lots (default 100 shares), so a weight can only
    be approximated: each name is floored to a lot, and the leftover cash is
    reported instead of being silently ignored.
    """
    lot = max(1, int(lot_size))
    equity = max(0.0, _f(equity, 0.0))
    rows = []
    allocated = 0.0
    for code, weight in sorted((target_weights or {}).items(), key=lambda item: -float(item[1] or 0.0)):
        price = _f((prices or {}).get(code))
        target_weight = max(0.0, _f(weight, 0.0))
        target_value = equity * target_weight
        if not np.isfinite(price) or price <= 0 or target_value <= 0:
            continue
        lots = int(np.floor(target_value / price / lot))
        shares = lots * lot
        value = shares * price
        allocated += value
        rows.append({
            "stock_code": str(code), "target_weight": round(target_weight, 4),
            "price": round(price, 2), "target_value": round(target_value, 2),
            "shares": shares, "lots": lots, "value": round(value, 2),
            "residual_value": round(target_value - value, 2),
        })
    plan = pd.DataFrame(rows)
    if not plan.empty:
        plan.attrs["allocated_value"] = round(allocated, 2)
        plan.attrs["cash_left"] = round(max(0.0, equity - allocated), 2)
        plan.attrs["lot_size"] = lot
    return plan


def render_lot_order_markdown(plan: pd.DataFrame, *, title="按整手的可执行委托清单") -> str:
    """Render an integer-lot order plan."""
    if plan is None or plan.empty:
        return ""
    lines = [f"## {title}", ""]
    lines.append(f"- 可投总额：{plan.attrs.get('allocated_value', 0) + plan.attrs.get('cash_left', 0):,.0f} 元 ｜ 已分配：{plan.attrs.get('allocated_value', 0):,.0f} 元 ｜ 整手取整后剩余现金：{plan.attrs.get('cash_left', 0):,.0f} 元")
    lines.append("")
    lines.append("| 代码 | 目标权重 | 价格 | 目标金额 | 可买股数 | 手数 | 实际金额 | 取整余量 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for _, row in plan.iterrows():
        lines.append(
            f"| {row['stock_code']} | {row['target_weight']:.1%} | {row['price']:.2f} | {row['target_value']:,.0f} | "
            f"{int(row['shares'])} | {int(row['lots'])} | {row['value']:,.0f} | {row['residual_value']:,.0f} |"
        )
    return "\n".join(lines) + "\n"
