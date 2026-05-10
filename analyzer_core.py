import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
import time

from backtest import backtest_strategy
from backtest_engine import TopNPortfolioBuilder
from data.store import DataLayout, MarketDataWarehouse
from factor_engine import FactorContext, create_factor_set
from indicators import calculate_technical_indicators
from reporting import generate_strategy_comparison_report, generate_trading_strategy
from data.model import normalize_feature_frame, normalize_ohlcv_frame
from factor_validation import FactorValidator
from strategy import BuyStrategy, CurrentStrategy, STRATEGY_SUITE, SellStrategy


DEFAULT_FACTOR_SET = "qlib_alpha158"
DEFAULT_FACTOR_SCORE_CONFIG = {
    "trend": {
        "MA5": {"weight": 0.14, "higher_is_better": False},
        "MA20": {"weight": 0.16, "higher_is_better": False},
        "MA60": {"weight": 0.10, "higher_is_better": False},
        "MAX20": {"weight": 0.10, "higher_is_better": False},
        "MAX60": {"weight": 0.08, "higher_is_better": False},
        "RSV20": {"weight": 0.10, "higher_is_better": True},
        "CNTD20": {"weight": 0.10, "higher_is_better": True},
        "SUMD20": {"weight": 0.12, "higher_is_better": True},
    },
    "quality": {
        "VMA20": {"weight": 0.16, "higher_is_better": False},
        "VSTD20": {"weight": 0.08, "higher_is_better": False},
        "WVMA20": {"weight": 0.10, "higher_is_better": False},
        "VSUMD20": {"weight": 0.12, "higher_is_better": True},
        "CORD20": {"weight": 0.10, "higher_is_better": True},
        "CNTP20": {"weight": 0.08, "higher_is_better": True},
        "SUMP20": {"weight": 0.12, "higher_is_better": True},
        "RSQR60": {"weight": 0.10, "higher_is_better": True},
        "RESI20": {"weight": 0.14, "higher_is_better": False},
    },
    "risk": {
        "STD20": {"weight": 0.38, "higher_is_better": False},
        "STD60": {"weight": 0.26, "higher_is_better": False},
        "WVMA20": {"weight": 0.18, "higher_is_better": False},
        "VSTD20": {"weight": 0.18, "higher_is_better": False},
    },
    "weights": {
        "trend_score": 0.46,
        "quality_score": 0.34,
        "risk_score": 0.20,
    },
}


class StockAnalyzer:
    """股票技术分析器"""

    def __init__(self, db_dir="./assets", buy_strategy=None, sell_strategy=None):
        """
        初始化分析器

        Args:
            db_dir (str): 数据库目录
            buy_strategy: 买入策略实例
            sell_strategy: 卖出策略实例
        """
        self.db_dir = Path(db_dir)
        self.data_layout = DataLayout(base_dir=str(self.db_dir / "data"))
        self.market_warehouse = MarketDataWarehouse(self.data_layout)
        if buy_strategy is None and sell_strategy is None:
            default_strategy = CurrentStrategy()
            self.buy_strategy = default_strategy
            self.sell_strategy = default_strategy
        else:
            self.buy_strategy = buy_strategy or (sell_strategy if isinstance(sell_strategy, BuyStrategy) else CurrentStrategy())
            self.sell_strategy = sell_strategy or (self.buy_strategy if isinstance(self.buy_strategy, SellStrategy) else CurrentStrategy())

    def get_all_stocks(self):
        """
        获取数据库中所有股票代码

        Returns:
            list: 股票代码列表
        """
        try:
            return self.market_warehouse.get_all_stock_codes(
                market="HK",
                asset_type="equity",
                frequency="daily",
                adjust="qfq",
            )
        except Exception as e:
            print(f"[ERROR] 获取股票列表失败: {e}")
            return []

    def load_stock_data(self, stock_code, days=365):
        """
        加载股票的历史数据

        Args:
            stock_code (str): 股票代码
            days (int): 加载最近多少天的数据

        Returns:
            DataFrame: 股票数据
        """
        try:
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)
            warehouse_df = self.market_warehouse.read_ohlcv(
                stock_code=stock_code,
                market="HK",
                asset_type="equity",
                frequency="daily",
                adjust="qfq",
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d'),
            )

            if warehouse_df is None or warehouse_df.empty:
                return None

            data = warehouse_df.copy()
            data["trade_date"] = pd.to_datetime(data["trade_date"])
            data.set_index("trade_date", inplace=True)
            data = data[["open", "close", "high", "low", "volume"]].rename(
                columns={"open": "Open", "close": "Close", "high": "High", "low": "Low", "volume": "Volume"}
            )
            data.index.name = "date"
            return data.sort_index()
        except Exception as e:
            print(f"[ERROR] 加载股票 {stock_code} 数据失败: {e}")
            return None

    def calculate_technical_indicators(self, data):
        return calculate_technical_indicators(data)

    @staticmethod
    def get_score_factor_names(score_config=None):
        config = score_config or DEFAULT_FACTOR_SCORE_CONFIG
        factor_names = []
        for component_name in ("trend", "quality", "risk"):
            component = config.get(component_name, {}) or {}
            for factor_name in component.keys():
                if factor_name not in factor_names:
                    factor_names.append(factor_name)
        return factor_names

    @staticmethod
    def _rolling_score(series, higher_is_better=True, window=120, min_periods=30, scale=12):
        numeric = pd.to_numeric(series, errors="coerce")
        rolling_mean = numeric.rolling(window=window, min_periods=min_periods).mean()
        rolling_std = numeric.rolling(window=window, min_periods=min_periods).std().replace(0, np.nan)
        zscore = (numeric - rolling_mean) / rolling_std
        score = (50 + zscore.clip(-3, 3) * scale).clip(0, 100)
        if not higher_is_better:
            score = 100 - score
        return score.clip(0, 100)

    def _compute_factor_scores(self, feature_frame, factor_set=DEFAULT_FACTOR_SET, score_config=None):
        if feature_frame is None or feature_frame.empty:
            return pd.DataFrame(), {}

        working = feature_frame.copy()
        config = score_config or DEFAULT_FACTOR_SCORE_CONFIG
        factor_details = {
            "factor_set": factor_set,
            "component_weights": dict(config.get("weights", DEFAULT_FACTOR_SCORE_CONFIG["weights"])),
            "factors": {},
        }

        component_frames = {}
        for component_name in ("trend", "quality", "risk"):
            component_def = config.get(component_name, {})
            component_series = []
            component_weights = []
            for column_name, rule in component_def.items():
                if column_name not in working.columns:
                    continue
                score = self._rolling_score(
                    working[column_name],
                    higher_is_better=bool(rule.get("higher_is_better", True)),
                )
                factor_details["factors"][column_name] = {
                    "component": component_name,
                    "weight": float(rule.get("weight", 1.0)),
                    "direction": "higher_is_better" if bool(rule.get("higher_is_better", True)) else "lower_is_better",
                    "raw_series": pd.to_numeric(working[column_name], errors="coerce"),
                    "score_series": score,
                }
                component_series.append(score)
                component_weights.append(float(rule.get("weight", 1.0)))
            if component_series:
                combined = pd.concat(component_series, axis=1)
                weights = np.array(component_weights, dtype=float)
                component_frames[component_name] = combined.mul(weights, axis=1).sum(axis=1) / weights.sum()
            else:
                component_frames[component_name] = pd.Series(np.nan, index=working.index)

        trend_score = component_frames["trend"].clip(0, 100)
        quality_score = component_frames["quality"].clip(0, 100)
        risk_score = component_frames["risk"].clip(0, 100)
        composite_weights = config.get("weights", DEFAULT_FACTOR_SCORE_CONFIG["weights"])
        composite_score = (
            trend_score * float(composite_weights.get("trend_score", 0.46))
            + quality_score * float(composite_weights.get("quality_score", 0.34))
            + risk_score * float(composite_weights.get("risk_score", 0.20))
        ).clip(0, 100)

        result = pd.DataFrame(
            {
                "trend_score": trend_score,
                "quality_score": quality_score,
                "risk_score": risk_score,
                "composite_score": composite_score,
            },
            index=working.index,
        )
        result["factor_set"] = factor_set
        return result, factor_details

    @staticmethod
    def _build_factor_explanation(factor_details, factor_scores, score_index):
        if factor_scores is None or factor_scores.empty or score_index not in factor_scores.index:
            return {}

        component_scores = factor_scores.loc[score_index]
        factors = factor_details.get("factors", {})
        contribution_rows = []
        for factor_name, meta in factors.items():
            raw_series = meta.get("raw_series")
            score_series = meta.get("score_series")
            if raw_series is None or score_series is None or score_index not in raw_series.index or score_index not in score_series.index:
                continue
            raw_value = raw_series.loc[score_index]
            score_value = score_series.loc[score_index]
            weight = float(meta.get("weight", 0.0))
            contribution_rows.append(
                {
                    "factor": factor_name,
                    "display_name": factor_name,
                    "component": meta.get("component"),
                    "weight": weight,
                    "direction": meta.get("direction"),
                    "raw_value": float(raw_value) if pd.notna(raw_value) else np.nan,
                    "score": float(score_value) if pd.notna(score_value) else np.nan,
                    "weighted_contribution": float(score_value) * weight if pd.notna(score_value) else np.nan,
                }
            )

        contribution_rows = [row for row in contribution_rows if pd.notna(row["weighted_contribution"])]
        contribution_rows.sort(key=lambda item: item["weighted_contribution"], reverse=True)

        return {
            "factor_set": factor_details.get("factor_set"),
            "component_weights": dict(factor_details.get("component_weights", {})),
            "component_scores": {
                "trend_score": float(component_scores.get("trend_score", np.nan)),
                "quality_score": float(component_scores.get("quality_score", np.nan)),
                "risk_score": float(component_scores.get("risk_score", np.nan)),
                "composite_score": float(component_scores.get("composite_score", np.nan)),
            },
            "top_positive_factors": contribution_rows[:5],
            "top_negative_factors": list(reversed(contribution_rows[-5:])) if contribution_rows else [],
        }

    @staticmethod
    def _compute_forward_metrics(data, horizons=(20, 40, 60)):
        if data is None or data.empty:
            return pd.DataFrame(index=pd.Index([], name="date"))

        working = data[["Close", "Low"]].copy()
        for horizon in horizons:
            future_close = working["Close"].shift(-horizon)
            working[f"forward_return_{horizon}"] = future_close / working["Close"] - 1.0

            drawdowns = []
            closes = working["Close"].to_numpy(dtype=float)
            lows = working["Low"].to_numpy(dtype=float)
            for index in range(len(working)):
                end = min(index + horizon + 1, len(working))
                if index + 1 >= end or not np.isfinite(closes[index]) or closes[index] == 0:
                    drawdowns.append(np.nan)
                    continue
                future_min_low = np.nanmin(lows[index + 1:end])
                drawdowns.append(future_min_low / closes[index] - 1.0 if np.isfinite(future_min_low) else np.nan)
            working[f"forward_max_drawdown_{horizon}"] = drawdowns
        return working

    def analyze_stock_factors(
        self,
        stock_code,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        persist_features=False,
        show_progress=False,
        enable_portfolio_replay=True,
    ):
        warmup_days = max(days + 180, days)
        full_data = self.load_stock_data(stock_code, warmup_days)
        if full_data is None or full_data.empty:
            return None

        ohlcv_frame = full_data.reset_index().rename(columns={"date": "trade_date"})
        factor = create_factor_set(factor_set)
        context = FactorContext(stock_code=stock_code, market="HK", frequency="daily", adjust="qfq")
        feature_frame = factor.transform(ohlcv_frame, context=context)
        if feature_frame is None or feature_frame.empty:
            return None

        feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan)
        factor_scores, factor_details = self._compute_factor_scores(feature_frame, factor_set=factor_set, score_config=factor_score_config)
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
        factor_explanation = (
            self._build_factor_explanation(factor_details, factor_scores, latest_score_index)
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
            "latest_signal_date": latest_signal["date"] if latest_signal is not None else None,
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
        }

    def build_factor_validation_report(
        self,
        stock_codes=None,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        horizons=(1, 5, 10, 20),
        quantiles=5,
        min_observations=5,
        max_workers=1,
        show_progress=False,
        validation_factor_scope="all",
        validated_feature_names=None,
    ):
        """构建因子验证报告：先并行产出全市场 feature，再做统一横截面验证。"""
        if stock_codes is None:
            stock_codes = self.get_all_stocks()
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return None

        if validation_factor_scope == "scoring_only" and not validated_feature_names:
            validated_feature_names = self.get_score_factor_names(factor_score_config)
        validated_feature_names = [
            str(item) for item in (validated_feature_names or []) if str(item).strip()
        ]
        validated_feature_name_set = set(validated_feature_names)

        validator = FactorValidator(horizons=horizons, quantiles=quantiles, min_observations=min_observations)
        max_workers = max(int(max_workers or 1), 1)

        def run_analysis(stock_code):
            warmup_days = max(days + 180, days)
            full_data = self.load_stock_data(stock_code, warmup_days)
            if full_data is None or full_data.empty:
                return None

            ohlcv_frame = normalize_ohlcv_frame(
                full_data.reset_index(),
                stock_code=stock_code,
                market="HK",
            )
            factor = create_factor_set(factor_set)
            context = FactorContext(stock_code=stock_code, market="HK", frequency="daily", adjust="qfq")
            feature_frame = factor.transform(ohlcv_frame, context=context)
            if feature_frame is None or feature_frame.empty:
                return None

            feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan)
            if validated_feature_name_set:
                keep_columns = [column for column in feature_frame.columns if column in validated_feature_name_set]
                if not keep_columns:
                    return None
                feature_frame = feature_frame[keep_columns].copy()
            feature_long = normalize_feature_frame(
                feature_frame.reset_index().rename(columns={feature_frame.index.name or "index": "trade_date"}),
                stock_code=stock_code,
                market="HK",
                frequency="daily",
                adjust="qfq",
                feature_set=factor_set,
                feature_columns=list(feature_frame.columns),
            )
            return {
                "stock_code": stock_code,
                "feature_frame": feature_long,
                "ohlcv_frame": ohlcv_frame,
                "feature_rows": len(feature_long),
                "feature_names": feature_long["feature_name"].nunique() if not feature_long.empty else 0,
                "date_count": feature_long["trade_date"].nunique() if not feature_long.empty else 0,
                "start_date": feature_long["trade_date"].min() if not feature_long.empty else pd.NaT,
                "end_date": feature_long["trade_date"].max() if not feature_long.empty else pd.NaT,
            }

        pool_results = []
        started_at = time.time()
        completed = 0
        success_count = 0

        if max_workers == 1 or len(stock_codes) <= 1:
            for stock_code in stock_codes:
                result = run_analysis(stock_code)
                completed += 1
                if result is not None:
                    pool_results.append(result)
                    success_count += 1
                if show_progress:
                    elapsed = max(time.time() - started_at, 1e-9)
                    rate = completed / elapsed
                    remaining = len(stock_codes) - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    print(
                        f"[PROGRESS] validation {completed}/{len(stock_codes)} "
                        f"({completed / len(stock_codes):.1%}) success={success_count} "
                        f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                    )
        else:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(stock_codes))) as executor:
                future_map = {executor.submit(run_analysis, stock_code): stock_code for stock_code in stock_codes}
                for future in as_completed(future_map):
                    stock_code = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        print(f"[ERROR] 因子验证 {stock_code} 失败: {exc}")
                        completed += 1
                        if show_progress:
                            elapsed = max(time.time() - started_at, 1e-9)
                            rate = completed / elapsed
                            remaining = len(stock_codes) - completed
                            eta = remaining / rate if rate > 0 else 0.0
                            print(
                                f"[PROGRESS] validation {completed}/{len(stock_codes)} "
                                f"({completed / len(stock_codes):.1%}) success={success_count} "
                                f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                            )
                        continue
                    completed += 1
                    if result is not None:
                        pool_results.append(result)
                        success_count += 1
                    if show_progress:
                        elapsed = max(time.time() - started_at, 1e-9)
                        rate = completed / elapsed
                        remaining = len(stock_codes) - completed
                        eta = remaining / rate if rate > 0 else 0.0
                        print(
                            f"[PROGRESS] validation {completed}/{len(stock_codes)} "
                            f"({completed / len(stock_codes):.1%}) success={success_count} "
                            f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                        )

        if not pool_results:
            return None

        if show_progress:
            print(
                f"[PROGRESS] validation cross_section start "
                f"stocks={success_count} feature_rows={sum(item['feature_rows'] for item in pool_results)} "
                f"scope={validation_factor_scope} "
                f"features={len(validated_feature_names) if validated_feature_names else 'all'}"
            )

        all_feature_frame = pd.concat(
            [item["feature_frame"] for item in pool_results if item.get("feature_frame") is not None and not item["feature_frame"].empty],
            ignore_index=True,
        ) if pool_results else pd.DataFrame()
        all_ohlcv_frame = pd.concat(
            [item["ohlcv_frame"] for item in pool_results if item.get("ohlcv_frame") is not None and not item["ohlcv_frame"].empty],
            ignore_index=True,
        ) if pool_results else pd.DataFrame()

        if validated_feature_name_set and not all_feature_frame.empty and "feature_name" in all_feature_frame.columns:
            all_feature_frame = all_feature_frame[
                all_feature_frame["feature_name"].isin(validated_feature_name_set)
            ].copy()

        def _validation_progress(stage):
            if not show_progress:
                return
            print(
                f"[PROGRESS] validation cross_section {stage} "
                f"stocks={success_count} feature_rows={len(all_feature_frame)}"
            )

        validation_result = validator.validate(
            all_feature_frame,
            all_ohlcv_frame,
            progress_callback=_validation_progress,
        )
        ic_summary = validation_result.get("ic_summary", pd.DataFrame())
        long_short_summary = validation_result.get("long_short_summary", pd.DataFrame())
        turnover_summary = validation_result.get("turnover_summary", pd.DataFrame())

        global_mean_ic = ic_summary["mean_ic"].mean() if not ic_summary.empty and "mean_ic" in ic_summary.columns else np.nan
        global_mean_rank_ic = (
            ic_summary["mean_rank_ic"].mean() if not ic_summary.empty and "mean_rank_ic" in ic_summary.columns else np.nan
        )
        global_mean_spread = (
            long_short_summary["mean_spread"].mean()
            if not long_short_summary.empty and "mean_spread" in long_short_summary.columns
            else np.nan
        )
        global_mean_turnover = (
            turnover_summary["mean_turnover"].mean()
            if not turnover_summary.empty and "mean_turnover" in turnover_summary.columns
            else np.nan
        )

        feature_rows = []
        stock_summary_rows = []
        for item in pool_results:
            feature_rows.append(
                {
                    "stock_code": item.get("stock_code"),
                    "feature_rows": int(item.get("feature_rows", 0)),
                    "feature_names": int(item.get("feature_names", 0)),
                    "date_count": int(item.get("date_count", 0)),
                }
            )
            stock_summary_rows.append(
                {
                    "stock_code": item.get("stock_code"),
                    "feature_set": factor_set,
                    "feature_rows": int(item.get("feature_rows", 0)),
                    "feature_names": int(item.get("feature_names", 0)),
                    "date_count": int(item.get("date_count", 0)),
                    "start_date": item.get("start_date"),
                    "end_date": item.get("end_date"),
                    "mean_ic": global_mean_ic,
                    "mean_rank_ic": global_mean_rank_ic,
                    "mean_spread": global_mean_spread,
                    "mean_turnover": global_mean_turnover,
                }
            )

        if show_progress:
            validation_frame = validation_result.get("validation_frame", pd.DataFrame())
            print(
                f"[PROGRESS] validation cross_section done "
                f"rows={len(validation_frame)} features={validation_frame['feature_name'].nunique() if not validation_frame.empty and 'feature_name' in validation_frame.columns else 0}"
            )

        report = {
            "metadata": {
                "factor_set": factor_set,
                "days": int(days),
                "horizons": tuple(int(item) for item in horizons),
                "quantiles": int(quantiles),
                "min_observations": int(min_observations),
                "stock_count": len(stock_codes),
                "success_count": success_count,
                "factor_score_config": factor_score_config or DEFAULT_FACTOR_SCORE_CONFIG,
                "validation_mode": "cross_sectional_panel",
                "validation_factor_scope": validation_factor_scope,
                "validated_feature_names": list(validated_feature_names),
            },
            "stock_summary": pd.DataFrame(stock_summary_rows),
            "factor_coverage": pd.DataFrame(feature_rows),
            "validation_frame": validation_result.get("validation_frame", pd.DataFrame()),
            "ic_by_date": validation_result.get("ic_by_date", pd.DataFrame()),
            "ic_summary": ic_summary,
            "quantile_returns_by_date": validation_result.get("quantile_returns_by_date", pd.DataFrame()),
            "quantile_summary": validation_result.get("quantile_summary", pd.DataFrame()),
            "long_short_by_date": validation_result.get("long_short_by_date", pd.DataFrame()),
            "long_short_summary": long_short_summary,
            "turnover_by_date": validation_result.get("turnover_by_date", pd.DataFrame()),
            "turnover_summary": turnover_summary,
            "decay_summary": validation_result.get("decay_summary", pd.DataFrame()),
            "analysis_results": pool_results,
        }
        return report

    def identify_buy_signals(self, data, stock_code=None):
        return self.buy_strategy.identify_buy_signals(data, stock_code=stock_code)

    def identify_sell_signals(self, data):
        return self.sell_strategy.identify_sell_signals(data)

    def merge_buy_signal_zones(self, buy_signals, stock_code=None):
        merge_method = getattr(self.buy_strategy, "merge_buy_signal_zones", None)
        if merge_method is None:
            return buy_signals
        return merge_method(buy_signals, stock_code=stock_code)

    def backtest_strategy(self, data, buy_signals, sell_signals, initial_capital=100000, default_holding_days=60):
        return backtest_strategy(
            data,
            buy_signals,
            sell_signals,
            initial_capital=initial_capital,
            default_holding_days=default_holding_days
        )

    def analyze_stock(self, stock_code, days=365):
        """
        分析单只股票

        Args:
            stock_code (str): 股票代码
            days (int): 分析最近多少天

        Returns:
            dict: 分析结果
        """
        print(f"\n[INFO] 分析股票 {stock_code}...")

        warmup_days = max(days + 120, days)
        full_data = self.load_stock_data(stock_code, warmup_days)
        if full_data is None:
            return None

        data_with_indicators = self.calculate_technical_indicators(full_data)
        analysis_start_idx = max(len(data_with_indicators) - days, 0)
        analysis_data = data_with_indicators.iloc[analysis_start_idx:].copy()
        analysis_start_date = analysis_data.index[0]

        buy_signals_full = self.identify_buy_signals(data_with_indicators, stock_code=stock_code)
        sell_signals_full = self.identify_sell_signals(data_with_indicators)

        buy_signals = None
        if buy_signals_full is not None and not buy_signals_full.empty:
            buy_signals = buy_signals_full[buy_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            buy_signals = self.merge_buy_signal_zones(buy_signals, stock_code=stock_code)
            if buy_signals is not None and buy_signals.empty:
                buy_signals = None

        sell_signals = None
        if sell_signals_full is not None and not sell_signals_full.empty:
            sell_signals = sell_signals_full[sell_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            if sell_signals.empty:
                sell_signals = None

        backtest_result = self.backtest_strategy(analysis_data, buy_signals, sell_signals)

        latest_expected_score = analysis_data['expected_3m_score'].dropna().iloc[-1] if 'expected_3m_score' in analysis_data and not analysis_data['expected_3m_score'].dropna().empty else np.nan
        latest_matrix_score = analysis_data['Matrix_Buy_Score'].dropna().iloc[-1] if 'Matrix_Buy_Score' in analysis_data and not analysis_data['Matrix_Buy_Score'].dropna().empty else np.nan
        latest_regime_score = analysis_data['Trend_Regime_Score'].dropna().iloc[-1] if 'Trend_Regime_Score' in analysis_data and not analysis_data['Trend_Regime_Score'].dropna().empty else np.nan
        latest_entry_type = None
        latest_signal_tier = None
        latest_signal_date = None
        current_signal_active = False
        current_signal_actionable = False
        current_signal_score = np.nan
        avg_forward_return_60_signal = 0
        avg_forward_return_60_watch = 0
        if buy_signals is not None and not buy_signals.empty:
            actionable_mask = buy_signals['actionable'] if 'actionable' in buy_signals.columns else pd.Series(True, index=buy_signals.index)
            actionable_signals = buy_signals[actionable_mask]
            watch_signals = buy_signals[~actionable_mask]
            if 'forward_return_60' in actionable_signals:
                avg_forward_return_60_signal = actionable_signals['forward_return_60'].dropna().mean() * 100 if not actionable_signals['forward_return_60'].dropna().empty else 0
            if 'forward_return_60' in watch_signals:
                avg_forward_return_60_watch = watch_signals['forward_return_60'].dropna().mean() * 100 if not watch_signals['forward_return_60'].dropna().empty else 0

            latest_signal = buy_signals.iloc[-1]
            latest_entry_type = latest_signal.get('entry_type')
            latest_signal_tier = latest_signal.get('signal_tier')
            latest_signal_date = latest_signal.get('date')
            recent_window_index = max(len(analysis_data) - 5, 0)
            recent_signal_cutoff = analysis_data.index[recent_window_index]
            current_signal_active = latest_signal_date >= recent_signal_cutoff
            current_signal_actionable = bool(latest_signal.get('actionable', False)) if current_signal_active else False
            if current_signal_active:
                current_signal_score = latest_signal.get('expected_3m_score', np.nan)

        return {
            'stock_code': stock_code,
            'data': analysis_data,
            'buy_signals': buy_signals,
            'sell_signals': sell_signals,
            'backtest': backtest_result,
            'latest_price': analysis_data['Close'].iloc[-1],
            'price_change_30d': (analysis_data['Close'].iloc[-1] - analysis_data['Close'].iloc[-30]) / analysis_data['Close'].iloc[-30] * 100 if len(analysis_data) >= 30 else 0,
            'latest_expected_3m_score': latest_expected_score,
            'latest_matrix_score': latest_matrix_score,
            'latest_regime_score': latest_regime_score,
            'latest_entry_type': latest_entry_type,
            'latest_signal_tier': latest_signal_tier,
            'latest_signal_date': latest_signal_date,
            'current_signal_active': current_signal_active,
            'current_signal_actionable': current_signal_actionable,
            'current_signal_score': current_signal_score,
            'avg_forward_return_60_signal': avg_forward_return_60_signal,
            'avg_forward_return_60_watch': avg_forward_return_60_watch
        }

    def backtest_portfolio(
        self,
        stock_codes=None,
        days=365,
        top_n=3,
        initial_capital=100000,
        weighting_mode="equal_weight",
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
        max_workers=1,
        analysis_mode="strategy",
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        persist_features=False,
        show_progress=False,
        enable_portfolio_replay=True,
    ):
        """固定股票池组合回测：按日期横向比较评分，只持有当日最优的 Top N 信号。"""
        if stock_codes is None:
            stock_codes = self.get_all_stocks()
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return None

        pool_results = []
        max_workers = max(int(max_workers or 1), 1)
        normalized_mode = str(analysis_mode or "strategy").strip().lower()
        if normalized_mode not in {"strategy", "factor"}:
            raise ValueError(f"unsupported analysis_mode: {analysis_mode}")

        def run_analysis(stock_code):
            if normalized_mode == "factor":
                return self.analyze_stock_factors(
                    stock_code,
                    days=days,
                    factor_set=factor_set,
                    factor_score_config=factor_score_config,
                    persist_features=persist_features,
                )
            return self.analyze_stock(stock_code, days=days)

        if max_workers == 1 or len(stock_codes) <= 1:
            started_at = time.time()
            total = len(stock_codes)
            completed = 0
            success_count = 0
            for stock_code in stock_codes:
                result = run_analysis(stock_code)
                completed += 1
                if result is not None:
                    pool_results.append(result)
                    success_count += 1
                if show_progress:
                    elapsed = max(time.time() - started_at, 1e-9)
                    rate = completed / elapsed
                    remaining = total - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    print(
                        f"[PROGRESS] {completed}/{total} "
                        f"({completed / total:.1%}) success={success_count} "
                        f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                    )
        else:
            started_at = time.time()
            total = len(stock_codes)
            completed = 0
            success_count = 0
            with ThreadPoolExecutor(max_workers=min(max_workers, len(stock_codes))) as executor:
                future_map = {
                    executor.submit(run_analysis, stock_code): stock_code
                    for stock_code in stock_codes
                }
                for future in as_completed(future_map):
                    stock_code = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        print(f"[ERROR] 并行分析股票 {stock_code} 失败: {exc}")
                        completed += 1
                        if show_progress:
                            elapsed = max(time.time() - started_at, 1e-9)
                            rate = completed / elapsed
                            remaining = total - completed
                            eta = remaining / rate if rate > 0 else 0.0
                            print(
                                f"[PROGRESS] {completed}/{total} "
                                f"({completed / total:.1%}) success={success_count} "
                                f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                            )
                        continue
                    completed += 1
                    if result is not None:
                        pool_results.append(result)
                        success_count += 1
                    if show_progress:
                        elapsed = max(time.time() - started_at, 1e-9)
                        rate = completed / elapsed
                        remaining = total - completed
                        eta = remaining / rate if rate > 0 else 0.0
                        print(
                            f"[PROGRESS] {completed}/{total} "
                            f"({completed / total:.1%}) success={success_count} "
                            f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                        )

        if not pool_results:
            return None
        builder = TopNPortfolioBuilder(
            top_n=top_n,
            initial_capital=initial_capital,
            weighting_mode=weighting_mode,
            buy_commission_rate=buy_commission_rate,
            sell_commission_rate=sell_commission_rate,
            slippage_rate=slippage_rate,
            min_commission=min_commission,
            enable_portfolio_replay=enable_portfolio_replay,
        )
        return builder.build(stock_codes=stock_codes, analysis_results=pool_results)

    def backtest_hk_market(
        self,
        days=365,
        top_n=3,
        initial_capital=100000,
        weighting_mode="equal_weight",
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
        max_workers=1,
        analysis_mode="strategy",
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        persist_features=False,
        stock_codes=None,
        show_progress=False,
        enable_portfolio_replay=True,
    ):
        """对本地已同步的全部港股执行 TopN 组合回测。"""
        return self.backtest_portfolio(
            stock_codes=stock_codes,
            days=days,
            top_n=top_n,
            initial_capital=initial_capital,
            weighting_mode=weighting_mode,
            buy_commission_rate=buy_commission_rate,
            sell_commission_rate=sell_commission_rate,
            slippage_rate=slippage_rate,
            min_commission=min_commission,
            max_workers=max_workers,
            analysis_mode=analysis_mode,
            factor_set=factor_set,
            factor_score_config=factor_score_config,
            persist_features=persist_features,
            show_progress=show_progress,
            enable_portfolio_replay=enable_portfolio_replay,
        )

    def generate_trading_strategy(self, analysis_results):
        return generate_trading_strategy(analysis_results)

    def close(self):
        if getattr(self, "market_warehouse", None):
            self.market_warehouse.close()
            self.market_warehouse = None

    def __del__(self):
        self.close()

    @staticmethod
    def compare_strategy_suite(
        stock_codes,
        days=365,
        top_n=3,
        initial_capital=100000,
        db_dir="./assets",
        weighting_mode="equal_weight",
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
    ):
        suite_results = []
        for strategy_config in STRATEGY_SUITE:
            analyzer = StockAnalyzer(
                db_dir=db_dir,
                buy_strategy=strategy_config['buy_strategy'],
                sell_strategy=strategy_config['sell_strategy']
            )
            portfolio_result = analyzer.backtest_portfolio(
                stock_codes,
                days=days,
                top_n=top_n,
                initial_capital=initial_capital,
                weighting_mode=weighting_mode,
                buy_commission_rate=buy_commission_rate,
                sell_commission_rate=sell_commission_rate,
                slippage_rate=slippage_rate,
                min_commission=min_commission,
            )
            if portfolio_result is None:
                continue

            per_stock_returns = {
                item['stock_code']: item.get('backtest', {}).get('total_return', 0)
                for item in portfolio_result.get('analysis_results', [])
            }
            suite_results.append({
                'strategy_code': strategy_config['code'],
                'strategy_name': strategy_config['name'],
                'buy_strategy': strategy_config['buy_strategy'].__class__.__name__,
                'sell_strategy': strategy_config['sell_strategy'].__class__.__name__,
                'portfolio_result': portfolio_result,
                'analysis_results': portfolio_result.get('analysis_results', []),
                'per_stock_returns': per_stock_returns,
                'summary': {
                    'estimated_portfolio_return': portfolio_result.get('estimated_portfolio_return', 0),
                    'estimated_portfolio_win_rate': portfolio_result.get('estimated_portfolio_win_rate', 0),
                    'estimated_trade_count': portfolio_result.get('estimated_trade_count', 0),
                    'selected_count': len(portfolio_result.get('selected', [])),
                }
            })

        return {
            'stock_pool': stock_codes,
            'days': days,
            'top_n': top_n,
            'initial_capital': initial_capital,
            'strategies': suite_results,
            'report': generate_strategy_comparison_report(suite_results, stock_codes)
        }
