"""LightGBM analysis mixin for StockAnalyzer."""

import sys
import time

import numpy as np
import pandas as pd

from core.constants import DEFAULT_FACTOR_SET
from core.formatting import _build_lightgbm_factor_explanation


class LightGBMAnalysisMixin:
    """Methods for LightGBM-based market analysis."""

    @staticmethod
    def _compute_lightgbm_tactical_overlay(data):
        if data is None or data.empty or "Close" not in data.columns:
            return {
                "startup_score": np.nan,
                "startup_candidate": False,
                "startup_candidate_score": np.nan,
                "overheat_penalty_score": np.nan,
                "downtrend_penalty_score": np.nan,
                "trend_state": "unknown",
            }

        from core.lightgbm_analysis import LightGBMAnalysisMixin as _Self
        startup_frame = _Self._compute_lightgbm_startup_feature_frame(data)
        if startup_frame.empty:
            return {
                "startup_score": np.nan,
                "startup_candidate": False,
                "startup_candidate_score": np.nan,
                "overheat_penalty_score": np.nan,
                "downtrend_penalty_score": np.nan,
                "trend_state": "unknown",
            }

        latest = startup_frame.iloc[-1]
        startup_score = float(np.clip(pd.to_numeric(latest.get("startup_candidate_score"), errors="coerce"), 0.0, 100.0))
        startup_candidate = bool(latest.get("startup_candidate", False))
        overheat_penalty_score = float(
            np.clip(pd.to_numeric(latest.get("overheat_penalty_score"), errors="coerce"), 0.0, 100.0)
        )
        downtrend_penalty_score = float(
            np.clip(pd.to_numeric(latest.get("downtrend_penalty_score"), errors="coerce"), 0.0, 100.0)
        )

        if downtrend_penalty_score >= 45.0 and not startup_candidate:
            trend_state = "downtrend"
        elif startup_candidate and startup_score >= 60.0 and overheat_penalty_score <= 35.0:
            trend_state = "startup"
        elif overheat_penalty_score >= 35.0:
            trend_state = "extended"
        else:
            trend_state = "continuation"

        return {
            "startup_score": startup_score,
            "startup_candidate": startup_candidate,
            "startup_candidate_score": startup_score,
            "overheat_penalty_score": overheat_penalty_score,
            "downtrend_penalty_score": downtrend_penalty_score,
            "trend_state": trend_state,
        }

    @staticmethod
    def _compute_lightgbm_startup_feature_frame(data):
        if data is None or data.empty or "Close" not in data.columns:
            return pd.DataFrame()

        close = pd.to_numeric(data["Close"], errors="coerce")
        open_ = pd.to_numeric(data.get("Open"), errors="coerce")
        high = pd.to_numeric(data.get("High"), errors="coerce")
        low = pd.to_numeric(data.get("Low"), errors="coerce")
        volume = pd.to_numeric(data.get("Volume"), errors="coerce")

        ma20 = close.rolling(20, min_periods=10).mean()
        ma60 = close.rolling(60, min_periods=20).mean()
        vol_ma5 = volume.rolling(5, min_periods=3).mean()
        vol_ma20 = volume.rolling(20, min_periods=10).mean()
        prev_close = close.shift(1)
        prev_high20 = high.shift(1).rolling(20, min_periods=10).max()
        low60 = low.rolling(60, min_periods=20).min()
        high60 = high.rolling(60, min_periods=20).max()
        range60 = (high60 - low60).replace(0, np.nan)

        ret5 = close / close.shift(5) - 1.0
        ret20 = close / close.shift(20) - 1.0
        open_gap = open_ / prev_close - 1.0
        price_position_60 = (close - low60) / range60
        ma20_gap = close / ma20 - 1.0
        ma60_gap = close / ma60 - 1.0
        breakout_gap_20 = close / prev_high20 - 1.0
        volume_ratio_5 = volume / vol_ma5
        volume_ratio_20 = volume / vol_ma20

        score = pd.Series(0.0, index=close.index, dtype=float)
        score += np.where(close >= ma20 * 0.99, 16.0, 0.0)
        score += np.where(ma20 >= ma60 * 0.98, 16.0, 0.0)
        score += np.where((ret20 >= -0.08) & (ret20 <= 0.22), 14.0, 0.0)
        score += np.where((ret5 >= -0.01) & (ret5 <= 0.12), 12.0, 0.0)
        score += np.where((price_position_60 >= 0.12) & (price_position_60 <= 0.55), 16.0, 0.0)
        score += np.where((breakout_gap_20 >= -0.04) & (breakout_gap_20 <= 0.05), 14.0, 0.0)
        score += np.where((volume_ratio_5 >= 1.1) & (volume_ratio_5 <= 3.5), 12.0, 0.0)
        score += np.where((ma20_gap >= -0.03) & (ma20_gap <= 0.08), 10.0, 0.0)

        overheat_penalty = pd.Series(0.0, index=close.index, dtype=float)
        overheat_penalty += np.clip((ma60_gap - 0.18) * 260.0, 0.0, 55.0)
        overheat_penalty += np.clip((ret20 - 0.28) * 180.0, 0.0, 30.0)
        overheat_penalty += np.clip((breakout_gap_20 - 0.08) * 260.0, 0.0, 25.0)
        overheat_penalty = overheat_penalty.clip(0.0, 100.0)

        downtrend_penalty = pd.Series(0.0, index=close.index, dtype=float)
        downtrend_penalty += np.where(close < ma20, 16.0, 0.0)
        downtrend_penalty += np.where(close < ma60, 18.0, 0.0)
        downtrend_penalty += np.clip((-ret20 - 0.02) * 220.0, 0.0, 32.0)
        downtrend_penalty += np.clip((-ret5 - 0.01) * 180.0, 0.0, 18.0)
        downtrend_penalty = downtrend_penalty.clip(0.0, 100.0)

        data_rows = len(close)
        first_date = close.index[0] if data_rows > 0 else pd.NaT
        last_date = close.index[-1] if data_rows > 0 else pd.NaT
        days_since_first_trade = pd.Series(
            (close.index - first_date).days if data_rows > 0 else 0,
            index=close.index,
            dtype=float,
        )
        trading_days = pd.Series(
            range(1, data_rows + 1),
            index=close.index,
            dtype=float,
        )
        is_new_listing = (data_rows < 60)

        startup_candidate = (
            (score >= 58.0)
            & (close >= ma20 * 0.98)
            & (ma20 >= ma60 * 0.97)
            & (ret20 > -0.10)
            & (ret20 < 0.30)
            & (price_position_60 >= 0.10)
            & (price_position_60 <= 0.65)
            & (breakout_gap_20 >= -0.06)
            & (breakout_gap_20 <= 0.08)
            & (volume_ratio_5 >= 1.0)
            & (overheat_penalty <= 45.0)
            & (downtrend_penalty <= 35.0)
        )

        return pd.DataFrame(
            {
                "startup_ret5": ret5,
                "startup_ret20": ret20,
                "startup_open_gap": open_gap,
                "startup_price_position_60": price_position_60,
                "startup_ma20_gap": ma20_gap,
                "startup_ma60_gap": ma60_gap,
                "startup_breakout_gap_20": breakout_gap_20,
                "startup_volume_ratio_5": volume_ratio_5,
                "startup_volume_ratio_20": volume_ratio_20,
                "startup_candidate_score": score.clip(0.0, 100.0),
                "startup_candidate": startup_candidate.astype(float),
                "overheat_penalty_score": overheat_penalty,
                "downtrend_penalty_score": downtrend_penalty,
                "ipo_days_since_first_trade": days_since_first_trade,
                "ipo_trading_days": trading_days,
                "ipo_is_new_listing": float(is_new_listing),
            },
            index=data.index,
        )

    @staticmethod
    def _compute_sector_features(batch_data_map):
        """从 batch OHLCV 数据计算行业/赛道特征。"""
        try:
            from core.sector_features import compute_sector_features
            return compute_sector_features(batch_data_map)
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _build_lightgbm_factor_explanation(
        model_metadata,
        latest_model_score,
        risk_adjusted_score,
        drawdown_penalty_score,
        recent_drawdown,
        risk_score,
        stock_shap_values=None,
        stock_feature_percentiles=None,
    ):
        return _build_lightgbm_factor_explanation(
            model_metadata,
            latest_model_score,
            risk_adjusted_score,
            drawdown_penalty_score,
            recent_drawdown,
            risk_score,
            stock_shap_values=stock_shap_values,
            stock_feature_percentiles=stock_feature_percentiles,
        )

    def _analyze_lightgbm_market(
        self,
        stock_codes,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        signal_recipes=None,
        persist_features=False,
        show_progress=False,
        max_features=0,
        backtest_date=None,
    ):
        from factor_engine import FactorContext, create_factor_set
        from factor_engine.ml import LightGBMRankerPipeline
        from factor_engine.signals import SignalRecipeRunner

        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return []

        ranker = LightGBMRankerPipeline(max_features=max_features)
        warmup_days = max(days + 180, days + ranker.label_horizon + 60)
        batch_data_map = self.load_stock_data_batch(stock_codes, warmup_days, end_date=backtest_date)

        sector_features = self._compute_sector_features(batch_data_map)

        batch_results = []
        feature_frames = []
        target_frames = []
        prepare_started_at = time.time()
        prepare_completed = 0
        prepare_success = 0

        if show_progress:
            print(
                f"[PROGRESS] analysis phase=lightgbm_prepare stocks={len(stock_codes)} "
                f"label_horizon={ranker.label_horizon} factor_set={factor_set}"
            )

        for stock_code in stock_codes:
            full_data = batch_data_map.get(stock_code)
            min_rows = 15 if backtest_date else 60
            if full_data is None or full_data.empty or len(full_data) < min_rows:
                prepare_completed += 1
                if show_progress:
                    self._emit_progress_line(
                        prefix="[PROGRESS] analysis phase=lightgbm_prepare",
                        completed=prepare_completed,
                        total=len(stock_codes),
                        success_count=prepare_success,
                        started_at=prepare_started_at,
                        extra_fields=[
                            ("feature_ready", prepare_success),
                            ("label_horizon", ranker.label_horizon),
                        ],
                    )
                continue

            ohlcv_frame = full_data.reset_index().rename(columns={"date": "trade_date"})
            factor = create_factor_set(factor_set)
            context = FactorContext(stock_code=stock_code, market="HK", frequency="daily", adjust="qfq")
            feature_frame = factor.transform(ohlcv_frame, context=context)
            if feature_frame is None or feature_frame.empty:
                prepare_completed += 1
                if show_progress:
                    self._emit_progress_line(
                        prefix="[PROGRESS] analysis phase=lightgbm_prepare",
                        completed=prepare_completed,
                        total=len(stock_codes),
                        success_count=prepare_success,
                        started_at=prepare_started_at,
                        extra_fields=[
                            ("feature_ready", prepare_success),
                            ("label_horizon", ranker.label_horizon),
                        ],
                    )
                continue

            feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan).copy()
            startup_feature_frame = self._compute_lightgbm_startup_feature_frame(full_data)
            if startup_feature_frame is not None and not startup_feature_frame.empty:
                feature_frame = feature_frame.join(startup_feature_frame, how="left")
            if not sector_features.empty:
                stock_sector = sector_features[sector_features["stock_code"] == stock_code]
                if not stock_sector.empty:
                    for col in stock_sector.columns:
                        if col != "stock_code":
                            feature_frame[col] = stock_sector[col].iloc[0]
            feature_frame["stock_code"] = stock_code
            feature_frames.append(feature_frame)

            forward_metrics = self._compute_forward_metrics(full_data, execution_delay=ranker.execution_delay)
            target_column = f"forward_return_{ranker.label_horizon}"
            target_columns = [target_column]
            target_frame = forward_metrics[target_columns].copy()
            target_frame["stock_code"] = stock_code
            target_frames.append(target_frame)

            batch_results.append(
                {
                    "stock_code": stock_code,
                    "full_data": full_data,
                    "feature_frame": feature_frame,
                }
            )
            prepare_completed += 1
            prepare_success += 1
            if show_progress:
                self._emit_progress_line(
                    prefix="[PROGRESS] analysis phase=lightgbm_prepare",
                    completed=prepare_completed,
                    total=len(stock_codes),
                    success_count=prepare_success,
                    started_at=prepare_started_at,
                    extra_fields=[
                        ("feature_ready", prepare_success),
                        ("label_horizon", ranker.label_horizon),
                    ],
                )

        if not batch_results or not feature_frames or not target_frames:
            if show_progress:
                print(file=sys.stderr)
            return []

        if show_progress:
            print(file=sys.stderr)

        panel_features = pd.concat(feature_frames, axis=0, sort=False)
        panel_targets = pd.concat(target_frames, axis=0, sort=False)
        fit_started_at = time.time()
        if show_progress:
            print(
                f"[PROGRESS] analysis phase=lightgbm_fit prepared_stocks={len(batch_results)} "
                f"feature_rows={len(panel_features)} target_rows={len(panel_targets)}"
            )
        try:
            panel_scores, model_metadata = ranker.fit_predict(panel_features, panel_targets)
        except Exception as exc:
            print(f"[ERROR] LightGBM 排序学习失败: {exc}")
            return []

        if panel_scores is None or panel_scores.empty:
            return []

        if show_progress:
            oos_metrics = model_metadata.get("oos_metrics", {})
            ic_str = f"IC={oos_metrics.get('ic_mean', 'N/A')}" if oos_metrics.get('ic_mean') else "IC=N/A"
            rank_ic_str = f"RankIC={oos_metrics.get('rank_ic_mean', 'N/A')}" if oos_metrics.get('rank_ic_mean') else "RankIC=N/A"
            print(
                f"[PROGRESS] analysis phase=lightgbm_fit "
                f"rolling_windows={model_metadata.get('rolling_windows', 0)} "
                f"oos_dates={model_metadata.get('oos_dates', 0)} "
                f"features={model_metadata.get('feature_count', 0)} "
                f"{ic_str} {rank_ic_str} "
                f"elapsed={max(time.time() - fit_started_at, 1e-9):.1f}s"
            )
            # Detailed model quality summary
            ic_mean = oos_metrics.get("ic_mean")
            rank_ic_mean = oos_metrics.get("rank_ic_mean")
            icir = oos_metrics.get("icir")
            rank_icir = oos_metrics.get("rank_icir")
            ic_pos_rate = oos_metrics.get("ic_positive_rate")
            eval_dates = oos_metrics.get("eval_dates", 0)
            print(f"\n{'='*60}")
            print(f"模型样本外评估 (OOS Evaluation)")
            print(f"{'='*60}")
            print(f"  评估交易日数:  {eval_dates}")
            print(f"  IC 均值:       {ic_mean:.4f}" if ic_mean and np.isfinite(ic_mean) else "  IC 均值:       N/A")
            print(f"  IC 标准差:     {oos_metrics.get('ic_std', 'N/A')}")
            print(f"  ICIR:          {icir:.4f}" if icir and np.isfinite(icir) else "  ICIR:          N/A")
            print(f"  IC 正率:       {ic_pos_rate:.1%}" if ic_pos_rate and np.isfinite(ic_pos_rate) else "  IC 正率:       N/A")
            print(f"  Rank IC 均值:  {rank_ic_mean:.4f}" if rank_ic_mean and np.isfinite(rank_ic_mean) else "  Rank IC 均值:  N/A")
            print(f"  Rank ICIR:     {rank_icir:.4f}" if rank_icir and np.isfinite(rank_icir) else "  Rank ICIR:     N/A")
            print(f"  标签方法:      {model_metadata.get('label_method', 'N/A')}")
            print(f"  执行延迟:      T+{model_metadata.get('execution_delay', 1)}")
            print(f"  预测周期:      {model_metadata.get('label_horizon', 20)}天")
            # Quality assessment
            quality = "优秀" if (ic_mean and ic_mean > 0.05) else "合格" if (ic_mean and ic_mean > 0.03) else "偏弱"
            print(f"  模型质量:      {quality}")
            if ic_mean and np.isfinite(ic_mean):
                # Rough annualized excess return estimate: IC * sqrt(252/horizon) * volatility
                # Simplified: IC * 10 ~ approximate annual excess %
                est_annual_excess = abs(ic_mean) * 10 * 100
                print(f"  预估年化超额:  ~{est_annual_excess:.0f}% (粗略估计, 实际取决于换手和成本)")
            print(f"{'='*60}\n")

        results = []
        finalize_started_at = time.time()
        finalize_total = len(batch_results)

        # Extract model and feature columns for per-stock explanations
        final_model = model_metadata.get("final_model")
        feature_columns = model_metadata.get("feature_columns", [])

        # Build cross-section of latest features for percentile calculation
        cross_section_features = None
        if feature_columns:
            latest_date = panel_features.index.max() if hasattr(panel_features.index, 'max') else None
            if latest_date is not None:
                cs_mask = panel_features.index == latest_date
                if cs_mask.any():
                    cross_section_features = panel_features.loc[cs_mask].copy()

        for item in batch_results:
            stock_code = item["stock_code"]
            full_data = item["full_data"]
            feature_frame = item["feature_frame"].drop(columns=["stock_code"], errors="ignore")
            stock_scores = panel_scores[panel_scores["stock_code"] == stock_code].copy()
            if stock_scores.empty:
                if show_progress:
                    self._emit_progress_line(
                        prefix="[PROGRESS] analysis phase=lightgbm_finalize",
                        completed=len(results),
                        total=finalize_total,
                        success_count=len(results),
                        started_at=finalize_started_at,
                        extra_fields=[
                            ("prepared", len(batch_results)),
                            ("predicted", int(panel_scores["stock_code"].nunique()) if "stock_code" in panel_scores.columns else 0),
                        ],
                    )
                continue

            stock_scores = stock_scores.sort_index()
            stock_scores["trend_score"] = stock_scores["model_score"]
            stock_scores["quality_score"] = np.nan
            stock_scores["risk_score"] = np.nan
            stock_scores["composite_score"] = stock_scores["model_score"]

            analysis_start_idx = max(len(feature_frame) - days, 0)
            analysis_start_date = feature_frame.index[analysis_start_idx]

            analysis_data = full_data.loc[full_data.index >= analysis_start_date].copy()
            feature_analysis = feature_frame.loc[feature_frame.index >= analysis_start_date].copy()
            score_analysis = stock_scores.loc[stock_scores.index >= analysis_start_date].copy()
            merged_scores = feature_analysis.join(
                score_analysis[["trend_score", "quality_score", "risk_score", "composite_score"]],
                how="left",
            )
            forward_metrics = self._compute_forward_metrics(full_data, execution_delay=ranker.execution_delay)
            forward_metrics = forward_metrics.loc[forward_metrics.index >= analysis_start_date]

            composite_threshold = score_analysis["composite_score"].rolling(window=60, min_periods=20).quantile(0.80)
            signals = score_analysis.join(forward_metrics, how="left")
            signals["date"] = signals.index
            signals["signal_strength"] = signals["composite_score"]
            signals["expected_3m_score"] = signals["composite_score"]
            signals["matrix_score"] = signals["trend_score"]
            signals["regime_score"] = signals["quality_score"]
            signals["entry_type"] = "lightgbm_rank"
            signals["holding_horizon"] = 60
            signals["actionable"] = signals["composite_score"] >= composite_threshold.fillna(70)
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
            latest_signal_date = latest_signal["date"] if latest_signal is not None else None
            latest_data_date = analysis_data.index[-1] if not analysis_data.empty else latest_signal_date
            freshness_score, signal_age_days = self._signal_freshness_score(latest_signal_date, latest_data_date)
            latest_model_score = (
                float(latest_score_row["composite_score"])
                if latest_score_row is not None and pd.notna(latest_score_row["composite_score"])
                else np.nan
            )
            drawdown_penalty_score, recent_drawdown, risk_score = self._compute_recent_drawdown_penalty(
                analysis_data,
                window=ranker.drawdown_horizon,
            )
            tactical_overlay = self._compute_lightgbm_tactical_overlay(analysis_data)
            risk_adjusted_score = (
                latest_model_score - drawdown_penalty_score * 0.65
                if pd.notna(latest_model_score) and pd.notna(drawdown_penalty_score)
                else latest_model_score
            )
            # Per-stock feature explanations
            stock_shap_values = None
            stock_feature_percentiles = None
            if feature_columns and not feature_frame.empty:
                from factor_engine.ml.lightgbm_ranker import compute_stock_shap, compute_feature_percentiles
                # Feature percentiles (cheap, always compute)
                if cross_section_features is not None and not cross_section_features.empty:
                    stock_cs = cross_section_features[cross_section_features["stock_code"] == stock_code]
                    if not stock_cs.empty:
                        stock_feature_percentiles = compute_feature_percentiles(
                            stock_cs, cross_section_features, feature_columns, top_k=5
                        )
                # SHAP (expensive, compute for all stocks with valid features)
                if final_model is not None:
                    stock_shap_values = compute_stock_shap(
                        final_model, feature_frame, feature_columns, top_k=5
                    )

            factor_explanation = self._build_lightgbm_factor_explanation(
                model_metadata,
                latest_model_score,
                risk_adjusted_score,
                drawdown_penalty_score,
                recent_drawdown,
                risk_score,
                stock_shap_values=stock_shap_values,
                stock_feature_percentiles=stock_feature_percentiles,
            )
            setup_runner = self.signal_recipe_runner if signal_recipes is None else SignalRecipeRunner(signal_recipes)
            setup_snapshot = setup_runner.evaluate(
                analysis_data,
                context={
                    "stock_code": stock_code,
                    "analysis_mode": "lightgbm",
                    "factor_set": factor_set,
                },
            )

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
                    "latest_expected_3m_score": latest_model_score,
                    "latest_matrix_score": latest_model_score,
                    "latest_regime_score": np.nan,
                    "latest_entry_type": "lightgbm_rank",
                    "latest_signal_tier": latest_signal["signal_tier"] if latest_signal is not None else None,
                    "latest_signal_date": latest_signal_date,
                    "current_signal_active": current_signal_active,
                    "current_signal_actionable": current_signal_actionable,
                    "current_signal_score": current_signal_score,
                    "avg_forward_return_60_signal": avg_forward_return_60_signal,
                    "avg_forward_return_60_watch": avg_forward_return_60_watch,
                    "factor_set": factor_set,
                    "selection_source": "lightgbm_ranker",
                    "factor_scores": {
                        "model_score": latest_model_score,
                        "risk_adjusted_score": risk_adjusted_score,
                        "drawdown_penalty_score": drawdown_penalty_score,
                        "risk_score": risk_score,
                    },
                    "risk_adjusted_score": risk_adjusted_score,
                    "drawdown_penalty_score": drawdown_penalty_score,
                    "recent_drawdown": recent_drawdown,
                    "latest_risk_score": risk_score,
                    "startup_score": tactical_overlay.get("startup_score"),
                    "startup_candidate": tactical_overlay.get("startup_candidate"),
                    "startup_candidate_score": tactical_overlay.get("startup_candidate_score"),
                    "overheat_penalty_score": tactical_overlay.get("overheat_penalty_score"),
                    "downtrend_penalty_score": tactical_overlay.get("downtrend_penalty_score"),
                    "trend_state": tactical_overlay.get("trend_state"),
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
            if show_progress:
                self._emit_progress_line(
                    prefix="[PROGRESS] analysis phase=lightgbm_finalize",
                    completed=len(results),
                    total=finalize_total,
                    success_count=len(results),
                    started_at=finalize_started_at,
                    extra_fields=[
                        ("prepared", len(batch_results)),
                        ("predicted", int(panel_scores["stock_code"].nunique()) if "stock_code" in panel_scores.columns else 0),
                    ],
                )

        if show_progress:
            print(file=sys.stderr)

        return results
