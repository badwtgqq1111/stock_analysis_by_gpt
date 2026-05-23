"""Buy/sell signal identification mixin for StockAnalyzer."""


class SignalsMixin:
    """Methods for identifying buy and sell signals."""

    def identify_buy_signals(self, data, stock_code=None):
        return self.buy_strategy.identify_buy_signals(data, stock_code=stock_code)

    def identify_sell_signals(self, data):
        return self.sell_strategy.identify_sell_signals(data)

    def merge_buy_signal_zones(self, buy_signals, stock_code=None):
        merge_method = getattr(self.buy_strategy, "merge_buy_signal_zones", None)
        if merge_method is None:
            return buy_signals
        return merge_method(buy_signals, stock_code=stock_code)
