#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""回测资金与仓位执行。"""


class SimulatedBroker:
    """最小撮合代理，按给定价格全仓买入/全部卖出。"""

    def __init__(
        self,
        initial_capital,
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
    ):
        self.cash = float(initial_capital)
        self.position_shares = 0
        self.buy_commission_rate = float(buy_commission_rate)
        self.sell_commission_rate = float(sell_commission_rate)
        self.slippage_rate = float(slippage_rate)
        self.min_commission = float(min_commission)

    def buy_all(self, price):
        """按价格全仓买入。"""
        if price <= 0 or self.cash <= 0:
            return 0, 0.0, price, 0.0

        execution_price = price * (1.0 + self.slippage_rate)
        if execution_price <= 0:
            return 0, 0.0, execution_price, 0.0

        denominator = execution_price * (1.0 + self.buy_commission_rate)
        shares = int(self.cash / denominator) if denominator > 0 else 0
        if shares <= 0:
            return 0, 0.0, execution_price, 0.0

        gross_amount = shares * execution_price
        commission = self._commission(gross_amount, self.buy_commission_rate)
        total_cost = gross_amount + commission
        if total_cost > self.cash:
            shares = int((self.cash - self.min_commission) / denominator) if denominator > 0 else 0
            if shares <= 0:
                return 0, 0.0, execution_price, 0.0
            gross_amount = shares * execution_price
            commission = self._commission(gross_amount, self.buy_commission_rate)
            total_cost = gross_amount + commission
            if total_cost > self.cash:
                return 0, 0.0, execution_price, 0.0

        self.cash -= total_cost
        self.position_shares += shares
        return shares, total_cost, execution_price, commission

    def sell_all(self, price):
        """按价格全部卖出。"""
        if price <= 0 or self.position_shares <= 0:
            return 0, 0.0, price, 0.0

        execution_price = price * (1.0 - self.slippage_rate)
        if execution_price <= 0:
            return 0, 0.0, execution_price, 0.0

        shares = self.position_shares
        gross_amount = shares * execution_price
        commission = self._commission(gross_amount, self.sell_commission_rate)
        net_amount = gross_amount - commission
        self.cash += net_amount
        self.position_shares = 0
        return shares, net_amount, execution_price, commission

    def mark_to_market(self, mark_price):
        """按价格计算组合权益。"""
        return self.cash + self.position_shares * mark_price

    def _commission(self, gross_amount, rate):
        """计算成交费用。"""
        if gross_amount <= 0 or rate <= 0:
            return 0.0
        return max(gross_amount * rate, self.min_commission)
