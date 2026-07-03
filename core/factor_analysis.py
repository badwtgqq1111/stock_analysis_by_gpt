"""Factor analysis mixin for StockAnalyzer."""

import numpy as np
import pandas as pd

from core.constants import DEFAULT_FACTOR_SET
from core.formatting import _build_factor_explanation


class FactorAnalysisMixin:
    """Methods for batch and single-stock factor analysis."""

    def _analyze_factor_batch(
        self,
        stock_codes,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        persist_features=False,
        ridge_factors=None,
        signal_recipes=None,
        progress_callback=None,
        batch_index=None,
        total_batches=None,
    ):
        from factor_engine import FactorContext, create_factor_set
        from factor_engine.signals import SignalRecipeRunner

        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return []
        market = getattr(self, "market", "HK")
        frequency = getattr(self, "frequency", "daily")
        adjust = getattr(self, "adjust", "qfq")

        warmup_days = max(days + 180, days)
        batch_results = []
        feature_frames = []
        batch_data_map = self.load_stock_data_batch(stock_codes, warmup_days)
        if len(batch_data_map) < len(stock_codes):
            for stock_code in stock_codes:
                if stock_code in batch_data_map:
                    continue
                stock_data = self.load_stock_data(stock_code, warmup_days)
                if stock_data is not None and not stock_data.empty:
                    batch_data_map[stock_code] = stock_data

        for stock_code in stock_codes:
            full_data = batch_data_map.get(stock_code)
            if full_data is None or full_data.empty or len(full_data) < 60:
                if callable(progress_callback):
                    progress_callback(stock_code)
                continue

            ohlcv_frame = full_data.reset_index().rename(columns={"date": "trade_date"})

            stock_info = self.market_warehouse.get_stock_info(stock_code, market=market)
            if stock_info and stock_info.get("total_shares"):
                ohlcv_frame["total_shares"] = float(stock_info["total_shares"])

            factor = create_factor_set(factor_set)
            context = FactorContext(stock_code=stock_code, market=market, frequency=frequency, adjust=adjust)
            feature_frame = factor.transform(ohlcv_frame, context=context)
            if feature_frame is None or feature_frame.empty:
                if callable(progress_callback):
                    progress_callback(stock_code)
                continue

            feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan)
            feature_frame = feature_frame.copy()
            feature_frame["stock_code"] = stock_code
            batch_results.append(
                {
                    "stock_code": stock_code,
                    "full_data": full_data,
                    "feature_frame": feature_frame,
                }
            )
            feature_frames.append(feature_frame)
            if callable(progress_callback):
                progress_callback(stock_code)

        if not batch_results:
            return []

        panel_features = pd.concat(feature_frames, axis=0, sort=False)
        panel_scores, factor_details = self._compute_factor_scores(
            panel_features,
            factor_set=factor_set,
            score_config=factor_score_config,
            ridge_factors=ridge_factors,
        )
        if panel_scores is None or panel_scores.empty:
            return []

        panel_scores = panel_scores.copy()
        panel_scores["stock_code"] = panel_features["stock_code"].values

        results = []
        for item in batch_results:
            stock_code = item["stock_code"]
            full_data = item["full_data"]
            feature_frame = item["feature_frame"].drop(columns=["stock_code"], errors="ignore")
            stock_mask = panel_features["stock_code"].eq(stock_code).to_numpy()
            stock_panel_scores = panel_scores[panel_scores["stock_code"] == stock_code].drop(columns=["stock_code"], errors="ignore")
            score_analysis = stock_panel_scores
            if score_analysis.empty:
                continue
            score_analysis = score_analysis.sort_index()
            analysis_start_idx = max(len(feature_frame) - days, 0)
            analysis_start_date = feature_frame.index[analysis_start_idx]

            analysis_data = full_data.loc[full_data.index >= analysis_start_date].copy()
            feature_analysis = feature_frame.loc[feature_frame.index >= analysis_start_date].copy()
            score_analysis = score_analysis.loc[score_analysis.index >= analysis_start_date].copy()
            merged_scores = feature_analysis.join(score_analysis[["trend_score", "quality_score", "risk_score", "composite_score"]], how="left")
            forward_metrics = self._compute_forward_metrics(full_data)
            forward_metrics = forward_metrics.loc[forward_metrics.index >= analysis_start_date]

            composite_threshold = score_analysis["composite_score"].rolling(window=60, min_periods=20).quantile(0.80)
            quality_threshold = score_analysis["quality_score"].rolling(window=60, min_periods=20).quantile(0.65)
            risk_threshold = score_analysis["risk_score"].rolling(window=60, min_periods=20).quantile(0.45)
            signals = score_analysis.join(forward_metrics, how="left")
            signals["date"] = signals.index
            signals["signal_strength"] = signals["composite_score"]
            signals["expected_3m_score"] = signals["composite_score"]
            signals["matrix_score"] = signals["trend_score"]
            signals["regime_score"] = signals["quality_score"]
            signals["risk_score"] = (100 - signals["risk_score"]).clip(0, 100) / 25.0
            signals["entry_type"] = "factor_rank"
            signals["holding_horizon"] = 60
            signals["actionable"] = (
                signals["composite_score"] >= composite_threshold.fillna(70)
            ) & (
                signals["quality_score"] >= quality_threshold.fillna(55)
            ) & (
                signals["risk_score"].notna()
            ) & (
                signals["risk_score"] <= ((100 - risk_threshold.fillna(55)).clip(0, 100) / 25.0)
            )
            signals["signal_tier"] = np.where(
                signals["composite_score"] >= 75,
                "strong",
                np.where(signals["composite_score"] >= 60, "medium", "weak"),
            )
            buy_signals = signals[
                [
                    "date",
                    "signal_strength",
                    "expected_3m_score",
                    "matrix_score",
                    "regime_score",
                    "risk_score",
                    "signal_tier",
                    "actionable",
                    "forward_return_20",
                    "forward_return_40",
                    "forward_return_60",
                    "forward_max_drawdown_60",
                    "entry_type",
                    "holding_horizon",
                ]
            ].copy()
            buy_signals.dropna(subset=["expected_3m_score"], inplace=True)
            buy_signals.reset_index(drop=True, inplace=True)

            actionable_signals = buy_signals[buy_signals["actionable"]].copy()
            watch_signals = buy_signals[~buy_signals["actionable"]].copy()
            latest_signal = buy_signals.iloc[-1] if not buy_signals.empty else None
            latest_score_row = score_analysis.iloc[-1] if not score_analysis.empty else None
            latest_score_index = score_analysis.index[-1] if not score_analysis.empty else None
            stock_factor_details = self._slice_factor_details(factor_details, stock_mask)
            factor_explanation = (
                _build_factor_explanation(stock_factor_details, stock_panel_scores, latest_score_index)
                if latest_score_index is not None
                else {}
            )
            setup_runner = self.signal_recipe_runner if signal_recipes is None else SignalRecipeRunner(signal_recipes)
            setup_snapshot = setup_runner.evaluate(
                analysis_data,
                context={
                    "stock_code": stock_code,
                    "analysis_mode": "factor",
                    "factor_set": factor_set,
                },
            )
            latest_signal_date = latest_signal["date"] if latest_signal is not None else None
            latest_data_date = analysis_data.index[-1] if not analysis_data.empty else latest_signal_date
            freshness_score, signal_age_days = self._signal_freshness_score(latest_signal_date, latest_data_date)

            avg_forward_return_60_signal = (
                actionable_signals["forward_return_60"].dropna().mean() * 100
                if not actionable_signals.empty and not actionable_signals["forward_return_60"].dropna().empty
                else 0
            )
            avg_forward_return_60_watch = (
                watch_signals["forward_return_60"].dropna().mean() * 100
                if not watch_signals.empty and not watch_signals["forward_return_60"].dropna().empty
                else 0
            )
            if not actionable_signals.empty:
                forward_series = actionable_signals["forward_return_60"].dropna()
                backtest_result = {
                    "total_return": float(forward_series.mean() * 100) if not forward_series.empty else 0.0,
                    "win_rate": float((forward_series > 0).mean() * 100) if not forward_series.empty else 0.0,
                    "total_trades": int(len(forward_series)),
                }
            else:
                backtest_result = {"total_return": 0.0, "win_rate": 0.0, "total_trades": 0}

            current_signal_score = latest_signal["expected_3m_score"] if latest_signal is not None else np.nan
            current_signal_actionable = bool(latest_signal["actionable"]) if latest_signal is not None else False
            current_signal_active = latest_signal is not None

            results.append(
                {
                    "stock_code": stock_code,
                    "data": analysis_data,
                    "feature_frame": merged_scores,
                    "buy_signals": buy_signals,
                    "sell_signals": None,
                    "backtest": backtest_result,
                    "latest_price": analysis_data["Close"].iloc[-1],
                    "price_change_30d": (analysis_data["Close"].iloc[-1] - analysis_data["Close"].iloc[-30]) / analysis_data["Close"].iloc[-30] * 100 if len(analysis_data) >= 30 else 0,
                    "latest_expected_3m_score": float(latest_score_row["composite_score"]) if latest_score_row is not None and pd.notna(latest_score_row["composite_score"]) else np.nan,
                    "latest_matrix_score": float(latest_score_row["trend_score"]) if latest_score_row is not None and pd.notna(latest_score_row["trend_score"]) else np.nan,
                    "latest_regime_score": float(latest_score_row["quality_score"]) if latest_score_row is not None and pd.notna(latest_score_row["quality_score"]) else np.nan,
                    "latest_entry_type": "factor_rank",
                    "latest_signal_tier": latest_signal["signal_tier"] if latest_signal is not None else None,
                    "latest_signal_date": latest_signal_date,
                    "current_signal_active": current_signal_active,
                    "current_signal_actionable": current_signal_actionable,
                    "current_signal_score": current_signal_score,
                    "avg_forward_return_60_signal": avg_forward_return_60_signal,
                    "avg_forward_return_60_watch": avg_forward_return_60_watch,
                    "factor_set": factor_set,
                    "selection_source": "factor_engine",
                    "factor_scores": (
                        {
                            "trend_score": float(latest_score_row["trend_score"]),
                            "quality_score": float(latest_score_row["quality_score"]),
                            "risk_score": float(latest_score_row["risk_score"]),
                            "composite_score": float(latest_score_row["composite_score"]),
                        }
                        if latest_score_row is not None
                        else {}
                    ),
                    "factor_explanation": factor_explanation,
                    "setup_type": setup_snapshot["setup_type"],
                    "setup_score": setup_snapshot["setup_score"],
                    "sideways_penalty": setup_snapshot["sideways_penalty"],
                    "low_price_candidate": setup_snapshot["low_price_candidate"],
                    "liquidity_ok": setup_snapshot["liquidity_ok"],
                    "signal_recipe_names": setup_snapshot.get("signal_recipe_names", list(self.signal_recipes)),
                    "signal_freshness_score": freshness_score,
                    "signal_age_days": signal_age_days,
                }
            )

        return results

    def analyze_stock_factors(
        self,
        stock_code,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        persist_features=False,
        show_progress=False,
        enable_portfolio_replay=True,
        ridge_factors=None,
        signal_recipes=None,
    ):
        from factor_engine import FactorContext, create_factor_set
        from factor_engine.signals import SignalRecipeRunner

        warmup_days = max(days + 180, days)
        full_data = self.load_stock_data(stock_code, warmup_days)
        if full_data is None or full_data.empty:
            return None

        MIN_TRADING_DAYS = 60
        if len(full_data) < MIN_TRADING_DAYS:
            return None

        ohlcv_frame = full_data.reset_index().rename(columns={"date": "trade_date"})

        stock_info = self.market_warehouse.get_stock_info(stock_code)
        if stock_info and stock_info.get("total_shares"):
            ohlcv_frame["total_shares"] = float(stock_info["total_shares"])

        factor = create_factor_set(factor_set)
        context = FactorContext(
            stock_code=stock_code,
            market=getattr(self, "market", "HK"),
            frequency=getattr(self, "frequency", "daily"),
            adjust=getattr(self, "adjust", "qfq"),
        )
        feature_frame = factor.transform(ohlcv_frame, context=context)
        if feature_frame is None or feature_frame.empty:
            return None

        feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan)
        factor_scores, factor_details = self._compute_factor_scores(feature_frame, factor_set=factor_set, score_config=factor_score_config, ridge_factors=ridge_factors)
        analysis_start_idx = max(len(feature_frame) - days, 0)
        analysis_start_date = feature_frame.index[analysis_start_idx]

        if persist_features:
            pass

        analysis_data = full_data.loc[full_data.index >= analysis_start_date].copy()
        feature_analysis = feature_frame.loc[feature_frame.index >= analysis_start_date].copy()
        score_analysis = factor_scores.loc[factor_scores.index >= analysis_start_date].copy()
        merged_scores = feature_analysis.join(score_analysis[["trend_score", "quality_score", "risk_score", "composite_score"]], how="left")
        forward_metrics = self._compute_forward_metrics(full_data)
        forward_metrics = forward_metrics.loc[forward_metrics.index >= analysis_start_date]

        composite_threshold = score_analysis["composite_score"].rolling(window=60, min_periods=20).quantile(0.80)
        quality_threshold = score_analysis["quality_score"].rolling(window=60, min_periods=20).quantile(0.65)
        risk_threshold = score_analysis["risk_score"].rolling(window=60, min_periods=20).quantile(0.45)
        signals = score_analysis.join(forward_metrics, how="left")
        signals["date"] = signals.index
        signals["signal_strength"] = signals["composite_score"]
        signals["expected_3m_score"] = signals["composite_score"]
        signals["matrix_score"] = signals["trend_score"]
        signals["regime_score"] = signals["quality_score"]
        signals["risk_score"] = (100 - signals["risk_score"]).clip(0, 100) / 25.0
        signals["entry_type"] = "factor_rank"
        signals["holding_horizon"] = 60
        signals["actionable"] = (
            signals["composite_score"] >= composite_threshold.fillna(70)
        ) & (
            signals["quality_score"] >= quality_threshold.fillna(55)
        ) & (
            signals["risk_score"].isna() | (signals["risk_score"] <= ((100 - risk_threshold.fillna(55)).clip(0, 100) / 25.0))
        )
        signals["signal_tier"] = np.where(
            signals["composite_score"] >= 75,
            "strong",
            np.where(signals["composite_score"] >= 60, "medium", "weak"),
        )
        buy_signals = signals[
            [
                "date",
                "signal_strength",
                "expected_3m_score",
                "matrix_score",
                "regime_score",
                "risk_score",
                "signal_tier",
                "actionable",
                "forward_return_20",
                "forward_return_40",
                "forward_return_60",
                "forward_max_drawdown_60",
                "entry_type",
                "holding_horizon",
            ]
        ].copy()
        buy_signals.dropna(subset=["expected_3m_score"], inplace=True)
        buy_signals.reset_index(drop=True, inplace=True)

        actionable_signals = buy_signals[buy_signals["actionable"]].copy()
        watch_signals = buy_signals[~buy_signals["actionable"]].copy()
        latest_signal = buy_signals.iloc[-1] if not buy_signals.empty else None
        latest_score_row = score_analysis.iloc[-1] if not score_analysis.empty else None
        latest_score_index = score_analysis.index[-1] if not score_analysis.empty else None
        factor_explanation = (
            _build_factor_explanation(factor_details, factor_scores, latest_score_index)
            if latest_score_index is not None
            else {}
        )

        avg_forward_return_60_signal = (
            actionable_signals["forward_return_60"].dropna().mean() * 100
            if not actionable_signals.empty and not actionable_signals["forward_return_60"].dropna().empty
            else 0
        )
        avg_forward_return_60_watch = (
            watch_signals["forward_return_60"].dropna().mean() * 100
            if not watch_signals.empty and not watch_signals["forward_return_60"].dropna().empty
            else 0
        )
        if not actionable_signals.empty:
            forward_series = actionable_signals["forward_return_60"].dropna()
            backtest_result = {
                "total_return": float(forward_series.mean() * 100) if not forward_series.empty else 0.0,
                "win_rate": float((forward_series > 0).mean() * 100) if not forward_series.empty else 0.0,
                "total_trades": int(len(forward_series)),
            }
        else:
            backtest_result = {"total_return": 0.0, "win_rate": 0.0, "total_trades": 0}

        current_signal_score = latest_signal["expected_3m_score"] if latest_signal is not None else np.nan
        current_signal_actionable = bool(latest_signal["actionable"]) if latest_signal is not None else False
        current_signal_active = latest_signal is not None
        setup_runner = self.signal_recipe_runner if signal_recipes is None else SignalRecipeRunner(signal_recipes)
        setup_snapshot = setup_runner.evaluate(
            analysis_data,
            context={
                "stock_code": stock_code,
                "analysis_mode": "factor",
                "factor_set": factor_set,
            },
        )
        latest_signal_date = latest_signal["date"] if latest_signal is not None else None
        latest_data_date = analysis_data.index[-1] if not analysis_data.empty else latest_signal_date
        freshness_score, signal_age_days = self._signal_freshness_score(latest_signal_date, latest_data_date)

        return {
            "stock_code": stock_code,
            "data": analysis_data,
            "feature_frame": merged_scores,
            "buy_signals": buy_signals,
            "sell_signals": None,
            "backtest": backtest_result,
            "latest_price": analysis_data["Close"].iloc[-1],
            "price_change_30d": (analysis_data["Close"].iloc[-1] - analysis_data["Close"].iloc[-30]) / analysis_data["Close"].iloc[-30] * 100 if len(analysis_data) >= 30 else 0,
            "latest_expected_3m_score": float(latest_score_row["composite_score"]) if latest_score_row is not None and pd.notna(latest_score_row["composite_score"]) else np.nan,
            "latest_matrix_score": float(latest_score_row["trend_score"]) if latest_score_row is not None and pd.notna(latest_score_row["trend_score"]) else np.nan,
            "latest_regime_score": float(latest_score_row["quality_score"]) if latest_score_row is not None and pd.notna(latest_score_row["quality_score"]) else np.nan,
            "latest_entry_type": "factor_rank",
            "latest_signal_tier": latest_signal["signal_tier"] if latest_signal is not None else None,
            "latest_signal_date": latest_signal_date,
            "current_signal_active": current_signal_active,
            "current_signal_actionable": current_signal_actionable,
            "current_signal_score": current_signal_score,
            "avg_forward_return_60_signal": avg_forward_return_60_signal,
            "avg_forward_return_60_watch": avg_forward_return_60_watch,
            "factor_set": factor_set,
            "selection_source": "factor_engine",
            "factor_scores": (
                {
                    "trend_score": float(latest_score_row["trend_score"]),
                    "quality_score": float(latest_score_row["quality_score"]),
                    "risk_score": float(latest_score_row["risk_score"]),
                    "composite_score": float(latest_score_row["composite_score"]),
                }
                if latest_score_row is not None
                else {}
            ),
            "factor_explanation": factor_explanation,
            "setup_type": setup_snapshot["setup_type"],
            "setup_score": setup_snapshot["setup_score"],
            "sideways_penalty": setup_snapshot["sideways_penalty"],
            "low_price_candidate": setup_snapshot["low_price_candidate"],
            "liquidity_ok": setup_snapshot["liquidity_ok"],
            "signal_recipe_names": setup_snapshot.get("signal_recipe_names", list(self.signal_recipes)),
            "signal_freshness_score": freshness_score,
            "signal_age_days": signal_age_days,
        }
