#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""复权口径与企业行为标准化测试。"""

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import (
    get_adjustment_profile,
    normalize_adjust,
    normalize_corporate_actions_frame,
    normalize_ohlcv_frame,
)


def test_normalize_adjust_aliases():
    assert normalize_adjust(None) == "qfq"
    assert normalize_adjust("forward") == "qfq"
    assert normalize_adjust("hfq") == "hfq"
    assert normalize_adjust("raw") == "raw"
    assert normalize_adjust("bfq") == "raw"

    profile = get_adjustment_profile("forward")
    assert profile.adjust == "qfq"
    assert profile.requires_corporate_actions is True


def test_normalize_ohlcv_frame_adjust_alias():
    frame = pd.DataFrame(
        {
            "date": ["2026-04-24", "2026-04-25"],
            "Open": [10.0, 10.2],
            "High": [10.3, 10.4],
            "Low": [9.9, 10.1],
            "Close": [10.1, 10.3],
            "Volume": [1000, 1200],
        }
    )

    normalized = normalize_ohlcv_frame(frame, stock_code="00700", market="HK", adjust="forward")
    assert list(normalized["adjust"].unique()) == ["qfq"]


def test_normalize_corporate_actions_frame():
    frame = pd.DataFrame(
        {
            "date": ["2026-04-20", "2026-04-21", "2026-04-21"],
            "action": ["dividend", "split", "split"],
            "cash": [0.5, 0.0, 0.0],
            "split_ratio": [0.0, 2.0, 2.0],
        }
    )

    normalized = normalize_corporate_actions_frame(
        frame,
        stock_code="00700",
        market="HK",
        source="unit_test",
    )
    assert len(normalized) == 2
    assert list(normalized["stock_code"].unique()) == ["00700"]
    assert list(normalized["market"].unique()) == ["HK"]
    assert set(normalized["action_type"].unique()) == {"cash_dividend", "stock_split"}
    assert normalized.loc[normalized["action_type"] == "stock_split", "stock_split_ratio"].iloc[0] == 2.0


if __name__ == "__main__":
    test_normalize_adjust_aliases()
    test_normalize_ohlcv_frame_adjust_alias()
    test_normalize_corporate_actions_frame()
    print("adjustment tests passed")
