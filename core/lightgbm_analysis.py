"""LightGBM analysis mixin for StockAnalyzer."""

import datetime as _dt
import sys
import time

import numpy as np
import pandas as pd

from data.model import normalize_bool, normalize_stock_code

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
    def _compute_industry_features(batch_data_map, stock_info_map):
        """从 batch OHLCV 数据计算真实行业特征。"""
        try:
            from core.industry_features import compute_industry_features
            return compute_industry_features(batch_data_map, stock_info_map, level="l2")
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _compute_industry_feature_panel(batch_data_map, stock_info_map):
        """从 batch OHLCV 数据计算按日期对齐的真实行业特征面板。"""
        try:
            from core.industry_features import compute_industry_feature_panel
            return compute_industry_feature_panel(batch_data_map, stock_info_map, level="l2")
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _stock_info_frame_to_map(info_frame):
        """Convert stock_info rows/DataFrame into {stock_code: dict}."""
        if info_frame is None:
            return {}
        if isinstance(info_frame, dict):
            return {
                str(code): dict(info or {})
                for code, info in info_frame.items()
            }
        if isinstance(info_frame, pd.DataFrame):
            if info_frame.empty or "stock_code" not in info_frame.columns:
                return {}
            deduped = info_frame.drop_duplicates(subset=["stock_code"], keep="last")
            return {
                str(row.get("stock_code", "")): row.to_dict()
                for _, row in deduped.iterrows()
                if row.get("stock_code") is not None
            }
        try:
            return {
                str(row.get("stock_code", "")): dict(row)
                for row in info_frame
                if row and row.get("stock_code") is not None
            }
        except Exception:
            return {}

    @staticmethod
    def _merge_alt_sentiment_features(panel_features, stock_codes, show_progress=False):
        """Merge alternative sentiment features into the LightGBM feature panel.

        Loads pre-computed alt_sentiment features from the feature layer.
        Falls back to live fetching only for a limited number of stocks.
        """
        try:
            from alt_data.service import AltDataService, merge_sentiment_features

            # 1. Try loading from warehouse first (fast path)
            panel_with_alt = LightGBMAnalysisMixin._load_alt_from_warehouse(
                panel_features, stock_codes, show_progress
            )
            if panel_with_alt is not None:
                return panel_with_alt

            # 2. Live fetch fallback — only for a manageable number of stocks
            MAX_LIVE_STOCKS = 30
            if len(stock_codes) > MAX_LIVE_STOCKS:
                if show_progress:
                    print(
                        f"[PROGRESS] analysis phase=lightgbm_alt_features "
                        f"skipped (live fetch limited to {MAX_LIVE_STOCKS} stocks, "
                        f"got {len(stock_codes)}). Run alt_data persistence first."
                    )
                return panel_features

            alt_service = AltDataService.get_or_create()
            alt_result = alt_service.fetch_and_analyze(
                stock_codes, lookback_days=30,
            )

            if alt_result.feature_df is None or alt_result.feature_df.empty:
                if show_progress:
                    print("[PROGRESS] analysis phase=lightgbm_alt_features alt_features=0 (no news)")
                return panel_features

            merged = merge_sentiment_features(panel_features, alt_result.feature_df)
            alt_cols = [c for c in merged.columns if c.startswith("alt_")]
            if show_progress:
                print(
                    f"[PROGRESS] analysis phase=lightgbm_alt_features "
                    f"alt_features={len(alt_cols)} "
                    f"news_records={len(alt_result.records)} "
                    f"stocks_with_news={alt_result.feature_df['stock_code'].nunique()}"
                )
            return merged

        except Exception as e:
            if show_progress:
                print(f"[PROGRESS] analysis phase=lightgbm_alt_features error={e}")
            return panel_features

    @staticmethod
    def _load_alt_from_warehouse(panel_features, stock_codes, show_progress=False):
        """Try to load pre-computed alt_sentiment features from the feature layer.

        Returns merged panel if features exist, None otherwise.
        """
        try:
            from data.store.layout import DataLayout
            from data.store.warehouse import MarketDataWarehouse
            from data.model.schemas import FEATURE_COLUMNS as _FEATURE_COLS

            layout = DataLayout(base_dir="assets/data")
            wh = MarketDataWarehouse(layout, read_only=True)

            trade_dates = panel_features.index.unique()
            start_date = str(pd.Timestamp(min(trade_dates)).date())
            end_date = str(pd.Timestamp(max(trade_dates)).date())

            feature_df = wh.read_features(
                feature_set="alt_sentiment",
                market="HK",
                frequency="daily",
                start_date=start_date,
                end_date=end_date,
            )

            if feature_df is None or feature_df.empty:
                return None

            # Convert long-format to wide for merge
            wide = feature_df.pivot_table(
                index=["trade_date", "stock_code"],
                columns="feature_name",
                values="feature_value",
            ).reset_index()

            if wide.empty:
                return None

            from alt_data.service import merge_sentiment_features

            merged = merge_sentiment_features(panel_features, wide)
            alt_cols = [c for c in merged.columns if c.startswith("alt_")]
            if show_progress:
                print(
                    f"[PROGRESS] analysis phase=lightgbm_alt_features "
                    f"alt_features={len(alt_cols)} source=warehouse "
                    f"stocks_with_news={wide['stock_code'].nunique()}"
                )
            return merged

        except Exception:
            return None

    @staticmethod
    def _merge_theme_opportunity_features(
        panel_features,
        stock_codes,
        show_progress=False,
        feature_set="theme_opportunity",
        feature_prefix="theme_",
    ):
        """Merge pre-computed theme opportunity features into LightGBM panel."""
        try:
            from data.store.layout import DataLayout
            from data.store.warehouse import MarketDataWarehouse

            if panel_features is None or panel_features.empty:
                return panel_features
            layout = DataLayout(base_dir="assets/data")
            wh = MarketDataWarehouse(layout, read_only=True)
            trade_dates = panel_features.index.unique()
            start_date = str(pd.Timestamp(min(trade_dates)).date())
            panel_end = pd.Timestamp(max(trade_dates)).normalize()
            end_date = str(max(panel_end, pd.Timestamp(_dt.date.today())).date())
            feature_df = wh.read_features(
                feature_set=feature_set,
                market="HK",
                frequency="daily",
                start_date=start_date,
                end_date=end_date,
            )
            if feature_df is None or feature_df.empty:
                if show_progress:
                    print("[PROGRESS] analysis phase=lightgbm_theme_features theme_features=0")
                return panel_features
            if stock_codes:
                allowed_codes = {normalize_stock_code(code, market="HK") for code in stock_codes}
                feature_df["stock_code"] = feature_df["stock_code"].astype(str).map(lambda code: normalize_stock_code(code, market="HK"))
                feature_df = feature_df.loc[feature_df["stock_code"].isin(allowed_codes)]
            if feature_df.empty:
                return panel_features
            wide = feature_df.pivot_table(
                index=["trade_date", "stock_code"],
                columns="feature_name",
                values="feature_value",
                aggfunc="last",
            ).reset_index()
            if wide.empty:
                return panel_features
            wide["trade_date"] = pd.to_datetime(wide["trade_date"], errors="coerce")
            base = panel_features.copy()
            base_reset = base.reset_index()
            date_col = base_reset.columns[0]
            base_reset.rename(columns={date_col: "trade_date"}, inplace=True)
            base_reset["trade_date"] = pd.to_datetime(base_reset["trade_date"], errors="coerce")
            base_reset["stock_code"] = base_reset["stock_code"].astype(str).map(lambda code: normalize_stock_code(code, market="HK"))
            theme_cols = [col for col in wide.columns if str(col).startswith(feature_prefix)]
            future_mask = wide["trade_date"] > panel_end
            if future_mask.any() and theme_cols:
                latest_future = (
                    wide.loc[future_mask]
                    .sort_values("trade_date")
                    .groupby("stock_code", as_index=False)
                    .tail(1)
                    .copy()
                )
                latest_future["trade_date"] = panel_end
                wide = pd.concat([wide.loc[~future_mask], latest_future], ignore_index=True, sort=False)
            merged = base_reset.merge(wide, on=["trade_date", "stock_code"], how="left")
            merged.set_index("trade_date", inplace=True)
            theme_cols = [col for col in merged.columns if str(col).startswith(feature_prefix)]
            if theme_cols:
                merged[theme_cols] = merged.groupby("stock_code", sort=False)[theme_cols].ffill().fillna(0.0)
            if show_progress:
                latest_theme_rows = int((wide["trade_date"] == panel_end).sum()) if "trade_date" in wide.columns else 0
                print(
                    f"[PROGRESS] analysis phase=lightgbm_theme_features "
                    f"theme_features={len(theme_cols)} stocks_with_theme={wide['stock_code'].nunique()} "
                    f"panel_end={panel_end.date()} latest_theme_rows={latest_theme_rows}"
                )
            return merged
        except Exception as exc:
            if show_progress:
                print(f"[PROGRESS] analysis phase=lightgbm_theme_features error={exc}")
            return panel_features

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
        model_type="lightgbm",
        backtest_date=None,
        enable_theme_features=True,
        theme_feature_set="theme_opportunity",
    ):
        import core as core_module
        from factor_engine import FactorContext
        from factor_engine.signals import SignalRecipeRunner

        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return []

        ranker_cls = core_module.LightGBMRankerPipeline
        try:
            ranker = ranker_cls(max_features=max_features, model_type=model_type, neutralize_cluster_features=True)
        except TypeError:
            ranker = ranker_cls()
        label_horizon = int(getattr(ranker, "label_horizon", 20))
        execution_delay = int(getattr(ranker, "execution_delay", 1))
        drawdown_horizon = int(getattr(ranker, "drawdown_horizon", 60))
        warmup_days = max(days + 180, days + label_horizon + 60)
        if show_progress:
            print(
                f"[INFO] 正在批量加载 {len(stock_codes)} 只股票的 OHLCV 数据 "
                f"(window={warmup_days}d)...",
                flush=True,
            )
        t_load = time.time()
        batch_data_map = self.load_stock_data_batch(stock_codes, warmup_days, end_date=backtest_date)
        if show_progress:
            print(
                f"[INFO] OHLCV 加载完成: {len(batch_data_map)} 只, "
                f"耗时 {time.time() - t_load:.1f}s",
                flush=True,
            )

        if show_progress:
            print("[INFO] 正在计算聚类 + 行业特征...", flush=True)
        sector_features = self._compute_sector_features(batch_data_map)

        # Compute real-industry features (parallel to correlation-cluster features)
        stock_info_map = {}
        try:
            stock_info_frame = self.market_warehouse.read_stock_info(
                stock_codes=stock_codes, market="HK",
                columns=["stock_code", "industry_l1", "industry_l2"],
            )
            stock_info_map = self._stock_info_frame_to_map(stock_info_frame)
        except Exception:
            stock_info_map = {}
        industry_features = self._compute_industry_features(batch_data_map, stock_info_map)
        if show_progress:
            print("[INFO] 特征计算完成，开始逐股构建特征面板...", flush=True)

        batch_results = []
        feature_frames = []
        target_frames = []
        prepare_started_at = time.time()
        prepare_completed = 0
        prepare_success = 0

        if show_progress:
            print(
                f"[PROGRESS] analysis phase=lightgbm_prepare stocks={len(stock_codes)} "
                f"label_horizon={label_horizon} factor_set={factor_set}"
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
                            ("label_horizon", label_horizon),
                        ],
                    )
                continue

            ohlcv_frame = full_data.reset_index().rename(columns={"date": "trade_date"})

            stock_info = self.market_warehouse.get_stock_info(stock_code)
            if stock_info and stock_info.get("total_shares"):
                ohlcv_frame["total_shares"] = float(stock_info["total_shares"])
            if stock_info and stock_info.get("market_cap"):
                ohlcv_frame["market_cap"] = float(stock_info["market_cap"])

            factor = core_module.create_factor_set(factor_set)
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
                            ("label_horizon", label_horizon),
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
            if not industry_features.empty:
                stock_ind = industry_features[industry_features["stock_code"] == stock_code]
                if not stock_ind.empty:
                    for col in stock_ind.columns:
                        if col not in ("stock_code", "industry_l1", "industry_l2"):
                            feature_frame[col] = stock_ind[col].iloc[0]
            feature_frame["stock_code"] = stock_code
            feature_frames.append(feature_frame)

            forward_metrics = self._compute_forward_metrics(full_data, execution_delay=execution_delay)
            target_column = f"forward_return_{label_horizon}"
            target_columns = [target_column]
            target_frame = forward_metrics[target_columns].copy()
            target_frame["stock_code"] = stock_code
            target_frames.append(target_frame)

            _stock_market_cap = np.nan
            if stock_info and stock_info.get("market_cap"):
                _stock_market_cap = float(stock_info["market_cap"])

            batch_results.append(
                {
                    "stock_code": stock_code,
                    "full_data": full_data,
                    "feature_frame": feature_frame,
                    "market_cap": _stock_market_cap,
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
                            ("label_horizon", label_horizon),
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

        # Merge alt sentiment features if available
        panel_features = self._merge_alt_sentiment_features(
            panel_features, stock_codes, show_progress=show_progress
        )
        if enable_theme_features:
            panel_features = self._merge_theme_opportunity_features(
                panel_features,
                stock_codes,
                show_progress=show_progress,
                feature_set=theme_feature_set,
            )

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

        batch_codes = [item["stock_code"] for item in batch_results]
        industry_l2_map = {
            code: (stock_info_map.get(code) or {}).get("industry_l2")
            for code in batch_codes
        }
        industry_l1_map = {
            code: (stock_info_map.get(code) or {}).get("industry_l1")
            for code in batch_codes
        }

        # Fetch fundamental quality scores for all stocks in batch
        quality_scores: dict[str, float] = {}
        quality_details: dict[str, dict] = {}
        try:
            from core.industry_scoring import compute_industry_quality_scores
            from core.quality import enrich_with_quality, fetch_quality_components_batch

            if show_progress:
                print("[QUALITY] 正在获取基本面质量评分...")
            quality_components = fetch_quality_components_batch(
                batch_codes,
                max_workers=8,
                progress_callback=(
                    lambda done, total: print(
                        f"\r[QUALITY] {done}/{total} ({done/total*100:.0f}%)",
                        end="", file=sys.stderr,
                    )
                    if show_progress else None
                ),
            )
            if show_progress:
                print(file=sys.stderr)
            industry_quality = compute_industry_quality_scores(
                quality_components,
                industry_l2_map,
                industry_l1_map,
            )
            if not industry_quality.empty:
                quality_details = {
                    str(row["stock_code"]): row.to_dict()
                    for _, row in industry_quality.iterrows()
                }
                quality_raw = {
                    code: details.get("quality_score", np.nan)
                    for code, details in quality_details.items()
                }
            else:
                quality_raw = {}
            quality_scores = enrich_with_quality(
                batch_codes,
                quality_raw,
                quality_details=quality_details,
                show_progress=show_progress,
            )
        except Exception as exc:
            if show_progress:
                print(f"[QUALITY] 质量评分获取失败，使用默认值: {exc}")

        # Fetch hot-sector + relative valuation scores
        valuation_scores: dict[str, dict] = {}
        industry_valuation_details: dict[str, dict] = {}
        try:
            from core.industry_scoring import compute_industry_valuation_scores
            from core.sector_valuation import compute_sector_valuation, fetch_valuation_batch

            if show_progress:
                print("[VALUATION] 正在获取估值与赛道热度评分...")
            pe_pb_data = fetch_valuation_batch(
                batch_codes,
                max_workers=20,
                progress_callback=(
                    lambda done, total: print(
                        f"\r[VALUATION] {done}/{total} ({done/total*100:.0f}%)",
                        end="", file=sys.stderr,
                    )
                    if show_progress else None
                ),
            )
            if show_progress:
                print(file=sys.stderr)
            valuation_df = compute_sector_valuation(batch_data_map, pe_pb_data, sector_features)
            valuation_payload = {
                code: {
                    "pe_ratio": values[0] if isinstance(values, tuple) and len(values) > 0 else np.nan,
                    "pb_ratio": values[1] if isinstance(values, tuple) and len(values) > 1 else np.nan,
                }
                for code, values in pe_pb_data.items()
            }
            industry_valuation_df = compute_industry_valuation_scores(
                valuation_payload,
                industry_l2_map,
                industry_l1_map,
            )
            if not industry_valuation_df.empty:
                industry_valuation_details = {
                    str(row["stock_code"]): row.to_dict()
                    for _, row in industry_valuation_df.iterrows()
                }
            for _, row in valuation_df.iterrows():
                row_dict = row.to_dict()
                ind_val = industry_valuation_details.get(str(row["stock_code"]), {})
                if ind_val:
                    row_dict.update(
                        {
                            "value_score": ind_val.get("valuation_score", row_dict.get("value_score")),
                            "valuation_score": ind_val.get("valuation_score"),
                            "valuation_metric_used": ind_val.get("valuation_metric_used"),
                            "valuation_data_coverage": ind_val.get("valuation_data_coverage"),
                            "valuation_peer_group": ind_val.get("valuation_peer_group"),
                            "industry_pe_percentile": ind_val.get("pe_percentile"),
                            "industry_pb_percentile": ind_val.get("pb_percentile"),
                            "industry_ps_percentile": ind_val.get("ps_percentile"),
                        }
                    )
                valuation_scores[row["stock_code"]] = row_dict
            valid_pe = sum(1 for v in valuation_scores.values() if pd.notna(v.get("pe_ratio")))
            valid_pb = sum(1 for v in valuation_scores.values() if pd.notna(v.get("pb_ratio")))
            if show_progress:
                print(f"[VALUATION] 估值评分完成: {valid_pe} 只PE有效, {valid_pb} 只PB有效")
        except Exception as exc:
            if show_progress:
                print(f"[VALUATION] 估值评分获取失败，使用默认值: {exc}")

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
            latest_theme_features = {}
            if not panel_features.empty and "stock_code" in panel_features.columns:
                theme_rows = panel_features.loc[panel_features["stock_code"].astype(str) == str(stock_code)]
                if not theme_rows.empty:
                    theme_cols = [col for col in theme_rows.columns if str(col).startswith("theme_")]
                    for col in theme_cols:
                        series = pd.to_numeric(theme_rows[col], errors="coerce").dropna()
                        if not series.empty:
                            latest_theme_features[col] = float(series.iloc[-1])
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
            q_score = quality_scores.get(stock_code, 50.0) if quality_scores else 50.0
            stock_scores["quality_score"] = q_score
            stock_scores["risk_score"] = np.nan
            # Blend: 60% trend + 40% quality
            stock_scores["composite_score"] = (
                stock_scores["model_score"] * 0.60 + q_score * 0.40
            )

            analysis_start_idx = max(len(feature_frame) - days, 0)
            analysis_start_date = feature_frame.index[analysis_start_idx]

            analysis_data = full_data.loc[full_data.index >= analysis_start_date].copy()
            feature_analysis = feature_frame.loc[feature_frame.index >= analysis_start_date].copy()
            score_analysis = stock_scores.loc[stock_scores.index >= analysis_start_date].copy()
            merged_scores = feature_analysis.join(
                score_analysis[["trend_score", "quality_score", "risk_score", "composite_score"]],
                how="left",
            )
            forward_metrics = self._compute_forward_metrics(full_data, execution_delay=execution_delay)
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
                window=drawdown_horizon,
            )
            # Compute annualized recent volatility (60-day) for volatility-managed position sizing
            # Reference: Barroso & Santa-Clara (2015)
            recent_vol = self._compute_recent_volatility(analysis_data, window=60)
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

            # Extract sector features and valuation for this stock
            _stock_sec = sector_features[sector_features["stock_code"] == stock_code] if not sector_features.empty else pd.DataFrame()
            _cluster_rps = float(_stock_sec["cluster_rps"].iloc[0]) if not _stock_sec.empty and "cluster_rps" in _stock_sec.columns else 50.0
            _cluster_breadth5 = float(_stock_sec["cluster_breadth5"].iloc[0]) if not _stock_sec.empty and "cluster_breadth5" in _stock_sec.columns else 0.5
            _cluster_breadth20 = float(_stock_sec["cluster_breadth20"].iloc[0]) if not _stock_sec.empty and "cluster_breadth20" in _stock_sec.columns else 0.5
            _hot_sector_leader = float(_stock_sec["hot_sector_leader"].iloc[0]) if not _stock_sec.empty and "hot_sector_leader" in _stock_sec.columns else 0.5
            _cluster_id = int(_stock_sec["cluster_id"].iloc[0]) if not _stock_sec.empty and "cluster_id" in _stock_sec.columns else -1
            _stock_ind = industry_features[industry_features["stock_code"] == stock_code] if not industry_features.empty else pd.DataFrame()
            _industry_feature_payload = {}
            if not _stock_ind.empty:
                for _col in (
                    "industry_member_count",
                    "industry_ret_5d",
                    "industry_ret_20d",
                    "industry_ret_60d",
                    "industry_rps_20d",
                    "industry_rps_60d",
                    "industry_breadth_5d",
                    "industry_breadth_20d",
                    "industry_vol_20d",
                    "industry_vol_60d",
                    "stock_vs_industry_ret_5d",
                    "stock_vs_industry_ret_20d",
                    "stock_vs_industry_rank",
                    "dip_buy_signal_industry",
                    "industry_leader",
                ):
                    if _col in _stock_ind.columns:
                        _industry_feature_payload[_col] = _stock_ind[_col].iloc[0]
            _val = valuation_scores.get(stock_code, {})
            _hsv_score = _val.get("hot_sector_value_score", 50.0)
            _pe = _val.get("pe_ratio", np.nan)
            _pb = _val.get("pb_ratio", np.nan)
            _value_score = _val.get("value_score", _val.get("valuation_score", 50.0))
            _valuation_metric_used = _val.get("valuation_metric_used")
            _valuation_data_coverage = _val.get("valuation_data_coverage")
            _valuation_peer_group = _val.get("valuation_peer_group")
            _quality_detail = quality_details.get(stock_code, {})
            _info = self.market_warehouse.get_stock_info(stock_code) or {}
            _industry_l1 = _info.get("industry_l1")
            _industry_l2 = _info.get("industry_l2")
            _industry_l3 = _info.get("industry_l3")
            _industry_source = _info.get("industry_source")
            _industry_updated_at = _info.get("industry_updated_at")
            _instrument_type = _info.get("instrument_type")
            _is_fund_like = normalize_bool(_info.get("is_fund_like"), default=False)
            _tradable_flag = normalize_bool(_info.get("tradable_flag"), default=True)
            _coverage_fields = {
                "industry_l1": _industry_l1,
                "market_cap": item.get("market_cap", np.nan),
                "pe_ratio": _pe,
                "pb_ratio": _pb,
                "quality_score": q_score,
                "liquidity_ok": setup_snapshot["liquidity_ok"],
                "latest_risk_score": risk_score,
            }
            _missing_fields = [
                field
                for field, value in _coverage_fields.items()
                if value is None
                or (isinstance(value, float) and np.isnan(value))
                or (isinstance(value, str) and not value.strip())
            ]
            _data_coverage_score = max(
                0.0,
                100.0 * (1.0 - len(_missing_fields) / max(len(_coverage_fields), 1)),
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
                    "latest_regime_score": q_score,
                    "quality_score": q_score,
                    "quality_data_coverage": _quality_detail.get("quality_data_coverage"),
                    "quality_peer_group": _quality_detail.get("quality_peer_group"),
                    "quality_missing_fields": _quality_detail.get("quality_missing_fields", []),
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
                    "recent_volatility": recent_vol,
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
                    # Market cap for double sort (QMJ size grouping)
                    "market_cap": item.get("market_cap", np.nan),
                    # Sector features
                    "cluster_id": _cluster_id,
                    "cluster_rps": _cluster_rps,
                    "cluster_breadth5": _cluster_breadth5,
                    "cluster_breadth20": _cluster_breadth20,
                    "hot_sector_leader": _hot_sector_leader,
                    # Valuation scores
                    "hot_sector_value_score": _hsv_score,
                    "value_score": _value_score,
                    "valuation_score": _val.get("valuation_score", _value_score),
                    "valuation_metric_used": _valuation_metric_used,
                    "valuation_data_coverage": _valuation_data_coverage,
                    "valuation_peer_group": _valuation_peer_group,
                    "pe_ratio": _pe,
                    "pb_ratio": _pb,
                    # Industry metadata and data quality
                    "industry_l1": _industry_l1,
                    "industry_l2": _industry_l2,
                    "industry_l3": _industry_l3,
                    **_industry_feature_payload,
                    "industry_source": _industry_source,
                    "industry_updated_at": _industry_updated_at,
                    "instrument_type": _instrument_type,
                    "is_fund_like": _is_fund_like,
                    "tradable_flag": _tradable_flag,
                    "data_coverage_score": _data_coverage_score,
                    "data_missing_fields": _missing_fields,
                    "theme_feature_set": theme_feature_set if enable_theme_features else None,
                    "theme_features": latest_theme_features,
                    "theme_opportunity_score": max(
                        [value for key, value in latest_theme_features.items() if str(key).startswith("theme_score__")]
                        or [np.nan]
                    ),
                    "theme_attention_score": max(
                        [value for key, value in latest_theme_features.items() if "attention_score" in str(key)]
                        or [np.nan]
                    ),
                    "theme_bottleneck_score": max(
                        [value for key, value in latest_theme_features.items() if "bottleneck_score" in str(key)]
                        or [np.nan]
                    ),
                    "theme_risk_penalty": max(
                        [value for key, value in latest_theme_features.items() if "risk_penalty" in str(key)]
                        or [np.nan]
                    ),
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
