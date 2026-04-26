#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Raw 层原始抓取快照存储。"""

from datetime import datetime
from pathlib import Path

import pandas as pd

from data.model.adjustments import normalize_adjust


class RawDataStore:
    """将抓取阶段的原始 OHLCV 快照落盘到 raw 层。"""

    RAW_OHLCV_DATASET = "ohlcv_snapshots"
    RAW_CORPORATE_ACTIONS_DATASET = "corporate_actions_snapshots"

    def __init__(self, layout):
        self.layout = layout

    def write_ohlcv_snapshot(
        self,
        frame,
        stock_code,
        market,
        exchange,
        asset_type,
        frequency,
        source,
        adjust,
        request_start_date=None,
        request_end_date=None,
    ):
        """写入一次抓取得到的原始 OHLCV 快照。"""
        if frame is None or frame.empty:
            return None

        prepared = frame.copy()
        if "date" not in prepared.columns:
            if prepared.index.name == "date" or isinstance(prepared.index, pd.DatetimeIndex):
                prepared = prepared.reset_index()
            else:
                raise ValueError("raw 快照缺少 date 列")

        prepared.rename(
            columns={
                "date": "raw_trade_date",
                "open": "raw_open",
                "high": "raw_high",
                "low": "raw_low",
                "close": "raw_close",
                "volume": "raw_volume",
                "Open": "raw_open",
                "High": "raw_high",
                "Low": "raw_low",
                "Close": "raw_close",
                "Volume": "raw_volume",
            },
            inplace=True,
        )

        required = ["raw_trade_date", "raw_open", "raw_high", "raw_low", "raw_close", "raw_volume"]
        missing = [column for column in required if column not in prepared.columns]
        if missing:
            raise ValueError(f"raw 快照缺少必要列: {', '.join(missing)}")

        prepared["raw_trade_date"] = pd.to_datetime(prepared["raw_trade_date"], errors="coerce")
        prepared.dropna(subset=["raw_trade_date"], inplace=True)
        if prepared.empty:
            return None

        captured_at = pd.Timestamp.utcnow()
        normalized_adjust = normalize_adjust(adjust)
        prepared["captured_at"] = captured_at
        prepared["stock_code"] = stock_code
        prepared["market"] = market
        prepared["exchange"] = exchange
        prepared["asset_type"] = asset_type
        prepared["frequency"] = frequency
        prepared["source"] = source
        prepared["adjust"] = normalized_adjust
        prepared["request_start_date"] = request_start_date
        prepared["request_end_date"] = request_end_date

        output_columns = [
            "captured_at",
            "stock_code",
            "market",
            "exchange",
            "asset_type",
            "frequency",
            "source",
            "adjust",
            "request_start_date",
            "request_end_date",
            "raw_trade_date",
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "raw_volume",
        ]
        payload = prepared[output_columns].copy()

        dataset_root = self.layout.dataset_path(self.RAW_OHLCV_DATASET, layer="raw")
        partition_dir = (
            Path(dataset_root)
            / f"market={market}"
            / f"frequency={frequency}"
            / f"source={source}"
            / f"stock_code={stock_code}"
        )
        partition_dir.mkdir(parents=True, exist_ok=True)

        filename = f"snapshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.parquet"
        output_path = partition_dir / filename
        payload.to_parquet(output_path, index=False)
        return output_path

    def write_corporate_actions_snapshot(
        self,
        frame,
        stock_code,
        market,
        exchange,
        asset_type,
        source,
        request_start_date=None,
        request_end_date=None,
    ):
        """写入一次抓取得到的原始企业行为快照。"""
        if frame is None or frame.empty:
            return None

        prepared = frame.copy()
        for column in ["event_date", "announcement_date", "ex_date", "record_date", "payment_date"]:
            if column in prepared.columns:
                prepared[column] = pd.to_datetime(prepared[column], errors="coerce")

        captured_at = pd.Timestamp.utcnow()
        prepared["captured_at"] = captured_at
        prepared["stock_code"] = stock_code
        prepared["market"] = market
        prepared["exchange"] = exchange
        prepared["asset_type"] = asset_type
        prepared["source"] = source
        prepared["request_start_date"] = request_start_date
        prepared["request_end_date"] = request_end_date

        dataset_root = self.layout.dataset_path(self.RAW_CORPORATE_ACTIONS_DATASET, layer="raw")
        partition_dir = Path(dataset_root) / f"market={market}" / f"source={source}" / f"stock_code={stock_code}"
        partition_dir.mkdir(parents=True, exist_ok=True)

        filename = f"snapshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.parquet"
        output_path = partition_dir / filename
        prepared.to_parquet(output_path, index=False)
        return output_path
