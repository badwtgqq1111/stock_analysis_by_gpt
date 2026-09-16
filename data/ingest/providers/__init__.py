#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股数据 provider 集合。"""

from . import cn_baostock
from .cn_baostock import (
    CNBaoStockBasicFetcher,
    CNBaoStockFinancialFetcher,
    CNBaoStockHistoryFetcher,
    CNBaoStockIndustryFetcher,
)
from .cn_baidu_valuation import CNBaiduValuationHistoryFetcher
from .cn_history import CNHistoryDataFetcher
from .cn_info import CNStockInfoFetcher
from .cn_sw2021 import SW2021IndustryFetcher
from .cn_sw2021_relay import SW2021RelayIndustryFetcher
from .cn_tushare_relay import CNAdjustmentFactorRelayFetcher, CNDailyBasicRelayFetcher, CNTushareRelayClient
from .cn_universe import CNMarketListFetcher
from .cn_valuation_history import CNEastmoneyValuationHistoryFetcher
from .hk_corporate_actions import HKCorporateActionsFetcher
from .hk_history import HistoryDataFetcher
from .hk_industry import HKIndustryFetcher
from .hk_info import StockInfoFetcher
from .hk_universe import HKMarketListFetcher

__all__ = [
    "cn_baostock",
    "CNBaoStockBasicFetcher",
    "CNBaoStockFinancialFetcher",
    "CNBaoStockHistoryFetcher",
    "CNBaoStockIndustryFetcher",
    "CNBaiduValuationHistoryFetcher",
    "CNHistoryDataFetcher",
    "CNMarketListFetcher",
    "CNStockInfoFetcher",
    "SW2021IndustryFetcher",
    "SW2021RelayIndustryFetcher",
    "CNDailyBasicRelayFetcher",
    "CNAdjustmentFactorRelayFetcher",
    "CNTushareRelayClient",
    "CNEastmoneyValuationHistoryFetcher",
    "HKCorporateActionsFetcher",
    "HKIndustryFetcher",
    "HistoryDataFetcher",
    "StockInfoFetcher",
    "HKMarketListFetcher",
]
