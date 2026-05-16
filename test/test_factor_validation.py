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


def test_factor_validator_streaming_matches_one_shot_small_panel():
    stock_codes, feature_frames, ohlcv_frame = _build_panel_frames()
    validator = FactorValidator(horizons=(1,), quantiles=3, min_observations=3)

    long_feature_frames = []
    for stock_code, feature_frame in zip(stock_codes, feature_frames):
        current = feature_frame.copy()
        current["stock_code"] = stock_code
        current["feature_set"] = "alpha_demo"
        long_feature_frames.append(current)
    feature_frame = pd.concat(long_feature_frames, ignore_index=True)

    one_shot = validator.validate(feature_frame=feature_frame, ohlcv_frame=ohlcv_frame)

    batches = []
    for start in range(0, len(stock_codes), 2):
        current_codes = set(stock_codes[start:start + 2])
        batch_feature = feature_frame[feature_frame["stock_code"].isin(current_codes)].copy()
        batch_ohlcv = ohlcv_frame[ohlcv_frame["stock_code"].isin(current_codes)].copy()
        batches.append({"feature_frame": batch_feature, "ohlcv_frame": batch_ohlcv})

    streamed = validator.validate_streaming(batches)

    def _assert_frames_close(left, right, float_rtol=1e-10):
        """Compare DataFrames with tolerance on float columns (DuckDB vs pandas precision)."""
        pd.testing.assert_index_equal(left.columns, right.columns)
        pd.testing.assert_frame_equal(
            left.reset_index(drop=True), right.reset_index(drop=True),
            check_dtype=False, check_exact=False,
            rtol=float_rtol, atol=1e-12,
        )

    _assert_frames_close(one_shot["ic_by_date"], streamed["ic_by_date"])
    _assert_frames_close(one_shot["ic_summary"], streamed["ic_summary"])
    _assert_frames_close(one_shot["quantile_returns_by_date"], streamed["quantile_returns_by_date"])
    _assert_frames_close(one_shot["quantile_summary"], streamed["quantile_summary"])
    _assert_frames_close(one_shot["long_short_by_date"], streamed["long_short_by_date"])
    _assert_frames_close(one_shot["long_short_summary"], streamed["long_short_summary"])
    _assert_frames_close(one_shot["turnover_by_date"], streamed["turnover_by_date"])
    _assert_frames_close(one_shot["turnover_summary"], streamed["turnover_summary"])
    _assert_frames_close(one_shot["decay_summary"], streamed["decay_summary"])
    assert streamed["validation_frame"].empty
    assert streamed["quantile_membership_by_date"].empty



def test_factor_validator_streaming_does_not_reload_all_batches(monkeypatch):
    stock_codes, feature_frames, ohlcv_frame = _build_panel_frames()
    validator = FactorValidator(horizons=(1,), quantiles=3, min_observations=3)

    long_feature_frames = []
    for stock_code, feature_frame in zip(stock_codes, feature_frames):
        current = feature_frame.copy()
        current["stock_code"] = stock_code
        current["feature_set"] = "alpha_demo"
        long_feature_frames.append(current)
    feature_frame = pd.concat(long_feature_frames, ignore_index=True)

    batches = []
    for start in range(0, len(stock_codes), 2):
        current_codes = set(stock_codes[start:start + 2])
        batch_feature = feature_frame[feature_frame["stock_code"].isin(current_codes)].copy()
        batch_ohlcv = ohlcv_frame[ohlcv_frame["stock_code"].isin(current_codes)].copy()
        batches.append({"feature_frame": batch_feature, "ohlcv_frame": batch_ohlcv})

    def _fail_load_all_batches(self):
        raise AssertionError("streaming finalize should not reload all batches into memory")

    monkeypatch.setattr("factor_validation.validator.FactorValidationAccumulator._load_all_batches", _fail_load_all_batches)

    streamed = validator.validate_streaming(batches)
    assert not streamed["ic_summary"].empty
    assert streamed["validation_frame"].empty


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
