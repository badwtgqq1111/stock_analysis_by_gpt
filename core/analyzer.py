"""Main StockAnalyzer class combining all mixin modules."""

from pathlib import Path

import numpy as np
import pandas as pd

from backtest_engine import backtest_strategy as _backtest_strategy_fn
from data.store import DataLayout, MarketDataWarehouse
from factor_engine.signals import DEFAULT_SIGNAL_RECIPES, SignalRecipeRunner

from core.backtest_ops import BacktestMixin
from core.data_loader import DataLoaderMixin
from core.factor_analysis import FactorAnalysisMixin
from core.factor_scoring import FactorScoringMixin
from core.formatting import _build_factor_explanation, _build_lightgbm_factor_explanation
from core.forward_metrics import ForwardMetricsMixin
from core.lightgbm_analysis import LightGBMAnalysisMixin
from core.signal_recipes import SignalRecipesMixin
from core.signals import SignalsMixin
from core.utils import UtilsMixin
from core.validation import ValidationMixin


class StockAnalyzer(
    DataLoaderMixin,
    FactorScoringMixin,
    ForwardMetricsMixin,
    LightGBMAnalysisMixin,
    FactorAnalysisMixin,
    ValidationMixin,
    SignalRecipesMixin,
    BacktestMixin,
    SignalsMixin,
    UtilsMixin,
):
    """港股技术分析器"""

    def __init__(
        self,
        db_dir="./assets",
        signal_recipes=None,
        market_read_only=True,
    ):
        """
        初始化分析器

        Args:
            db_dir (str): 数据库目录
            signal_recipes: 信号 recipe 名称列表
        """
        self.db_dir = Path(db_dir)
        self.data_layout = DataLayout(base_dir=str(self.db_dir / "data"))
        self.market_warehouse = MarketDataWarehouse(self.data_layout, read_only=market_read_only)
        self.signal_recipes = tuple(signal_recipes or DEFAULT_SIGNAL_RECIPES)
        self.signal_recipe_runner = SignalRecipeRunner(self.signal_recipes)

    def get_all_stocks(self):
        """
        获取数据库中所有股票代码

        Returns:
            list: 股票代码列表
        """
        try:
            return self.market_warehouse.get_all_stock_codes(
                market="HK",
                asset_type="equity",
                frequency="daily",
                adjust="qfq",
            )
        except Exception as e:
            print(f"[ERROR] 获取股票列表失败: {e}")
            return []

    @staticmethod
    def _build_factor_explanation(factor_details, factor_scores, score_index):
        return _build_factor_explanation(factor_details, factor_scores, score_index)

    @staticmethod
    def _slice_factor_details(factor_details, row_mask):
        if not factor_details:
            return factor_details

        mask_series = pd.Series(np.asarray(row_mask, dtype=bool))
        sliced = {
            "factor_set": factor_details.get("factor_set"),
            "component_weights": dict(factor_details.get("component_weights", {})),
            "factors": {},
        }
        for factor_name, meta in (factor_details.get("factors") or {}).items():
            factor_meta = dict(meta or {})
            raw_series = factor_meta.get("raw_series")
            score_series = factor_meta.get("score_series")
            if isinstance(raw_series, pd.Series) and len(raw_series) == len(mask_series):
                factor_meta["raw_series"] = raw_series.iloc[mask_series.to_numpy()].copy()
            if isinstance(score_series, pd.Series) and len(score_series) == len(mask_series):
                factor_meta["score_series"] = score_series.iloc[mask_series.to_numpy()].copy()
            sliced["factors"][factor_name] = factor_meta
        return sliced

    @staticmethod
    def _build_low_price_setup_snapshot(data):
        return SignalRecipeRunner(DEFAULT_SIGNAL_RECIPES).evaluate(data)

    def _build_signal_setup_snapshot(self, data, context=None):
        return self.signal_recipe_runner.evaluate(data, context=context)

    def close(self):
        if getattr(self, "market_warehouse", None):
            self.market_warehouse.close()
            self.market_warehouse = None

    def __del__(self):
        self.close()


StockAnalyzer._default_analyze_stock_factors = StockAnalyzer.analyze_stock_factors
