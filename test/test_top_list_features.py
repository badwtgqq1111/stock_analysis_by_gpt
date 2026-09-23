#!/usr/bin/env python3
"""龙虎榜（top_list / top_inst）派生特征测试（P1.18 §3.1 / §4 验收）。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.providers.cn_top_list import (
    build_seat_features,
    build_top_list_features,
    classify_seat,
    reason_label,
    reason_labels,
)


@pytest.mark.parametrize(
    "exalter,expected",
    [
        ("机构专用", "institution"),
        ("深股通专用", "northbound"),
        ("沪股通专用", "northbound"),
        ("国泰海通证券股份有限公司武汉紫阳东路证券营业部", "broker"),
        ("中信证券股份有限公司总部", "other"),
        (None, "other"),
    ],
)
def test_classify_seat(exalter, expected):
    assert classify_seat(exalter) == expected


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("日涨幅偏离值达到7%的前5只证券", "price_deviation"),
        ("日跌幅偏离值达到7%的前5只证券", "price_deviation"),
        ("日换手率达到20%的前5只证券", "turnover"),
        ("日振幅值达到15%的前5只证券", "amplitude"),
        ("连续三个交易日内，涨幅偏离值累计达到20%的证券", "three_day"),
        ("ST、*ST证券连续三个交易日内收盘价格涨幅偏离值累计达15%", "st"),
        ("其他", "other"),
    ],
)
def test_reason_label(reason, expected):
    assert reason_label(reason) == expected


def test_reason_labels_keeps_every_bucket():
    labels = reason_labels("ST、*ST证券连续三个交易日内收盘价格涨幅偏离值累计达15%")
    assert labels == ["st", "three_day", "price_deviation"]
    assert reason_labels("其他") == ["other"]


def _inst_frame():
    return pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-09-16"] * 4),
        "ts_code": ["000592.SZ"] * 4,
        "exalter": ["机构专用", "机构专用", "某证券营业部", "深股通专用"],
        "buy": [6e6, 5e6, 4e6, 2e6],
        "sell": [0.0, 1e6, 0.0, 0.0],
        "net_buy": [6e6, 4e6, 4e6, 2e6],
        "side": ["0", "1", "0", "0"],
        "reason": ["日涨幅偏离值达到7%的前5只证券"] * 4,
    })


def test_seat_shares_sum_to_one_and_concentration_is_hhi():
    seats = build_seat_features(_inst_frame()).set_index(["trade_date", "stock_code"])
    row = seats.iloc[0]
    assert row["lhb_inst_net_share"] == pytest.approx(10e6 / 16e6)
    assert row["lhb_hot_money_share"] == pytest.approx(4e6 / 16e6)
    assert row["lhb_north_net_share"] == pytest.approx(2e6 / 16e6)
    # HHI over the top-5 buy seats: buys are 6/5/4/2 → shares 6/17,5/17,4/17,2/17
    buys = np.array([6.0, 5.0, 4.0, 2.0])
    expected = float(((buys / buys.sum()) ** 2).sum())
    assert row["lhb_seat_concentration"] == pytest.approx(expected)
    assert row["lhb_inst_buy_flag"] == 1.0


def _top_list_frame():
    return pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-09-16"]),
        "ts_code": ["000592.SZ"],
        "name": ["平潭发展"],
        "net_amount": [4.566261e8],
        "float_values": [1.668046e10],
        "net_rate": [15.80],
        "amount_rate": [28.43],
        "pct_change": [9.9747],
        "reason": ["日涨幅偏离值达到7%的前5只证券"],
    })


def test_event_layer_scales_net_amount_by_float_value():
    sessions = pd.to_datetime(["2026-09-15", "2026-09-16", "2026-09-17"])
    features, audit = build_top_list_features(
        _top_list_frame(), _inst_frame(), sessions=sessions
    )
    event = features[features["lhb_listed"] > 0].iloc[0]
    assert event["lhb_net_ratio"] == pytest.approx(4.566261e8 / 1.668046e10)
    assert event["lhb_net_rate"] == pytest.approx(15.80)
    assert event["lhb_reason_price_deviation"] == 1.0
    assert event["lhb_inst_net_share"] == pytest.approx(10e6 / 16e6)
    assert audit["event_rows"] == 1
    assert audit["sessions"] == 3


def test_time_series_layer_counts_sessions_and_keeps_zero_semantics():
    sessions = pd.to_datetime(["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"])
    features, _ = build_top_list_features(_top_list_frame(), _inst_frame(), sessions=sessions)
    row = features.sort_values("trade_date")
    assert row["lhb_listed"].tolist() == [0.0, 1.0, 0.0, 0.0]
    # 未上榜的交易日数值列取 0，但 lhb_is_missing 为 False（未触发上榜 != 抓取失败）
    assert row["lhb_net_ratio"].isna().tolist() == [True, False, True, True]
    assert str(row["lhb_is_missing"].dtype) == "bool"
    assert row["lhb_count_5d"].tolist() == [0.0, 1.0, 1.0, 1.0]
    assert row["lhb_days_since_last"].tolist()[1:] == [0.0, 1.0, 2.0]
    assert np.isnan(row["lhb_days_since_last"].tolist()[0])
    assert row["lhb_inst_buy_days_5d"].tolist()[-1] == pytest.approx(1.0)


def test_empty_input_returns_empty_frame_and_reason():
    features, audit = build_top_list_features(pd.DataFrame(), None)
    assert features.empty
    assert audit["rows"] == 0
