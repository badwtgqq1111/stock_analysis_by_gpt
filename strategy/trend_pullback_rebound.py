import pandas as pd

from .base import BuyStrategy, build_buy_signal, is_bullish_trend


class TrendPullbackReboundBuyStrategy(BuyStrategy):
    """顺势超跌反弹：趋势过滤后，StochRSI 回踩低位并出现反弹信号。"""

    def identify_buy_signals(self, data, stock_code=None):
        if data is None or data.empty:
            return None

        signals = []
        for i in range(2, len(data)):
            row = data.iloc[i]
            prev_row = data.iloc[i - 1]
            prev_prev_row = data.iloc[i - 2]

            if not is_bullish_trend(row, price_buffer=0.05):
                continue
            if pd.isna(row.get('StochRSI_K')) or pd.isna(row.get('StochRSI_D')):
                continue

            current_stoch_k = row.get('StochRSI_K')
            current_stoch_d = row.get('StochRSI_D')
            prev_stoch_k = prev_row.get('StochRSI_K')
            prev_stoch_d = prev_row.get('StochRSI_D')
            recent_low_stoch = min(
                value for value in [current_stoch_k, current_stoch_d, prev_stoch_k, prev_stoch_d, prev_prev_row.get('StochRSI_K')]
                if pd.notna(value)
            )
            if recent_low_stoch > 30:
                continue

            stoch_cross_now = prev_stoch_k <= prev_stoch_d and current_stoch_k > current_stoch_d
            stoch_cross_recent = (
                pd.notna(prev_prev_row.get('StochRSI_K')) and pd.notna(prev_prev_row.get('StochRSI_D')) and
                prev_prev_row.get('StochRSI_K') <= prev_prev_row.get('StochRSI_D') and prev_stoch_k > prev_stoch_d
            )
            stoch_rebound = current_stoch_k > prev_stoch_k and current_stoch_k >= current_stoch_d
            if not (stoch_cross_now or stoch_cross_recent or stoch_rebound):
                continue

            has_macd_divergence = bool(row.get('MACD_Bullish_Divergence', False)) or bool(prev_row.get('MACD_Bullish_Divergence', False))
            close_to_ma25 = pd.notna(row.get('Distance_to_MA25')) and row['Distance_to_MA25'] <= 0.03
            if not (has_macd_divergence or close_to_ma25):
                continue

            reasons = ['MA25 上穿 MA60 且继续上行', 'StochRSI 回踩低位后反弹']
            if stoch_cross_now:
                reasons.append('StochRSI 当日形成金叉')
            elif stoch_cross_recent:
                reasons.append('StochRSI 近1日内形成金叉')
            else:
                reasons.append('StochRSI 低位拐头向上')
            if has_macd_divergence:
                reasons.append('MACD 出现底背离')
            if close_to_ma25:
                reasons.append('价格回踩至 MA25 附近')

            signals.append(build_buy_signal(
                row,
                signal_strength=7 if has_macd_divergence else 6,
                signal_mode='trend_pullback_rebound',
                entry_type='trend_pullback_rebound',
                reasons=reasons,
                risk_score=3,
                trailing_stop_pct=0.95,
                trailing_activation_gain=0.06,
                min_holding_bars_for_trend_exit=3,
            ))

        return pd.DataFrame(signals) if signals else None
