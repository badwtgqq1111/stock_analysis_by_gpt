#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""龙虎榜（top_list / top_inst）→ 知情交易代理特征。

交易所口径的龙虎榜在 **T 日盘后**公布（实测 2026-09-18 的榜单在 19:45 可取到），
因此特征只能使用 ``trade_date <= T`` 的行，和生产链路的
``AVAILABILITY_RULE``（T 日收盘决策、T+1 开盘执行）一致。

三层特征（P1.18 §3.1）：

* 事件层：``lhb_net_ratio`` / ``lhb_net_rate`` / ``lhb_amount_rate`` / ``lhb_reason_*``
* 席位层：机构专用、沪深股通、营业部各自的净买入占比 + 买入集中度 HHI
* 时序层：近 5/20 个交易日的上榜次数、净额之和、距最近一次上榜的交易日数

**稀疏语义**：某只股票当天没上榜，说明"未触发上榜"，不是抓取失败 —— 数值列取 0，
另有 ``lhb_is_missing`` 标记"该 (stock, date) 是否落在龙虎榜数据覆盖范围内"，
两者必须分开读。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

from data.model.schemas import normalize_stock_code

# 席位分类：先用交易所公布的"机构专用/股通专用"，其余归营业部（游资）。
SEAT_INSTITUTION = "机构专用"
SEAT_NORTHBOUND = ("沪股通专用", "深股通专用")
SEAT_BROKER = "营业部"

# ``reason`` 文本 → 短标签。同一只股票一天可能同时命中多条。
# 顺序即优先级：ST/连续三日这类更强的监管口径先判，避免被"涨幅偏离"吃掉。
REASON_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("st", ("ST", "退市")),
    ("three_day", ("连续三个交易日内", "连续三日")),
    ("price_deviation", ("涨幅偏离", "跌幅偏离")),
    ("turnover", ("换手率",)),
    ("amplitude", ("振幅",)),
    ("other", ("",)),
)

DEFAULT_WINDOWS = (5, 20)


def classify_seat(exalter: object) -> str:
    """把席位全称归到 ``institution`` / ``northbound`` / ``broker`` / ``other``。"""
    name = str(exalter or "")
    if SEAT_INSTITUTION in name:
        return "institution"
    if any(token in name for token in SEAT_NORTHBOUND):
        return "northbound"
    if SEAT_BROKER in name:
        return "broker"
    return "other"


def reason_label(reason: object) -> str:
    """单标签（按 :data:`REASON_BUCKETS` 优先级取第一个命中的桶）。"""
    text = str(reason or "")
    for label, tokens in REASON_BUCKETS:
        if label == "other":
            continue
        if any(token in text for token in tokens):
            return label
    return "other"


def reason_labels(reason: object) -> list[str]:
    """多标签：一条上榜原因可能同时是"连续三日 + ST"，两条都要留下。"""
    text = str(reason or "")
    hits = [
        label for label, tokens in REASON_BUCKETS
        if label != "other" and any(token in text for token in tokens)
    ]
    return hits or ["other"]


def load_top_list_snapshots(raw_dir: str | Path) -> pd.DataFrame:
    """读取全部 ``top_list_*.parquet`` 快照并按主键去重（保留最新抓取）。"""
    return _load_snapshots(raw_dir, "top_list")


def load_top_inst_snapshots(raw_dir: str | Path) -> pd.DataFrame:
    return _load_snapshots(raw_dir, "top_inst")


def _load_snapshots(raw_dir: str | Path, prefix: str) -> pd.DataFrame:
    root = Path(raw_dir)
    files = sorted(root.glob(f"{prefix}_*.parquet"))
    if not files:
        return pd.DataFrame()
    frames = []
    for path in files:
        frame = pd.read_parquet(path)
        frame["_snapshot"] = path.name
        frames.append(frame)
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    out = out.dropna(subset=["trade_date"])
    out["stock_code"] = out["ts_code"].map(lambda code: normalize_stock_code(code, market="CN"))
    key = ["trade_date", "stock_code"]
    if "exalter" in out.columns:
        key.append("exalter")
    if "side" in out.columns:
        # The same seat can legitimately appear on both the buy and the sell
        # side of one name on one day; dropping by (seat, reason) alone loses one.
        key.append("side")
    if "reason" in out.columns:
        key.append("reason")
    if "retrieved_at" in out.columns:
        out["retrieved_at"] = pd.to_datetime(out["retrieved_at"], errors="coerce")
        out = out.sort_values("retrieved_at")
    return out.drop_duplicates(subset=key, keep="last").reset_index(drop=True)


def build_seat_features(top_inst: pd.DataFrame) -> pd.DataFrame:
    """按 (stock, date) 聚合席位明细 → 机构/北向/游资净买入占比 + 买入集中度。"""
    if top_inst is None or top_inst.empty:
        return pd.DataFrame(columns=["trade_date", "stock_code"])
    frame = top_inst.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["stock_code"] = frame["ts_code"].map(lambda code: normalize_stock_code(code, market="CN"))
    frame["seat_kind"] = frame["exalter"].map(classify_seat)
    for column in ("buy", "sell", "net_buy"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce").fillna(0.0)

    grouped = frame.groupby(["trade_date", "stock_code"], sort=False)
    total_abs = grouped["net_buy"].apply(lambda values: float(np.abs(values).sum()))
    rows = {"lhb_seat_net_abs": total_abs}
    for kind in ("institution", "northbound", "broker"):
        net = frame[frame["seat_kind"] == kind].groupby(["trade_date", "stock_code"])["net_buy"].sum()
        rows[f"lhb_{kind}_net"] = net
    seats = pd.DataFrame(rows).reset_index()
    denominator = seats["lhb_seat_net_abs"].replace(0.0, np.nan)
    for kind in ("institution", "northbound", "broker"):
        column = f"lhb_{kind}_net"
        if column not in seats:
            seats[column] = 0.0
        seats[column] = seats[column].fillna(0.0)
        label = {"institution": "lhb_inst_net_share", "northbound": "lhb_north_net_share",
                 "broker": "lhb_hot_money_share"}[kind]
        seats[label] = seats[column] / denominator

    buys = frame[frame["buy"] > 0].sort_values(["trade_date", "stock_code", "buy"], ascending=[True, True, False])
    top5 = buys.groupby(["trade_date", "stock_code"]).head(5)
    hhi = top5.groupby(["trade_date", "stock_code"])["buy"].apply(
        lambda values: float(((values / values.sum()) ** 2).sum()) if values.sum() > 0 else np.nan
    )
    seats = seats.merge(hhi.rename("lhb_seat_concentration"), on=["trade_date", "stock_code"], how="left")
    seats["lhb_inst_buy_flag"] = (seats["lhb_inst_net_share"].fillna(0.0) > 0.0).astype(float)
    return seats


def build_top_list_features(
    top_list: pd.DataFrame,
    top_inst: pd.DataFrame | None = None,
    *,
    sessions: Iterable[pd.Timestamp] | None = None,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
) -> tuple[pd.DataFrame, dict]:
    """事件层 + 席位层 + 时序层特征；返回 ``(features, audit)``。

    输出行是"股票 × 覆盖范围内每个交易日"（只包含窗口内至少上榜一次的股票），
    未上榜的交易日数值列取 0 且 ``lhb_listed`` 为 False —— 便于直接并入面板。
    """
    if top_list is None or top_list.empty:
        return pd.DataFrame(), {"rows": 0, "reason": "top_list empty"}

    events = top_list.copy()
    events["trade_date"] = pd.to_datetime(events["trade_date"], errors="coerce")
    events["stock_code"] = events["ts_code"].map(lambda code: normalize_stock_code(code, market="CN"))
    events = events.dropna(subset=["trade_date", "stock_code"])
    for column in ("net_amount", "float_values", "net_rate", "amount_rate", "amount", "pct_change"):
        events[column] = pd.to_numeric(events.get(column), errors="coerce")
    events["lhb_reason"] = events["reason"].map(reason_label)
    # 一条上榜原因可能同时属于多个桶（"连续三日 + ST"），展开后再做哑变量。
    events = events.assign(lhb_reason=events["reason"].map(reason_labels)).explode("lhb_reason")
    events = events.drop_duplicates(subset=["trade_date", "stock_code", "lhb_reason"], keep="last")

    # 事件层：同一股票一天可能命中多条 reason，净额类字段只在第一行保留一次。
    per_day = events.sort_values("trade_date").groupby(["trade_date", "stock_code"], sort=False)
    event_layer = per_day.agg(
        lhb_net_amount=("net_amount", "max"),
        lhb_net_rate=("net_rate", "max"),
        lhb_amount_rate=("amount_rate", "max"),
        lhb_float_values=("float_values", "max"),
        lhb_pct_change=("pct_change", "max"),
        lhb_reason_count=("lhb_reason", "nunique"),
    ).reset_index()
    float_values = event_layer["lhb_float_values"].replace(0.0, np.nan)
    event_layer["lhb_net_ratio"] = event_layer["lhb_net_amount"] / float_values
    reason_dummies = (
        events.assign(value=1.0)
        .pivot_table(index=["trade_date", "stock_code"], columns="lhb_reason", values="value", aggfunc="max")
        .fillna(0.0)
    )
    reason_dummies.columns = [f"lhb_reason_{column}" for column in reason_dummies.columns]
    event_layer = event_layer.merge(reason_dummies.reset_index(), on=["trade_date", "stock_code"], how="left")
    for column in [c for c in event_layer.columns if c.startswith("lhb_reason_")]:
        event_layer[column] = event_layer[column].fillna(0.0)
    event_layer["lhb_listed"] = 1.0

    seat_layer = build_seat_features(top_inst) if top_inst is not None else pd.DataFrame()
    merged = event_layer.merge(seat_layer, on=["trade_date", "stock_code"], how="left") if not seat_layer.empty else event_layer

    # 时序列：先铺到交易日网格再滚动，保证"近 N 个交易日"按会话数而不是行数计算。
    if sessions is None:
        session_index = pd.DatetimeIndex(sorted(merged["trade_date"].unique()))
    else:
        session_index = pd.DatetimeIndex(sorted(pd.to_datetime(list(sessions))))
    codes = sorted(merged["stock_code"].unique())
    grid = pd.MultiIndex.from_product([session_index, codes], names=["trade_date", "stock_code"]).to_frame(index=False)
    wide = grid.merge(merged, on=["trade_date", "stock_code"], how="left").sort_values(["stock_code", "trade_date"])
    wide["lhb_listed"] = wide["lhb_listed"].fillna(0.0)
    value_columns = [c for c in wide.columns if c.startswith("lhb_") and c != "lhb_listed"]
    grouped = wide.groupby("stock_code", sort=False)
    wide["lhb_days_since_last"] = grouped["lhb_listed"].transform(
        lambda series: _days_since_last(series.to_numpy())
    )
    for window in windows:
        labelled = f"{window}d"
        wide[f"lhb_count_{labelled}"] = grouped["lhb_listed"].transform(
            lambda series, w=window: series.rolling(w, min_periods=1).sum()
        )
        wide[f"lhb_net_{labelled}_sum"] = grouped["lhb_net_ratio"].transform(
            lambda series, w=window: series.fillna(0.0).rolling(w, min_periods=1).sum()
        )
        if "lhb_inst_buy_flag" in wide.columns:
            wide[f"lhb_inst_buy_days_{labelled}"] = grouped["lhb_inst_buy_flag"].transform(
                lambda series, w=window: series.fillna(0.0).rolling(w, min_periods=1).sum()
            )
    for column in value_columns:
        if column in {"lhb_net_ratio", "lhb_net_rate", "lhb_amount_rate", "lhb_inst_net_share",
                      "lhb_north_net_share", "lhb_hot_money_share", "lhb_seat_concentration"}:
            continue
        wide[column] = wide[column].fillna(0.0)

    coverage_start = session_index.min()
    wide["lhb_is_missing"] = 0.0
    wide["lhb_is_missing"] = wide["lhb_is_missing"].astype(bool)
    audit = {
        "rows": int(len(wide)),
        "event_rows": int(event_layer.shape[0]),
        "stocks": int(len(codes)),
        "sessions": int(len(session_index)),
        "session_start": str(pd.Timestamp(coverage_start).date()) if coverage_start is not None else None,
        "session_end": str(pd.Timestamp(session_index.max()).date()) if len(session_index) else None,
        "listed_days": int(wide["lhb_listed"].sum()),
        "reason_counts": events["lhb_reason"].value_counts().to_dict(),
        "feature_columns": [c for c in wide.columns if c.startswith("lhb_")],
    }
    return wide.reset_index(drop=True), audit


def _days_since_last(flags: np.ndarray) -> np.ndarray:
    """距最近一次上榜的会话数（从未上榜 → NaN）。"""
    out = np.full(len(flags), np.nan)
    counter = np.nan
    for index, flag in enumerate(flags):
        if flag and flag > 0:
            counter = 0.0
        elif not np.isnan(counter):
            counter += 1.0
        out[index] = counter
    return out
