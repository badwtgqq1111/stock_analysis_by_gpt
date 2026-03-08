from abc import ABC, abstractmethod


class BuyStrategy(ABC):
    """买入策略基类。"""

    @abstractmethod
    def identify_buy_signals(self, data, stock_code=None):
        """识别买入信号。"""


class SellStrategy(ABC):
    """卖出策略基类。"""

    @abstractmethod
    def identify_sell_signals(self, data):
        """识别卖出信号。"""
