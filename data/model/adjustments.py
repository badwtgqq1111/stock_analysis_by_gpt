#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""复权口径与企业行为标准化。"""

from dataclasses import dataclass

import pandas as pd


ADJUSTMENT_ALIASES = {
    None: "qfq",
    "": "raw",
    "none": "raw",
    "raw": "raw",
    "bfq": "raw",
    "unadjusted": "raw",
    "no_adjust": "raw",
    "qfq": "qfq",
    "forward": "qfq",
    "forward_adjusted": "qfq",
    "pre": "qfq",
    "hfq": "hfq",
    "backward": "hfq",
    "backward_adjusted": "hfq",
    "post": "hfq",
}

SUPPORTED_ADJUSTMENTS = ("raw", "qfq", "hfq")

CORPORATE_ACTION_FIELDS = [
    "event_date",
    "announcement_date",
    "ex_date",
    "record_date",
    "payment_date",
    "fiscal_year",
    "plan_explain",
    "distribution_type",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "action_type",
    "cash_dividend",
    "stock_split_ratio",
    "bonus_share_ratio",
    "rights_issue_ratio",
    "rights_issue_price",
    "source",
    "ingest_time",
]

CORPORATE_ACTION_TYPE_ALIASES = {
    "cash_dividend": "cash_dividend",
    "dividend": "cash_dividend",
    "cash": "cash_dividend",
    "split": "stock_split",
    "stock_split": "stock_split",
    "reverse_split": "reverse_split",
    "bonus": "bonus_issue",
    "bonus_issue": "bonus_issue",
    "rights": "rights_issue",
    "rights_issue": "rights_issue",
    "spinoff": "spin_off",
    "spin_off": "spin_off",
    "merger": "merger",
}


@dataclass(frozen=True)
class AdjustmentProfile:
    """统一复权口径描述。"""

    adjust: str
    label: str
    description: str
    requires_corporate_actions: bool = False


ADJUSTMENT_PROFILES = {
    "raw": AdjustmentProfile(
        adjust="raw",
        label="不复权",
        description="保持交易所原始价格口径，不做分红拆股价格回溯处理。",
        requires_corporate_actions=False,
    ),
    "qfq": AdjustmentProfile(
        adjust="qfq",
        label="前复权",
        description="用当前口径回溯历史价格，便于做技术分析、研究和回测连续收益。",
        requires_corporate_actions=True,
    ),
    "hfq": AdjustmentProfile(
        adjust="hfq",
        label="后复权",
        description="将历史口径映射到当时真实成交价格附近，便于核对长期涨跌幅。",
        requires_corporate_actions=True,
    ),
}


def normalize_adjust(adjust, default="qfq"):
    """标准化复权方式。"""
    normalized_default = str(default or "qfq").strip().lower()
    normalized_key = normalized_default if adjust is None else str(adjust).strip().lower()
    if normalized_key not in ADJUSTMENT_ALIASES:
        raise ValueError(f"不支持的复权方式: {adjust}")
    normalized_adjust = ADJUSTMENT_ALIASES[normalized_key]
    if normalized_adjust not in SUPPORTED_ADJUSTMENTS:
        raise ValueError(f"未注册的复权方式: {normalized_adjust}")
    return normalized_adjust


def get_adjustment_profile(adjust):
    """获取复权口径说明。"""
    normalized_adjust = normalize_adjust(adjust)
    return ADJUSTMENT_PROFILES[normalized_adjust]


def normalize_corporate_action_type(action_type):
    """标准化企业行为类型。"""
    normalized = str(action_type or "").strip().lower()
    if normalized not in CORPORATE_ACTION_TYPE_ALIASES:
        raise ValueError(f"不支持的企业行为类型: {action_type}")
    return CORPORATE_ACTION_TYPE_ALIASES[normalized]


def normalize_corporate_actions_frame(
    frame,
    stock_code,
    market="HK",
    exchange=None,
    asset_type="equity",
    source=None,
):
    """将企业行为数据标准化为统一 schema。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=CORPORATE_ACTION_FIELDS)

    from .schemas import infer_exchange, normalize_stock_code

    normalized_market = (market or "HK").upper()
    normalized_code = normalize_stock_code(stock_code, market=normalized_market)
    normalized_exchange = (exchange or infer_exchange(normalized_code, market=normalized_market)).upper()

    working = frame.copy()
    working.rename(
        columns={
            "date": "event_date",
            "Date": "event_date",
            "event_date": "event_date",
            "announcement_date": "announcement_date",
            "最新公告日期": "announcement_date",
            "ex_date": "ex_date",
            "除净日": "ex_date",
            "record_date": "record_date",
            "截至过户日": "record_date",
            "payment_date": "payment_date",
            "发放日": "payment_date",
            "fiscal_year": "fiscal_year",
            "财政年度": "fiscal_year",
            "plan_explain": "plan_explain",
            "分红方案": "plan_explain",
            "distribution_type": "distribution_type",
            "分配类型": "distribution_type",
            "action": "action_type",
            "action_type": "action_type",
            "cash": "cash_dividend",
            "cash_dividend": "cash_dividend",
            "split_ratio": "stock_split_ratio",
            "stock_split_ratio": "stock_split_ratio",
            "bonus_ratio": "bonus_share_ratio",
            "bonus_share_ratio": "bonus_share_ratio",
            "rights_ratio": "rights_issue_ratio",
            "rights_issue_ratio": "rights_issue_ratio",
            "rights_price": "rights_issue_price",
            "rights_issue_price": "rights_issue_price",
        },
        inplace=True,
    )

    required_defaults = {
        "event_date": pd.NaT,
        "announcement_date": pd.NaT,
        "ex_date": pd.NaT,
        "record_date": pd.NaT,
        "payment_date": pd.NaT,
        "fiscal_year": pd.NA,
        "plan_explain": pd.NA,
        "distribution_type": pd.NA,
        "action_type": pd.NA,
        "cash_dividend": 0.0,
        "stock_split_ratio": 0.0,
        "bonus_share_ratio": 0.0,
        "rights_issue_ratio": 0.0,
        "rights_issue_price": 0.0,
    }
    for column, default_value in required_defaults.items():
        if column not in working.columns:
            working[column] = default_value

    for column in ["event_date", "announcement_date", "ex_date", "record_date", "payment_date"]:
        working[column] = pd.to_datetime(working[column], errors="coerce")
    working.loc[working["event_date"].isna(), "event_date"] = working["ex_date"]
    working.loc[working["event_date"].isna(), "event_date"] = working["payment_date"]
    working.loc[working["event_date"].isna(), "event_date"] = working["announcement_date"]
    working.dropna(subset=["event_date", "action_type"], inplace=True)
    if working.empty:
        return pd.DataFrame(columns=CORPORATE_ACTION_FIELDS)

    working["action_type"] = working["action_type"].map(normalize_corporate_action_type)
    for column in [
        "cash_dividend",
        "stock_split_ratio",
        "bonus_share_ratio",
        "rights_issue_ratio",
        "rights_issue_price",
    ]:
        working[column] = pd.to_numeric(working[column], errors="coerce").fillna(0.0)

    working["stock_code"] = normalized_code
    working["market"] = normalized_market
    working["exchange"] = normalized_exchange
    working["asset_type"] = asset_type
    working["source"] = source or "unknown"
    working["ingest_time"] = pd.Timestamp.utcnow()

    working = working[CORPORATE_ACTION_FIELDS].copy()
    working.sort_values("event_date", inplace=True)
    working.drop_duplicates(
        subset=["market", "stock_code", "event_date", "action_type"],
        keep="last",
        inplace=True,
    )
    working.reset_index(drop=True, inplace=True)
    return working
