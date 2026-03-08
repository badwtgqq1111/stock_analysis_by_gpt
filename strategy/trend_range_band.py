import pandas as pd

from .base import BuyStrategy, build_buy_signal, is_bullish_trend


class TrendRangeBandBuyStrategy(BuyStrategy):
    """顺势震荡低吸：以 StochRSI 为主导，辅以震荡市 Alpha 风格因子做确认与风控。"""

    def identify_buy_signals(self, data, stock_code=None):
        if data is None or data.empty:
            return None

        signals = []
        for i in range(3, len(data)):
            row = data.iloc[i]
            prev_row = data.iloc[i - 1]
            recent_window = data.iloc[max(0, i - 4):i + 1]
            base_window = data.iloc[max(0, i - 14):i + 1]
            half_year_window = data.iloc[max(0, i - 120):i + 1]
            prior_half_year_window = data.iloc[max(0, i - 120):max(0, i - 14)]

            prev_bearish = (
                (pd.notna(prev_row.get('Open')) and pd.notna(prev_row.get('Close')) and prev_row['Close'] < prev_row['Open']) or
                (pd.notna(prev_row.get('Intraday_Return')) and prev_row['Intraday_Return'] < 0)
            )
            bullish_now = (
                (pd.notna(row.get('Open')) and pd.notna(row.get('Close')) and row['Close'] > row['Open']) or
                (pd.notna(row.get('Intraday_Return')) and row['Intraday_Return'] > 0)
            ) and row['Close'] >= prev_row['Close']

            recent_base_low = base_window['Low'].dropna().min() if 'Low' in base_window.columns else pd.NA
            prior_half_year_low = prior_half_year_window['Low'].dropna().min() if 'Low' in prior_half_year_window.columns and not prior_half_year_window.empty else pd.NA
            half_year_low = half_year_window['Low'].dropna().min() if 'Low' in half_year_window.columns else pd.NA
            no_break_prior_low = pd.notna(recent_base_low) and pd.notna(prior_half_year_low) and recent_base_low >= prior_half_year_low * 0.985
            makes_half_year_new_low = pd.notna(prior_half_year_low) and pd.notna(row.get('Low')) and row['Low'] <= prior_half_year_low * 1.002

            recent_volume_mean = base_window['Volume'].dropna().tail(5).mean() if 'Volume' in base_window.columns else pd.NA
            prior_volume_mean = data.iloc[max(0, i - 40):max(0, i - 5)]['Volume'].dropna().mean() if i >= 5 else pd.NA
            current_volume = row.get('Volume')
            prev_volume = prev_row.get('Volume')
            volume_contraction = False
            if pd.notna(current_volume) and pd.notna(prev_volume) and current_volume <= prev_volume * 0.90:
                volume_contraction = True
            elif pd.notna(recent_volume_mean) and pd.notna(prior_volume_mean) and recent_volume_mean <= prior_volume_mean * 0.92:
                volume_contraction = True
            elif pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] <= 0.95:
                volume_contraction = True

            distance_to_ma25 = row.get('Distance_to_MA25')
            prev_distance_to_ma25 = prev_row.get('Distance_to_MA25')

            half_year_base_reversal = (
                no_break_prior_low and
                bullish_now and
                (
                    volume_contraction or
                    (pd.notna(row.get('Volume_Ratio_10')) and 0.85 <= row['Volume_Ratio_10'] <= 1.25)
                )
            )
            high_volume_bullish_new_low = (
                makes_half_year_new_low and
                bullish_now and
                pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] >= 2.0
            )
            rebound_reclaim_signal = (
                bullish_now and
                pd.notna(distance_to_ma25) and distance_to_ma25 >= -0.03 and
                pd.notna(prev_distance_to_ma25) and prev_distance_to_ma25 <= -0.06 and
                pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] >= 1.2
            )
            special_reversal_signal = half_year_base_reversal or high_volume_bullish_new_low or rebound_reclaim_signal

            trend_ok = is_bullish_trend(row, price_buffer=0.03)
            if not trend_ok and not special_reversal_signal:
                continue

            stoch_k = row.get('StochRSI_K')
            stoch_d = row.get('StochRSI_D')
            prev_stoch_k = prev_row.get('StochRSI_K')
            prev_stoch_d = prev_row.get('StochRSI_D')
            if pd.isna(stoch_k) or pd.isna(stoch_d) or pd.isna(prev_stoch_k) or pd.isna(prev_stoch_d) or pd.isna(distance_to_ma25):
                continue

            recent_stoch = recent_window[['StochRSI_K', 'StochRSI_D']].stack().dropna()
            if recent_stoch.empty or recent_stoch.min() > 40:
                continue

            stoch_cross_now = prev_stoch_k <= prev_stoch_d and stoch_k > stoch_d
            stoch_rebound = stoch_k > prev_stoch_k and stoch_k >= stoch_d
            reversal_override = special_reversal_signal and (
                (0 <= stoch_k <= 62 and stoch_k >= prev_stoch_k) or
                (high_volume_bullish_new_low and stoch_k <= 20) or
                (rebound_reclaim_signal and stoch_k >= stoch_d)
            )
            stoch_band_ok = 15 <= stoch_k <= 58
            if not ((stoch_band_ok and (stoch_cross_now or stoch_rebound)) or reversal_override):
                continue

            range_reversion = row.get('Alpha101_Range_Reversion')
            compression_score = row.get('Alpha101_Volatility_Compression')
            vwap_stretch = row.get('Alpha101_VWAP_Stretch')
            range_stability = row.get('Alpha101_Range_Stability')
            composite_score = row.get('Alpha101_Range_Long_Composite')
            range_position = row.get('Range_Position_20')
            bb_position = row.get('BB_Position')

            risk_score = 2
            signal_tier = 'strong'
            actionable = True
            risk_filters = []

            recent_distance_to_ma25 = recent_window['Distance_to_MA25'].dropna()
            if recent_distance_to_ma25.empty:
                continue
            recent_pullback = recent_distance_to_ma25.min() <= 0.025
            if not recent_pullback:
                recent_bb_position = recent_window['BB_Position'].dropna() if 'BB_Position' in recent_window.columns else pd.Series(dtype=float)
                recent_pullback = not recent_bb_position.empty and recent_bb_position.min() <= 0.50
            if not recent_pullback:
                actionable = False
                signal_tier = 'weak'
                risk_score += 2
                risk_filters.append('近期未出现有效回踩')

            if recent_distance_to_ma25.min() < -0.065 and not special_reversal_signal:
                actionable = False
                signal_tier = 'weak'
                risk_score += 2
                risk_filters.append('回踩过深，偏离 MA25 过大')

            if distance_to_ma25 > 0.075:
                actionable = False
                signal_tier = 'weak'
                risk_score += 2
                risk_filters.append('当前距离 MA25 偏远，追高风险较大')

            if pd.notna(composite_score):
                if composite_score < 45 and not special_reversal_signal:
                    actionable = False
                    signal_tier = 'weak'
                    risk_score += 2
                    risk_filters.append('震荡因子综合得分偏弱')
                elif composite_score < 55:
                    risk_score += 1
                    risk_filters.append('震荡因子综合得分一般')

            if pd.notna(range_reversion):
                if range_reversion < 45 and not special_reversal_signal:
                    actionable = False
                    signal_tier = 'weak'
                    risk_score += 2
                    risk_filters.append('回归区间下沿特征不足')
                elif range_reversion < 55:
                    risk_score += 1
                    risk_filters.append('区间回归特征尚可')

            if pd.notna(compression_score):
                if compression_score < 40:
                    risk_score += 1
                    risk_filters.append('波动压缩不明显')
                elif compression_score >= 65:
                    risk_score -= 1

            if pd.notna(vwap_stretch) and vwap_stretch < 40:
                risk_score += 1
                risk_filters.append('价格偏离 VWAP，震荡修复优势不足')

            if pd.notna(range_stability) and range_stability < 40:
                risk_score += 1
                risk_filters.append('量能稳定性不足')

            if pd.notna(range_position):
                if range_position > 0.72 and not special_reversal_signal:
                    actionable = False
                    signal_tier = 'weak'
                    risk_score += 2
                    risk_filters.append('价格位于20日区间偏上沿')
                elif range_position > 0.60:
                    risk_score += 1
                    risk_filters.append('价格已离开较优震荡低位')

            if pd.notna(bb_position):
                if bb_position > 0.72 and not special_reversal_signal:
                    actionable = False
                    signal_tier = 'weak'
                    risk_score += 2
                    risk_filters.append('布林位置偏高，不符低吸区间')
                elif bb_position > 0.58:
                    risk_score += 1
                    risk_filters.append('布林位置不再低位')

            if pd.notna(row.get('RSI')):
                if row['RSI'] < 45 and not special_reversal_signal:
                    actionable = False
                    signal_tier = 'weak'
                    risk_score += 2
                    risk_filters.append('RSI 偏弱')
                elif row['RSI'] < 50:
                    risk_score += 1
                    risk_filters.append('RSI 尚未完全转强')
                elif row['RSI'] > 67:
                    risk_score += 1
                    risk_filters.append('RSI 偏高，震荡优势下降')

            if pd.notna(row.get('Distance_to_20d_High')):
                if row['Distance_to_20d_High'] < -0.15 and not special_reversal_signal:
                    actionable = False
                    signal_tier = 'weak'
                    risk_score += 2
                    risk_filters.append('距离阶段高点过远')
                elif row['Distance_to_20d_High'] < -0.08:
                    risk_score += 1
                    risk_filters.append('尚未回到强势区间')

            if pd.notna(row.get('Volume_Ratio_10')):
                if row['Volume_Ratio_10'] < 0.82 and not ((half_year_base_reversal and volume_contraction) or high_volume_bullish_new_low or rebound_reclaim_signal):
                    actionable = False
                    signal_tier = 'weak'
                    risk_score += 2
                    risk_filters.append('量能不足')
                elif row['Volume_Ratio_10'] < 0.95 and not volume_contraction:
                    risk_score += 1
                    risk_filters.append('量能恢复一般')
                elif row['Volume_Ratio_10'] > 1.75 and not high_volume_bullish_new_low:
                    risk_score += 1
                    risk_filters.append('量能过热，震荡低吸性价比下降')

            if row['Close'] < prev_row['Close']:
                risk_score += 1
                risk_filters.append('收盘未明显走强')
            if pd.notna(row.get('MA10')) and row['Close'] < row['MA10'] * 0.995:
                risk_score += 1
                risk_filters.append('价格仍在 MA10 下方')
            if pd.notna(row.get('MA5_10_Gap')) and pd.notna(prev_row.get('MA5_10_Gap')) and row['MA5_10_Gap'] < prev_row['MA5_10_Gap']:
                risk_score += 1
                risk_filters.append('短期均线扩张未改善')

            signal_strength = 5
            reasons = ['MA25 上穿 MA60 且继续上行', 'StochRSI 为主导触发信号']
            if high_volume_bullish_new_low:
                signal_strength += 4
                reasons.append('半年内创新低后收阳')
                reasons.append('放量超过10日均量两倍')
            if rebound_reclaim_signal:
                signal_strength += 3
                reasons.append('深度回踩后重新收回 MA25 附近')
                reasons.append('放量阳线确认底部修复')
            if half_year_base_reversal:
                signal_strength += 3
                reasons.append('半年窗口内未破前低')
                reasons.append('缩量后阴线转阳线')
            if stoch_cross_now:
                signal_strength += 2
                reasons.append('StochRSI 当日金叉')
            elif reversal_override:
                signal_strength += 1
                reasons.append('StochRSI 配合底部转阳修复')
            else:
                signal_strength += 1
                reasons.append('StochRSI 低位继续拐头向上')
            if 15 <= stoch_k <= 35:
                signal_strength += 2
                reasons.append('StochRSI 位于较优低位区间')
            elif 35 < stoch_k <= 45:
                signal_strength += 1
                reasons.append('StochRSI 位于可接受低位区间')
            if stoch_k - stoch_d >= 3:
                signal_strength += 1
                reasons.append('K 线领先 D 线，动能修复更明确')

            if recent_pullback:
                reasons.append('近期出现回踩支撑')
            if makes_half_year_new_low:
                reasons.append('半年窗口出现新低测试')
            if no_break_prior_low:
                reasons.append('半年窗口基底未破前低')
            if volume_contraction:
                reasons.append('近期量能收缩，抛压释放较充分')
            if high_volume_bullish_new_low:
                reasons.append('新低后放量转阳，反转力度较强')
            if rebound_reclaim_signal:
                reasons.append('深度回踩后放量收回关键均线')
            if prev_bearish and bullish_now:
                reasons.append('前一日阴线后出现阳线反包修复')
            if pd.notna(distance_to_ma25) and distance_to_ma25 <= 0.03:
                reasons.append('当前价格仍靠近 MA25')
            if pd.notna(range_reversion) and range_reversion >= 60:
                signal_strength += 1
                reasons.append('区间回归因子支持震荡低吸')
            if pd.notna(compression_score) and compression_score >= 60:
                signal_strength += 1
                reasons.append('波动压缩因子显示整理充分')
            if pd.notna(vwap_stretch) and vwap_stretch >= 60:
                signal_strength += 1
                reasons.append('价格回归 VWAP 附近，均值回归空间更好')
            if pd.notna(range_stability) and range_stability >= 60:
                signal_strength += 1
                reasons.append('量能稳定性较好，适合震荡市低吸')

            risk_score = max(risk_score, 0)
            if actionable and risk_score <= 2:
                signal_tier = 'strong'
            elif actionable and risk_score <= 4:
                signal_tier = 'medium'
            else:
                signal_tier = 'weak'
                actionable = False

            signals.append(build_buy_signal(
                row,
                signal_strength=signal_strength,
                signal_mode='trend_range_band',
                entry_type='trend_range_band',
                reasons=reasons,
                risk_score=risk_score,
                signal_tier=signal_tier,
                actionable=actionable,
                trailing_stop_pct=0.955,
                trailing_activation_gain=0.07,
                min_holding_bars_for_trend_exit=4,
                risk_filters=risk_filters,
            ))

        return pd.DataFrame(signals) if signals else None
