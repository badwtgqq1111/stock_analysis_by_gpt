#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""factor_validation 本地测试。"""

import sys
import tempfile
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService
from data.model import normalize_ohlcv_frame
from factor_validation import FactorValidator


def _build_panel_frames(stock_count=6, periods=8):
    dates = pd.date_range("2024-01-02", periods=periods, freq="B")
    stock_codes = [f"{index + 1:05d}" for index in range(stock_count)]
    ohlcv_frames = []
    feature_frames = []

    for rank, stock_code in enumerate(stock_codes, start=1):
        close_prices = [100.0]
        for _ in range(periods - 1):
            daily_return = 0.01 * (rank - (stock_count + 1) / 2)
            close_prices.append(close_prices[-1] * (1.0 + daily_return))

        raw_ohlcv = pd.DataFrame(
            {
                "date": dates,
                "Open": close_prices,
                "High": [price * 1.01 for price in close_prices],
                "Low": [price * 0.99 for price in close_prices],
                "Close": close_prices,
                "Volume": [1_000_000 + rank * 1_000] * periods,
            }
        )
        ohlcv_frames.append(normalize_ohlcv_frame(raw_ohlcv, stock_code=stock_code, market="HK", source="unit_test"))
        feature_frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "alpha_score": [float(rank)] * periods,
                }
            )
        )

    ohlcv_frame = pd.concat(ohlcv_frames, ignore_index=True)
    return stock_codes, feature_frames, ohlcv_frame


def test_factor_validator_direct_pipeline():
    stock_codes, feature_frames, ohlcv_frame = _build_panel_frames()
    validator = FactorValidator(horizons=(1,), quantiles=3, min_observations=3)

    long_feature_frames = []
    for stock_code, feature_frame in zip(stock_codes, feature_frames):
        current = feature_frame.copy()
        current["stock_code"] = stock_code
        current["feature_set"] = "alpha_demo"
        long_feature_frames.append(current)
    feature_frame = pd.concat(long_feature_frames, ignore_index=True)

    result = validator.validate(feature_frame=feature_frame, ohlcv_frame=ohlcv_frame)
    assert not result["validation_frame"].empty
    assert not result["ic_summary"].empty
    assert not result["quantile_summary"].empty
    assert not result["long_short_summary"].empty
    assert not result["turnover_summary"].empty
    assert not result["decay_summary"].empty

    ic_summary = result["ic_summary"].iloc[0]
    assert ic_summary["mean_ic"] > 0.99
    assert ic_summary["mean_rank_ic"] > 0.99
    assert "ic_pvalue" in result["ic_by_date"].columns
    assert "rank_ic_pvalue" in result["ic_by_date"].columns
    assert "ic_tstat" in result["ic_summary"].columns
    assert "rank_ic_tstat" in result["ic_summary"].columns

    quantile_summary = result["quantile_summary"]
    low_group = quantile_summary.loc[quantile_summary["quantile"] == 1, "mean_return"].iloc[0]
    high_group = quantile_summary.loc[quantile_summary["quantile"] == 3, "mean_return"].iloc[0]
    assert high_group > low_group

    long_short_summary = result["long_short_summary"].iloc[0]
    assert long_short_summary["mean_spread"] > 0

    turnover_summary = result["turnover_summary"]
    assert "mean_turnover" in turnover_summary.columns
    assert turnover_summary["valid_dates"].iloc[0] > 0

    decay_summary = result["decay_summary"].sort_values("horizon").reset_index(drop=True)
    assert decay_summary["ic_decay_ratio"].iloc[0] == 1.0
    assert "spread_decay_ratio" in decay_summary.columns


def test_service_can_validate_feature_set():
    stock_codes, feature_frames, ohlcv_frame = _build_panel_frames()

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            service.warehouse.upsert_ohlcv(ohlcv_frame)
            for stock_code, feature_frame in zip(stock_codes, feature_frames):
                service.write_feature_frame(
                    feature_frame,
                    stock_code=stock_code,
                    market="HK",
                    feature_set="alpha_demo",
                    source="unit_test",
                )

            result = service.validate_feature_set(
                feature_set="alpha_demo",
                market="HK",
                frequency="daily",
                adjust="qfq",
                horizons=(1,),
                quantiles=3,
                min_observations=3,
            )
            assert result["metadata"]["feature_set"] == "alpha_demo"
            assert not result["ic_by_date"].empty
            assert result["ic_summary"]["mean_ic"].iloc[0] > 0.99
            assert "ic_pvalue" in result["ic_summary"].columns
            assert "rank_ic_pvalue" in result["ic_summary"].columns
            assert result["long_short_summary"]["mean_spread"].iloc[0] > 0
            assert not result["turnover_by_date"].empty
            assert not result["decay_summary"].empty
        finally:
            service.close()


if __name__ == "__main__":
    test_factor_validator_direct_pipeline()
    test_service_can_validate_feature_set()
    print("factor validation tests passed")
