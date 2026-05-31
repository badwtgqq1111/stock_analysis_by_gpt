#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""数据层 smoke test，不依赖外网。"""

import tempfile
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import normalize_ohlcv_frame, normalize_stock_info
from data.ingest.providers.hk_industry import HKIndustryFetcher
from data.ingest.service import MarketDataService
from data.store import DataLayout, MarketDataWarehouse
from data.store import DatabaseManager


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
        assert loaded["theme_tags"] == "platform,gaming"
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
