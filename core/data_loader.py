"""Data loading mixin for StockAnalyzer."""

from datetime import datetime, timedelta

import pandas as pd


class DataLoaderMixin:
    """Methods for loading stock OHLCV data from the warehouse."""

    def load_stock_data(self, stock_code, days=365):
        """
        加载股票的历史数据

        Args:
            stock_code (str): 股票代码
            days (int): 加载最近多少天的数据

        Returns:
            DataFrame: 股票数据
        """
        try:
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)
            batch_map = self.load_stock_data_batch([stock_code], days=days)
            return batch_map.get(stock_code)
        except Exception as e:
            print(f"[ERROR] 加载股票 {stock_code} 数据失败: {e}")
            return None

    @staticmethod
    def _normalize_loaded_ohlcv_frame(warehouse_df):
        if warehouse_df is None or warehouse_df.empty:
            return None

        data = warehouse_df.copy()
        data["trade_date"] = pd.to_datetime(data["trade_date"])
        data.set_index("trade_date", inplace=True)
        data = data[["open", "close", "high", "low", "volume"]].rename(
            columns={"open": "Open", "close": "Close", "high": "High", "low": "Low", "volume": "Volume"}
        )
        data.index.name = "date"
        return data.sort_index()

    def load_stock_data_batch(self, stock_codes, days=365):
        """批量加载多只股票的历史数据，减少并发 parquet 打开次数。"""
        normalized_codes = [str(code).strip() for code in (stock_codes or []) if str(code).strip()]
        if not normalized_codes:
            return {}

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        warehouse_df = self.market_warehouse.read_ohlcv(
            stock_code=normalized_codes,
            market="HK",
            asset_type="equity",
            frequency="daily",
            adjust="qfq",
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d'),
        )
        if warehouse_df is None or warehouse_df.empty:
            return {}

        stock_data_map = {}
        for stock_code, stock_frame in warehouse_df.groupby("stock_code", sort=False):
            normalized_frame = self._normalize_loaded_ohlcv_frame(stock_frame)
            if normalized_frame is not None and not normalized_frame.empty:
                stock_data_map[str(stock_code)] = normalized_frame
        return stock_data_map
