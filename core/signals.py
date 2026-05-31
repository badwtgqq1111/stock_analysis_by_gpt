"""Model-driven signal extraction.

Reads LightGBM continuous scores from the data columns and produces
minimal signal dicts for the backtest engine.

No hand-crafted indicator rules — exit decisions belong to the portfolio
strategy layer (TopkDropoutStrategy.n_drop + TurnoverControl.hold_thresh)
and the backtest engine (stop-loss / trailing-stop / time-exit).
"""

import numpy as np
import pandas as pd


class SignalsMixin:
    """Signal extraction from model scores."""

    # ---- buy signals --------------------------------------------------

    def identify_buy_signals(self, data, stock_code=None):
        if data is None or data.empty:
            return None

        model_col = self._find_model_score_column(data)
        if model_col is None:
            return None

        threshold = data[model_col].rolling(60, min_periods=20).quantile(0.75)
        signals = []

        for i in range(len(data)):
            score = data[model_col].iloc[i]
            thresh_val = threshold.iloc[i]
            if pd.isna(score) or pd.isna(thresh_val) or score < thresh_val * 0.90:
                continue

            row = data.iloc[i]
            actionable = score >= thresh_val
            signals.append({
                "date": row.name,
                "close": row["Close"],
                "signal_strength": int(np.clip(score / 10.0, 1, 9)),
                "signal_mode": "model_rank",
                "entry_type": "model_driven",
                "signal_tier": "strong" if score >= thresh_val * 1.10 else "weak",
                "actionable": actionable,
                "expected_3m_score": score,
                "matrix_score": np.nan,
                "regime_score": np.nan,
                "risk_score": 1,
                "holding_horizon": 60,
                "min_holding_bars_for_trend_exit": 0,
                "trailing_stop_pct": 0.92,
                "trailing_activation_gain": 0.05,
                "stop_loss_price": (
                    row["Close"] - row["ATR"] * 2
                    if pd.notna(row.get("ATR"))
                    else row["Close"] * 0.92
                ),
                "forward_return_20": row.get("forward_return_20"),
                "forward_return_40": row.get("forward_return_40"),
                "forward_return_60": row.get("forward_return_60"),
                "forward_max_drawdown_60": row.get("forward_max_drawdown_60"),
            })

        return pd.DataFrame(signals) if signals else None

    # ---- sell signals -------------------------------------------------

    def identify_sell_signals(self, data):
        """Sell decisions are handled by portfolio strategy + backtest engine.

        The industrial model does not use discrete sell signals. Exits come from:
          - TopkDropoutStrategy.n_drop (末位淘汰)
          - TurnoverControl.hold_thresh (到期退出)
          - trailing stop / hard stop (回测引擎内置)
        """
        return None

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def _find_model_score_column(data):
        for c in ("composite_score", "expected_3m_score", "Matrix_Buy_Score"):
            if c in data.columns and not data[c].isna().all():
                return c
        return None

    def merge_buy_signal_zones(self, buy_signals, stock_code=None):
        if buy_signals is None or buy_signals.empty:
            return buy_signals
        return buy_signals.sort_values("date").reset_index(drop=True)
