#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""最小回测引擎骨架。"""

from backtest_engine.engine import BacktestEngine
from backtest_engine.models import BacktestConfig
from backtest_engine.portfolio import TopNPortfolioBuilder


def backtest_strategy(data, buy_signals, sell_signals, initial_capital=100000, default_holding_days=60):
    engine = BacktestEngine(
        config=BacktestConfig(
            initial_capital=initial_capital,
            default_holding_days=default_holding_days,
        )
    )
    return engine.run(data, buy_signals, sell_signals)


__all__ = ["BacktestConfig", "BacktestEngine", "TopNPortfolioBuilder", "backtest_strategy"]
