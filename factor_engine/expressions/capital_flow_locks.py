#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""P1.16 三把锁 / 主力资金 / 敢死队资金特征引擎（``capital_flow_locks.v1``）。

本模块实现 `docs/todo/P1_16_three_locks_capital_features_plan.md` 第 3 节的
可审计代理特征：

* ``cyc_*``        —— 成交额/成交量滚动成本线（CYC 代理）与筹码成本分布；
* ``mf_*``         —— 标准 ``moneyflow`` 连续净流入及其归一化；
* ``main_*``       —— 大单 + 特大单主力资金代理；
* ``dare_*`` / ``daredevil_*`` —— 特大单 + 龙虎榜 + 游资的敢死队代理；
* ``three_lock_*`` —— 趋势锁 / 成本锁 / 资金锁与合成分；
* ``consensus_*``  —— DC / THS 来源共识（不同供应商金额不相加）。

约定（与方案 2.2 节一致）：

* 主键 ``(stock_code, trade_date)``，所有滚动窗口只使用当日及之前的 bar；
* 连续资金字段缺失保留 NaN 并附 ``*_is_missing`` 掩码，绝不填 0；
* 龙虎榜“当天未上榜”编码为事件 0；源未覆盖的交易日编码为 ``is_missing=1``；
* 横截面 rank 只在同一 ``trade_date`` 内计算，不使用未来分位。

价格单位说明：``close`` 为前复权价，``amount/volume`` 为成交额/成交量（原始口径），
因此 ``cyc_*`` 保留方案里的原始定义，同时额外给出用前复权价加权重算的
``cyc_adj_*``（``sum(close*volume)/sum(volume)``），后者与 ``close`` 同口径，
在窗口内发生除权时会与 ``cyc_*`` 出现差异。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from data.ingest.providers.cn_common import normalize_cn_stock_code


FEATURE_VERSION = "capital_flow_locks.v1"

CYC_WINDOWS: tuple[int, ...] = (5, 13, 34)
MF_WINDOWS: tuple[int, ...] = (3, 5, 10, 20, 60)

KEY_COLUMNS = ["stock_code", "trade_date"]

# 分组只用于消融实验选择特征子集；顺序即实验的累加顺序。
# ``pvx`` 是量价上下文（不是资金流），它进入所有变体的基线，因此 E1..E6 的增量
# 只能来自真正的资金流 / 成本 / 事件特征。
GROUP_ORDER: tuple[str, ...] = ("pvx", "mf", "cyc", "main", "daredevil", "locks", "consensus")
PVX_GROUP = "pvx"
GROUP_PREFIXES: dict[str, tuple[str, ...]] = {
    "pvx": (
        "ret_", "close_to_ma20", "volatility_", "vr20_", "turnover_", "intraday_range_pct",
        "close_position_20d", "amount_trend_5_20", "vol_ratio_rank_20d", "ret_3d_rank",
        "turnover_rank_20d", "volatility_rank_20d", "circ_mv", "total_mv", "volume_ratio",
    ),
    "mf": ("mf_",),
    "cyc": ("cyc", "close_to_cyc", "cyq_", "close_to_cyq"),
    "main": ("main_",),
    "daredevil": ("dare_", "daredevil_", "top_list_", "top_inst_", "hm_"),
    "locks": ("three_lock_", "trend_lock", "cost_lock", "flow_lock"),
    "consensus": ("consensus_", "dc_", "ths_"),
}

# 需要显式缺失掩码的关键连续字段（mask 在 assemble 中统一生成）。
MASK_COLUMNS: tuple[str, ...] = (
    "mf_net_1d",
    "mf_net_3d",
    "mf_net_5d",
    "mf_net_20d",
    "mf_net_pct_5d",
    "mf_net_pct_20d",
    "cyc_5",
    "cyc_13",
    "cyc_34",
    "cyc_adj_5",
    "close_to_cyc_5",
    "cyq_cost_50pct",
    "cyq_winner_rate",
    "main_net_3d",
    "main_net_5d",
    "main_strength_3d",
    "main_strength_5d",
    "main_float_cap_5d",
    "dare_net_3d",
    "dare_strength_3d",
    "daredevil_score",
    "top_list_flag",
    "top_list_days_since",
    "top_inst_net_buy_rate",
    "hm_net_rate",
    "consensus_source_count",
    "consensus_sign_agreement",
    "consensus_sign",
    "three_lock_score",
    "three_lock_days_since_entry",
    "turnover_rate",
)


def feature_group(column: str) -> str | None:
    """Return the ablation group for a feature column, or ``None`` for base features."""
    name = str(column)
    if name.endswith("_is_missing"):
        name = name[: -len("_is_missing")]
    for group in GROUP_ORDER:
        if any(name.startswith(prefix) for prefix in GROUP_PREFIXES[group]):
            return group
    return None


def _to_numeric(frame: pd.DataFrame, columns) -> pd.DataFrame:
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _prepare(
    frame: pd.DataFrame | None, *, ts_column: str | None = None, dedupe: bool = True
) -> pd.DataFrame:
    """Normalize keys, coerce dates and (optionally) collapse duplicate keys.

    ``dedupe=False`` is required for seat-level event tables: ``top_inst`` and
    ``hm_detail`` legitimately carry several rows per stock and trading day, and
    collapsing them would silently turn an aggregation into a single seat.
    """
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=KEY_COLUMNS)
    out = frame.copy()
    if "stock_code" not in out.columns:
        for candidate in (ts_column, "ts_code"):
            if candidate and candidate in out.columns:
                out["stock_code"] = out[candidate].map(normalize_cn_stock_code)
                break
    if "stock_code" not in out.columns:
        return pd.DataFrame(columns=KEY_COLUMNS)
    out["stock_code"] = out["stock_code"].astype(str)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    out = out.dropna(subset=KEY_COLUMNS)
    out = out.sort_values(KEY_COLUMNS)
    if dedupe:
        out = out.drop_duplicates(KEY_COLUMNS, keep="last")
    return out.reset_index(drop=True)


def _grouped(frame: pd.DataFrame):
    return frame.groupby("stock_code", sort=False, group_keys=False)


def _roll(frame: pd.DataFrame, column: str, window: int, *, how: str = "sum", min_periods: int | None = None):
    periods = int(min_periods if min_periods is not None else window)
    grouped = _grouped(frame)[column]
    if how == "sum":
        return grouped.transform(lambda value: value.rolling(window, min_periods=periods).sum())
    if how == "mean":
        return grouped.transform(lambda value: value.rolling(window, min_periods=periods).mean())
    if how == "std":
        return grouped.transform(lambda value: value.rolling(window, min_periods=max(2, periods)).std())
    raise ValueError(f"unsupported rolling aggregation: {how}")


def _shift(frame: pd.DataFrame, column: str, periods: int):
    return _grouped(frame)[column].transform(lambda value: value.shift(periods))


def _cross_section_rank(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame.groupby("trade_date", sort=False)[column].rank(pct=True)


def sessions_since_flag(frame: pd.DataFrame, flag: pd.Series, *, cap: float = 60.0) -> pd.Series:
    """Trading sessions elapsed since the most recent ``flag == 1`` row per stock.

    Vectorized replacement for a per-stock Python loop: the position of the last
    active row is forward-filled inside each stock and subtracted from the
    running row position. Values before the first event stay NaN.
    """
    position = pd.Series(np.arange(len(frame), dtype=float), index=frame.index)
    active = pd.to_numeric(flag, errors="coerce").fillna(0.0) > 0
    last_position = position.where(active).groupby(frame["stock_code"].astype(str)).ffill()
    elapsed = position - last_position
    return elapsed.clip(upper=cap)


def _safe_divide(numerator, denominator):
    denominator = pd.to_numeric(denominator, errors="coerce")
    result = pd.to_numeric(numerator, errors="coerce") / denominator.replace(0, np.nan)
    return result.replace([np.inf, -np.inf], np.nan)


# ---------------------------------------------------------------------------
# CYC 成本线代理
# ---------------------------------------------------------------------------
def build_cyc_features(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Rolling cost-line proxies plus trend/volatility context from daily bars."""
    bars = _prepare(ohlcv, ts_column="ts_code")
    if bars.empty:
        return pd.DataFrame(columns=KEY_COLUMNS)
    bars = _to_numeric(bars, ["close", "high", "low", "open", "volume", "amount"])
    close = bars["close"]
    volume = bars["volume"].clip(lower=0)
    amount = bars["amount"].clip(lower=0)
    bars["_volume"] = volume
    bars["_amount"] = amount
    bars["_amount_adj"] = close * volume
    out = bars[KEY_COLUMNS].copy()

    for window in CYC_WINDOWS:
        sum_amount = _roll(bars, "_amount", window)
        sum_amount_adj = _roll(bars, "_amount_adj", window)
        sum_volume = _roll(bars, "_volume", window)
        valid = sum_volume > 0
        cyc = _safe_divide(sum_amount.where(valid), sum_volume.where(valid))
        cyc_adj = _safe_divide(sum_amount_adj.where(valid), sum_volume.where(valid))
        out[f"cyc_{window}"] = cyc
        out[f"cyc_adj_{window}"] = cyc_adj
        out[f"close_to_cyc_{window}"] = _safe_divide(close, cyc) - 1.0
        out[f"close_to_cyc_adj_{window}"] = _safe_divide(close, cyc_adj) - 1.0

    for window, periods in ((5, 3), (5, 5), (13, 5), (34, 10)):
        column = f"cyc_{window}"
        out[f"cyc{window}_slope_{periods}d"] = _safe_divide(out[column], _shift(out, column, periods)) - 1.0

    out["cyc_spread_5_34"] = _safe_divide(out["cyc_5"], out["cyc_34"]) - 1.0
    out["cyc_spread_5_13"] = _safe_divide(out["cyc_5"], out["cyc_13"]) - 1.0
    out["cyc5_stretch_20d"] = _safe_divide(out["cyc_5"], _roll(bars.assign(_c=out["cyc_5"]), "_c", 20, how="mean")) - 1.0

    bars["_ret1"] = _grouped(bars)["close"].transform(lambda value: value.pct_change())
    out["ret_1d"] = bars["_ret1"]
    out["ret_3d"] = _grouped(bars)["close"].transform(lambda value: value.pct_change(3))
    out["ret_5d"] = _grouped(bars)["close"].transform(lambda value: value.pct_change(5))
    out["ret_10d"] = _grouped(bars)["close"].transform(lambda value: value.pct_change(10))
    out["ret_20d"] = _grouped(bars)["close"].transform(lambda value: value.pct_change(20))
    out["close_to_ma20"] = _safe_divide(
        close, _grouped(bars)["close"].transform(lambda value: value.rolling(20, min_periods=20).mean())
    ) - 1.0
    out["volatility_20d_pct"] = _grouped(bars)["_ret1"].transform(
        lambda value: value.rolling(20, min_periods=20).std()
    )
    out["volatility_60d_pct"] = _grouped(bars)["_ret1"].transform(
        lambda value: value.rolling(60, min_periods=40).std()
    )
    mean_volume_20 = _roll(bars, "_volume", 20, how="mean")
    out["vr20_volume_ratio"] = _safe_divide(volume, mean_volume_20)
    out["vr20_amount_ratio"] = _safe_divide(amount, _roll(bars, "_amount", 20, how="mean"))
    out["turnover_ratio_5_20"] = _safe_divide(
        _roll(bars, "_volume", 5, how="mean"), mean_volume_20
    )
    out["intraday_range_pct"] = _safe_divide(bars["high"] - bars["low"], close)
    out["close_position_20d"] = _safe_divide(
        close - _grouped(bars)["low"].transform(lambda value: value.rolling(20, min_periods=20).min()),
        _grouped(bars)["high"].transform(lambda value: value.rolling(20, min_periods=20).max())
        - _grouped(bars)["low"].transform(lambda value: value.rolling(20, min_periods=20).min()),
    )
    out["amount_trend_5_20"] = _safe_divide(
        _roll(bars, "_amount", 5, how="mean"), _roll(bars, "_amount", 20, how="mean")
    )
    return out


# ---------------------------------------------------------------------------
# 标准 moneyflow：连续净流入 + 主力 / 敢死队订单分层
# ---------------------------------------------------------------------------
def build_moneyflow_features(moneyflow: pd.DataFrame, *, daily_basic: pd.DataFrame | None = None) -> pd.DataFrame:
    """Main-capital and daredevil proxies from the standard ``moneyflow`` table."""
    flow = _prepare(moneyflow)
    if flow.empty:
        return pd.DataFrame(columns=KEY_COLUMNS)
    numeric = {}
    for name, column in (
        ("buy_sm", "buy_sm_amount"), ("sell_sm", "sell_sm_amount"),
        ("buy_md", "buy_md_amount"), ("sell_md", "sell_md_amount"),
        ("buy_lg", "buy_lg_amount"), ("sell_lg", "sell_lg_amount"),
        ("buy_elg", "buy_elg_amount"), ("sell_elg", "sell_elg_amount"),
    ):
        numeric[name] = pd.to_numeric(flow[column], errors="coerce") if column in flow.columns else pd.Series(np.nan, index=flow.index)
    net = pd.to_numeric(flow["net_mf_amount"], errors="coerce") if "net_mf_amount" in flow.columns else pd.Series(np.nan, index=flow.index)

    turnover = sum(value.abs() for value in numeric.values())
    main_buy = numeric["buy_lg"] + numeric["buy_elg"]
    main_sell = numeric["sell_lg"] + numeric["sell_elg"]
    main_net = main_buy - main_sell
    dare_net = numeric["buy_elg"] - numeric["sell_elg"]
    lg_net = numeric["buy_lg"] - numeric["sell_lg"]

    working = flow[KEY_COLUMNS].copy()
    working["_turnover"] = turnover
    working["_main_net"] = main_net
    working["_dare_net"] = dare_net
    working["_net"] = net
    working["_main_turnover"] = main_buy.abs() + main_sell.abs()

    out = working[KEY_COLUMNS].copy()
    out["mf_net_1d"] = net
    out["mf_net_pct_1d"] = _safe_divide(net, turnover)
    out["main_net_1d"] = main_net
    out["dare_net_1d"] = dare_net
    out["main_elg_net_1d"] = dare_net
    out["main_lg_net_1d"] = lg_net
    out["main_turnover_share_1d"] = _safe_divide(working["_main_turnover"], turnover)

    for window in MF_WINDOWS:
        out[f"mf_net_{window}d"] = _roll(working, "_net", window)
        out[f"mf_net_pct_{window}d"] = _safe_divide(
            _roll(working, "_net", window), _roll(working, "_turnover", window)
        )
        out[f"mf_net_z_{window}d"] = _safe_divide(
            net - _roll(working, "_net", window, how="mean"),
            _roll(working, "_net", window, how="std"),
        )
        out[f"mf_positive_ratio_{window}d"] = _roll(
            working.assign(_pos=(net > 0).astype(float)), "_pos", window, how="mean"
        )
        out[f"main_net_{window}d"] = _roll(working, "_main_net", window)
        out[f"main_strength_{window}d"] = _safe_divide(
            _roll(working, "_main_net", window), _roll(working, "_turnover", window)
        )
        out[f"main_positive_ratio_{window}d"] = _roll(
            working.assign(_pos=(main_net > 0).astype(float)), "_pos", window, how="mean"
        )
        out[f"dare_net_{window}d"] = _roll(working, "_dare_net", window)
        out[f"dare_strength_{window}d"] = _safe_divide(
            _roll(working, "_dare_net", window), _roll(working, "_turnover", window)
        )

    out["main_acceleration_3d"] = out["main_net_3d"] - _shift(out, "main_net_3d", 3)
    out["main_acceleration_5d"] = out["main_net_5d"] - _shift(out, "main_net_5d", 5)
    out["dare_acceleration_3d"] = out["dare_net_3d"] - _shift(out, "dare_net_3d", 3)
    out["dare_net_ratio_3d"] = _safe_divide(out["dare_net_3d"], out["main_net_3d"].abs())
    out["main_net_1d_norm"] = _safe_divide(working["_main_net"], _roll(working, "_turnover", 20, how="mean"))

    if daily_basic is not None and len(daily_basic):
        basic = _prepare(daily_basic)
        basic = _to_numeric(basic, ["circ_mv", "total_mv", "turnover_rate", "turnover_rate_f", "volume_ratio", "pe_ttm", "pb"])
        keep = [column for column in ("circ_mv", "total_mv", "turnover_rate", "turnover_rate_f", "volume_ratio") if column in basic.columns]
        merged = out.merge(basic[KEY_COLUMNS + keep], on=KEY_COLUMNS, how="left")
        if "circ_mv" in merged.columns:
            circ_mv = merged["circ_mv"]
            for window in (3, 5, 10, 20):
                merged[f"main_float_cap_{window}d"] = _safe_divide(merged[f"main_net_{window}d"], circ_mv)
            merged["main_turnover_share_20d_mean"] = _roll(merged, "main_turnover_share_1d", 20, how="mean")
        out = merged
    return out


# ---------------------------------------------------------------------------
# 龙虎榜 / 游资事件
# ---------------------------------------------------------------------------
def build_event_features(
    *,
    top_list: pd.DataFrame | None = None,
    top_inst: pd.DataFrame | None = None,
    hm_detail: pd.DataFrame | None = None,
    calendar: pd.DataFrame | None = None,
    coverage: tuple[str | None, str | None] | None = None,
) -> pd.DataFrame:
    """Sparse LHB / hot-money events aggregated to ``stock_code + trade_date``.

    ``coverage`` is the ``(first_date, last_date)`` range the event sources were
    actually retrieved for. Rows outside it are reset to NaN so a fetch gap can
    never be mistaken for "not on the list today" (which stays ``flag = 0``).
    """
    grid = _prepare(calendar)
    if grid.empty:
        return pd.DataFrame(columns=KEY_COLUMNS)
    out = grid[KEY_COLUMNS].copy()
    event_columns: list[str] = []

    listed = _prepare(top_list, dedupe=False)
    if not listed.empty:
        listed = _to_numeric(listed, ["net_amount", "net_rate", "amount_rate", "l_buy", "l_sell", "amount", "turnover_rate", "pct_change"])
        grouped = listed.groupby(KEY_COLUMNS, sort=False)
        aggregated = grouped.agg(
            top_list_flag=("stock_code", "size"),
            top_list_net_rate=("net_rate", "max") if "net_rate" in listed.columns else ("stock_code", "size"),
            top_list_amount_rate=("amount_rate", "max") if "amount_rate" in listed.columns else ("stock_code", "size"),
            top_list_net_amount=("net_amount", "sum") if "net_amount" in listed.columns else ("stock_code", "size"),
            top_list_l_buy=("l_buy", "sum") if "l_buy" in listed.columns else ("stock_code", "size"),
            top_list_l_sell=("l_sell", "sum") if "l_sell" in listed.columns else ("stock_code", "size"),
        ).reset_index()
        aggregated["top_list_flag"] = (aggregated["top_list_flag"] > 0).astype(float)
        out = out.merge(aggregated, on=KEY_COLUMNS, how="left")
        out["top_list_flag"] = out["top_list_flag"].fillna(0.0)
        event_columns.extend([column for column in aggregated.columns if column != "stock_code" and column != "trade_date"])

    inst = _prepare(top_inst, dedupe=False)
    if not inst.empty:
        inst = _to_numeric(inst, ["buy", "sell", "net_buy", "buy_rate", "sell_rate"])
        if "side" in inst.columns:
            inst["side"] = pd.to_numeric(inst["side"], errors="coerce")
        grouped = inst.groupby(KEY_COLUMNS, sort=False)
        aggregated = grouped.agg(
            top_inst_net_buy=("net_buy", "sum"),
            top_inst_buy=("buy", "sum"),
            top_inst_sell=("sell", "sum"),
            top_inst_seats=("net_buy", "size"),
        ).reset_index()
        aggregated["top_inst_net_buy_rate"] = _safe_divide(
            aggregated["top_inst_net_buy"], aggregated["top_inst_buy"] + aggregated["top_inst_sell"]
        )
        if "side" in inst.columns:
            buy_side = inst[inst["side"] == 0].groupby(KEY_COLUMNS, sort=False)["net_buy"].sum().rename("top_inst_buy_side_net").reset_index()
            sell_side = inst[inst["side"] == 1].groupby(KEY_COLUMNS, sort=False)["net_buy"].sum().rename("top_inst_sell_side_net").reset_index()
            aggregated = aggregated.merge(buy_side, on=KEY_COLUMNS, how="left").merge(sell_side, on=KEY_COLUMNS, how="left")
            aggregated["top_inst_net_buy_rate_side"] = _safe_divide(
                aggregated["top_inst_buy_side_net"], aggregated["top_inst_buy_side_net"].abs() + aggregated["top_inst_sell_side_net"].abs()
            )
        out = out.merge(aggregated, on=KEY_COLUMNS, how="left")
        event_columns.extend([column for column in aggregated.columns if column not in KEY_COLUMNS])

    hot = _prepare(hm_detail, dedupe=False)
    if not hot.empty:
        hot = _to_numeric(hot, ["buy_amount", "sell_amount", "net_amount"])
        grouped = hot.groupby(KEY_COLUMNS, sort=False).agg(
            hm_net=("net_amount", "sum"),
            hm_buy=("buy_amount", "sum"),
            hm_sell=("sell_amount", "sum"),
            hm_count=("net_amount", "size"),
        ).reset_index()
        aggregated = grouped
        aggregated["hm_net_rate"] = _safe_divide(aggregated["hm_net"], aggregated["hm_buy"] + aggregated["hm_sell"])
        aggregated["hm_buy_ratio"] = _safe_divide(aggregated["hm_buy"], aggregated["hm_buy"] + aggregated["hm_sell"])
        out = out.merge(aggregated, on=KEY_COLUMNS, how="left")
        event_columns.extend([column for column in aggregated.columns if column not in KEY_COLUMNS])

    for column in ("top_list_net_rate", "top_list_amount_rate", "top_list_net_amount", "top_list_l_buy", "top_list_l_sell"):
        if column in out.columns:
            out[f"{column}_filled"] = out[column].fillna(0.0)
    out = out.sort_values(KEY_COLUMNS).reset_index(drop=True)
    if "top_list_flag" in out.columns:
        out["top_list_days_since"] = sessions_since_flag(out, out["top_list_flag"]).to_numpy()
        event_columns.append("top_list_days_since")
    if coverage is not None:
        start, end = coverage
        dates = pd.to_datetime(out["trade_date"])
        covered = pd.Series(True, index=out.index)
        if start is not None:
            covered &= dates >= pd.Timestamp(start)
        if end is not None:
            covered &= dates <= pd.Timestamp(end)
        if not bool(covered.all()):
            for column in dict.fromkeys(event_columns):
                out[column] = out[column].where(covered, np.nan)
    return out


# ---------------------------------------------------------------------------
# 筹码成本分布（cyq_perf）
# ---------------------------------------------------------------------------
def build_cyq_features(cyq_perf: pd.DataFrame | None, ohlcv: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cost distribution levels from ``cyq_perf`` joined to same-day closes."""
    chips = _prepare(cyq_perf)
    if chips.empty:
        return pd.DataFrame(columns=KEY_COLUMNS)
    chips = _to_numeric(
        chips,
        ["cost_5pct", "cost_15pct", "cost_50pct", "cost_85pct", "cost_95pct", "weight_avg", "winner_rate", "his_low", "his_high"],
    )
    out = chips[KEY_COLUMNS].copy()
    for column in ("cost_5pct", "cost_15pct", "cost_50pct", "cost_85pct", "cost_95pct", "weight_avg", "winner_rate"):
        if column in chips.columns:
            out[f"cyq_{column}"] = chips[column]
    if "cost_50pct" in chips.columns:
        out["cyq_cost_concentration"] = _safe_divide(
            chips["cost_85pct"] - chips["cost_15pct"], chips["cost_50pct"]
        )
        out["cyq_cost_50_slope_5d"] = _safe_divide(out["cyq_cost_50pct"], _shift(out, "cyq_cost_50pct", 5)) - 1.0
        out["cyq_cost_50_slope_20d"] = _safe_divide(out["cyq_cost_50pct"], _shift(out, "cyq_cost_50pct", 20)) - 1.0
    if "winner_rate" in chips.columns:
        out["cyq_winner_rate_chg_5d"] = out["cyq_winner_rate"] - _shift(out, "cyq_winner_rate", 5)
    if ohlcv is not None and len(ohlcv):
        bars = _to_numeric(_prepare(ohlcv), ["close"])
        merged = out.merge(bars[KEY_COLUMNS + ["close"]], on=KEY_COLUMNS, how="left")
        if "cost_50pct" in merged.columns:
            merged["close_to_cyq_cost_50"] = _safe_divide(merged["close"], merged["cyq_cost_50pct"]) - 1.0
        if {"cost_5pct", "cost_95pct"}.issubset(merged.columns):
            merged["cyq_price_position"] = _safe_divide(
                merged["close"] - merged["cyq_cost_5pct"], merged["cyq_cost_95pct"] - merged["cyq_cost_5pct"]
            )
        out = merged.drop(columns=[column for column in ("close",) if column in merged.columns])
    return out


# ---------------------------------------------------------------------------
# 来源共识（standard / DC / THS）
# ---------------------------------------------------------------------------
def build_consensus_features(
    moneyflow: pd.DataFrame,
    *,
    moneyflow_dc: pd.DataFrame | None = None,
    moneyflow_ths: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Source consensus without summing different vendors' amounts."""
    base = _prepare(moneyflow)
    if base.empty:
        return pd.DataFrame(columns=KEY_COLUMNS)
    net_std = pd.to_numeric(base.get("net_mf_amount"), errors="coerce")
    out = base[KEY_COLUMNS].copy()
    out["_std"] = net_std
    sources = {"std": net_std}

    for name, frame in (("dc", moneyflow_dc), ("ths", moneyflow_ths)):
        source = _prepare(frame)
        if source.empty or "net_amount" not in source.columns:
            continue
        column = f"{name}_net_amount"
        sources[name] = pd.to_numeric(source["net_amount"], errors="coerce").rename(column)
        out = out.merge(
            source[KEY_COLUMNS].assign(**{column: pd.to_numeric(source["net_amount"], errors="coerce")}),
            on=KEY_COLUMNS,
            how="left",
        )
        if name == "ths" and "net_d5_amount" in source.columns:
            out = out.merge(
                source[KEY_COLUMNS].assign(
                    ths_net_d5_amount=pd.to_numeric(source["net_d5_amount"], errors="coerce")
                ),
                on=KEY_COLUMNS,
                how="left",
            )

    source_columns = [column for column in out.columns if column.endswith("_net_amount") or column == "_std"]
    out["consensus_source_count"] = out[source_columns].notna().sum(axis=1).astype(float)
    signs = np.sign(out[source_columns])
    positive = (signs > 0).sum(axis=1).astype(float)
    negative = (signs < 0).sum(axis=1).astype(float)
    available = out[source_columns].notna().sum(axis=1).astype(float)
    out["consensus_sign_agreement"] = _safe_divide(
        pd.concat([positive, negative], axis=1).max(axis=1), available
    )
    out["consensus_sign"] = np.sign(positive - negative).where(available > 0, np.nan)

    def _z(column: str) -> pd.Series:
        series = pd.to_numeric(out[column], errors="coerce")
        frame = out[KEY_COLUMNS].copy()
        frame["_v"] = series
        mean = _roll(frame, "_v", 20, how="mean")
        std = _roll(frame, "_v", 20, how="std")
        return _safe_divide(series - mean, std)

    for name in ("dc", "ths"):
        column = f"{name}_net_amount"
        if column not in out.columns:
            continue
        out[f"{name}_net_z_20d"] = _z(column)
        frame = out[KEY_COLUMNS].copy()
        frame["_v"] = pd.to_numeric(out[column], errors="coerce")
        out[f"{name}_net_5d"] = _roll(frame, "_v", 5)
        out[f"{name}_positive_ratio_20d"] = _roll(
            frame.assign(_p=(frame["_v"] > 0).astype(float)), "_p", 20, how="mean"
        )
    if "ths_net_d5_amount" in out.columns:
        out["ths_net_d5_z_20d"] = _z("ths_net_d5_amount")
    return out


# ---------------------------------------------------------------------------
# 派生：横截面 rank、敢死队评分、三把锁
# ---------------------------------------------------------------------------
def add_cross_sectional_ranks(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.copy()
    rank_specs = {
        "mf_net_rank_5d": "mf_net_5d",
        "mf_net_rank_20d": "mf_net_20d",
        "mf_strength_rank_5d": "mf_net_pct_5d",
        "main_strength_rank_3d": "main_strength_3d",
        "main_strength_rank_5d": "main_strength_5d",
        "main_net_rank_3d": "main_net_3d",
        "dare_strength_rank_3d": "dare_strength_3d",
        "dare_strength_rank_5d": "dare_strength_5d",
        "vol_ratio_rank_20d": "vr20_volume_ratio",
        "ret_3d_rank": "ret_3d",
        "turnover_rank_20d": "turnover_rate",
        "volatility_rank_20d": "volatility_20d_pct",
    }
    columns: dict[str, pd.Series] = {}
    for name, source in rank_specs.items():
        if source in out.columns:
            columns[name] = _cross_section_rank(out, source)
        else:
            columns[name] = pd.Series(np.nan, index=out.index)
    return pd.concat([out, pd.DataFrame(columns, index=out.index)], axis=1)


def add_daredevil_features(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.copy()
    components = [
        "dare_strength_rank_3d",
        "vol_ratio_rank_20d",
        "ret_3d_rank",
        "turnover_rank_20d",
    ]
    blended = out[components].sum(axis=1, min_count=1) / out[components].notna().sum(axis=1).replace(0, np.nan)
    daredevil_score = (
        0.80 * blended
        + 0.10 * out.get("top_list_flag", pd.Series(0.0, index=out.index)).fillna(0.0)
        + 0.10 * out.get("top_inst_net_buy_rate", pd.Series(0.0, index=out.index)).fillna(0.0).clip(-1.0, 1.0)
    )
    overheat = (
        (out.get("dare_strength_rank_3d", pd.Series(np.nan, index=out.index)) >= 0.90)
        & (out.get("ret_3d", pd.Series(np.nan, index=out.index)) >= 0.15)
        & (out.get("volatility_20d_pct", pd.Series(np.nan, index=out.index)) >= 0.060)
    )
    columns = {
        "daredevil_score": daredevil_score,
        "daredevil_overheat_flag": overheat.astype(float).where(
            out.get("dare_strength_rank_3d", pd.Series(np.nan, index=out.index)).notna(), np.nan
        ),
        "daredevil_event_flag": out.get("top_list_flag", pd.Series(0.0, index=out.index)).fillna(0.0).clip(0, 1),
    }
    return pd.concat([out, pd.DataFrame(columns, index=out.index)], axis=1)


def add_lock_features(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.copy()
    derived: dict[str, pd.Series] = {}
    close = out.get("close")
    cyc_5 = out.get("cyc_5")
    cyc_5_slope_1d = _safe_divide(cyc_5, _shift(out, "cyc_5", 1)) - 1.0
    derived["cyc5_slope_1d"] = cyc_5_slope_1d
    trend_lock = (
        (close > cyc_5)
        & (cyc_5_slope_1d > 0)
        & (out.get("close_to_ma20", pd.Series(np.nan, index=out.index)) > 0)
    )
    derived["trend_lock"] = trend_lock.astype(float).where(cyc_5.notna() & close.notna(), np.nan)

    cost_lock = (
        (cyc_5_slope_1d > 0)
        & (out.get("close_to_cyc_5", pd.Series(np.nan, index=out.index)).between(-0.02, 0.12))
        & (out.get("cyc5_slope_3d", pd.Series(np.nan, index=out.index)) > 0)
    )
    derived["cost_lock"] = cost_lock.astype(float).where(out["close_to_cyc_5"].notna(), np.nan)

    flow_lock = (
        (out.get("main_strength_rank_3d", pd.Series(np.nan, index=out.index)) > 0.70)
        & (out.get("dare_strength_rank_3d", pd.Series(np.nan, index=out.index)) > 0.60)
        & (out.get("vr20_volume_ratio", pd.Series(np.nan, index=out.index)) > 1.20)
    )
    derived["flow_lock"] = flow_lock.astype(float).where(
        out.get("main_strength_rank_3d", pd.Series(np.nan, index=out.index)).notna()
        & out.get("vr20_volume_ratio", pd.Series(np.nan, index=out.index)).notna(),
        np.nan,
    )
    out = pd.concat([out, pd.DataFrame(derived, index=out.index)], axis=1)
    locks = out[["trend_lock", "cost_lock", "flow_lock"]]
    available = locks.notna().sum(axis=1)
    three_lock_score = (locks.sum(axis=1, min_count=1) / available.replace(0, np.nan)).where(available > 0, np.nan)
    three_lock_entry = ((available == 3) & (locks.sum(axis=1) == 3)).astype(float).where(available == 3, np.nan)
    days_since_entry = sessions_since_flag(out, three_lock_entry)
    three_lock_failure = (
        (days_since_entry <= 5) & (out.get("trend_lock", pd.Series(np.nan, index=out.index)) == 0)
    ).astype(float).where(days_since_entry.notna(), np.nan)
    three_lock_overheat = (
        (out.get("daredevil_overheat_flag", pd.Series(np.nan, index=out.index)) == 1)
        & (out.get("flow_lock", pd.Series(np.nan, index=out.index)) == 1)
    ).astype(float).where(out["flow_lock"].notna(), np.nan)
    derived = {
        "three_lock_score": three_lock_score,
        "three_lock_entry": three_lock_entry,
        "three_lock_days_since_entry": days_since_entry,
        "three_lock_failure": three_lock_failure,
        "three_lock_overheat": three_lock_overheat,
    }
    return pd.concat([out, pd.DataFrame(derived, index=out.index)], axis=1)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def assemble_capital_flow_locks_panel(
    ohlcv: pd.DataFrame,
    moneyflow: pd.DataFrame,
    *,
    moneyflow_dc: pd.DataFrame | None = None,
    moneyflow_ths: pd.DataFrame | None = None,
    daily_basic: pd.DataFrame | None = None,
    top_list: pd.DataFrame | None = None,
    top_inst: pd.DataFrame | None = None,
    hm_detail: pd.DataFrame | None = None,
    cyq_perf: pd.DataFrame | None = None,
    event_coverage: tuple[str | None, str | None] | None = None,
    add_missing_masks: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """Join every proxy onto the daily-bar grid and return ``(panel, manifest)``."""
    bars = _prepare(ohlcv, ts_column="ts_code")
    if bars.empty:
        raise ValueError("capital flow panel requires a non-empty OHLCV frame")
    bars = _to_numeric(bars, ["close", "high", "low", "open", "volume", "amount"])

    cyc = build_cyc_features(bars)
    flow = build_moneyflow_features(moneyflow, daily_basic=daily_basic)
    events = build_event_features(
        top_list=top_list, top_inst=top_inst, hm_detail=hm_detail, calendar=bars[KEY_COLUMNS],
        coverage=event_coverage,
    )
    chips = build_cyq_features(cyq_perf, bars)
    consensus = build_consensus_features(moneyflow, moneyflow_dc=moneyflow_dc, moneyflow_ths=moneyflow_ths)

    panel = bars[KEY_COLUMNS + ["close", "high", "low", "open", "volume", "amount"]].merge(
        cyc, on=KEY_COLUMNS, how="left"
    )
    for frame in (flow, events, chips, consensus):
        if frame is not None and len(frame):
            panel = panel.merge(frame, on=KEY_COLUMNS, how="left", suffixes=("", "_dup"))
    panel = panel.drop(
        columns=[
            column
            for column in panel.columns
            if column.endswith("_dup") or column.startswith("_")
        ]
    )

    event_columns = [column for column in ("top_list_flag",) if column in panel.columns]
    for column in event_columns:
        panel[column] = pd.to_numeric(panel[column], errors="coerce").fillna(0.0)
    panel = panel.sort_values(KEY_COLUMNS).reset_index(drop=True)
    panel = add_cross_sectional_ranks(panel)
    panel = add_daredevil_features(panel)
    panel = add_lock_features(panel)

    if add_missing_masks:
        masks: dict[str, pd.Series] = {}
        for column in MASK_COLUMNS:
            if column in panel.columns:
                masks[f"{column}_is_missing"] = panel[column].isna().astype(float)
        if masks:
            panel = pd.concat([panel, pd.DataFrame(masks, index=panel.index)], axis=1)

    feature_columns = [
        column
        for column in panel.columns
        if column not in set(KEY_COLUMNS) | {"close", "high", "low", "open", "volume", "amount"}
    ]
    coverage = {column: float(panel[column].notna().mean()) for column in feature_columns}
    dates = pd.to_datetime(panel["trade_date"])
    groups: dict[str, list[str]] = {group: [] for group in GROUP_ORDER}
    ungrouped: list[str] = []
    for column in feature_columns:
        group = feature_group(column)
        if group is None:
            ungrouped.append(column)
        else:
            groups[group].append(column)
    manifest = {
        "feature_version": FEATURE_VERSION,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "rows": int(len(panel)),
        "stocks": int(panel["stock_code"].nunique()),
        "start_date": str(dates.min().date()),
        "end_date": str(dates.max().date()),
        "trading_days": int(dates.nunique()),
        "feature_count": len(feature_columns),
        "feature_groups": groups,
        "ungrouped_features": ungrouped,
        "coverage": coverage,
        "mask_columns": [f"{column}_is_missing" for column in MASK_COLUMNS if f"{column}_is_missing" in panel.columns],
        "pit_rules": [
            "features use trade_date close and earlier only",
            "moneyflow amounts stay in vendor units; vendors are never summed",
            "missing moneyflow keeps NaN plus an is_missing mask",
            "top_list_flag=0 means 'not on the list today'; uncovered dates are reported separately",
            "cross-sectional ranks are computed within one trade_date",
        ],
        "unit_notes": {
            "moneyflow_amounts": "vendor units (万元), used only in ratios or within-vendor windows",
            "cyc": "rolling sum(amount)/sum(volume) from the daily bar table",
            "cyc_adj": "rolling sum(close*volume)/sum(volume) so the ratio shares the qfq close unit",
            "daily_basic_circ_mv": "vendor units (万元), paired with moneyflow amounts (万元)",
        },
    }
    return panel, manifest


__all__ = [
    "FEATURE_VERSION",
    "GROUP_ORDER",
    "GROUP_PREFIXES",
    "MASK_COLUMNS",
    "feature_group",
    "build_cyc_features",
    "build_moneyflow_features",
    "build_event_features",
    "build_cyq_features",
    "build_consensus_features",
    "add_cross_sectional_ranks",
    "add_daredevil_features",
    "add_lock_features",
    "assemble_capital_flow_locks_panel",
]
