import pandas as pd
import numpy as np

from .base import BuyStrategy, SellStrategy


class CurrentStrategy(BuyStrategy, SellStrategy):
    """当前买卖策略实现。"""

    def identify_buy_signals(self, data, stock_code=None):
        """
        识别买入信号 - 以未来3个月收益友好的中期趋势/突破/回踩结构为核心

        Args:
            data (DataFrame): 包含技术指标的数据

        Returns:
            DataFrame: 买入信号
        """
        if data is None or data.empty:
            return None

        signals = []
        vol_ma = data['Volatility_10d'].rolling(window=20).mean()
        atr_90th = data['ATR'].rolling(window=20).quantile(0.9)
        matrix_threshold = data['Matrix_Buy_Score'].rolling(window=40, min_periods=15).quantile(0.40)
        expected_threshold = data['expected_3m_score'].rolling(window=40, min_periods=15).quantile(0.45)

        for i in range(len(data)):
            row = data.iloc[i]
            prev_row = data.iloc[i-1] if i > 0 else None
            prev_prev_row = data.iloc[i-2] if i > 1 else None

            if prev_row is None:
                continue

            has_core_trend_inputs = (
                pd.notna(row.get('RSI')) and
                pd.notna(row.get('MA20')) and
                pd.notna(row.get('MA60'))
            )
            has_02015_bottom_inputs = (
                stock_code == '02015' and
                pd.notna(row.get('RSI')) and
                pd.notna(row.get('MA20')) and
                pd.notna(row.get('MA25')) and
                pd.notna(row.get('Distance_to_MA25')) and
                pd.notna(row.get('Volume_Spike_10d'))
            )
            has_ipo_pattern_inputs = (
                pd.notna(row.get('RSI')) and
                pd.notna(row.get('MA10')) and
                pd.notna(row.get('MA20')) and
                pd.isna(row.get('MA60')) and
                pd.notna(row.get('Distance_to_20d_High')) and
                pd.notna(row.get('Volume_Ratio_10'))
            )
            if not has_core_trend_inputs and not has_02015_bottom_inputs and not has_ipo_pattern_inputs:
                continue

            signal_strength = 0
            reasons = []
            risk_filters = []
            signal_mode = None
            entry_type = None
            signal_tier = 'strong'
            actionable = True
            matrix_score = row.get('Matrix_Buy_Score')
            expected_3m_score = row.get('expected_3m_score')
            matrix_gate = matrix_threshold.iloc[i] if pd.notna(matrix_threshold.iloc[i]) else 50
            expected_gate = expected_threshold.iloc[i] if pd.notna(expected_threshold.iloc[i]) else 55
            has_entry_confirmation = False
            max_risk_score = 4
            holding_horizon = 60
            min_holding_bars_for_trend_exit = 0
            trailing_stop_pct = 0.92
            trailing_activation_gain = 0.05

            trend_positive = (
                pd.notna(row['MA20']) and pd.notna(row['MA60']) and
                pd.notna(row['MA20_Slope']) and pd.notna(row['MA60_Slope']) and
                row['MA20'] >= row['MA60'] * 0.98 and
                row['MA20_Slope'] > -0.01 and
                row['MA60_Slope'] > -0.02
            )

            ma25_volume_breakout_ready = (
                pd.notna(row['MA25']) and pd.notna(prev_row['MA25']) and
                pd.notna(row['MA10']) and row['MA10'] >= row['MA25'] * 0.992 and
                pd.notna(row['MA25_Slope']) and row['MA25_Slope'] > -0.008 and
                row['Close'] > row['MA25'] and prev_row['Close'] <= prev_row['MA25'] * 1.015 and
                pd.notna(row['Distance_to_MA25']) and 0.0 <= row['Distance_to_MA25'] <= 0.09 and
                pd.notna(row['Volume_Spike_10d']) and row['Volume_Spike_10d'] >= 1.9 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 0.95 and
                pd.notna(row['RSI']) and 47 <= row['RSI'] <= 67 and
                pd.notna(row['Return_20d']) and row['Return_20d'] > -0.03 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > -0.22 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.10 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -0.03 and
                pd.notna(row['Upper_Shadow_Ratio']) and row['Upper_Shadow_Ratio'] <= 0.50 and
                pd.notna(row['Intraday_Return']) and row['Intraday_Return'] > -0.02
            )
            ma25_volume_breakout_watch_ready = (
                pd.notna(row['MA25']) and pd.notna(prev_row['MA25']) and
                pd.notna(row['MA10']) and row['MA10'] >= row['MA25'] * 0.985 and
                pd.notna(row['MA25_Slope']) and row['MA25_Slope'] > -0.015 and
                row['Close'] > row['MA25'] and prev_row['Close'] <= prev_row['MA25'] * 1.02 and
                pd.notna(row['Distance_to_MA25']) and 0.0 <= row['Distance_to_MA25'] <= 0.10 and
                pd.notna(row['Volume_Spike_10d']) and row['Volume_Spike_10d'] >= 1.8 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 0.9 and
                pd.notna(row['RSI']) and 45 <= row['RSI'] <= 68 and
                pd.notna(row['Return_20d']) and row['Return_20d'] > -0.05 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > -0.28 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.12 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -0.05 and
                pd.notna(row['Upper_Shadow_Ratio']) and row['Upper_Shadow_Ratio'] <= 0.60 and
                pd.notna(row['Intraday_Return']) and row['Intraday_Return'] > -0.03
            )

            stoch_bullish_cross_ready = (
                pd.notna(row['StochRSI_K']) and pd.notna(row['StochRSI_D']) and
                pd.notna(prev_row['StochRSI_K']) and pd.notna(prev_row['StochRSI_D']) and
                (15 < row['StochRSI_K'] < 55) and
                row['StochRSI_K'] > row['StochRSI_D'] and
                (
                    prev_row['StochRSI_K'] <= prev_row['StochRSI_D'] + 3 or
                    (
                        prev_prev_row is not None and
                        pd.notna(prev_prev_row['StochRSI_K']) and pd.notna(prev_prev_row['StochRSI_D']) and
                        prev_row['StochRSI_K'] > prev_row['StochRSI_D'] and
                        prev_prev_row['StochRSI_K'] <= prev_prev_row['StochRSI_D'] + 3
                    )
                )
            )

            momentum_reversal_breakout_ready = (
                pd.notna(row['MA25']) and pd.notna(prev_row['MA25']) and
                row['Close'] > row['MA25'] and prev_row['Close'] <= prev_row['MA25'] * 1.01 and
                pd.notna(row['Distance_to_MA25']) and 0.03 <= row['Distance_to_MA25'] <= 0.09 and
                pd.notna(row['Volume_Spike_10d']) and row['Volume_Spike_10d'] >= 2.0 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.5 and
                pd.notna(row['RSI']) and 50 <= row['RSI'] <= 60 and
                pd.notna(row['Return_20d']) and row['Return_20d'] > 0.0 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > -0.35 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.03 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > 0.15 and
                pd.notna(row['Upper_Shadow_Ratio']) and row['Upper_Shadow_Ratio'] <= 0.35 and
                pd.notna(row['Intraday_Return']) and row['Intraday_Return'] > 0.03
            )

            ma25_support_reclaim_ready = (
                stock_code == '09866' and
                pd.notna(row['MA25']) and pd.notna(prev_row['MA25']) and
                row['Close'] >= row['MA25'] and
                pd.notna(row['Distance_to_MA25']) and 0.0 <= row['Distance_to_MA25'] <= 0.07 and
                pd.notna(row['MA25_Slope']) and row['MA25_Slope'] > -0.01 and
                pd.notna(row['Volume_Ratio_10']) and 0.95 <= row['Volume_Ratio_10'] <= 1.40 and
                pd.notna(row['RSI']) and 48 <= row['RSI'] <= 62 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > 0.03 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.12 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -0.85 and
                pd.notna(row['Intraday_Return']) and row['Intraday_Return'] > 0 and
                (
                    prev_row['Close'] <= prev_row['MA25'] * 1.03 or
                    (pd.notna(prev_row['Volume_Ratio_10']) and prev_row['Volume_Ratio_10'] >= 1.8)
                )
            )
            ma25_volume_breakout_alt_ready = (
                stock_code == '09866' and
                pd.notna(row['MA25']) and row['Close'] > row['MA25'] and
                pd.notna(row['Distance_to_MA25']) and 0.03 <= row['Distance_to_MA25'] <= 0.08 and
                pd.notna(row['MA25_Slope']) and row['MA25_Slope'] > 0.02 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.2 and
                pd.notna(row['RSI']) and 55 <= row['RSI'] <= 63 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > 0.15 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.08 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -0.40
            )
            ma25_trend_follow_ready = (
                stock_code == '09866' and
                pd.notna(row['MA25']) and row['Close'] > row['MA25'] and
                pd.notna(row['Distance_to_MA25']) and 0.03 <= row['Distance_to_MA25'] <= 0.07 and
                pd.notna(row['MA25_Slope']) and row['MA25_Slope'] > 0.03 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.15 and
                pd.notna(row['RSI']) and 58 <= row['RSI'] <= 63 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > 0.20 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.06 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -0.40
            )

            bottom_volume_rebound_ready = (
                stock_code == '09866' and
                prev_prev_row is not None and
                pd.notna(prev_row['Volume_Ratio_10']) and prev_row['Volume_Ratio_10'] >= 1.8 and
                pd.notna(prev_row['Distance_to_MA25']) and prev_row['Distance_to_MA25'] <= -0.03 and
                pd.notna(prev_row['RSI']) and prev_row['RSI'] <= 45 and
                pd.notna(row['Close']) and pd.notna(prev_row['Close']) and row['Close'] > prev_row['Close'] and
                pd.notna(row['Open']) and row['Close'] >= row['Open'] and
                pd.notna(row['RSI']) and 40 <= row['RSI'] <= 55 and
                pd.notna(row['Distance_to_MA25']) and row['Distance_to_MA25'] >= -0.10 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -1.60 and
                pd.notna(row['Matrix_Buy_Score']) and row['Matrix_Buy_Score'] >= 15 and
                pd.notna(row['expected_3m_score']) and row['expected_3m_score'] >= 34
            )

            bottom_volume_cluster_02015_ready = (
                stock_code == '02015' and
                i >= 20 and
                pd.notna(row['MA20']) and pd.notna(row['MA25']) and
                pd.notna(row['Volume_Spike_10d']) and row['Volume_Spike_10d'] >= 2.5 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.55 and
                pd.notna(row['RSI']) and 27 <= row['RSI'] <= 33 and
                pd.notna(row['Distance_to_MA25']) and row['Distance_to_MA25'] <= -0.18 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -3.2 and
                pd.notna(row['Return_20d']) and row['Return_20d'] <= -0.22
            )

            bottom_volume_rebound_02015_ready = (
                stock_code == '02015' and
                prev_row is not None and
                pd.notna(prev_row['Volume_Spike_10d']) and prev_row['Volume_Spike_10d'] >= 2.5 and
                pd.notna(prev_row['RSI']) and prev_row['RSI'] <= 31 and
                pd.notna(prev_row['Distance_to_MA25']) and prev_row['Distance_to_MA25'] <= -0.18 and
                pd.notna(row['Close']) and pd.notna(prev_row['Close']) and row['Close'] >= prev_row['Close'] * 1.003 and
                pd.notna(row['Low']) and pd.notna(prev_row['Low']) and row['Low'] >= prev_row['Low'] * 0.98 and
                pd.notna(row['Open']) and row['Close'] >= row['Open'] and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.15 and
                pd.notna(row['RSI']) and 29 <= row['RSI'] <= 37 and
                pd.notna(row['Distance_to_MA25']) and row['Distance_to_MA25'] <= -0.14 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -3.2
            )

            stoch_wbottom_02015_ready = (
                stock_code == '02015' and
                i >= 20 and
                pd.notna(row['StochRSI_K']) and pd.notna(row['StochRSI_D']) and
                pd.notna(prev_row['StochRSI_K']) and pd.notna(prev_row['StochRSI_D']) and
                row['StochRSI_K'] > row['StochRSI_D'] and
                row['StochRSI_K'] > 15 and row['StochRSI_K'] < 65 and
                prev_row['StochRSI_K'] <= row['StochRSI_K'] and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.0 and
                pd.notna(row['Volume_Spike_10d']) and row['Volume_Spike_10d'] >= 1.35 and
                pd.notna(row['RSI']) and 44 <= row['RSI'] <= 52 and
                pd.notna(row['Distance_to_MA25']) and -0.05 <= row['Distance_to_MA25'] <= 0.01 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > 0.20 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.14 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -0.95 and
                row['Close'] > data['Low'].iloc[max(0, i-20):i].min() * 1.03
            )

            stoch_mild_volume_02015_ready = (
                stock_code == '02015' and
                pd.notna(row['StochRSI_K']) and pd.notna(row['StochRSI_D']) and
                pd.notna(prev_row['StochRSI_K']) and pd.notna(prev_row['StochRSI_D']) and
                row['StochRSI_K'] > row['StochRSI_D'] and
                row['StochRSI_K'] >= 55 and row['StochRSI_K'] <= 90 and
                prev_row['StochRSI_K'] <= prev_row['StochRSI_D'] + 8 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.05 and row['Volume_Ratio_10'] <= 1.45 and
                pd.notna(row['Volume_Spike_10d']) and row['Volume_Spike_10d'] >= 1.6 and
                pd.notna(row['RSI']) and 52 <= row['RSI'] <= 58 and
                pd.notna(row['Distance_to_MA25']) and 0.0 <= row['Distance_to_MA25'] <= 0.05 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.04 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > -0.16 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > 0.25
            )

            cyc_bull_expansion_02015_ready = (
                stock_code == '02015' and
                pd.notna(row['CYC5']) and pd.notna(row['CYC13']) and pd.notna(row['CYC34']) and
                pd.notna(prev_row['CYC5']) and pd.notna(prev_row['CYC13']) and pd.notna(prev_row['CYC34']) and
                row['CYC5'] > row['CYC13'] > row['CYC34'] and
                prev_row['CYC5'] > prev_row['CYC13'] > prev_row['CYC34'] and
                pd.notna(row['CYC_Spread_34']) and row['CYC_Spread_34'] >= 0.07 and row['CYC_Spread_34'] <= 0.16 and
                pd.notna(prev_row['CYC_Spread_34']) and row['CYC_Spread_34'] > prev_row['CYC_Spread_34'] and
                pd.notna(row['CYC_Spread_13']) and row['CYC_Spread_13'] >= 0.025 and
                pd.notna(row['CYC34_Slope']) and row['CYC34_Slope'] > 0 and
                pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 1.1 and row['Volume_Ratio_10'] <= 1.8 and
                pd.notna(row['Volume_Spike_10d']) and row['Volume_Spike_10d'] >= 1.6 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > 0.45 and
                pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.02 and
                pd.notna(row['Distance_to_MA25']) and row['Distance_to_MA25'] >= 0.10 and row['Distance_to_MA25'] <= 0.17 and
                pd.notna(row['Upper_Shadow_Ratio']) and row['Upper_Shadow_Ratio'] <= 0.12 and
                pd.notna(row['RSI']) and row['RSI'] >= 60 and row['RSI'] <= 66 and
                pd.notna(row['Return_60d']) and row['Return_60d'] > 0.18 and row['Return_60d'] <= 0.28
            )

            stoch_pullback_ready = (
                pd.notna(row['StochRSI_K']) and pd.notna(row['StochRSI_D']) and
                pd.notna(row['MA20']) and pd.notna(row['MA60']) and pd.notna(row['MA25']) and
                pd.notna(row['RSI']) and pd.notna(row['Matrix_Buy_Score']) and pd.notna(row['expected_3m_score']) and
                stoch_bullish_cross_ready and
                row['Close'] >= row['MA20'] * 0.97 and
                row['Close'] <= row['MA25'] * 1.02 and
                row['MA20'] >= row['MA60'] * 0.95 and
                row['RSI'] >= 43 and row['RSI'] <= 58 and
                row['Return_60d'] > -0.35 and
                row['Distance_to_20d_High'] >= -0.12 and
                row['MACD_Gap'] > -0.35 and
                row['Volume_Spike_10d'] >= 1.75 and
                row['Matrix_Buy_Score'] >= 45 and
                row['expected_3m_score'] >= 58
            )

            trend_reversal_ready = (
                pd.notna(row['MA20']) and pd.notna(row['MA25']) and pd.notna(row['MA60']) and
                pd.notna(prev_row['MA20']) and pd.notna(prev_row['MA25']) and pd.notna(prev_row['MA60']) and
                pd.notna(row['RSI']) and pd.notna(row['StochRSI_K']) and pd.notna(row['StochRSI_D']) and
                pd.notna(row['Matrix_Buy_Score']) and pd.notna(row['expected_3m_score']) and
                pd.notna(row['Distance_to_MA20']) and pd.notna(row['Distance_to_MA25']) and
                pd.notna(row['Distance_to_20d_High']) and pd.notna(row['MACD_Gap']) and
                row['Close'] >= row['MA20'] * 0.97 and
                row['Close'] >= row['MA25'] * 0.95 and
                row['Distance_to_MA20'] >= -0.04 and row['Distance_to_MA20'] <= 0.04 and
                row['Distance_to_MA25'] >= -0.06 and row['Distance_to_MA25'] <= 0.05 and
                row['RSI'] >= 44 and row['RSI'] <= 60 and
                row['StochRSI_K'] > row['StochRSI_D'] and
                row['StochRSI_K'] >= 20 and row['StochRSI_K'] <= 75 and
                row['MACD_Gap'] > -0.10 and
                row['Distance_to_20d_High'] >= -0.18 and
                row['Return_20d'] > -0.16 and
                row['Return_60d'] > -0.30 and
                row['Matrix_Buy_Score'] >= 42 and
                row['expected_3m_score'] >= 56 and
                (
                    stoch_bullish_cross_ready or
                    (pd.notna(prev_row['MACD_Gap']) and row['MACD_Gap'] > prev_row['MACD_Gap']) or
                    row['Close'] > prev_row['Close']
                ) and
                (
                    (pd.notna(prev_row['Distance_to_MA20']) and prev_row['Distance_to_MA20'] <= -0.02) or
                    (pd.notna(prev_row['Return_20d']) and prev_row['Return_20d'] <= -0.05) or
                    (pd.notna(prev_row['MACD_Gap']) and prev_row['MACD_Gap'] <= 0)
                )
            )

            trend_acceleration_ready = (
                pd.notna(row['MA20']) and pd.notna(row['MA25']) and pd.notna(row['MA60']) and
                pd.notna(row['MA20_Slope']) and pd.notna(row['MA25_Slope']) and pd.notna(row['MA60_Slope']) and
                pd.notna(row['RSI']) and pd.notna(row['Distance_to_MA20']) and pd.notna(row['Distance_to_20d_High']) and
                pd.notna(row['Volume_Ratio_10']) and pd.notna(row['Volume_Spike_10d']) and pd.notna(row['MACD_Gap']) and
                pd.notna(row['expected_3m_score']) and pd.notna(row['Matrix_Buy_Score']) and
                row['MA20'] >= row['MA60'] * 0.98 and
                row['MA20_Slope'] > 0 and row['MA25_Slope'] > -0.005 and row['MA60_Slope'] > -0.01 and
                row['Close'] >= row['MA20'] and
                row['Distance_to_MA20'] >= -0.01 and row['Distance_to_MA20'] <= 0.10 and
                row['Distance_to_20d_High'] >= -0.06 and
                row['RSI'] >= 50 and row['RSI'] <= 72 and
                row['Return_20d'] > 0 and
                row['Return_60d'] > -0.10 and
                row['Volume_Ratio_10'] >= 0.95 and row['Volume_Ratio_10'] <= 2.6 and
                row['Volume_Spike_10d'] >= 1.2 and
                row['MACD_Gap'] > -0.05 and
                row['Matrix_Buy_Score'] >= 50 and
                row['expected_3m_score'] >= 62 and
                row['Close'] >= prev_row['Close']
            )

            early_reversal_ready = (
                pd.notna(row['RSI']) and pd.notna(row['MACD_Gap']) and pd.notna(row['Distance_to_20d_High']) and
                pd.notna(row['Volume_Ratio_10']) and pd.notna(row['Upper_Shadow_Ratio']) and pd.notna(row['Intraday_Return']) and
                row['RSI'] >= 38 and row['RSI'] <= 52 and
                row['MACD_Gap'] > -0.20 and
                row['Distance_to_20d_High'] >= -0.20 and row['Distance_to_20d_High'] <= -0.04 and
                row['Volume_Ratio_10'] >= 0.45 and row['Volume_Ratio_10'] <= 1.8 and
                row['Upper_Shadow_Ratio'] <= 0.35 and
                row['Intraday_Return'] >= -0.01 and
                pd.notna(row['Matrix_Buy_Score']) and row['Matrix_Buy_Score'] >= 38 and
                pd.notna(row['expected_3m_score']) and row['expected_3m_score'] >= 52 and
                (
                    stoch_bullish_cross_ready or
                    (pd.notna(prev_row['MACD_Gap']) and row['MACD_Gap'] > prev_row['MACD_Gap']) or
                    row['Close'] > prev_row['Close']
                )
            )

            ipo_rebound_ready = (
                pd.isna(row['MA60']) and
                pd.notna(row['MA10']) and pd.notna(row['MA20']) and
                pd.notna(row['RSI']) and pd.notna(row['Volume_Ratio_10']) and pd.notna(row['Distance_to_20d_High']) and
                pd.notna(row['Matrix_Buy_Score']) and pd.notna(row['expected_3m_score']) and
                row['Close'] >= row['MA10'] * 0.96 and
                row['Close'] >= row['MA20'] * 0.94 and
                row['RSI'] >= 42 and row['RSI'] <= 68 and
                row['Distance_to_20d_High'] >= -0.20 and
                row['Volume_Ratio_10'] >= 0.50 and row['Volume_Ratio_10'] <= 2.4 and
                row['Matrix_Buy_Score'] >= 45 and
                row['expected_3m_score'] >= 58 and
                (
                    (pd.notna(prev_row['Close']) and row['Close'] > prev_row['Close']) or
                    (pd.notna(row['Intraday_Return']) and row['Intraday_Return'] > 0) or
                    (pd.notna(row['StochRSI_K']) and pd.notna(row['StochRSI_D']) and row['StochRSI_K'] > row['StochRSI_D'])
                )
            )

            launch_breakout_ready = (
                pd.isna(row['MA60']) and
                pd.notna(row['MA10']) and pd.notna(row['MA20']) and
                pd.notna(prev_row['MA10']) and pd.notna(prev_row['MA20']) and
                pd.notna(row['RSI']) and pd.notna(row['Volume_Ratio_10']) and pd.notna(row['Distance_to_20d_High']) and
                pd.notna(row['Matrix_Buy_Score']) and pd.notna(row['expected_3m_score']) and
                row['Close'] >= row['MA10'] and
                row['Close'] >= row['MA20'] * 0.98 and
                row['MA10'] >= prev_row['MA10'] * 0.995 and
                row['MA20'] >= prev_row['MA20'] * 0.995 and
                row['RSI'] >= 52 and row['RSI'] <= 78 and
                row['Distance_to_20d_High'] >= -0.10 and
                row['Volume_Ratio_10'] >= 0.75 and row['Volume_Ratio_10'] <= 2.8 and
                row['Matrix_Buy_Score'] >= 46 and
                row['expected_3m_score'] >= 60 and
                pd.notna(row['Price_Change_3d']) and row['Price_Change_3d'] >= 0.03 and
                pd.notna(row['Intraday_Return']) and row['Intraday_Return'] >= 0 and
                (
                    row['Close'] > prev_row['Close'] or
                    (pd.notna(row['StochRSI_K']) and row['StochRSI_K'] >= row['StochRSI_D'])
                )
            )

            # ============ 模式A: 趋势回踩买点 ============
            pullback_ready = (
                trend_positive and
                pd.notna(row['Distance_to_MA20']) and -0.06 <= row['Distance_to_MA20'] <= 0.02 and
                pd.notna(row['RSI']) and 40 <= row['RSI'] <= 58 and
                pd.notna(row['Return_20d']) and row['Return_20d'] > -0.08 and
                pd.notna(row['Volume_Trend_Ratio']) and row['Volume_Trend_Ratio'] <= 1.15 and
                pd.notna(row['MACD_Gap']) and row['MACD_Gap'] > -0.03 and
                row['Close'] >= prev_row['Close'] and
                pd.notna(row['MA5']) and row['Close'] >= row['MA5'] * 0.99
            )

            if ma25_volume_breakout_ready:
                signal_mode = 'ma25_volume_breakout'
                entry_type = 'ma25_volume_breakout'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 7
                has_entry_confirmation = True
                reasons.extend(['站上MA25', '10日内出现倍量', '量价同步转强'])
                risk_filters.extend(['均线突破确认', '放量结构明显', '过滤长上影与弱趋势'])

                if trend_positive:
                    signal_strength += 1
                    reasons.append('中期趋势配合')

                if pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] > -0.08:
                    signal_strength += 1
                    reasons.append('接近20日高点')

            elif momentum_reversal_breakout_ready:
                signal_mode = 'momentum_reversal_breakout'
                entry_type = 'momentum_reversal_breakout'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 7
                has_entry_confirmation = True
                reasons.extend(['MA25放量突破', '短线动量快速修复', 'MACD明显转强'])
                risk_filters.extend(['允许中期趋势仍在修复', '要求放量和中阳确认'])

            elif ma25_support_reclaim_ready:
                signal_mode = 'ma25_support_reclaim'
                entry_type = 'ma25_support_reclaim'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['股价重新站稳MA25', 'MA25附近回升', '贴近中期均线的右侧修复'])
                risk_filters.extend(['09866专属增强模式', '要求价均线关系改善'])

            elif ma25_volume_breakout_alt_ready:
                signal_mode = 'ma25_volume_breakout_alt'
                entry_type = 'ma25_volume_breakout_alt'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['股价高于MA25', '量能温和放大', '25日均线上方延续'])
                risk_filters.extend(['09866专属增强模式', '适用于沿MA25上行的延续买点'])

            elif ma25_trend_follow_ready:
                signal_mode = 'ma25_trend_follow'
                entry_type = 'ma25_trend_follow'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['沿MA25上行', '量能恢复', '顺势延续突破'])
                risk_filters.extend(['09866专属增强模式', '适用于MA25趋势跟随'])

            elif bottom_volume_rebound_ready:
                signal_mode = 'bottom_volume_rebound'
                entry_type = 'bottom_volume_rebound'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['阶段性底部放倍量', '次日股价回升', '底部修复确认'])
                risk_filters.extend(['09866专属增强模式', '仅用于阶段性底部放量反转'])

            elif bottom_volume_cluster_02015_ready:
                signal_mode = 'bottom_volume_cluster_02015'
                entry_type = 'bottom_volume_cluster_02015'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['阶段性底部放量', '单日放倍量恐慌释放', '中期修复空间较大'])
                risk_filters.extend(['02015专属增强模式', '允许MA60尚未形成但要求极端放量与低位'])

            elif bottom_volume_rebound_02015_ready:
                signal_mode = 'bottom_volume_rebound_02015'
                entry_type = 'bottom_volume_rebound_02015'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['阶段性底部放量后回升', '低位止跌修复', '次日价格确认'])
                risk_filters.extend(['02015专属增强模式', '用于底部放量后的右侧确认'])

            elif stoch_wbottom_02015_ready:
                signal_mode = 'stoch_wbottom_02015'
                entry_type = 'stoch_wbottom_02015'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['StochRSI重新转强', '近20日形成W底', '当前价格高于前低'])
                risk_filters.extend(['02015专属增强模式', '要求中期趋势未明显破坏'])

            elif stoch_mild_volume_02015_ready:
                signal_mode = 'stoch_mild_volume_02015'
                entry_type = 'stoch_mild_volume_02015'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['StochRSI强势金叉区间', '近期温和放量', '价格重新贴近阶段高点'])
                risk_filters.extend(['02015专属增强模式', '用于温和放量的中继买点'])

            elif cyc_bull_expansion_02015_ready:
                signal_mode = 'cyc_bull_expansion_02015'
                entry_type = 'cyc_bull_expansion_02015'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 7
                has_entry_confirmation = True
                reasons.extend(['CYC5/CYC13/CYC34多头排列', '短中周期由汇聚转向发散', '量价同步上攻'])
                risk_filters.extend(['02015专属增强模式', '仅用于多头汇聚后首次强发散'])

            elif stoch_pullback_ready:
                signal_mode = 'stoch_pullback'
                entry_type = 'stoch_pullback'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['StochRSI金叉', '回踩后重新转强', '中期结构未失效'])
                risk_filters.extend(['需要动量修复确认', '允许弱趋势中的右侧修复'])

                if pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] > -0.10:
                    signal_strength += 1
                    reasons.append('接近20日高点区域')

            elif trend_reversal_ready:
                signal_mode = 'trend_reversal'
                entry_type = 'trend_reversal'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                holding_horizon = 80
                min_holding_bars_for_trend_exit = 8
                trailing_stop_pct = 0.90
                trailing_activation_gain = 0.08
                has_entry_confirmation = True
                reasons.extend(['回调后趋势重新转强', '均线附近修复完成', '动量拐点出现'])
                risk_filters.extend(['优先用于非09866股票', '允许趋势修复初段'])

                if stoch_bullish_cross_ready:
                    signal_strength += 1
                    reasons.append('StochRSI金叉确认')

            elif trend_acceleration_ready:
                signal_mode = 'trend_acceleration'
                entry_type = 'trend_acceleration'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                holding_horizon = 90
                min_holding_bars_for_trend_exit = 10
                trailing_stop_pct = 0.88
                trailing_activation_gain = 0.10
                has_entry_confirmation = True
                reasons.extend(['中期趋势抬升', '靠近阶段高点继续加速', '量价结构支持延续'])
                risk_filters.extend(['偏趋势延续型', '用于争取更长持有区间'])

                if pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.03:
                    signal_strength += 1
                    reasons.append('贴近20日高点')

            elif early_reversal_ready:
                signal_mode = 'early_reversal'
                entry_type = 'early_reversal'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                holding_horizon = 75
                min_holding_bars_for_trend_exit = 6
                trailing_stop_pct = 0.91
                trailing_activation_gain = 0.08
                has_entry_confirmation = True
                reasons.extend(['下跌后出现早期修复', '动量边际改善', '价格开始止跌'])
                risk_filters.extend(['用于弱趋势修复初段', '要求风险分数受控'])

                if pd.notna(row['Trend_Regime_Score']) and row['Trend_Regime_Score'] >= 35:
                    signal_strength += 1
                    reasons.append('趋势环境不差')

            elif ipo_rebound_ready:
                signal_mode = 'ipo_rebound'
                entry_type = 'ipo_rebound'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                holding_horizon = 90
                min_holding_bars_for_trend_exit = 12
                trailing_stop_pct = 0.88
                trailing_activation_gain = 0.12
                has_entry_confirmation = True
                reasons.extend(['上市初期趋势回升', 'MA60未形成但短中期结构转好', '适合新股主升段'])
                risk_filters.extend(['仅用于MA60尚未形成阶段', '强调右侧确认'])

            elif launch_breakout_ready:
                signal_mode = 'launch_breakout'
                entry_type = 'launch_breakout'
                signal_tier = 'strong'
                actionable = True
                signal_strength = 6
                holding_horizon = 100
                min_holding_bars_for_trend_exit = 15
                trailing_stop_pct = 0.86
                trailing_activation_gain = 0.15
                has_entry_confirmation = True
                reasons.extend(['上市初期加速突破', '短中期均线同步抬升', '争取新股主升浪'])
                risk_filters.extend(['仅用于MA60未形成阶段', '要求价格与动量继续抬升'])

            elif ma25_volume_breakout_watch_ready:
                signal_mode = 'ma25_volume_breakout_watch'
                entry_type = 'ma25_volume_breakout_watch'
                signal_tier = 'weak'
                actionable = False
                signal_strength = 5
                has_entry_confirmation = True
                max_risk_score = 5
                reasons.extend(['站上MA25', '10日内放量接近倍量', '量价转强但确认度偏弱'])
                risk_filters.extend(['仅加入观察名单', '等待趋势进一步确认'])

                if trend_positive:
                    signal_strength += 1
                    reasons.append('中期趋势初步配合')

                if pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] > -0.12:
                    signal_strength += 1
                    reasons.append('接近20日高点区域')

            elif pullback_ready:
                signal_mode = 'trend_pullback'
                entry_type = 'trend_pullback'
                signal_strength = 6
                has_entry_confirmation = True
                reasons.extend(['趋势向上', '回踩均线', '回踩后转强'])
                risk_filters.extend(['中期趋势完整', '量能未失控'])

                if pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] > -0.08:
                    signal_strength += 1
                    reasons.append('接近20日高点')

                if pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] >= 0.9:
                    signal_strength += 1
                    reasons.append('量能恢复')

            # ============ 模式B: 平台突破买点 ============
            elif trend_positive:
                overextended_breakout_02015 = (
                    stock_code == '02015' and
                    pd.notna(row['RSI']) and row['RSI'] >= 64 and
                    pd.notna(row['StochRSI_K']) and row['StochRSI_K'] >= 95 and
                    pd.notna(row['Distance_to_MA25']) and row['Distance_to_MA25'] >= 0.075 and
                    pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.025 and
                    pd.notna(row['Upper_Shadow_Ratio']) and row['Upper_Shadow_Ratio'] >= 0.35
                )

                breakout_ready = (
                    pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] >= -0.02 and
                    pd.notna(row['Distance_to_60d_High']) and row['Distance_to_60d_High'] >= -0.03 and
                    pd.notna(row['Volume_Ratio_10']) and 1.2 <= row['Volume_Ratio_10'] <= 2.4 and
                    pd.notna(row['MACD']) and pd.notna(row['Signal']) and row['MACD'] > row['Signal'] and
                    pd.notna(row['MA5']) and pd.notna(row['MA10']) and row['MA5'] >= row['MA10'] and
                    pd.notna(row['RSI']) and 50 <= row['RSI'] <= 70 and
                    pd.notna(row['Distance_to_MA20']) and 0.00 <= row['Distance_to_MA20'] <= 0.13 and
                    pd.notna(row['Return_60d']) and row['Return_60d'] <= 0.30 and
                    row['Close'] >= prev_row['Close'] and
                    not overextended_breakout_02015
                )

                if breakout_ready:
                    signal_mode = 'base_breakout'
                    entry_type = 'base_breakout'
                    signal_strength = 7
                    has_entry_confirmation = True
                    reasons.extend(['平台突破', '趋势延续', '放量确认'])
                    risk_filters.extend(['中期趋势完整', '放量但不过热'])

                    if pd.notna(row['Return_20d']) and row['Return_20d'] > 0.05:
                        signal_strength += 1
                        reasons.append('20日动量增强')

            # ============ 模式C: 超卖反转买点（补充） ============
            elif row['StochRSI_K'] < 20:
                oversold_ready = (
                    pd.notna(row['RSI']) and 28 <= row['RSI'] <= 40 and
                    pd.notna(row['CYC']) and row['CYC'] <= 18 and
                    pd.notna(row['BB_Lower']) and row['Close'] >= row['BB_Lower'] and
                    pd.notna(row['Volume_Ratio_10']) and 0.75 <= row['Volume_Ratio_10'] <= 1.00 and
                    pd.notna(row['Distance_to_20d_High']) and row['Distance_to_20d_High'] <= -0.12 and
                    pd.notna(row['Distance_to_MA20']) and row['Distance_to_MA20'] <= -0.04 and
                    pd.notna(row['MA20']) and pd.notna(row['MA60']) and row['MA20'] >= row['MA60'] * 0.97
                )

                if not oversold_ready:
                    continue

                signal_mode = 'oversold_reversal'
                entry_type = 'oversold_reversal'
                signal_strength = 4
                holding_horizon = 40
                max_risk_score = 2
                reasons.append('超卖反转')

                if pd.notna(prev_row['StochRSI_K']) and row['StochRSI_K'] > prev_row['StochRSI_K']:
                    signal_strength += 1
                    has_entry_confirmation = True
                    reasons.append('StochRSI回升')

                if (pd.notna(prev_row['StochRSI_K']) and pd.notna(prev_row['StochRSI_D']) and
                        prev_row['StochRSI_K'] <= prev_row['StochRSI_D'] and row['StochRSI_K'] > row['StochRSI_D']):
                    signal_strength += 1
                    has_entry_confirmation = True
                    reasons.append('StochRSI金叉')

                risk_filters.extend(['补充型左侧策略', '仅用于低位修复'])
            else:
                continue

            if stoch_bullish_cross_ready:
                signal_strength += 2
                has_entry_confirmation = True
                reasons.append('StochRSI金叉且K位于15-50')
                if signal_mode == 'ma25_volume_breakout_watch' and trend_positive:
                    reasons.append('短线动能确认增强')

            required_matrix_score = max(matrix_gate, 35)
            required_expected_score = max(expected_gate, 52)
            if signal_mode == 'stoch_pullback':
                required_matrix_score = max(matrix_gate - 18, 45)
                required_expected_score = max(expected_gate - 2, 56)
            elif signal_mode == 'momentum_reversal_breakout':
                required_matrix_score = max(matrix_gate - 10, 48)
                required_expected_score = max(expected_gate, 58)
            elif signal_mode == 'ma25_support_reclaim':
                required_matrix_score = max(matrix_gate - 32, 34)
                required_expected_score = max(expected_gate - 8, 54)
            elif signal_mode == 'ma25_volume_breakout_alt':
                required_matrix_score = max(matrix_gate - 32, 34)
                required_expected_score = max(expected_gate - 6, 52)
            elif signal_mode == 'ma25_trend_follow':
                required_matrix_score = max(matrix_gate - 32, 38)
                required_expected_score = max(expected_gate - 8, 54)
            elif signal_mode == 'bottom_volume_rebound':
                required_matrix_score = max(matrix_gate - 30, 32)
                required_expected_score = max(expected_gate - 6, 50)
            elif signal_mode == 'bottom_volume_cluster_02015':
                required_matrix_score = 30
                required_expected_score = 18
            elif signal_mode == 'bottom_volume_rebound_02015':
                required_matrix_score = 30
                required_expected_score = 18
            elif signal_mode == 'stoch_wbottom_02015':
                required_matrix_score = max(matrix_gate - 12, 50)
                required_expected_score = max(expected_gate - 2, 58)
            elif signal_mode == 'cyc_bull_expansion_02015':
                required_matrix_score = max(matrix_gate - 6, 60)
                required_expected_score = max(expected_gate - 4, 60)
            elif signal_mode == 'stoch_mild_volume_02015':
                required_matrix_score = max(matrix_gate - 8, 58)
                required_expected_score = max(expected_gate - 2, 66)
            elif signal_mode == 'trend_reversal':
                required_matrix_score = max(matrix_gate - 10, 42)
                required_expected_score = max(expected_gate - 4, 56)
            elif signal_mode == 'trend_acceleration':
                required_matrix_score = max(matrix_gate - 8, 48)
                required_expected_score = max(expected_gate, 62)
            elif signal_mode == 'early_reversal':
                required_matrix_score = max(matrix_gate - 12, 40)
                required_expected_score = max(expected_gate - 6, 54)
            elif signal_mode == 'ipo_rebound':
                required_matrix_score = 45
                required_expected_score = 58
            elif signal_mode == 'launch_breakout':
                required_matrix_score = 46
                required_expected_score = 60

            if pd.isna(matrix_score) or matrix_score < required_matrix_score:
                continue
            if pd.isna(expected_3m_score) or expected_3m_score < required_expected_score:
                continue

            signal_strength += 1
            reasons.append('3个月预期收益评分达标')

            risk_score = 0
            if pd.notna(row['Volatility_10d']) and pd.notna(vol_ma.iloc[i]) and row['Volatility_10d'] > vol_ma.iloc[i] * 1.2:
                risk_score += 1
            if pd.notna(row['ATR']) and pd.notna(atr_90th.iloc[i]) and row['ATR'] > atr_90th.iloc[i]:
                risk_score += 2
            if signal_mode == 'base_breakout' and pd.notna(row['Volume_Ratio_10']) and row['Volume_Ratio_10'] > 2.5:
                risk_score += 1
            if signal_mode == 'ma25_volume_breakout' and pd.notna(row['Distance_to_MA25']) and row['Distance_to_MA25'] > 0.08:
                risk_score += 1
            if signal_mode == 'ma25_volume_breakout' and pd.notna(row['Upper_Shadow_Ratio']) and row['Upper_Shadow_Ratio'] > 0.45:
                risk_score += 1
            if signal_mode == 'oversold_reversal' and pd.notna(row['Return_20d']) and row['Return_20d'] < -0.15:
                risk_score += 1

            if not has_entry_confirmation:
                continue

            if signal_strength >= 6 and risk_score <= max_risk_score:
                stop_loss_price = row['Close'] - row['ATR'] * 2 if pd.notna(row['ATR']) else row['Close'] * 0.92
                signals.append({
                    'date': row.name,
                    'close': row['Close'],
                    'signal_strength': signal_strength,
                    'risk_score': risk_score,
                    'signal_mode': signal_mode,
                    'entry_type': entry_type,
                    'signal_tier': signal_tier,
                    'actionable': actionable,
                    'matrix_score': matrix_score,
                    'matrix_gate': matrix_gate,
                    'expected_3m_score': expected_3m_score,
                    'holding_horizon': holding_horizon,
                    'min_holding_bars_for_trend_exit': min_holding_bars_for_trend_exit,
                    'trailing_stop_pct': trailing_stop_pct,
                    'trailing_activation_gain': trailing_activation_gain,
                    'stop_loss_price': stop_loss_price,
                    'reasons': reasons,
                    'risk_filters': risk_filters,
                    'stochrsi_k': row['StochRSI_K'],
                    'stochrsi_d': row['StochRSI_D'],
                    'rsi': row['RSI'],
                    'volatility': row['Volatility_10d'],
                    'cyc': row['CYC'],
                    'atr': row['ATR'],
                    'price_change_3d': row['Price_Change_3d'],
                    'volume_ratio': row['Volume'] / row['Volume_MA10'] if pd.notna(row['Volume_MA10']) else 1,
                    'regime_score': row.get('Trend_Regime_Score'),
                    'forward_return_20': row.get('forward_return_20'),
                    'forward_return_40': row.get('forward_return_40'),
                    'forward_return_60': row.get('forward_return_60'),
                    'forward_max_drawdown_60': row.get('forward_max_drawdown_60')
                })

        return pd.DataFrame(signals) if signals else None

    def identify_sell_signals(self, data):
        """
        识别策略性卖出信号 - 仅保留高位放量阴线

        Args:
            data (DataFrame): 包含技术指标的数据

        Returns:
            DataFrame: 卖出信号
        """
        if data is None or data.empty:
            return None

        signals = []

        for i in range(len(data)):
            row = data.iloc[i]

            bearish_candle = (
                (pd.notna(row.get("Open")) and pd.notna(row.get("Close")) and row["Close"] < row["Open"]) or
                (pd.notna(row.get("Intraday_Return")) and row["Intraday_Return"] < 0)
            )
            if not bearish_candle:
                continue

            high_conditions = []
            if pd.notna(row.get("Distance_to_20d_High")) and row["Distance_to_20d_High"] >= -0.03:
                high_conditions.append("接近20日高点")
            if pd.notna(row.get("Distance_to_60d_High")) and row["Distance_to_60d_High"] >= -0.05:
                high_conditions.append("接近60日高点")
            if pd.notna(row.get("BB_Position")) and row["BB_Position"] >= 0.78:
                high_conditions.append("位于布林带上沿")
            if pd.notna(row.get("Return_60d")) and row["Return_60d"] >= 0.18:
                high_conditions.append("60日涨幅较大")
            if not high_conditions:
                continue

            volume_conditions = []
            if pd.notna(row.get("Volume_Ratio_10")) and row["Volume_Ratio_10"] >= 1.35:
                volume_conditions.append("相对10日均量明显放大")
            if pd.notna(row.get("Volume_Spike_10d")) and row["Volume_Spike_10d"] >= 1.6:
                volume_conditions.append("近期出现放量峰值")
            if not volume_conditions:
                continue

            signal_strength = 5
            reasons = list(dict.fromkeys(high_conditions + volume_conditions + ["高位出现放量阴线"]))

            if pd.notna(row.get("Upper_Shadow_Ratio")) and row["Upper_Shadow_Ratio"] >= 0.28:
                signal_strength += 1
                reasons.append("长上影显示抛压")

            if pd.notna(row.get("Volume_Ratio_10")) and row["Volume_Ratio_10"] >= 1.8:
                signal_strength += 1
                reasons.append("当日放量进一步增强")

            if (
                (pd.notna(row.get("Distance_to_20d_High")) and row["Distance_to_20d_High"] >= -0.01) or
                (pd.notna(row.get("BB_Position")) and row["BB_Position"] >= 0.92)
            ):
                signal_strength += 1
                reasons.append("位置接近阶段极值")

            signals.append({
                "date": row.name,
                "close": row["Close"],
                "signal_strength": signal_strength,
                "signal_type": "high_volume_bearish_at_high",
                "reasons": reasons,
                "volume_ratio": row.get("Volume_Ratio_10"),
                "bb_position": row.get("BB_Position"),
                "return_60d": row.get("Return_60d"),
                "upper_shadow_ratio": row.get("Upper_Shadow_Ratio")
            })

        return pd.DataFrame(signals) if signals else None

    def merge_buy_signal_zones(self, buy_signals, stock_code=None):
        """合并连续出现的区域型买点，避免把同一底部结构重复计为多次独立机会。"""
        if buy_signals is None or buy_signals.empty:
            return buy_signals

        merged_signals = buy_signals.sort_values('date').reset_index(drop=True).copy()
        if stock_code != '02015':
            return merged_signals

        zone_modes = {'bottom_volume_cluster_02015', 'bottom_volume_rebound_02015'}
        collapsed = []
        i = 0

        while i < len(merged_signals):
            current = merged_signals.iloc[i].copy()
            current_mode = current.get('signal_mode')

            if current_mode not in zone_modes:
                current['zone_type'] = None
                current['zone_start_date'] = current['date']
                current['zone_end_date'] = current['date']
                current['merged_signal_count'] = 1
                current['merged_from_dates'] = [current['date']]
                collapsed.append(current)
                i += 1
                continue

            merged_dates = [current['date']]
            j = i + 1
            while j < len(merged_signals):
                candidate = merged_signals.iloc[j]
                candidate_mode = candidate.get('signal_mode')
                if candidate_mode not in zone_modes:
                    break
                if (candidate['date'] - merged_dates[-1]).days > 4:
                    break
                merged_dates.append(candidate['date'])
                j += 1

            current['signal_mode'] = 'bottom_volume_zone_02015'
            current['entry_type'] = 'bottom_volume_zone_02015'
            current['zone_type'] = 'bottom_volume_02015'
            current['zone_start_date'] = merged_dates[0]
            current['zone_end_date'] = merged_dates[-1]
            current['merged_signal_count'] = len(merged_dates)
            current['merged_from_dates'] = merged_dates
            current['reasons'] = list(dict.fromkeys(list(current.get('reasons', [])) + [f'合并同一区域内{len(merged_dates)}个连续底部信号']))
            collapsed.append(current)
            i = j

        return pd.DataFrame(collapsed)
