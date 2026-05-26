"""Main StockAnalyzer class combining all mixin modules."""

from pathlib import Path

import numpy as np
import pandas as pd

from backtest import backtest_strategy as _backtest_strategy_fn
from data.store import DataLayout, MarketDataWarehouse
from factor_engine.signals import DEFAULT_SIGNAL_RECIPES, SignalRecipeRunner
from indicators import calculate_technical_indicators
from reporting import generate_trading_strategy
from strategy_signals import BuyStrategy, CurrentStrategy, SellStrategy

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
        buy_strategy=None,
        sell_strategy=None,
        signal_recipes=None,
        market_read_only=True,
    ):
        """
        初始化分析器

        Args:
            db_dir (str): 数据库目录
            buy_strategy: 买入策略实例
            sell_strategy: 卖出策略实例
            signal_recipes: 信号 recipe 名称列表
        """
        self.db_dir = Path(db_dir)
        self.data_layout = DataLayout(base_dir=str(self.db_dir / "data"))
        self.market_warehouse = MarketDataWarehouse(self.data_layout, read_only=market_read_only)
        if buy_strategy is None and sell_strategy is None:
            default_strategy = CurrentStrategy()
            self.buy_strategy = default_strategy
            self.sell_strategy = default_strategy
        else:
            self.buy_strategy = buy_strategy or (sell_strategy if isinstance(sell_strategy, BuyStrategy) else CurrentStrategy())
            self.sell_strategy = sell_strategy or (self.buy_strategy if isinstance(self.buy_strategy, SellStrategy) else CurrentStrategy())
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

    def calculate_technical_indicators(self, data):
        return calculate_technical_indicators(data)

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

    def analyze_stock(self, stock_code, days=365):
        """
        分析单只股票

        Args:
            stock_code (str): 股票代码
            days (int): 分析最近多少天

        Returns:
            dict: 分析结果
        """
        print(f"\n[INFO] 分析股票 {stock_code}...")

        warmup_days = max(days + 120, days)
        full_data = self.load_stock_data(stock_code, warmup_days)
        if full_data is None:
            return None

        data_with_indicators = self.calculate_technical_indicators(full_data)
        analysis_start_idx = max(len(data_with_indicators) - days, 0)
        analysis_data = data_with_indicators.iloc[analysis_start_idx:].copy()
        analysis_start_date = analysis_data.index[0]

        buy_signals_full = self.identify_buy_signals(data_with_indicators, stock_code=stock_code)
        sell_signals_full = self.identify_sell_signals(data_with_indicators)

        buy_signals = None
        if buy_signals_full is not None and not buy_signals_full.empty:
            buy_signals = buy_signals_full[buy_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            buy_signals = self.merge_buy_signal_zones(buy_signals, stock_code=stock_code)
            if buy_signals is not None and buy_signals.empty:
                buy_signals = None

        sell_signals = None
        if sell_signals_full is not None and not sell_signals_full.empty:
            sell_signals = sell_signals_full[sell_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            if sell_signals.empty:
                sell_signals = None

        backtest_result = self.backtest_strategy(analysis_data, buy_signals, sell_signals)

        latest_expected_score = analysis_data['expected_3m_score'].dropna().iloc[-1] if 'expected_3m_score' in analysis_data and not analysis_data['expected_3m_score'].dropna().empty else np.nan
        latest_matrix_score = analysis_data['Matrix_Buy_Score'].dropna().iloc[-1] if 'Matrix_Buy_Score' in analysis_data and not analysis_data['Matrix_Buy_Score'].dropna().empty else np.nan
        latest_regime_score = analysis_data['Trend_Regime_Score'].dropna().iloc[-1] if 'Trend_Regime_Score' in analysis_data and not analysis_data['Trend_Regime_Score'].dropna().empty else np.nan
        latest_entry_type = None
        latest_signal_tier = None
        latest_signal_date = None
        current_signal_active = False
        current_signal_actionable = False
        current_signal_score = np.nan
        avg_forward_return_60_signal = 0
        avg_forward_return_60_watch = 0
        if buy_signals is not None and not buy_signals.empty:
            actionable_mask = buy_signals['actionable'] if 'actionable' in buy_signals.columns else pd.Series(True, index=buy_signals.index)
            actionable_signals = buy_signals[actionable_mask]
            watch_signals = buy_signals[~actionable_mask]
            if 'forward_return_60' in actionable_signals:
                avg_forward_return_60_signal = actionable_signals['forward_return_60'].dropna().mean() * 100 if not actionable_signals['forward_return_60'].dropna().empty else 0
            if 'forward_return_60' in watch_signals:
                avg_forward_return_60_watch = watch_signals['forward_return_60'].dropna().mean() * 100 if not watch_signals['forward_return_60'].dropna().empty else 0

            latest_signal = buy_signals.iloc[-1]
            latest_entry_type = latest_signal.get('entry_type')
            latest_signal_tier = latest_signal.get('signal_tier')
            latest_signal_date = latest_signal.get('date')
            recent_window_index = max(len(analysis_data) - 5, 0)
            recent_signal_cutoff = analysis_data.index[recent_window_index]
            current_signal_active = latest_signal_date >= recent_signal_cutoff
            current_signal_actionable = bool(latest_signal.get('actionable', False)) if current_signal_active else False
            if current_signal_active:
                current_signal_score = latest_signal.get('expected_3m_score', np.nan)

        return {
            'stock_code': stock_code,
            'data': analysis_data,
            'buy_signals': buy_signals,
            'sell_signals': sell_signals,
            'backtest': backtest_result,
            'latest_price': analysis_data['Close'].iloc[-1],
            'price_change_30d': (analysis_data['Close'].iloc[-1] - analysis_data['Close'].iloc[-30]) / analysis_data['Close'].iloc[-30] * 100 if len(analysis_data) >= 30 else 0,
            'latest_expected_3m_score': latest_expected_score,
            'latest_matrix_score': latest_matrix_score,
            'latest_regime_score': latest_regime_score,
            'latest_entry_type': latest_entry_type,
            'latest_signal_tier': latest_signal_tier,
            'latest_signal_date': latest_signal_date,
            'current_signal_active': current_signal_active,
            'current_signal_actionable': current_signal_actionable,
            'current_signal_score': current_signal_score,
            'avg_forward_return_60_signal': avg_forward_return_60_signal,
            'avg_forward_return_60_watch': avg_forward_return_60_watch
        }

    def generate_trading_strategy(self, analysis_results):
        return generate_trading_strategy(analysis_results)

    def close(self):
        if getattr(self, "market_warehouse", None):
            self.market_warehouse.close()
            self.market_warehouse = None

    def __del__(self):
        self.close()


StockAnalyzer._default_analyze_stock_factors = StockAnalyzer.analyze_stock_factors
