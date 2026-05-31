"""Forward metrics computation mixin."""

import numpy as np
import pandas as pd


class ForwardMetricsMixin:
    """Methods for computing forward-looking return and drawdown metrics."""

    @staticmethod
    def _compute_forward_metrics(data, horizons=(20, 40, 60), execution_delay=1, skip_days=0):
        if data is None or data.empty:
            return pd.DataFrame(index=pd.Index([], name="date"))

        working = data[["Close", "Low", "High"]].copy()
        for horizon in horizons:
            # With execution delay: buy at T+delay+skip, sell at T+delay+skip+horizon
            # skip_days skips the immediate reversal period (e.g. skip 21d for t-1 reversal)
            future_close = working["Close"].shift(-(horizon + execution_delay + skip_days))
            entry_close = working["Close"].shift(-(execution_delay + skip_days))
            working[f"forward_return_{horizon}"] = future_close / entry_close - 1.0

            drawdowns = []
            runups = []
            closes = working["Close"].to_numpy(dtype=float)
            lows = working["Low"].to_numpy(dtype=float)
            highs = working["High"].to_numpy(dtype=float)
            for index in range(len(working)):
                start = index + execution_delay
                end = min(index + execution_delay + horizon + 1, len(working))
                if start >= len(working) or start >= end:
                    drawdowns.append(np.nan)
                    runups.append(np.nan)
                    continue
                entry_price = closes[start] if np.isfinite(closes[start]) and closes[start] != 0 else np.nan
                if not np.isfinite(entry_price):
                    drawdowns.append(np.nan)
                    runups.append(np.nan)
                    continue
                future_min_low = np.nanmin(lows[start:end])
                future_max_high = np.nanmax(highs[start:end])
                drawdowns.append(future_min_low / entry_price - 1.0 if np.isfinite(future_min_low) else np.nan)
                runups.append(future_max_high / entry_price - 1.0 if np.isfinite(future_max_high) else np.nan)
            working[f"forward_max_drawdown_{horizon}"] = drawdowns
            working[f"forward_max_return_{horizon}"] = runups
        return working

    @staticmethod
    def _compute_recent_drawdown_penalty(data, window=60, penalty_scale=400.0):
        if data is None or data.empty or "Close" not in data.columns:
            return np.nan, np.nan, np.nan

        closes = pd.to_numeric(data["Close"], errors="coerce")
        rolling_peak = closes.rolling(window=window, min_periods=max(10, window // 3)).max()
        drawdown_series = closes / rolling_peak - 1.0
        latest_drawdown = drawdown_series.iloc[-1] if not drawdown_series.empty else np.nan
        if pd.isna(latest_drawdown):
            return np.nan, np.nan, np.nan

        drawdown_penalty_score = float(np.clip(abs(min(float(latest_drawdown), 0.0)) * penalty_scale, 0.0, 100.0))
        risk_score = float(np.clip(100.0 - drawdown_penalty_score, 0.0, 100.0))
        return drawdown_penalty_score, float(latest_drawdown), risk_score

    @staticmethod
    def _compute_recent_volatility(data, window=60):
        """Compute annualized realized volatility from daily returns.

        Reference: Barroso & Santa-Clara (2015) — volatility-managed portfolios.
        Returns annualized volatility as a decimal (e.g. 0.25 = 25% annualized).
        """
        if data is None or data.empty or "Close" not in data.columns:
            return np.nan

        closes = pd.to_numeric(data["Close"], errors="coerce")
        returns = closes.pct_change().dropna()
        if len(returns) < max(10, window // 3):
            return np.nan

        recent = returns.tail(min(window, len(returns)))
        daily_vol = recent.std()
        annualized_vol = daily_vol * np.sqrt(252)
        return float(annualized_vol)
