import pandas as pd

from .base import BuyStrategy, build_buy_signal, is_bullish_trend


class BullishTrend2560BuyStrategy(BuyStrategy):
    """2560 顺势量价策略：站上 MA25、MA25 上行、MA25 > MA60，量能近期转强或持续强势。"""

    def identify_buy_signals(self, data, stock_code=None):
        if data is None or data.empty:
            return None

        signals = []
        for i in range(1, len(data)):
            row = data.iloc[i]
            prev_row = data.iloc[i - 1]

            if not is_bullish_trend(row):
                continue
            if pd.isna(row.get('Volume_MA5')) or pd.isna(row.get('Volume_MA60')):
                continue
            if row['Close'] <= row['MA25']:
                continue
            if pd.notna(row.get('RSI')) and row['RSI'] < 52:
                continue
            if pd.notna(row.get('Distance_to_20d_High')) and row['Distance_to_20d_High'] < -0.12:
                continue
            if pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] < 0.95:
                continue

            volume_cross_up = (
                prev_row.get('Volume_MA5') <= prev_row.get('Volume_MA60') and
                row.get('Volume_MA5') > row.get('Volume_MA60')
            )
            recent_cross_window = data.iloc[max(0, i - 3):i + 1]
            recent_cross_up = False
            if 'Volume_Cross_5_60' in recent_cross_window.columns:
                recent_cross_up = recent_cross_window['Volume_Cross_5_60'].fillna(0).gt(0).any()
            sustained_volume_strength = row.get('Volume_MA5') >= row.get('Volume_MA60') * 1.05
            if not (volume_cross_up or recent_cross_up or sustained_volume_strength):
                continue

            breakout_quality = (
                pd.notna(row.get('Distance_to_20d_High')) and row['Distance_to_20d_High'] >= -0.06
            ) or row['Close'] >= prev_row['Close']
            if not breakout_quality:
                continue

            reasons = ['股价站上MA25', 'MA25持续向上', 'MA25位于MA60上方', 'RSI 维持强势区间']
            if volume_cross_up:
                reasons.append('5日均量当日上穿60日均量')
            elif recent_cross_up:
                reasons.append('5日均量近3日内上穿60日均量')
            else:
                reasons.append('5日均量持续高于60日均量')
            if pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] >= 1.1:
                reasons.append('量能明显活跃')
            if pd.notna(row.get('Distance_to_20d_High')) and row['Distance_to_20d_High'] >= -0.06:
                reasons.append('价格接近20日强势区间')
            else:
                reasons.append('价格延续上攻而未转弱')

            signals.append(build_buy_signal(
                row,
                signal_strength=8 if volume_cross_up and breakout_quality else 7,
                signal_mode='bullish_trend_2560',
                entry_type='bullish_trend_2560',
                reasons=reasons,
                risk_score=2,
                trailing_stop_pct=0.945,
                trailing_activation_gain=0.07,
                min_holding_bars_for_trend_exit=4,
            ))

        return pd.DataFrame(signals) if signals else None
