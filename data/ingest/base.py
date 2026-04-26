#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""数据接入抽象基类。"""

from abc import ABC, abstractmethod


class BaseMarketDataLoader(ABC):
    """统一的市场数据加载接口。"""

    @abstractmethod
    def fetch_history(self, stock_code, **kwargs):
        """获取并标准化历史数据。"""

    @abstractmethod
    def fetch_info(self, stock_code, **kwargs):
        """获取并标准化基础信息。"""

    @abstractmethod
    def sync(self, stock_code, **kwargs):
        """拉取并写入底层存储。"""
