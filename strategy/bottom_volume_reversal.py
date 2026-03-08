import pandas as pd

from .base import BuyStrategy, build_buy_signal


class BottomVolumeReversalBuyStrategy(BuyStrategy):
    """底部倍量反转：阶段低位出现放量阳线，捕捉超跌后的首波修复。"""

    def identify_buy_signals(self, data, stock_code=None):
        if data is None or data.empty:
            return None

        signals = []
        for i in range(1, len(data)):
            row = data.iloc[i]
            prev_row = data.iloc[i - 1]
            prior_20_window = data.iloc[max(0, i - 20):i]
            prior_60_window = data.iloc[max(0, i - 60):i]

            if prior_20_window.empty:
                continue

            open_price = row.get('Open')
            high_price = row.get('High')
            low_price = row.get('Low')
            close_price = row.get('Close')
            current_volume = row.get('Volume')
            prev_volume = prev_row.get('Volume')
            volume_ratio_10 = row.get('Volume_Ratio_10')
            volume_spike_10d = row.get('Volume_Spike_10d')
            rsi = row.get('RSI')
            stoch_k = row.get('StochRSI_K')
            return_20d = row.get('Return_20d')
            return_60d = row.get('Return_60d')
            distance_to_ma25 = row.get('Distance_to_MA25')
            distance_to_20d_high = row.get('Distance_to_20d_High')
            bb_position = row.get('BB_Position')
            range_position = row.get('Range_Position_20')
            macd_gap = row.get('MACD_Gap')

            required_values = [
                open_price,
                high_price,
                low_price,
                close_price,
                current_volume,
                volume_ratio_10,
                volume_spike_10d,
                rsi,
                distance_to_ma25,
                distance_to_20d_high,
                bb_position,
                range_position,
                macd_gap,
            ]
            if any(pd.isna(value) for value in required_values):
                continue

            bullish_candle = close_price > open_price
            if not bullish_candle:
                continue

            candle_range = high_price - low_price
            if candle_range <= 0:
                continue
            close_position = (close_price - low_price) / candle_range

            prior_20_low = prior_20_window['Low'].dropna().min()
            prior_60_low = prior_60_window['Low'].dropna().min() if not prior_60_window.empty else prior_20_low
            if pd.isna(prior_20_low) or pd.isna(prior_60_low):
                continue

            bottom_zone = (
                low_price <= prior_20_low * 1.03 or
                low_price <= prior_60_low * 1.06 or
                range_position <= 0.25 or
                bb_position <= 0.20 or
                distance_to_ma25 <= -0.10
            )
            if not bottom_zone:
                continue

            capitulation_volume = (
                volume_ratio_10 >= 1.8 and (
                    volume_spike_10d >= 2.0 or
                    volume_ratio_10 >= 2.3 or
                    (pd.notna(prev_volume) and prev_volume > 0 and current_volume >= prev_volume * 1.5)
                )
            )
            if not capitulation_volume:
                continue

            oversold_context = (
                rsi <= 36 or
                (pd.notna(stoch_k) and stoch_k <= 12) or
                (pd.notna(return_20d) and return_20d <= -0.12)
            )
            if not oversold_context:
                continue

            repair_confirmation = (
                close_position >= 0.62 and
                macd_gap > -0.9 and
                pd.notna(distance_to_20d_high) and distance_to_20d_high <= -0.10 and
                pd.notna(distance_to_ma25) and distance_to_ma25 <= -0.08
            )
            if not repair_confirmation:
                continue

            signal_strength = 6
            risk_score = 2
            signal_tier = 'medium'
            actionable = True
            reasons = ['阶段低位出现放量阳线', '成交量显著放大，存在底部换手']
            risk_filters = []

            if low_price <= prior_20_low * 1.01:
                signal_strength += 2
                reasons.append('回踩近20日低点后企稳')
            elif low_price <= prior_60_low * 1.03:
                signal_strength += 1
                reasons.append('接近近60日阶段低位')

            if volume_ratio_10 >= 3.0:
                signal_strength += 2
                reasons.append('放量达到10日均量3倍以上')
            elif volume_ratio_10 >= 2.0:
                signal_strength += 1
                reasons.append('放量达到10日均量2倍以上')

            if close_position >= 0.70:
                signal_strength += 1
                reasons.append('收盘位于当日高位区域')
            if pd.notna(row.get('Body_Ratio')) and row['Body_Ratio'] >= 0.55:
                signal_strength += 1
                reasons.append('实体阳线较饱满')

            if rsi <= 28:
                signal_strength += 1
                reasons.append('RSI 处于深度超跌区间')
            elif rsi <= 32:
                signal_strength += 1
                reasons.append('RSI 处于超跌区间')
            elif rsi >= 38:
                risk_score += 1
                risk_filters.append('超跌程度一般')

            if pd.notna(prev_row.get('Close')) and close_price < prev_row['Close']:
                risk_score += 1
                risk_filters.append('收盘仍未收复前一日收盘')
            elif close_price >= prev_row['Close'] * 0.995:
                signal_strength += 1
                reasons.append('当日收盘接近收复前一日跌幅')
            else:
                risk_score += 1
                risk_filters.append('收盘修复力度一般')

            if distance_to_ma25 <= -0.18:
                signal_strength += 1
                reasons.append('价格远离 MA25，超跌修复空间较大')
            elif distance_to_ma25 > -0.08:
                risk_score += 1
                risk_filters.append('价格未明显进入深度超跌区')

            if pd.notna(row.get('MA25_Slope')) and row['MA25_Slope'] < 0:
                risk_score += 1
                risk_filters.append('MA25 仍在下行，反转延续性待确认')
            if pd.notna(row.get('Return_60d')) and row['Return_60d'] <= -0.50:
                risk_score += 1
                risk_filters.append('60日跌幅极深，波动风险较大')
            elif pd.notna(row.get('Return_60d')) and row['Return_60d'] <= -0.35:
                reasons.append('60日跌幅较深，具备超跌反转背景')
            if pd.notna(row.get('Upper_Shadow_Ratio')) and row['Upper_Shadow_Ratio'] >= 0.38:
                risk_score += 1
                risk_filters.append('上影偏长，追价资金仍有分歧')

            if risk_score <= 3:
                signal_tier = 'strong'
            elif risk_score <= 5:
                signal_tier = 'medium'
            else:
                signal_tier = 'weak'
                actionable = False

            signals.append(build_buy_signal(
                row,
                signal_strength=signal_strength,
                signal_mode='bottom_volume_reversal',
                entry_type='bottom_volume_reversal',
                reasons=reasons,
                risk_score=risk_score,
                signal_tier=signal_tier,
                actionable=actionable,
                trailing_stop_pct=0.94,
                trailing_activation_gain=0.08,
                min_holding_bars_for_trend_exit=3,
                stop_loss_price=low_price * 0.97,
                risk_filters=risk_filters,
            ))

        return pd.DataFrame(signals) if signals else None

    def merge_buy_signal_zones(self, buy_signals, stock_code=None):
        if buy_signals is None or buy_signals.empty:
            return buy_signals

        merged_signals = buy_signals.sort_values('date').reset_index(drop=True).copy()
        collapsed = []
        i = 0

        while i < len(merged_signals):
            current = merged_signals.iloc[i].copy()
            merged_dates = [current['date']]
            j = i + 1

            while j < len(merged_signals):
                candidate = merged_signals.iloc[j]
                if (candidate['date'] - merged_dates[-1]).days > 5:
                    break
                merged_dates.append(candidate['date'])
                if candidate.get('signal_strength', 0) > current.get('signal_strength', 0):
                    current = candidate.copy()
                j += 1

            current['zone_type'] = 'bottom_volume_reversal'
            current['zone_start_date'] = merged_dates[0]
            current['zone_end_date'] = merged_dates[-1]
            current['merged_signal_count'] = len(merged_dates)
            current['merged_from_dates'] = merged_dates
            current['reasons'] = list(dict.fromkeys(list(current.get('reasons', [])) + [f'合并同一区域内{len(merged_dates)}个连续底部倍量信号']))
            collapsed.append(current)
            i = j

        return pd.DataFrame(collapsed)
