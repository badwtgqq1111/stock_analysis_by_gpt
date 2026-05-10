from backtest_engine import BacktestConfig, BacktestEngine


def backtest_strategy(data, buy_signals, sell_signals, initial_capital=100000, default_holding_days=60):
    """
    回测交易策略 - 次日开盘买入，默认持有60个交易日，风险退出优先于策略性卖出。

    Args:
        data (DataFrame): 股票数据
        buy_signals (DataFrame): 买入信号
        sell_signals (DataFrame): 卖出信号
        initial_capital (float): 初始资金
        default_holding_days (int): 默认持有交易日

    Returns:
        dict: 回测结果
    """
    engine = BacktestEngine(
        config=BacktestConfig(
            initial_capital=initial_capital,
            default_holding_days=default_holding_days,
        )
    )
    return engine.run(data, buy_signals, sell_signals)
