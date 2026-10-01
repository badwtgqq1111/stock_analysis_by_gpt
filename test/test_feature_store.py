#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""feature 层本地 smoke test。"""

import sys
import tempfile
import uuid
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService
from data.model import normalize_feature_frame


def test_normalize_feature_frame():
    raw_frame = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03"],
            "momentum_5": [0.12, 0.15],
            "volatility_10": [0.21, 0.19],
        }
    )

    normalized = normalize_feature_frame(
        raw_frame,
        stock_code="00700",
        market="HK",
        feature_set="alpha_demo",
        source="unit_test",
    )
    assert len(normalized) == 4
    assert set(normalized["feature_name"].unique()) == {"momentum_5", "volatility_10"}
    assert normalized["feature_set"].nunique() == 1
    assert normalized["feature_set"].iloc[0] == "alpha_demo"


def test_feature_store_roundtrip():
    raw_frame = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03"],
            "momentum_5": [0.12, 0.15],
            "volatility_10": [0.21, 0.19],
        }
    )
    # ClickHouse 后端是跨 layout 共享的库表，沿用固定 feature_set 会让每次运行的 4 行
    # 累加到下一次读取里（历史上这条用例因此随运行次数“漂移”失败）。用一次性集合名隔离。
    feature_set = f"alpha_demo_{uuid.uuid4().hex[:8]}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            write_result = service.write_feature_frame(
                raw_frame,
                stock_code="00700",
                market="HK",
                feature_set=feature_set,
                source="unit_test",
            )
            assert write_result["rows"] == 4

            loaded = service.get_feature_frame(
                stock_code="00700",
                market="HK",
                feature_set=feature_set,
            )
            assert len(loaded) == 4
            assert set(loaded["feature_name"].unique()) == {"momentum_5", "volatility_10"}

            momentum_only = service.get_feature_frame(
                stock_code="00700",
                market="HK",
                feature_set=feature_set,
                feature_name="momentum_5",
            )
            assert len(momentum_only) == 2
            assert set(momentum_only["feature_name"].unique()) == {"momentum_5"}
        finally:
            service.close()


if __name__ == "__main__":
    test_normalize_feature_frame()
    test_feature_store_roundtrip()
    print("feature store tests passed")
