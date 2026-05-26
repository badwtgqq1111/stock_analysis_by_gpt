from abc import ABC, abstractmethod

import pandas as pd


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


def is_bullish_trend(row, price_buffer=0.0):
    """统一的顺势过滤：MA25 在 MA60 上方且 MA25 斜率向上。"""
    if row is None:
        return False

    ma25 = row.get('MA25')
    ma60 = row.get('MA60')
    ma25_slope = row.get('MA25_Slope')
    close = row.get('Close')

    if pd.isna(ma25) or pd.isna(ma60) or pd.isna(ma25_slope) or pd.isna(close):
        return False
    if ma25 <= ma60 or ma25_slope <= 0:
        return False
    if price_buffer is not None and close < ma25 * (1 - price_buffer):
        return False
    return True


def build_buy_signal(
    row,
    signal_strength,
    signal_mode,
    reasons,
    risk_score=2,
    signal_tier='strong',
    actionable=True,
    holding_horizon=60,
    min_holding_bars_for_trend_exit=0,
    trailing_stop_pct=0.92,
    trailing_activation_gain=0.05,
    stop_loss_price=None,
    risk_filters=None,
    entry_type=None,
):
    """为新增策略生成统一结构的买入信号。"""
    close_price = row['Close']
    atr_value = row.get('ATR')
    volume_ma10 = row.get('Volume_MA10')
    volume_ratio = close_price

    if pd.notna(volume_ma10) and volume_ma10 not in (0, 0.0):
        volume_ratio = row['Volume'] / volume_ma10
    else:
        volume_ratio = 1

    if stop_loss_price is None:
        if pd.notna(atr_value):
            stop_loss_price = close_price - atr_value * 2
        else:
            stop_loss_price = close_price * 0.92

    return {
        'date': row.name,
        'close': close_price,
        'signal_strength': signal_strength,
        'risk_score': risk_score,
        'signal_mode': signal_mode,
        'entry_type': entry_type or signal_mode,
        'signal_tier': signal_tier,
        'actionable': actionable,
        'matrix_score': row.get('Matrix_Buy_Score'),
        'expected_3m_score': row.get('expected_3m_score'),
        'holding_horizon': holding_horizon,
        'min_holding_bars_for_trend_exit': min_holding_bars_for_trend_exit,
        'trailing_stop_pct': trailing_stop_pct,
        'trailing_activation_gain': trailing_activation_gain,
        'stop_loss_price': stop_loss_price,
        'reasons': reasons,
        'risk_filters': risk_filters or [],
        'stochrsi_k': row.get('StochRSI_K'),
        'stochrsi_d': row.get('StochRSI_D'),
        'rsi': row.get('RSI'),
        'volatility': row.get('Volatility_10d'),
        'cyc': row.get('CYC'),
        'atr': atr_value,
        'price_change_3d': row.get('Price_Change_3d'),
        'volume_ratio': volume_ratio,
        'regime_score': row.get('Trend_Regime_Score'),
        'forward_return_20': row.get('forward_return_20'),
        'forward_return_40': row.get('forward_return_40'),
        'forward_return_60': row.get('forward_return_60'),
        'forward_max_drawdown_60': row.get('forward_max_drawdown_60')
    }
