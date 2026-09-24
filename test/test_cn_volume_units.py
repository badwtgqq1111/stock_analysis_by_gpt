"""Tencent daily lots/shares conversion must not silently corrupt CN factors."""

import pandas as pd
import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from data.ingest.providers import cn_history
from scripts.audit_cn_volume_units import classify_volume_units
from scripts.repair_cn_volume_units_copy import repair_parquet_copy
from scripts.snapshot_cn_clickhouse_volume_copy import snapshot_and_repair_copy


def _bars(volumes, amounts):
    return pd.DataFrame(
        {
            "Open": [9.9] * len(volumes),
            "High": [10.1] * len(volumes),
            "Low": [9.8] * len(volumes),
            "Close": [10.0] * len(volumes),
            "Volume": volumes,
            "amount": amounts,
        },
        index=pd.date_range("2026-08-27", periods=len(volumes), freq="D"),
    )


def test_tencent_daily_mixed_lots_and_shares_are_normalized_rowwise():
    raw = _bars([1_000_000, 10_000, 0], [10_000_000, 10_000_000, 0])
    result = cn_history.normalize_tencent_daily_volume(raw)
    assert result["Volume"].tolist() == [1_000_000, 1_000_000, 0]
    assert raw["Volume"].tolist() == [1_000_000, 10_000, 0]
    assert result["amount"].tolist() == raw["amount"].tolist()
    assert result.attrs["volume_unit_conversion"] == {
        "shares_rows": 1, "lots_to_shares_rows": 1, "zero_rows": 1,
    }
    assert cn_history.normalize_tencent_daily_volume(result)["Volume"].tolist() == result["Volume"].tolist()


@pytest.mark.parametrize("volume,amount", [(10_000, 1_000_000), (10_000, None), (0, 1_000_000)])
def test_tencent_daily_ambiguous_positive_bar_fails(volume, amount):
    with pytest.raises(ValueError, match="unit ambiguous"):
        cn_history.normalize_tencent_daily_volume(_bars([volume], [amount]))


def test_tencent_fetcher_converts_lots_and_keeps_share_rows(monkeypatch):
    class FakeAk:
        @staticmethod
        def stock_zh_a_hist_tx(**kwargs):
            return pd.DataFrame({
                "date": ["2026-08-27", "2026-08-28"],
                "open": [9.9, 9.9], "high": [10.1, 10.1],
                "low": [9.8, 9.8], "close": [10.0, 10.0],
                "volume": [1_000_000, 10_000],
                "amount": [10_000_000, 10_000_000],
            })

    monkeypatch.setattr(cn_history, "ak", FakeAk)
    fetcher = cn_history.CNHistoryDataFetcher("000721.SZ", data_source="tencent", verbose=False)
    frame = fetcher._fetch_tencent_daily_hist(start_date="2026-08-27", end_date="2026-08-28")
    assert frame["Volume"].tolist() == [1_000_000, 1_000_000]
    assert frame["amount"].tolist() == [10_000_000, 10_000_000]


def test_tencent_fetcher_rejects_missing_amount(monkeypatch):
    class FakeAk:
        @staticmethod
        def stock_zh_a_hist_tx(**kwargs):
            return pd.DataFrame({
                "date": ["2026-08-28"], "open": [10], "high": [10.1],
                "low": [9.9], "close": [10], "volume": [10_000],
            })

    monkeypatch.setattr(cn_history, "ak", FakeAk)
    fetcher = cn_history.CNHistoryDataFetcher("000721.SZ", data_source="tencent", verbose=False)
    with pytest.raises(ValueError, match="requires amount"):
        fetcher._fetch_tencent_daily_hist(start_date="2026-08-28", end_date="2026-08-28")


def test_volume_unit_audit_and_parquet_copy_repair(tmp_path):
    original = pd.DataFrame({
        "market": ["CN", "CN", "CN"], "asset_type": ["equity"] * 3,
        "frequency": ["daily"] * 3, "adjust": ["qfq"] * 3,
        "source": ["tencent"] * 3,
        "stock_code": ["000721.SZ", "000796.SZ", "689009.SH"],
        "trade_date": pd.to_datetime(["2026-08-28"] * 3),
        "open": [10.0] * 3, "high": [10.1] * 3, "low": [9.9] * 3,
        "close": [10.0] * 3,
        "volume": [10_000.0, 1_000_000.0, 10_000.0],
        "amount": [10_000_000.0, 10_000_000.0, 1_000_000.0],
        "vwap": [1000.0, 10.0, 100.0],
    })
    source = tmp_path / "source.parquet"
    target = tmp_path / "fixed.parquet"
    pq.write_table(pa.Table.from_pandas(original, preserve_index=False), source)
    audit = classify_volume_units(original)
    assert audit["unit_class"].tolist() == ["lots_100", "shares", "ambiguous"]
    summary = repair_parquet_copy(source, target, start_date="2026-08-28", end_date="2026-08-28")
    fixed = pq.read_table(target).to_pandas()
    assert summary["lots_repaired"] == 1
    assert summary["ambiguous_untouched"] == 1
    assert summary["other_fields_unchanged"] is True
    assert fixed["volume"].tolist() == [1_000_000.0, 1_000_000.0, 10_000.0]
    assert fixed["vwap"].tolist() == [10.0, 10.0, 100.0]
    assert fixed.drop(columns=["volume", "vwap"]).equals(original.drop(columns=["volume", "vwap"]))
    assert pq.read_table(source).to_pandas().equals(original)


def test_clickhouse_snapshot_repairs_only_copy_and_keeps_other_rows(tmp_path):
    frame = pd.DataFrame({
        "market": ["CN"] * 3, "asset_type": ["equity"] * 3,
        "frequency": ["daily"] * 3, "adjust": ["qfq"] * 3,
        "source": ["tencent", "tencent", "baostock"],
        "stock_code": ["000721.SZ", "000796.SZ", "600000.SH"],
        "trade_date": pd.to_datetime(["2026-09-23"] * 3),
        "open": [10.0] * 3, "high": [10.1] * 3, "low": [9.9] * 3,
        "close": [10.0] * 3, "volume": [10_000.0, 1_000_000.0, 500.0],
        "amount": [10_000_000.0, 10_000_000.0, 5_000.0],
        "vwap": [1000.0, 10.0, 10.0],
    })

    class ReadOnlyStore:
        def read_frame(self, **kwargs):
            assert kwargs["range_filters"]["trade_date"] == {
                "gte": "2026-09-23", "lte": "2026-09-23",
            }
            return frame.copy()

    out = tmp_path / "snapshot"
    result = snapshot_and_repair_copy(
        ReadOnlyStore(), "ohlcv", out, start_date="2026-09-23", end_date="2026-09-23",
    )
    source = pq.read_table(out / "SOURCE_SNAPSHOT.parquet").to_pandas()
    repaired = pq.read_table(out / "REPAIRED_SNAPSHOT.parquet").to_pandas()
    assert result["repair"]["lots_repaired"] == 1
    assert result["unit_counts_before"] == {"lots_100": 1, "shares": 1}
    assert result["unit_counts_after"] == {"shares": 2}
    assert source.equals(frame)
    assert repaired["volume"].tolist() == [1_000_000.0, 1_000_000.0, 500.0]
    assert repaired["vwap"].tolist() == [10.0, 10.0, 10.0]
    assert repaired.drop(columns=["volume", "vwap"]).equals(frame.drop(columns=["volume", "vwap"]))
    assert result["production_mutated"] is False
    with pytest.raises(FileExistsError):
        snapshot_and_repair_copy(ReadOnlyStore(), "ohlcv", out,
                                 start_date="2026-09-23", end_date="2026-09-23")


def test_clickhouse_snapshot_rejects_duplicate_natural_keys(tmp_path):
    frame = pd.DataFrame({
        "market": ["CN", "CN"], "asset_type": ["equity", "equity"],
        "frequency": ["daily", "daily"], "adjust": ["qfq", "qfq"],
        "source": ["tencent", "tencent"], "stock_code": ["000721.SZ"] * 2,
        "trade_date": pd.to_datetime(["2026-09-23"] * 2),
        "close": [10.0, 10.0], "volume": [10_000.0, 10_000.0],
        "amount": [10_000_000.0] * 2, "vwap": [1000.0] * 2,
    })

    class DuplicateStore:
        def read_frame(self, **kwargs):
            return frame

    out = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="duplicate natural keys"):
        snapshot_and_repair_copy(DuplicateStore(), "ohlcv", out,
                                 start_date="2026-09-23", end_date="2026-09-23")
    assert not out.exists()
