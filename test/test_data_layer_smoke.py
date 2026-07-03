#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""数据层 smoke test，不依赖外网。"""

import tempfile
import sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import normalize_ohlcv_frame, normalize_stock_info
from data.ingest.providers.hk_industry import HKIndustryFetcher
from data.ingest.service import MarketDataService
from data.store import DataLayout, MarketDataWarehouse
from data.store import DatabaseManager
from core.market_filter import apply_filters, build_market_info_from_warehouse


def test_hk_industry_fetcher_parses_eastmoney_profile_shape():
    frame = pd.DataFrame(
        {
            "项目": ["一级行业", "二级行业", "概念题材"],
            "内容": ["资讯科技业", "互联网服务", "平台经济;游戏"],
        }
    )

    parsed = HKIndustryFetcher._parse_industry_payload(
        frame,
        source_name="unit_test_profile",
        stock_code="700",
    )

    assert parsed["stock_code"] == "00700"
    assert parsed["industry_l1"] == "资讯科技业"
    assert parsed["industry_l2"] == "互联网服务"
    assert parsed["theme_tags"] == "平台经济;游戏"
    assert parsed["industry_source"] == "unit_test_profile"


def test_hk_industry_fetcher_infers_l1_from_lone_eastmoney_industry():
    frame = pd.DataFrame(
        {
            "项目": ["所属行业"],
            "内容": ["软件服务"],
        }
    )

    parsed = HKIndustryFetcher._parse_industry_payload(
        frame,
        source_name="akshare_eastmoney_company_profile",
        stock_code="700",
    )

    assert parsed["industry_l1"] == "资讯科技业"
    assert parsed["industry_l2"] == "软件服务"


def test_stock_info_infers_fund_like_instrument_type():
    etf_info = normalize_stock_info(
        {"name": "恒生科技ETF"},
        stock_code="03032",
        source="unit_test",
    )
    stock_info = normalize_stock_info(
        {"name": "Tencent", "is_fund_like": float("nan"), "tradable_flag": float("nan")},
        stock_code="00700",
        source="unit_test",
    )

    assert etf_info["instrument_type"] == "fund_like"
    assert etf_info["is_fund_like"] is True
    assert stock_info["instrument_type"] == "common_stock"
    assert stock_info["is_fund_like"] is False
    assert stock_info["tradable_flag"] is True


def test_stock_info_preserves_industry_metadata():
    with tempfile.TemporaryDirectory() as tmp_dir:
        layout = DataLayout(base_dir=tmp_dir)
        warehouse = MarketDataWarehouse(layout)

        info = normalize_stock_info(
            {
                "name": "Tencent",
                "industry_l1": "Communication Services",
                "industry_l2": "Interactive Media",
                "industry_l3": "Internet Platforms",
                "theme_tags": ["platform", "gaming"],
                "industry_source": "unit_test_industry",
                "industry_updated_at": "2026-05-31T00:00:00",
            },
            stock_code="700",
            source="unit_test",
        )
        warehouse.upsert_stock_info(info)

        loaded = warehouse.get_stock_info("00700", market="HK")

        assert loaded["industry_l1"] == "Communication Services"
        assert loaded["industry_l2"] == "Interactive Media"
        assert loaded["industry_l3"] == "Internet Platforms"
        assert loaded["theme_tags"] == "platform;gaming"
        assert loaded["industry_source"] == "unit_test_industry"
        assert pd.Timestamp(loaded["industry_updated_at"]) == pd.Timestamp("2026-05-31T00:00:00")

        price_only = normalize_stock_info(
            {
                "name": "Tencent",
                "current_price": 390.0,
            },
            stock_code="700",
            source="unit_test_price_update",
        )
        warehouse.upsert_stock_info(price_only)

        reloaded = warehouse.get_stock_info("00700", market="HK")
        assert reloaded["current_price"] == 390.0
        assert reloaded["industry_l1"] == "Communication Services"
        assert reloaded["industry_l2"] == "Interactive Media"
        assert reloaded["industry_source"] == "unit_test_industry"
        warehouse.close()


def test_stock_info_persists_richer_financial_fields_with_turnover_fallback():
    with tempfile.TemporaryDirectory() as tmp_dir:
        layout = DataLayout(base_dir=tmp_dir)
        warehouse = MarketDataWarehouse(layout)

        info = normalize_stock_info(
            {
                "name": "Tencent",
                "current_price": 390.0,
                "volume": 2_000_000.0,
                "daily_turnover": 780_000_000.0,
                "turnover_rate": None,
                "market_cap": 3_600.0,
                "pe_ratio": 22.4,
                "pb_ratio": 3.1,
                "dividend_yield": 0.82,
                "total_shares": 9_600_000_000.0,
                "circulating_shares": 8_000_000_000.0,
            },
            stock_code="700",
            source="unit_test",
        )
        warehouse.upsert_stock_info(info)

        loaded = warehouse.get_stock_info("00700", market="HK")

        assert loaded["daily_turnover"] == 780_000_000.0
        assert round(float(loaded["turnover_rate"]), 6) == 0.025
        assert loaded["market_cap"] == 3_600.0
        assert loaded["pe_ratio"] == 22.4
        assert loaded["pb_ratio"] == 3.1
        assert loaded["dividend_yield"] == 0.82
        warehouse.close()


def test_build_market_info_from_warehouse_prefers_stored_fields_and_computes_turnover_fallback():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            service.warehouse.upsert_stock_info(
                normalize_stock_info(
                    {
                        "name": "Tencent",
                        "market_cap": 3_500.0,
                        "pe_ratio": 20.0,
                        "pb_ratio": 3.0,
                        "volume": 2_000_000.0,
                        "circulating_shares": 8_000_000_000.0,
                        "daily_turnover": 780_000_000.0,
                        "turnover_rate": 0.4,
                    },
                    stock_code="00700",
                    source="unit_test",
                )
            )
            service.warehouse.upsert_stock_info(
                normalize_stock_info(
                    {
                        "name": "HSBC",
                        "market_cap": 1_200.0,
                        "pe_ratio": 9.0,
                        "pb_ratio": 0.8,
                        "volume": 1_000_000.0,
                        "circulating_shares": 2_000_000_000.0,
                        "daily_turnover": 150_000_000.0,
                        "turnover_rate": None,
                    },
                    stock_code="00005",
                    source="unit_test",
                )
            )

            market_data = build_market_info_from_warehouse(
                ["00700", "00005"],
                service.warehouse,
            )
        finally:
            service.close()

    assert market_data["00700"].daily_turnover == 780_000_000.0
    assert market_data["00700"].turnover_rate == 0.4
    assert market_data["00005"].daily_turnover == 150_000_000.0
    assert round(float(market_data["00005"].turnover_rate), 6) == 0.05
    assert market_data["00005"].market_cap == 1_200.0


def test_apply_filters_uses_warehouse_market_info_without_live_fetch():
    stock_info_frame = pd.DataFrame(
        [
            {
                "stock_code": "00700",
                "name": "Tencent",
                "market_cap": 3500.0,
                "daily_turnover": 780_000_000.0,
                "turnover_rate": 0.4,
                "pe_ratio": 20.0,
                "pb_ratio": 3.0,
            },
            {
                "stock_code": "00005",
                "name": "HSBC",
                "market_cap": 1200.0,
                "daily_turnover": 90_000.0,
                "turnover_rate": 0.01,
                "pe_ratio": 9.0,
                "pb_ratio": 0.8,
            },
        ]
    )
    market_data = build_market_info_from_warehouse([], None, stock_info_frame=stock_info_frame)

    result = apply_filters(
        ["00700", "00005"],
        market_data,
        min_market_cap=1000.0,
        min_daily_turnover=100_000.0,
    )

    assert result.passed == ["00700"]
    assert result.excluded[0]["stock_code"] == "00005"


def test_backtest_filters_prefer_warehouse_market_info_before_live_fetch(monkeypatch):
    from core.backtest_ops import BacktestMixin

    class DummyWarehouse:
        def read_stock_info(self, stock_codes=None, market=None, columns=None, order_by=None):
            return pd.DataFrame(
                [
                    {
                        "stock_code": "00700",
                        "name": "Tencent",
                        "market_cap": 3500.0,
                        "daily_turnover": 780_000_000.0,
                        "turnover_rate": 0.4,
                        "pe_ratio": 20.0,
                        "pb_ratio": 3.0,
                    }
                ]
            )

        def read_ohlcv(self, stock_code=None, market=None, asset_type=None, frequency=None, adjust=None):
            return pd.DataFrame({"trade_date": pd.date_range("2026-01-01", periods=300, freq="B")})

    class DummyAnalyzer(BacktestMixin):
        def __init__(self):
            self.market_warehouse = DummyWarehouse()

        def get_all_stocks(self):
            return ["00700"]

        def _resolve_safe_analysis_workers(self, requested_workers, analysis_mode="lightgbm"):
            return 1

        def _analyze_lightgbm_market(self, stock_codes, **kwargs):
            assert stock_codes == ["00700"]
            return []

    analyzer = DummyAnalyzer()

    monkeypatch.setattr(
        "core.market_filter.fetch_market_data_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not use live fetch when warehouse has data")),
    )
    monkeypatch.setattr(
        "backtest_engine.TopNPortfolioBuilder",
        SimpleNamespace,
        raising=False,
    )

    with patch("core.backtest_ops.time.time", side_effect=[0.0, 0.1]):
        try:
            analyzer.backtest_portfolio(
                stock_codes=["00700"],
                min_market_cap=1000.0,
                min_daily_turnover=100.0,
            )
        except TypeError:
            # We only care that the market filter stage succeeds without invoking live fetch.
            pass


def test_service_backfills_hk_industry_metadata(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            base_info = normalize_stock_info(
                {"name": "Tencent", "current_price": 380.0},
                stock_code="00700",
                source="unit_test",
            )
            service.warehouse.upsert_stock_info(base_info)

            def fake_fetch(self):
                return {
                    "stock_code": self.stock_code,
                    "industry_l1": "Information Technology",
                    "industry_l2": "Internet Services",
                    "industry_source": "unit_test_industry",
                    "industry_updated_at": "2026-05-31T00:00:00",
                }

            monkeypatch.setattr(HKIndustryFetcher, "fetch", fake_fetch)

            summary = service.backfill_hk_industry(
                stock_codes=["700"],
                max_workers=1,
                force=True,
            )
            loaded = service.get_hk_stock_info("00700")
        finally:
            service.close()

    assert summary["updated"] == 1
    assert summary["coverage"]["industry_l1_rate"] == 1.0
    assert loaded["name"] == "Tencent"
    assert loaded["current_price"] == 380.0
    assert loaded["industry_l1"] == "Information Technology"
    assert loaded["industry_l2"] == "Internet Services"


def test_service_refreshes_hk_stock_info_financial_fields(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            def fake_fetch(self):
                return {
                    "name": "Tencent",
                    "current_price": 390.0,
                    "volume": 2_000_000.0,
                    "daily_turnover": 780_000_000.0,
                    "market_cap": 3500.0,
                    "pe_ratio": 20.0,
                    "pb_ratio": 3.0,
                    "circulating_shares": 8_000_000_000.0,
                }

            monkeypatch.setattr("data.ingest.providers.hk_info.StockInfoFetcher.fetch", fake_fetch)

            summary = service.refresh_hk_stock_info(stock_codes=["00700"], max_workers=1)
            loaded = service.get_hk_stock_info("00700")
        finally:
            service.close()

    assert summary["updated"] == 1
    assert loaded["daily_turnover"] == 780_000_000.0
    assert round(float(loaded["turnover_rate"]), 6) == 0.025
    assert loaded["market_cap"] == 3500.0
    assert loaded["pe_ratio"] == 20.0


def test_service_imports_hk_industry_registry_csv_without_network_fetch():
    with patch.dict("os.environ", {"CLICKHOUSE_HOST": ""}):
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "hk_industry_registry.csv"
            pd.DataFrame(
                [
                    {
                        "stock_code": "700",
                        "market": "HK",
                        "name": "Tencent",
                        "industry_l1": "资讯科技业",
                        "industry_l2": "互联网服务",
                        "industry_source": "manual_csv",
                        "industry_updated_at": "2026-06-02T00:00:00",
                        "instrument_type": "common_stock",
                        "is_fund_like": False,
                        "tradable_flag": True,
                    }
                ]
            ).to_csv(csv_path, index=False, encoding="utf-8-sig")

            service = MarketDataService(base_dir=tmp_dir)
            try:
                base_info = normalize_stock_info(
                    {"name": "Tencent Holdings", "current_price": 380.0},
                    stock_code="00700",
                    source="unit_test",
                )
                service.warehouse.upsert_stock_info(base_info)

                summary = service.import_hk_industry_registry_csv(csv_path)
                loaded = service.get_hk_stock_info("00700")
            finally:
                service.close()

    assert summary["updated"] == 1
    assert summary["skipped"] == 0
    assert loaded["name"] == "Tencent"
    assert loaded["current_price"] == 380.0
    assert loaded["industry_l1"] == "资讯科技业"
    assert loaded["industry_l2"] == "互联网服务"
    assert loaded["industry_source"] == "manual_csv"


def test_service_imports_hk_industry_registry_csv_loads_existing_registry_once():
    with patch.dict("os.environ", {"CLICKHOUSE_HOST": ""}):
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "hk_industry_registry.csv"
            pd.DataFrame(
                [
                    {
                        "stock_code": "700",
                        "market": "HK",
                        "industry_l1": "资讯科技业",
                        "industry_l2": "互联网服务",
                    },
                    {
                        "stock_code": "5",
                        "market": "HK",
                        "industry_l1": "金融业",
                        "industry_l2": "银行",
                    },
                ]
            ).to_csv(csv_path, index=False, encoding="utf-8-sig")

            service = MarketDataService(base_dir=tmp_dir)
            read_calls = []
            original_read_stock_info = service.warehouse.read_stock_info

            def counting_read_stock_info(*args, **kwargs):
                read_calls.append(kwargs.get("stock_codes"))
                return original_read_stock_info(*args, **kwargs)

            service.warehouse.read_stock_info = counting_read_stock_info
            try:
                summary = service.import_hk_industry_registry_csv(csv_path)
            finally:
                service.close()

    assert summary["updated"] == 2
    assert read_calls == [["00700", "00005"]]


def test_stock_info_preserve_existing_fields_chunks_large_key_sets():
    with tempfile.TemporaryDirectory() as tmp_dir:
        warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))
        payload = pd.DataFrame(
            [
                {"market": "HK", "stock_code": "00001", "industry_l1": "综合企业"},
                {"market": "HK", "stock_code": "00002", "industry_l1": "公用事业"},
                {"market": "HK", "stock_code": "00003", "industry_l1": "公用事业"},
            ]
        )
        read_calls = []

        def fake_read_stock_info_registry(filters=None, columns=None, order_by=None):
            read_calls.append(filters["stock_code"])
            return pd.DataFrame(columns=columns)

        warehouse._read_stock_info_registry = fake_read_stock_info_registry
        with patch.dict("os.environ", {"STOCK_INFO_LOOKUP_CHUNK_ROWS": "2"}):
            warehouse._preserve_existing_stock_info_fields(payload)

    assert read_calls == [["00001", "00002"], ["00003"]]


def test_service_normalizes_existing_hk_industry_levels():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            stale_info = normalize_stock_info(
                {
                    "name": "Tencent",
                    "industry_l1": "软件服务",
                    "industry_source": "unit_test_old",
                },
                stock_code="00700",
                source="unit_test",
            )
            service.warehouse.upsert_stock_info(stale_info)

            summary = service.normalize_existing_hk_industry(stock_codes=["00700"])
            loaded = service.get_hk_stock_info("00700")
        finally:
            service.close()

    assert summary["updated"] == 1
    assert loaded["industry_l1"] == "资讯科技业"
    assert loaded["industry_l2"] == "软件服务"
    assert loaded["industry_source"] == "unit_test_old+local_taxonomy"


def test_data_layer_smoke():
    raw_frame = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", "2024-01-03"],
            "Open": [100, 101, 101],
            "High": [102, 103, 103],
            "Low": [99, 100, 100],
            "Close": [101, 102, 102],
            "Volume": [1000, 2000, 2000],
        }
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        layout = DataLayout(base_dir=tmp_dir)
        warehouse = MarketDataWarehouse(layout)

        clean_frame = normalize_ohlcv_frame(raw_frame, stock_code="00700", source="unit_test")
        assert len(clean_frame) == 2
        assert clean_frame["stock_code"].iloc[0] == "00700"
        assert clean_frame["exchange"].iloc[0] == "HKEX"
        assert clean_frame["frequency"].iloc[0] == "daily"

        upsert_stats = warehouse.upsert_ohlcv(clean_frame)
        assert upsert_stats["rows"] == 2

        info = normalize_stock_info(
            {
                "name": "Tencent",
                "current_price": 102.0,
                "close_price": 101.0,
            },
            stock_code="00700",
            source="unit_test",
        )
        warehouse.upsert_stock_info(info)

        loaded = warehouse.read_ohlcv(stock_code="00700")
        assert len(loaded) == 2
        assert set(["stock_code", "trade_date", "exchange", "frequency", "open", "high", "low", "close", "volume"]).issubset(loaded.columns)

        parquet_path = warehouse.sync_ohlcv_to_parquet()
        assert parquet_path is not None
        assert parquet_path.exists()
        assert any(parquet_path.rglob("*.parquet"))

        warehouse.close()

    with tempfile.TemporaryDirectory() as tmp_dir:
        layout = DataLayout(base_dir=tmp_dir)
        read_only_warehouse = MarketDataWarehouse(layout, read_only=True)
        assert read_only_warehouse.read_only is True
        try:
            try:
                read_only_warehouse.upsert_stock_info({"stock_code": "00700"})
                raise AssertionError("expected read-only warehouse to reject writes")
            except RuntimeError as exc:
                assert "只读仓库不支持写入" in str(exc)
        finally:
            read_only_warehouse.close()

    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = DatabaseManager(tmp_dir)
        index_frame = raw_frame.copy()
        index_frame["date"] = pd.to_datetime(index_frame["date"])
        index_frame.set_index("date", inplace=True)
        index_frame.index.name = "date"

        bridge_payload = index_frame.rename(columns={
            "Open": "Open",
            "High": "High",
            "Low": "Low",
            "Close": "Close",
            "Volume": "Volume",
        })

        save_stats = manager.save_kline_data(bridge_payload, "00700")
        assert save_stats["total_records"] == 2
        assert save_stats["parquet_path"] is not None

        loaded = manager.get_kline_data("00700")
        assert loaded is not None
        assert len(loaded) == 2
        assert list(loaded.columns) == ["Open", "Close", "High", "Low", "Volume"]

        stats = manager.get_statistics("00700")
        assert stats is not None
        assert stats["total_records"] == 2

        manager.close()


if __name__ == "__main__":
    test_data_layer_smoke()
    print("data layer smoke test passed")
