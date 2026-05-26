import pandas as pd

from .base import BuyStrategy, build_buy_signal


class BullishImpulseCYCBuyStrategy(BuyStrategy):
    """CYC 主升浪策略：CYC 多头排列，成本线整体抬升，价格处于强势成本区。"""

    def identify_buy_signals(self, data, stock_code=None):
        if data is None or data.empty:
            return None

        signals = []
        for i in range(1, len(data)):
            row = data.iloc[i]
            prev_row = data.iloc[i - 1]

            cyc5 = row.get('CYC5')
            cyc13 = row.get('CYC13')
            cyc34 = row.get('CYC34')
            if pd.isna(cyc5) or pd.isna(cyc13) or pd.isna(cyc34):
                continue
            if not (cyc5 > cyc13 > cyc34):
                continue

            if pd.notna(row.get('MA25')) and pd.notna(row.get('MA60')) and row['MA25'] <= row['MA60']:
                continue

            cyc5_up = pd.notna(prev_row.get('CYC5')) and cyc5 > prev_row.get('CYC5')
            cyc13_up = pd.notna(prev_row.get('CYC13')) and cyc13 > prev_row.get('CYC13')
            cyc34_up = pd.notna(prev_row.get('CYC34')) and cyc34 >= prev_row.get('CYC34')
            slope_score = sum([cyc5_up, cyc13_up, cyc34_up])
            if slope_score < 2:
                continue

            if row['Close'] < cyc5 * 0.995:
                continue

            reasons = ['CYC5 > CYC13 > CYC34 多头排列']
            if slope_score == 3:
                reasons.append('三条成本线同步向上')
            else:
                reasons.append('多数成本线继续抬升')
            if row['Close'] >= cyc5:
                reasons.append('价格站上 CYC5')
            else:
                reasons.append('价格贴近 CYC5 强势成本区')
            if pd.notna(row.get('Volume_Ratio_10')) and row['Volume_Ratio_10'] >= 0.95:
                reasons.append('量能未明显走弱')

            signals.append(build_buy_signal(
                row,
                signal_strength=8 if slope_score == 3 and row['Close'] >= cyc5 else 7,
                signal_mode='bullish_impulse_cyc',
                entry_type='bullish_impulse_cyc',
                reasons=reasons,
                risk_score=2,
                trailing_stop_pct=0.94,
                trailing_activation_gain=0.07,
                min_holding_bars_for_trend_exit=5,
            ))

        return pd.DataFrame(signals) if signals else None
