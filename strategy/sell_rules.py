import pandas as pd

from .base import SellStrategy


class UnifiedHighVolumeBearishSellStrategy(SellStrategy):
    """统一高位放量阴线卖出规则。"""

    def identify_sell_signals(self, data):
        if data is None or data.empty:
            return None

        signals = []
        for _, row in data.iterrows():
            bearish_candle = (
                (pd.notna(row.get('Open')) and pd.notna(row.get('Close')) and row['Close'] < row['Open']) or
                (pd.notna(row.get('Intraday_Return')) and row['Intraday_Return'] < 0)
            )
            if not bearish_candle:
                continue

            high_conditions = []
            if pd.notna(row.get('Distance_to_20d_High')) and row['Distance_to_20d_High'] >= -0.03:
                high_conditions.append('接近20日高点')
            if pd.notna(row.get('Distance_to_60d_High')) and row['Distance_to_60d_High'] >= -0.05:
                high_conditions.append('接近60日高点')
            if pd.notna(row.get('BB_Position')) and row['BB_Position'] >= 0.78:
                high_conditions.append('位于布林带上沿')
            if pd.notna(row.get('Return_60d')) and row['Return_60d'] >= 0.18:
                high_conditions.append('60日涨幅较大')
            if not high_conditions:
                continue

            volume_conditions = []
            if pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] >= 1.35:
                volume_conditions.append('相对10日均量明显放大')
            if pd.notna(row.get('Volume_Spike_10d')) and row['Volume_Spike_10d'] >= 1.6:
                volume_conditions.append('近期出现放量峰值')
            if not volume_conditions:
                continue

            signal_strength = 5
            reasons = list(dict.fromkeys(high_conditions + volume_conditions + ['高位出现放量阴线']))

            if pd.notna(row.get('Upper_Shadow_Ratio')) and row['Upper_Shadow_Ratio'] >= 0.28:
                signal_strength += 1
                reasons.append('长上影显示抛压')

            if pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] >= 1.8:
                signal_strength += 1
                reasons.append('当日放量进一步增强')

            if (
                (pd.notna(row.get('Distance_to_20d_High')) and row['Distance_to_20d_High'] >= -0.01) or
                (pd.notna(row.get('BB_Position')) and row['BB_Position'] >= 0.92)
            ):
                signal_strength += 1
                reasons.append('位置接近阶段极值')

            signals.append({
                'date': row.name,
                'close': row['Close'],
                'signal_strength': signal_strength,
                'signal_type': 'high_volume_bearish_at_high',
                'reasons': reasons,
                'volume_ratio': row.get('Volume_Ratio_10'),
                'bb_position': row.get('BB_Position'),
                'return_60d': row.get('Return_60d'),
                'upper_shadow_ratio': row.get('Upper_Shadow_Ratio')
            })

        return pd.DataFrame(signals) if signals else None


class StochOverboughtOrUnifiedSellStrategy(UnifiedHighVolumeBearishSellStrategy):
    """StochRSI 超买或统一高位放量阴线卖出。"""

    def identify_sell_signals(self, data):
        unified = super().identify_sell_signals(data)
        extra_signals = []

        if data is not None and not data.empty:
            for _, row in data.iterrows():
                if pd.isna(row.get('StochRSI_K')) or row.get('StochRSI_K') < 80:
                    continue
                if pd.notna(row.get('StochRSI_D')) and row.get('StochRSI_D') < 70:
                    continue

                extra_signals.append({
                    'date': row.name,
                    'close': row['Close'],
                    'signal_strength': 5,
                    'signal_type': 'stochrsi_overbought',
                    'reasons': ['StochRSI进入超买区', '短线动能可能衰竭'],
                    'volume_ratio': row.get('Volume_Ratio_10'),
                    'bb_position': row.get('BB_Position'),
                    'return_60d': row.get('Return_60d'),
                    'upper_shadow_ratio': row.get('Upper_Shadow_Ratio')
                })

        if unified is None and not extra_signals:
            return None
        extra_df = pd.DataFrame(extra_signals) if extra_signals else None
        if unified is None:
            return extra_df.sort_values('date').reset_index(drop=True)
        if extra_df is None:
            return unified.sort_values('date').reset_index(drop=True)

        merged = pd.concat([unified, extra_df], ignore_index=True)
        merged = merged.sort_values(['date', 'signal_strength'], ascending=[True, False])
        merged = merged.drop_duplicates(subset=['date'], keep='first').reset_index(drop=True)
        return merged


class BollingerUpperStochOrUnifiedSellStrategy(UnifiedHighVolumeBearishSellStrategy):
    """布林上轨 + StochRSI 超买或统一高位放量阴线卖出。"""

    def identify_sell_signals(self, data):
        unified = super().identify_sell_signals(data)
        extra_signals = []

        if data is not None and not data.empty:
            for _, row in data.iterrows():
                bb_upper = row.get('BB_Upper')
                close = row.get('Close')
                stoch_k = row.get('StochRSI_K')
                if pd.isna(bb_upper) or pd.isna(close) or pd.isna(stoch_k):
                    continue
                if close < bb_upper * 0.99 or stoch_k < 80:
                    continue

                extra_signals.append({
                    'date': row.name,
                    'close': close,
                    'signal_strength': 6,
                    'signal_type': 'bollinger_upper_stoch_overbought',
                    'reasons': ['价格接近布林上轨', 'StochRSI进入超买区'],
                    'volume_ratio': row.get('Volume_Ratio_10'),
                    'bb_position': row.get('BB_Position'),
                    'return_60d': row.get('Return_60d'),
                    'upper_shadow_ratio': row.get('Upper_Shadow_Ratio')
                })

        if unified is None and not extra_signals:
            return None
        extra_df = pd.DataFrame(extra_signals) if extra_signals else None
        if unified is None:
            return extra_df.sort_values('date').reset_index(drop=True)
        if extra_df is None:
            return unified.sort_values('date').reset_index(drop=True)

        merged = pd.concat([unified, extra_df], ignore_index=True)
        merged = merged.sort_values(['date', 'signal_strength'], ascending=[True, False])
        merged = merged.drop_duplicates(subset=['date'], keep='first').reset_index(drop=True)
        return merged
