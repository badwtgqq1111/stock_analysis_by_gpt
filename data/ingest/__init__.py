#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""数据接入层。"""

from .base import BaseMarketDataLoader
from .cn_stock_loader import CNStockDataLoader
from .hk_stock_loader import HKStockDataLoader
from .providers import (
    CNHistoryDataFetcher,
    CNStockInfoFetcher,
    HKCorporateActionsFetcher,
    HKMarketListFetcher,
    HistoryDataFetcher,
    StockInfoFetcher,
)
from .service import MarketDataService

__all__ = [
    "BaseMarketDataLoader",
    "CNHistoryDataFetcher",
    "CNStockDataLoader",
    "CNStockInfoFetcher",
    "HKCorporateActionsFetcher",
    "HKMarketListFetcher",
    "HKStockDataLoader",
    "HistoryDataFetcher",
    "MarketDataService",
    "StockInfoFetcher",
]
