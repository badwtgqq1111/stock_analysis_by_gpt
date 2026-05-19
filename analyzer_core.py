import pandas as pd
import numpy as np
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import time

from backtest import backtest_strategy
from backtest_engine import TopNPortfolioBuilder
from data.store import DataLayout, MarketDataWarehouse
from factor_engine import FactorContext, create_factor_set
from factor_engine.signals import DEFAULT_SIGNAL_RECIPES, SignalRecipeRunner
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
    "validated": {},
    "weights": {
        "trend_score": 0.40,
        "quality_score": 0.30,
        "risk_score": 0.15,
        "validated_score": 0.15,
    },
}


# Factor-to-component classification rules based on Alpha158 operator semantics
FACTOR_CLASSIFICATION_RULES = {
    "trend": {
        "operators": {
            "MA", "ROC", "BETA", "MAX", "MIN", "RSV", "IMAX", "IMIN", "IMXD",
            "RANK", "QTLU", "QTLD",
        },
        "price_fields": {"OPEN", "HIGH", "LOW", "CLOSE", "VWAP"},
        "description": "Price trend and momentum factors",
    },
    "quality": {
        "operators": {
            "VMA", "WVMA", "VSUMP", "VSUMN", "VSUMD", "CORR", "CORD",
            "CNTP", "CNTN", "CNTD", "SUMP", "SUMN", "SUMD", "RSQR", "RESI",
        },
        "kbar_operators": {
            "KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2",
        },
        "volume_prefix": "VOLUME",
        "description": "Volume-price relationship and quality factors",
    },
    "risk": {
        "operators": {"STD", "VSTD"},
        "description": "Volatility and risk factors",
    },
}

VALIDATION_FEATURE_BASE_COLUMNS = [
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "adjust",
    "feature_set",
    "feature_name",
    "feature_value",
]

VALIDATION_OHLCV_BASE_COLUMNS = [
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "adjust",
    "close",
]

VALIDATION_FEATURE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


def classify_factor(factor_name):
    """将 Alpha158 因子名分类到 trend/quality/risk/validated 组件。

    Args:
        factor_name: e.g. "MA30", "CNTD20", "KMID", "VOLUME5"

    Returns:
        str: one of "trend", "quality", "risk", "validated"
    """
    import re

    name = str(factor_name).strip().upper()
    if not name:
        return "validated"

    # Volume raw level columns: VOLUME0, VOLUME5, ...
    if name.startswith(FACTOR_CLASSIFICATION_RULES["quality"]["volume_prefix"]):
        return "quality"

    # Kbar factors: KMID, KLEN, KMID2, ...
    if name in FACTOR_CLASSIFICATION_RULES["quality"]["kbar_operators"]:
        return "quality"

    # Price level columns: OPEN0, HIGH2, CLOSE5, VWAP3, ...
    for pf in FACTOR_CLASSIFICATION_RULES["trend"]["price_fields"]:
        if name.startswith(pf):
            return "trend"

    # Operator-based factors: parse {OPERATOR}{WINDOW} pattern
    match = re.match(r"([A-Z]+)(\d+)", name)
    if match:
        operator = match.group(1)
        for component, rules in FACTOR_CLASSIFICATION_RULES.items():
            if component == "validated":
                continue
            if operator in rules.get("operators", set()):
                return component

    return "validated"


class StockAnalyzer:
    """股票技术分析器"""

    def __init__(self, db_dir="./assets", buy_strategy=None, sell_strategy=None, signal_recipes=None):
        """
        初始化分析器

        Args:
            db_dir (str): 数据库目录
            buy_strategy: 买入策略实例
            sell_strategy: 卖出策略实例
            signal_recipes: 信号 recipe 名称列表
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
        self.signal_recipes = tuple(signal_recipes or DEFAULT_SIGNAL_RECIPES)
        self.signal_recipe_runner = SignalRecipeRunner(self.signal_recipes)

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
            batch_map = self.load_stock_data_batch([stock_code], days=days)
            return batch_map.get(stock_code)
        except Exception as e:
            print(f"[ERROR] 加载股票 {stock_code} 数据失败: {e}")
            return None

    @staticmethod
    def _normalize_loaded_ohlcv_frame(warehouse_df):
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

    def load_stock_data_batch(self, stock_codes, days=365):
        """批量加载多只股票的历史数据，减少并发 parquet 打开次数。"""
        normalized_codes = [str(code).strip() for code in (stock_codes or []) if str(code).strip()]
        if not normalized_codes:
            return {}

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        warehouse_df = self.market_warehouse.read_ohlcv(
            stock_code=normalized_codes,
            market="HK",
            asset_type="equity",
            frequency="daily",
            adjust="qfq",
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d'),
        )
        if warehouse_df is None or warehouse_df.empty:
            return {}

        stock_data_map = {}
        for stock_code, stock_frame in warehouse_df.groupby("stock_code", sort=False):
            normalized_frame = self._normalize_loaded_ohlcv_frame(stock_frame)
            if normalized_frame is not None and not normalized_frame.empty:
                stock_data_map[str(stock_code)] = normalized_frame
        return stock_data_map

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
    def _parse_scoring_factors_to_alpha158_config(validated_feature_names):
        """从评分因子名列表反推最小化 Alpha158 计算配置，减少约 70% 算子计算。"""
        needed_operators = set()
        needed_windows = set()
        for name in (validated_feature_names or []):
            match = re.match(r"([A-Z]+)(\d+)", str(name))
            if match:
                needed_operators.add(match.group(1))
                needed_windows.add(int(match.group(2)))
        if not needed_operators or not needed_windows:
            return {}
        return {
            "price": {"windows": [], "feature": []},
            "volume": {"windows": []},
            "rolling": {
                "windows": sorted(needed_windows),
                "include": list(needed_operators),
            },
        }

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

    def _compute_factor_scores(self, feature_frame, factor_set=DEFAULT_FACTOR_SET, score_config=None, ridge_factors=None):
        if feature_frame is None or feature_frame.empty:
            return pd.DataFrame(), {}

        # New: Ridge-weighted cross-sectional path
        if ridge_factors is not None and not ridge_factors.empty:
            return self._compute_ridge_cross_sectional_scores(feature_frame, factor_set, ridge_factors)

        working = feature_frame.copy()
        config = score_config or DEFAULT_FACTOR_SCORE_CONFIG
        component_names = [k for k in config if k != "weights"]
        factor_details = {
            "factor_set": factor_set,
            "component_weights": dict(config.get("weights", DEFAULT_FACTOR_SCORE_CONFIG["weights"])),
            "factors": {},
        }

        component_frames = {}
        for component_name in component_names:
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

        composite_weights = config.get("weights", DEFAULT_FACTOR_SCORE_CONFIG["weights"])
        composite_score = pd.Series(0.0, index=working.index)
        for comp_name in component_names:
            comp_score = component_frames[comp_name].clip(0, 100)
            comp_weight = float(composite_weights.get(f"{comp_name}_score", 0.0))
            composite_score = composite_score + comp_score * comp_weight
        composite_score = composite_score.clip(0, 100)

        result_data = {"composite_score": composite_score}
        for comp_name in component_names:
            result_data[f"{comp_name}_score"] = component_frames[comp_name].clip(0, 100)
        # Ensure backward-compat trend/quality/risk columns exist
        for default_comp in ("trend", "quality", "risk"):
            if f"{default_comp}_score" not in result_data:
                result_data[f"{default_comp}_score"] = np.nan

        result = pd.DataFrame(result_data, index=working.index)
        result["factor_set"] = factor_set
        return result, factor_details

    def _compute_ridge_cross_sectional_scores(self, feature_frame, factor_set, ridge_factors):
        """用 Ridge 系数做横截面打分 — 替代组件分桶逻辑。

        对每个交易日，对每个 Ridge 选中的因子：
          1. 全市场横截面 z-score
          2. 乘以 ridge_coef（方向 + 量级）
          3. 按组件聚合得 trend/quality/risk/validated 子分数
          4. 全量求和得 composite_score

        当只有单只股票时回退到滚动时序 z-score + Ridge 权重。

        Returns (result_df, factor_details) matching _compute_factor_scores contract.
        """
        import re

        working = feature_frame.copy()
        factor_columns = [c for c in ridge_factors["feature_name"].tolist() if c in working.columns]
        if not factor_columns:
            return pd.DataFrame(), {}

        score_components = ridge_factors.set_index("feature_name")
        components_present = sorted(set(
            score_components.loc[score_components.index.isin(factor_columns), "component"].dropna()
        ))
        component_weights = {"trend_score": 0.40, "quality_score": 0.30, "risk_score": 0.15, "validated_score": 0.15}

        unique_dates = working.index.unique() if isinstance(working.index, pd.DatetimeIndex) else pd.Index([])
        is_single_stock = len(unique_dates) == 0 or (
            "stock_code" not in getattr(working, "columns", pd.Index([]))
            and len(working) <= len(unique_dates) * 2
        )

        composite = pd.Series(np.nan, index=working.index, dtype=float)
        component_scores = {comp: pd.Series(np.nan, index=working.index, dtype=float) for comp in components_present}

        if is_single_stock and len(unique_dates) > 0:
            # Fallback: per-stock rolling time-series z-score with Ridge weights
            any_valid_contribution = False
            for col_name in factor_columns:
                row = score_components.loc[col_name]
                coef = row["ridge_coef"]
                component = row.get("component", "validated")
                raw = pd.to_numeric(working[col_name], errors="coerce")
                rolling_mean = raw.rolling(window=120, min_periods=30).mean()
                rolling_std = raw.rolling(window=120, min_periods=30).std().replace(0, np.nan)
                zscore = ((raw - rolling_mean) / rolling_std).clip(-3, 3)
                contribution = zscore * coef
                valid = contribution.notna()
                if valid.any():
                    any_valid_contribution = True
                    composite = composite.fillna(0) + contribution.fillna(0)
                    if component in component_scores:
                        component_scores[component] = component_scores[component].fillna(0) + contribution.fillna(0)
            if not any_valid_contribution:
                composite = pd.Series(np.nan, index=working.index, dtype=float)
                for comp in components_present:
                    component_scores[comp] = pd.Series(np.nan, index=working.index, dtype=float)
        else:
            for trade_date in unique_dates:
                date_mask = working.index == trade_date
                row_count = date_mask.sum()
                if row_count < 2:
                    continue

                date_composite = 0.0
                date_components = {comp: 0.0 for comp in components_present}

                for col_name in factor_columns:
                    row = score_components.loc[col_name]
                    coef = row["ridge_coef"]
                    component = row.get("component", "validated")
                    raw = pd.to_numeric(working.loc[date_mask, col_name], errors="coerce")
                    valid = raw.notna()
                    if valid.sum() < 2:
                        continue
                    date_mean = raw.mean()
                    date_std = raw.std(ddof=1)
                    if date_std == 0 or np.isnan(date_std):
                        continue
                    zscore = ((raw - date_mean) / date_std).clip(-3, 3).fillna(0)
                    contribution = zscore * coef
                    date_composite += contribution.values
                    if component in date_components:
                        date_components[component] = date_components[component] + contribution.values

                composite.loc[date_mask] = date_composite
                for comp, contrib in date_components.items():
                    component_scores[comp].loc[date_mask] = contrib

        # Normalize component sub-scores to 0-100 using percentile
        for comp in components_present:
            raw = pd.to_numeric(component_scores[comp], errors="coerce")
            finite = raw[np.isfinite(raw)]
            if len(finite) >= 2:
                pct = raw.rank(pct=True) * 100
                component_scores[comp] = pct.clip(0, 100)
            else:
                component_scores[comp] = pd.Series(np.nan, index=raw.index, dtype=float)

        composite_raw = pd.to_numeric(composite, errors="coerce")
        finite_c = composite_raw[np.isfinite(composite_raw)]
        if len(finite_c) >= 2:
            composite_pct = composite_raw.rank(pct=True) * 100
            composite_pct = composite_pct.clip(0, 100)
        else:
            composite_pct = pd.Series(np.nan, index=composite_raw.index, dtype=float)

        result = pd.DataFrame(
            {"composite_score": composite_pct},
            index=working.index,
        )
        for comp in components_present:
            result[f"{comp}_score"] = component_scores[comp]
        # Ensure all four component columns exist for backward compat
        for default_comp in ("trend", "quality", "risk"):
            if f"{default_comp}_score" not in result.columns:
                result[f"{default_comp}_score"] = np.nan

        result["factor_set"] = factor_set

        factor_details = {
            "factor_set": factor_set,
            "component_weights": component_weights,
            "ridge_factors": ridge_factors.to_dict(orient="records"),
        }
        return result, factor_details

    @staticmethod
    def _select_top_ridge_factors(factor_scorecard, top_k=30, min_abs_coef=0.0):
        """从 Ridge 评分卡中选择 Top-K 因子用于横截面打分。

        Args:
            factor_scorecard: DataFrame from _build_factor_scorecard_ridge()
            top_k: max number of factors to select
            min_abs_coef: minimum |ridge_coef| threshold

        Returns:
            pd.DataFrame with [feature_name, ridge_coef, abs_ridge_coef, higher_is_better, component]
        """
        if factor_scorecard is None or factor_scorecard.empty:
            return pd.DataFrame(columns=["feature_name", "ridge_coef", "abs_ridge_coef", "higher_is_better", "component"])

        working = factor_scorecard.copy()
        if "ridge_coef" not in working.columns and "abs_ridge_coef" not in working.columns:
            return pd.DataFrame(columns=["feature_name", "ridge_coef", "abs_ridge_coef", "higher_is_better", "component"])

        if "abs_ridge_coef" not in working.columns:
            working["abs_ridge_coef"] = working["ridge_coef"].abs()
        if "higher_is_better" not in working.columns:
            working["higher_is_better"] = working["ridge_coef"].fillna(0) > 0
        if "component" not in working.columns:
            working["component"] = working["feature_name"].apply(classify_factor)

        working = working[working["abs_ridge_coef"] >= min_abs_coef]
        working = working.sort_values("abs_ridge_coef", ascending=False)
        working = working.head(int(top_k))

        keep_cols = ["feature_name", "ridge_coef", "abs_ridge_coef", "higher_is_better", "component"]
        return working[[c for c in keep_cols if c in working.columns]].reset_index(drop=True)

    @staticmethod
    def _prune_redundant_factors(factor_scorecard, corr_matrix, threshold=0.80):
        """移除冗余因子 — 贪婪算法按 |ridge_coef| 排序逐一遍历。

        Args:
            factor_scorecard: DataFrame with [feature_name, abs_ridge_coef]
            corr_matrix: factor×factor correlation DataFrame
            threshold: pairwise correlation above which a factor is pruned

        Returns:
            list of retained factor names
        """
        if factor_scorecard is None or factor_scorecard.empty:
            return []
        if corr_matrix is None or corr_matrix.empty:
            return factor_scorecard["feature_name"].tolist()

        sorted_factors = factor_scorecard.sort_values("abs_ridge_coef", ascending=False)["feature_name"].tolist()
        corr_factors = [f for f in sorted_factors if f in corr_matrix.index and f in corr_matrix.columns]
        if len(corr_factors) < 2:
            return sorted_factors

        retained = []
        for factor_name in corr_factors:
            keep = True
            for accepted in retained:
                corr_val = abs(corr_matrix.loc[factor_name, accepted])
                if pd.notna(corr_val) and corr_val > threshold:
                    keep = False
                    break
            if keep:
                retained.append(factor_name)

        # Append factors not in correlation matrix (no pruning info)
        for factor_name in sorted_factors:
            if factor_name not in retained and factor_name not in corr_factors:
                retained.append(factor_name)

        return retained

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
    def _slice_factor_details(factor_details, row_mask):
        if not factor_details:
            return factor_details

        mask_series = pd.Series(np.asarray(row_mask, dtype=bool))
        sliced = {
            "factor_set": factor_details.get("factor_set"),
            "component_weights": dict(factor_details.get("component_weights", {})),
            "factors": {},
        }
        for factor_name, meta in (factor_details.get("factors") or {}).items():
            factor_meta = dict(meta or {})
            raw_series = factor_meta.get("raw_series")
            score_series = factor_meta.get("score_series")
            if isinstance(raw_series, pd.Series) and len(raw_series) == len(mask_series):
                factor_meta["raw_series"] = raw_series.iloc[mask_series.to_numpy()].copy()
            if isinstance(score_series, pd.Series) and len(score_series) == len(mask_series):
                factor_meta["score_series"] = score_series.iloc[mask_series.to_numpy()].copy()
            sliced["factors"][factor_name] = factor_meta
        return sliced

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

    @staticmethod
    def _build_low_price_setup_snapshot(data):
        return SignalRecipeRunner(DEFAULT_SIGNAL_RECIPES).evaluate(data)

    def _build_signal_setup_snapshot(self, data, context=None):
        return self.signal_recipe_runner.evaluate(data, context=context)

    @staticmethod
    def _available_memory_bytes():
        try:
            if Path("/proc/meminfo").exists():
                for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
        except Exception:
            pass
        return None

    @classmethod
    def _resolve_safe_validation_workers(cls, requested_workers, validation_factor_scope="all"):
        requested = int(requested_workers or 0)
        cpu_count = max(int(os.cpu_count() or 1), 1)
        if requested <= 0:
            requested = cpu_count
        available_bytes = cls._available_memory_bytes()
        available_gb = (available_bytes / (1024 ** 3)) if available_bytes else None
        cpu_cap = max(1, cpu_count - 1)
        if str(validation_factor_scope) == "all":
            cpu_cap = min(cpu_cap, 8)
        else:
            cpu_cap = min(cpu_cap, 16)

        if available_gb is None:
            return min(requested, cpu_cap)

        per_worker_gb = 1.5 if str(validation_factor_scope) == "all" else 0.75
        memory_cap = max(1, int(available_gb / per_worker_gb))
        if str(validation_factor_scope) == "all":
            memory_cap = min(memory_cap, 8)
        else:
            memory_cap = min(memory_cap, 16)
        return max(1, min(requested, cpu_cap, memory_cap))

    @classmethod
    def _resolve_safe_analysis_workers(cls, requested_workers, analysis_mode="factor"):
        requested = int(requested_workers or 0)
        cpu_count = max(int(os.cpu_count() or 1), 1)
        if requested <= 0:
            requested = cpu_count
        cpu_cap = max(1, cpu_count - 1)
        if str(analysis_mode) == "factor":
            cpu_cap = min(cpu_cap, 12)
        else:
            cpu_cap = min(cpu_cap, 16)

        available_bytes = cls._available_memory_bytes()
        if available_bytes is None:
            return max(1, min(requested, cpu_cap))

        available_gb = available_bytes / (1024 ** 3)
        per_worker_gb = 1.0 if str(analysis_mode) == "factor" else 0.5
        memory_cap = max(1, int(available_gb / per_worker_gb))
        return max(1, min(requested, cpu_cap, memory_cap))

    @classmethod
    def _resolve_factor_analysis_batch_size(cls, total_stocks, max_workers, analysis_mode="factor"):
        total = max(int(total_stocks or 0), 0)
        workers = max(int(max_workers or 1), 1)
        normalized_mode = str(analysis_mode or "factor").strip().lower()
        if total <= 1 or normalized_mode != "factor":
            return max(1, total)

        available_bytes = cls._available_memory_bytes()
        if available_bytes is None:
            available_gb = None
        else:
            available_gb = available_bytes / (1024 ** 3)

        # Keep multiple waves per worker for smoother progress and more balanced completion.
        target_waves = 3 if total >= workers * 12 else 2
        baseline = int(np.ceil(total / max(workers * target_waves, 1)))

        if available_gb is None:
            memory_cap = 96
        elif available_gb <= 2:
            memory_cap = 16
        elif available_gb <= 4:
            memory_cap = 24
        elif available_gb <= 8:
            memory_cap = 48
        elif available_gb <= 16:
            memory_cap = 96
        else:
            memory_cap = 128

        if total <= workers * 2:
            return max(1, int(np.ceil(total / workers)))

        lower_bound = 8 if total >= workers * 4 else 4
        return max(lower_bound, min(memory_cap, baseline, total))

    @staticmethod
    def _emit_progress_line(
        *,
        prefix,
        completed,
        total,
        success_count,
        started_at,
        stream=None,
        extra_fields=None,
    ):
        target_stream = stream or sys.stderr
        total = max(int(total or 0), 1)
        completed = max(int(completed or 0), 0)
        success_count = max(int(success_count or 0), 0)
        elapsed = max(time.time() - started_at, 1e-9)
        rate = completed / elapsed if completed > 0 else 0.0
        remaining = max(total - completed, 0)
        eta = remaining / rate if rate > 0 else 0.0
        fields = [
            f"stocks_done={completed}/{total}",
            f"({completed / total:.1%})",
            f"success={success_count}",
        ]
        if extra_fields:
            for name, value in extra_fields:
                fields.append(f"{name}={value}")
        fields.extend(
            [
                f"rate={rate:.1f}/s",
                f"elapsed={elapsed:.1f}s",
                f"eta={eta:.1f}s",
            ]
        )
        print(
            "\r" + prefix + " " + " ".join(fields),
            end="",
            flush=True,
            file=target_stream,
        )

    @staticmethod
    def _signal_freshness_score(latest_signal_date, latest_data_date=None):
        if latest_signal_date is None or pd.isna(latest_signal_date):
            return 0.0, 999
        signal_date = pd.Timestamp(latest_signal_date).tz_localize(None).normalize()
        if latest_data_date is None or pd.isna(latest_data_date):
            reference_date = pd.Timestamp.now("UTC").tz_localize(None).normalize()
        else:
            reference_date = pd.Timestamp(latest_data_date).tz_localize(None).normalize()
        signal_age_days = max(int((reference_date - signal_date).days), 0)
        freshness_score = max(0.0, 100.0 - signal_age_days * 4.0)
        return float(freshness_score), int(signal_age_days)

    @staticmethod
    def _trim_validation_feature_frame(feature_long):
        if feature_long is None or feature_long.empty:
            return pd.DataFrame(columns=VALIDATION_FEATURE_BASE_COLUMNS)

        keep_columns = [column for column in VALIDATION_FEATURE_BASE_COLUMNS if column in feature_long.columns]
        trimmed = feature_long[keep_columns].copy()
        for column in ("stock_code", "market", "exchange", "asset_type", "frequency", "adjust", "feature_set", "feature_name"):
            if column in trimmed.columns and trimmed[column].dtype == object:
                trimmed[column] = trimmed[column].astype("category")
        return trimmed

    @staticmethod
    def _trim_validation_ohlcv_frame(ohlcv_frame):
        if ohlcv_frame is None or ohlcv_frame.empty:
            return pd.DataFrame(columns=VALIDATION_OHLCV_BASE_COLUMNS)

        keep_columns = [column for column in VALIDATION_OHLCV_BASE_COLUMNS if column in ohlcv_frame.columns]
        trimmed = ohlcv_frame[keep_columns].copy()
        for column in ("stock_code", "market", "exchange", "asset_type", "frequency", "adjust"):
            if column in trimmed.columns and trimmed[column].dtype == object:
                trimmed[column] = trimmed[column].astype("category")
        return trimmed

    def get_validation_feature_cache_dir(self):
        return self.data_layout.layer_path("meta") / "validation_feature_cache"

    @staticmethod
    def _build_validation_feature_cache_key(stock_code, days, factor_set, validation_factor_scope="all", validated_feature_names=None):
        identity = {
            "stock_code": str(stock_code),
            "days": int(days),
            "factor_set": str(factor_set),
            "validation_factor_scope": str(validation_factor_scope),
            "validated_feature_names": [str(item) for item in (validated_feature_names or []) if str(item).strip()],
        }
        cache_key = hashlib.sha1(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        return cache_key, identity

    @staticmethod
    def _is_validation_feature_cache_fresh(cache_path, ttl_seconds=VALIDATION_FEATURE_CACHE_TTL_SECONDS):
        path = Path(cache_path)
        if not path.exists():
            return False
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            return False
        return (time.time() - modified_at) <= int(ttl_seconds)

    def _load_validation_feature_cache(self, cache_path):
        path = Path(cache_path)
        if not path.exists():
            return None
        payload = pd.read_pickle(path)
        if not isinstance(payload, dict):
            return None
        feature_frame = self._trim_validation_feature_frame(payload.get("feature_frame"))
        ohlcv_frame = self._trim_validation_ohlcv_frame(payload.get("ohlcv_frame"))
        if feature_frame.empty or ohlcv_frame.empty:
            return None
        return {
            "stock_code": payload.get("stock_code"),
            "feature_frame": feature_frame,
            "ohlcv_frame": ohlcv_frame,
            "feature_rows": int(payload.get("feature_rows", len(feature_frame))),
            "feature_names": int(payload.get("feature_names", feature_frame["feature_name"].nunique() if "feature_name" in feature_frame.columns else 0)),
            "date_count": int(payload.get("date_count", feature_frame["trade_date"].nunique() if "trade_date" in feature_frame.columns else 0)),
            "start_date": payload.get("start_date", feature_frame["trade_date"].min() if not feature_frame.empty else pd.NaT),
            "end_date": payload.get("end_date", feature_frame["trade_date"].max() if not feature_frame.empty else pd.NaT),
            "cache_hit": True,
        }

    def _write_validation_feature_cache(self, cache_path, payload):
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle(payload, path)
        return path

    @classmethod
    def _estimate_safe_validation_stock_count(cls, pool_results, row_safety_fraction=0.30, bytes_per_row=320):
        results = list(pool_results or [])
        if not results:
            return 0

        available_bytes = cls._available_memory_bytes()
        if available_bytes is None:
            return len(results)

        total_rows = sum(max(int(item.get("feature_rows", 0) or 0), 0) for item in results)
        if total_rows <= 0:
            return len(results)

        row_budget = max(int((available_bytes * float(row_safety_fraction)) / max(int(bytes_per_row), 1)), 1)
        if total_rows <= row_budget:
            return len(results)

        avg_rows_per_stock = max(total_rows / max(len(results), 1), 1)
        safe_count = max(32, int(row_budget / avg_rows_per_stock))
        return max(1, min(len(results), safe_count))

    @staticmethod
    def _downsample_validation_pool_results(pool_results, target_count):
        results = list(pool_results or [])
        if target_count is None or target_count <= 0 or len(results) <= target_count:
            return results
        if target_count == 1:
            return [results[0]]

        indices = np.linspace(0, len(results) - 1, num=int(target_count), dtype=int)
        selected = []
        seen = set()
        for index in indices.tolist():
            if index in seen:
                continue
            selected.append(results[index])
            seen.add(index)
        return selected

    @classmethod
    def _resolve_validation_batch_size(cls, validation_factor_scope="all", requested_workers=1):
        available_bytes = cls._available_memory_bytes()
        available_gb = (available_bytes / (1024 ** 3)) if available_bytes else None
        if str(validation_factor_scope) == "all":
            default_batch = 24
            if available_gb is None:
                return default_batch
            if available_gb <= 4:
                return 8
            if available_gb <= 8:
                return 12
            if available_gb <= 16:
                return 16
            return default_batch
        default_batch = 96
        if available_gb is None:
            return default_batch
        if available_gb <= 4:
            return 24
        if available_gb <= 8:
            return 48
        return default_batch

    def _iter_factor_validation_batches(
        self,
        stock_codes,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        validation_factor_scope="all",
        validated_feature_names=None,
        batch_size=None,
        max_workers=1,
        show_progress=False,
    ):
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return

        validated_feature_names = [str(item) for item in (validated_feature_names or []) if str(item).strip()]
        validated_feature_name_set = set(validated_feature_names)
        restricted_config = {}
        if validated_feature_name_set and factor_set == "qlib_alpha158":
            restricted_config = self._parse_scoring_factors_to_alpha158_config(validated_feature_name_set)
            if restricted_config and show_progress:
                ops = restricted_config.get("rolling", {}).get("include", [])
                wins = restricted_config.get("rolling", {}).get("windows", [])
                print(
                    f"[PROGRESS] validation factor_config restricted "
                    f"operators={ops} windows={wins}"
                )
        factor_set_config = restricted_config
        cache_dir = self.get_validation_feature_cache_dir()

        def run_analysis(stock_code):
            try:
                cache_key, _ = self._build_validation_feature_cache_key(
                    stock_code=stock_code,
                    days=days,
                    factor_set=factor_set,
                    validation_factor_scope=validation_factor_scope,
                    validated_feature_names=validated_feature_names,
                )
                cache_path = Path(cache_dir) / f"{cache_key}.pkl"
                if self._is_validation_feature_cache_fresh(cache_path):
                    cached_result = self._load_validation_feature_cache(cache_path)
                    if cached_result is not None:
                        return cached_result

                warmup_days = max(days + 180, days)
                full_data = self.load_stock_data(stock_code, warmup_days)
                if full_data is None or full_data.empty:
                    return None

                ohlcv_frame = normalize_ohlcv_frame(
                    full_data.reset_index(),
                    stock_code=stock_code,
                    market="HK",
                )
                factor = create_factor_set(factor_set, config=factor_set_config)
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
                feature_long = self._trim_validation_feature_frame(feature_long)
                ohlcv_frame = self._trim_validation_ohlcv_frame(ohlcv_frame)
                result = {
                    "stock_code": stock_code,
                    "feature_frame": feature_long,
                    "ohlcv_frame": ohlcv_frame,
                    "feature_rows": len(feature_long),
                    "feature_names": feature_long["feature_name"].nunique() if not feature_long.empty else 0,
                    "date_count": feature_long["trade_date"].nunique() if not feature_long.empty else 0,
                    "start_date": feature_long["trade_date"].min() if not feature_long.empty else pd.NaT,
                    "end_date": feature_long["trade_date"].max() if not feature_long.empty else pd.NaT,
                }
                self._write_validation_feature_cache(
                    cache_path,
                    {
                        "stock_code": stock_code,
                        "feature_frame": feature_long,
                        "ohlcv_frame": ohlcv_frame,
                        "feature_rows": result["feature_rows"],
                        "feature_names": result["feature_names"],
                        "date_count": result["date_count"],
                        "start_date": result["start_date"],
                        "end_date": result["end_date"],
                    },
                )
                return result
            except Exception:
                import traceback
                print(f"\n[ERROR] 因子验证 {stock_code} 异常:", flush=True)
                traceback.print_exc()
                return None

        batch_size = max(int(batch_size or 1), 1)
        started_at = time.time()
        completed = 0
        success_count = 0
        pending_results = []

        def flush_pending():
            nonlocal pending_results
            if not pending_results:
                return None
            batch_feature_frames = [item["feature_frame"] for item in pending_results if item.get("feature_frame") is not None and not item["feature_frame"].empty]
            batch_ohlcv_frames = [item["ohlcv_frame"] for item in pending_results if item.get("ohlcv_frame") is not None and not item["ohlcv_frame"].empty]
            batch_payload = {
                "feature_frame": pd.concat(batch_feature_frames, ignore_index=True) if batch_feature_frames else pd.DataFrame(columns=VALIDATION_FEATURE_BASE_COLUMNS),
                "ohlcv_frame": pd.concat(batch_ohlcv_frames, ignore_index=True) if batch_ohlcv_frames else pd.DataFrame(columns=VALIDATION_OHLCV_BASE_COLUMNS),
                "stock_results": [
                    {
                        "stock_code": item.get("stock_code"),
                        "feature_rows": int(item.get("feature_rows", 0)),
                        "feature_names": int(item.get("feature_names", 0)),
                        "date_count": int(item.get("date_count", 0)),
                        "start_date": item.get("start_date"),
                        "end_date": item.get("end_date"),
                    }
                    for item in pending_results
                ],
            }
            pending_results = []
            return batch_payload

        if max_workers == 1 or len(stock_codes) <= 1:
            for stock_code in stock_codes:
                result = run_analysis(stock_code)
                completed += 1
                if result is not None:
                    pending_results.append(result)
                    success_count += 1
                if show_progress:
                    elapsed = max(time.time() - started_at, 1e-9)
                    rate = completed / elapsed
                    remaining = len(stock_codes) - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    print(
                        f"\r[PROGRESS] validation {completed}/{len(stock_codes)} "
                        f"({completed / len(stock_codes):.1%}) success={success_count} "
                        f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                    , end="", flush=True, file=sys.stderr)
                if len(pending_results) >= batch_size:
                    batch_payload = flush_pending()
                    if batch_payload is not None:
                        yield batch_payload
            if show_progress:
                print(file=sys.stderr)
        else:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(stock_codes))) as executor:
                future_map = {executor.submit(run_analysis, stock_code): stock_code for stock_code in stock_codes}
                for future in as_completed(future_map):
                    stock_code = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        print(f"\n[ERROR] 因子验证 {stock_code} 失败: {exc}")
                        completed += 1
                        if show_progress:
                            elapsed = max(time.time() - started_at, 1e-9)
                            rate = completed / elapsed
                            remaining = len(stock_codes) - completed
                            eta = remaining / rate if rate > 0 else 0.0
                            print(
                                f"\r[PROGRESS] validation {completed}/{len(stock_codes)} "
                                f"({completed / len(stock_codes):.1%}) success={success_count} "
                                f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                            , end="", flush=True, file=sys.stderr)
                        continue
                    completed += 1
                    if result is not None:
                        pending_results.append(result)
                        success_count += 1
                    if show_progress:
                        elapsed = max(time.time() - started_at, 1e-9)
                        rate = completed / elapsed
                        remaining = len(stock_codes) - completed
                        eta = remaining / rate if rate > 0 else 0.0
                        print(
                            f"\r[PROGRESS] validation {completed}/{len(stock_codes)} "
                            f"({completed / len(stock_codes):.1%}) success={success_count} "
                            f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                        , end="", flush=True, file=sys.stderr)
                    if len(pending_results) >= batch_size:
                        batch_payload = flush_pending()
                        if batch_payload is not None:
                            yield batch_payload
            if show_progress:
                print(file=sys.stderr)

        batch_payload = flush_pending()
        if batch_payload is not None:
            yield batch_payload

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
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return []

        warmup_days = max(days + 180, days)
        batch_results = []
        feature_frames = []
        batch_data_map = self.load_stock_data_batch(stock_codes, warmup_days)

        for stock_code in stock_codes:
            full_data = batch_data_map.get(stock_code)
            if full_data is None or full_data.empty or len(full_data) < 60:
                if callable(progress_callback):
                    progress_callback(stock_code)
                continue

            ohlcv_frame = full_data.reset_index().rename(columns={"date": "trade_date"})
            factor = create_factor_set(factor_set)
            context = FactorContext(stock_code=stock_code, market="HK", frequency="daily", adjust="qfq")
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
                self._build_factor_explanation(stock_factor_details, stock_panel_scores, latest_score_index)
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
        warmup_days = max(days + 180, days)
        full_data = self.load_stock_data(stock_code, warmup_days)
        if full_data is None or full_data.empty:
            return None

        MIN_TRADING_DAYS = 60
        if len(full_data) < MIN_TRADING_DAYS:
            return None

        ohlcv_frame = full_data.reset_index().rename(columns={"date": "trade_date"})
        factor = create_factor_set(factor_set)
        context = FactorContext(stock_code=stock_code, market="HK", frequency="daily", adjust="qfq")
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

    def build_signal_recipe_report(
        self,
        stock_codes=None,
        days=365,
        signal_recipes=None,
        horizons=(20, 40, 60),
        max_workers=1,
        show_progress=False,
        min_history_days=60,
        signal_cooldown_days=20,
        signal_event_policy="first",
    ):
        """评估信号 recipe 触发后的 forward return 表现。"""
        if stock_codes is None:
            stock_codes = self.get_all_stocks()
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return None

        horizons = tuple(int(horizon) for horizon in horizons)
        warmup_days = max(days + int(max(horizons or (0,))) + int(min_history_days), days)
        recipe_names = tuple(signal_recipes or self.signal_recipes)
        rows = []

        def evaluate_stock(stock_code):
            data = self.load_stock_data(stock_code, warmup_days)
            if data is None or data.empty or len(data) < min_history_days:
                return []
            data = data.copy().sort_index()
            start_idx = max(len(data) - days, min_history_days)
            stock_rows = []
            for index in range(start_idx, len(data)):
                history = data.iloc[: index + 1]
                if len(history) < min_history_days:
                    continue
                for recipe_name in recipe_names:
                    snapshot = SignalRecipeRunner((recipe_name,)).evaluate(
                        history,
                        context={"stock_code": stock_code, "analysis_mode": "signal_report"},
                    )
                    setup_type = snapshot.get("setup_type", "neutral")
                    if setup_type in {"neutral", "sideways"}:
                        continue
                    event = {
                        "stock_code": stock_code,
                        "date": history.index[-1],
                        "recipe_name": recipe_name,
                        "setup_type": setup_type,
                        "setup_score": float(snapshot.get("setup_score", 0.0) or 0.0),
                        "sideways_penalty": float(snapshot.get("sideways_penalty", 0.0) or 0.0),
                        "close": float(history["Close"].iloc[-1]),
                    }
                    event.update(self._compute_signal_forward_metrics(data, index, horizons))
                    stock_rows.append(event)
            return stock_rows

        started_at = time.time()
        completed = 0
        if max_workers and int(max_workers) > 1 and len(stock_codes) > 1:
            with ThreadPoolExecutor(max_workers=min(int(max_workers), len(stock_codes))) as executor:
                future_map = {executor.submit(evaluate_stock, stock_code): stock_code for stock_code in stock_codes}
                for future in as_completed(future_map):
                    stock_code = future_map[future]
                    try:
                        rows.extend(future.result())
                    except Exception as exc:
                        print(f"\n[ERROR] 信号评估 {stock_code} 失败: {exc}")
                    completed += 1
                    if show_progress:
                        self._emit_progress_line(
                            prefix="[PROGRESS] signal_report",
                            completed=completed,
                            total=len(stock_codes),
                            success_count=len(rows),
                            started_at=started_at,
                        )
        else:
            for stock_code in stock_codes:
                rows.extend(evaluate_stock(stock_code))
                completed += 1
                if show_progress:
                    self._emit_progress_line(
                        prefix="[PROGRESS] signal_report",
                        completed=completed,
                        total=len(stock_codes),
                        success_count=len(rows),
                        started_at=started_at,
                    )
        if show_progress:
            print(file=sys.stderr)

        events_raw = pd.DataFrame(rows)
        events = self._merge_signal_recipe_events(
            events_raw,
            cooldown_days=signal_cooldown_days,
            event_policy=signal_event_policy,
        )
        summary = self._summarize_signal_recipe_events(events, horizons)
        return {
            "metadata": {
                "stock_count": len(stock_codes),
                "raw_event_count": len(events_raw),
                "event_count": len(events),
                "days": days,
                "signal_recipes": recipe_names,
                "horizons": horizons,
                "signal_cooldown_days": int(signal_cooldown_days),
                "signal_event_policy": str(signal_event_policy),
            },
            "summary": summary,
            "events": events,
            "events_raw": events_raw,
        }

    @staticmethod
    def _compute_signal_forward_metrics(data, event_index, horizons):
        close = pd.to_numeric(data["Close"], errors="coerce")
        low = pd.to_numeric(data["Low"], errors="coerce") if "Low" in data.columns else close
        entry_close = float(close.iloc[event_index])
        metrics = {}
        for horizon in horizons:
            horizon = int(horizon)
            end_index = min(event_index + horizon, len(data) - 1)
            if end_index <= event_index or not np.isfinite(entry_close) or entry_close == 0:
                metrics[f"forward_return_{horizon}"] = np.nan
                metrics[f"forward_max_drawdown_{horizon}"] = np.nan
                continue
            future_close = float(close.iloc[end_index])
            future_low = low.iloc[event_index + 1 : end_index + 1]
            future_min_low = float(future_low.min()) if not future_low.dropna().empty else np.nan
            metrics[f"forward_return_{horizon}"] = future_close / entry_close - 1.0 if np.isfinite(future_close) else np.nan
            metrics[f"forward_max_drawdown_{horizon}"] = future_min_low / entry_close - 1.0 if np.isfinite(future_min_low) else np.nan
        return metrics

    @staticmethod
    def _merge_signal_recipe_events(events, cooldown_days=20, event_policy="first"):
        if events is None or events.empty:
            return pd.DataFrame() if events is None else events.copy()

        cooldown_days = max(int(cooldown_days or 0), 0)
        event_policy = str(event_policy or "first").strip().lower()
        if event_policy not in {"first", "latest", "best_score"}:
            raise ValueError(f"unsupported signal_event_policy: {event_policy}")

        working = events.copy()
        working["date"] = pd.to_datetime(working["date"])
        working.sort_values(["stock_code", "recipe_name", "setup_type", "date"], inplace=True)

        merged_rows = []
        zone_counter = 0
        group_columns = ["stock_code", "recipe_name", "setup_type"]
        for (stock_code, recipe_name, setup_type), group in working.groupby(group_columns, dropna=False):
            current_zone_rows = []
            last_date = None

            def flush_zone():
                nonlocal zone_counter
                if not current_zone_rows:
                    return
                zone = pd.DataFrame(current_zone_rows)
                if event_policy == "latest":
                    selected = zone.sort_values("date").iloc[-1].copy()
                elif event_policy == "best_score":
                    selected = zone.sort_values(["setup_score", "date"], ascending=[False, True]).iloc[0].copy()
                else:
                    selected = zone.sort_values("date").iloc[0].copy()
                zone_counter += 1
                selected["signal_zone_id"] = f"{stock_code}:{recipe_name}:{setup_type}:{zone_counter}"
                selected["zone_start_date"] = zone["date"].min()
                selected["zone_end_date"] = zone["date"].max()
                selected["merged_signal_count"] = int(len(zone))
                selected["max_setup_score"] = float(zone["setup_score"].max()) if "setup_score" in zone else np.nan
                merged_rows.append(selected.to_dict())

            for _, row in group.iterrows():
                row_date = row["date"]
                if last_date is not None and cooldown_days > 0 and (row_date - last_date).days > cooldown_days:
                    flush_zone()
                    current_zone_rows = []
                elif last_date is not None and cooldown_days == 0:
                    flush_zone()
                    current_zone_rows = []
                current_zone_rows.append(row.to_dict())
                last_date = row_date
            flush_zone()

        merged = pd.DataFrame(merged_rows)
        if not merged.empty:
            merged.sort_values(["date", "stock_code", "recipe_name", "setup_type"], inplace=True)
            merged.reset_index(drop=True, inplace=True)
        return merged

    @staticmethod
    def _summarize_signal_recipe_events(events, horizons):
        if events is None or events.empty:
            columns = [
                "recipe_name",
                "setup_type",
                "event_count",
                "unique_stock_count",
                "top5_stock_event_share",
                "avg_setup_score",
            ]
            for horizon in horizons:
                columns.extend(
                    [
                        f"avg_forward_return_{horizon}",
                        f"median_forward_return_{horizon}",
                        f"p25_forward_return_{horizon}",
                        f"p75_forward_return_{horizon}",
                        f"win_rate_{horizon}",
                        f"avg_forward_max_drawdown_{horizon}",
                        f"p95_forward_drawdown_{horizon}",
                        f"return_drawdown_ratio_{horizon}",
                        f"avg_win_{horizon}",
                        f"avg_loss_{horizon}",
                    ]
                )
            return pd.DataFrame(columns=columns)

        rows = []
        grouped = events.groupby(["recipe_name", "setup_type"], dropna=False)
        for (recipe_name, setup_type), group in grouped:
            stock_counts = group["stock_code"].value_counts() if "stock_code" in group else pd.Series(dtype=float)
            row = {
                "recipe_name": recipe_name,
                "setup_type": setup_type,
                "event_count": int(len(group)),
                "unique_stock_count": int(group["stock_code"].nunique()) if "stock_code" in group else 0,
                "top5_stock_event_share": float(stock_counts.head(5).sum() / len(group)) if len(group) else np.nan,
                "avg_setup_score": float(group["setup_score"].mean()) if "setup_score" in group else np.nan,
            }
            for horizon in horizons:
                return_col = f"forward_return_{int(horizon)}"
                drawdown_col = f"forward_max_drawdown_{int(horizon)}"
                returns = group[return_col].dropna() if return_col in group else pd.Series(dtype=float)
                drawdowns = group[drawdown_col].dropna() if drawdown_col in group else pd.Series(dtype=float)
                wins = returns[returns > 0]
                losses = returns[returns <= 0]
                avg_return = float(returns.mean()) if not returns.empty else np.nan
                avg_drawdown = float(drawdowns.mean()) if not drawdowns.empty else np.nan
                row[f"avg_forward_return_{int(horizon)}"] = float(returns.mean()) if not returns.empty else np.nan
                row[f"median_forward_return_{int(horizon)}"] = float(returns.median()) if not returns.empty else np.nan
                row[f"p25_forward_return_{int(horizon)}"] = float(returns.quantile(0.25)) if not returns.empty else np.nan
                row[f"p75_forward_return_{int(horizon)}"] = float(returns.quantile(0.75)) if not returns.empty else np.nan
                row[f"win_rate_{int(horizon)}"] = float((returns > 0).mean()) if not returns.empty else np.nan
                row[f"avg_forward_max_drawdown_{int(horizon)}"] = avg_drawdown
                row[f"p95_forward_drawdown_{int(horizon)}"] = float(drawdowns.quantile(0.05)) if not drawdowns.empty else np.nan
                row[f"return_drawdown_ratio_{int(horizon)}"] = avg_return / abs(avg_drawdown) if pd.notna(avg_return) and pd.notna(avg_drawdown) and avg_drawdown != 0 else np.nan
                row[f"avg_win_{int(horizon)}"] = float(wins.mean()) if not wins.empty else np.nan
                row[f"avg_loss_{int(horizon)}"] = float(losses.mean()) if not losses.empty else np.nan
            rows.append(row)
        return pd.DataFrame(rows).sort_values(["recipe_name", "setup_type"]).reset_index(drop=True)

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
        """构建因子验证报告：按股票批次流式产出 feature，再统一做横截面验证。"""
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

        validator = FactorValidator(horizons=horizons, quantiles=quantiles, min_observations=min_observations)
        requested_workers = max(int(max_workers or 1), 1)
        max_workers = self._resolve_safe_validation_workers(
            requested_workers,
            validation_factor_scope=validation_factor_scope,
        )
        if show_progress and max_workers != requested_workers:
            print(
                f"[INFO] validation workers auto-clamped from {requested_workers} to {max_workers} "
                f"for scope={validation_factor_scope}"
            )

        batch_size = self._resolve_validation_batch_size(
            validation_factor_scope=validation_factor_scope,
            requested_workers=max_workers,
        )
        if show_progress:
            print(
                f"[PROGRESS] validation batches start stocks={len(stock_codes)} batch_size={batch_size} "
                f"workers={max_workers} scope={validation_factor_scope}"
            )

        batch_iter = self._iter_factor_validation_batches(
            stock_codes=stock_codes,
            days=days,
            factor_set=factor_set,
            validation_factor_scope=validation_factor_scope,
            validated_feature_names=validated_feature_names,
            batch_size=batch_size,
            max_workers=max_workers,
            show_progress=show_progress,
        )

        factor_coverage_rows = []
        stock_summary_rows = []
        success_count = 0
        seen_batch = False

        def _stream_batches():
            nonlocal success_count, seen_batch
            for batch_index, batch_payload in enumerate(batch_iter, start=1):
                stock_results = list(batch_payload.get("stock_results") or [])
                success_count += len(stock_results)
                for item in stock_results:
                    factor_coverage_rows.append(
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
                        }
                    )
                if show_progress:
                    print(
                        f"[PROGRESS] validation batch_ready index={batch_index} "
                        f"stocks={len(stock_results)} feature_rows={len(batch_payload.get('feature_frame', pd.DataFrame()))}"
                    )
                seen_batch = True
                yield {
                    "feature_frame": batch_payload.get("feature_frame"),
                    "ohlcv_frame": batch_payload.get("ohlcv_frame"),
                }

        def _validation_progress(stage):
            if not show_progress:
                return
            print(f"[PROGRESS] validation stream {stage} stocks={success_count}")

        validation_result = validator.validate_streaming(
            _stream_batches(),
            progress_callback=_validation_progress,
            include_validation_frame=False,
            include_membership=False,
        )
        if not seen_batch:
            return None
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

        for row in stock_summary_rows:
            row["mean_ic"] = global_mean_ic
            row["mean_rank_ic"] = global_mean_rank_ic
            row["mean_spread"] = global_mean_spread
            row["mean_turnover"] = global_mean_turnover

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
                "validation_frame_included": False,
                "quantile_membership_included": False,
                "validation_batch_size": int(batch_size),
            },
            "stock_summary": pd.DataFrame(stock_summary_rows),
            "factor_coverage": pd.DataFrame(factor_coverage_rows),
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
            "analysis_results": [],
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
        ridge_factors=None,
        signal_recipes=None,
    ):
        """固定股票池组合回测：按日期横向比较评分，只持有当日最优的 Top N 信号。"""
        if stock_codes is None:
            stock_codes = self.get_all_stocks()
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return None

        pool_results = []
        requested_workers = int(max_workers or 0)
        max_workers = self._resolve_safe_analysis_workers(requested_workers, analysis_mode=analysis_mode)
        normalized_mode = str(analysis_mode or "strategy").strip().lower()
        if normalized_mode not in {"strategy", "factor"}:
            raise ValueError(f"unsupported analysis_mode: {analysis_mode}")
        if show_progress and requested_workers > 0 and max_workers != requested_workers:
            print(
                f"[INFO] analysis workers auto-clamped from {requested_workers} to {max_workers} "
                f"for mode={normalized_mode}"
            )

        def run_analysis(stock_code):
            if normalized_mode == "factor":
                factor_kwargs = {
                    "days": days,
                    "factor_set": factor_set,
                    "factor_score_config": factor_score_config,
                    "persist_features": persist_features,
                }
                if ridge_factors is not None:
                    factor_kwargs["ridge_factors"] = ridge_factors
                if signal_recipes is not None:
                    factor_kwargs["signal_recipes"] = signal_recipes
                return self.analyze_stock_factors(stock_code, **factor_kwargs)
            return self.analyze_stock(stock_code, days=days)

        default_analyze_stock_factors = getattr(StockAnalyzer, "_default_analyze_stock_factors", None)
        supports_batch_factor_analysis = (
            default_analyze_stock_factors is not None
            and type(self).__dict__.get("analyze_stock_factors") is default_analyze_stock_factors
        )

        if normalized_mode == "factor" and supports_batch_factor_analysis and max_workers > 1 and len(stock_codes) > 1:
            batch_size = self._resolve_factor_analysis_batch_size(
                total_stocks=len(stock_codes),
                max_workers=max_workers,
                analysis_mode=normalized_mode,
            )
            stock_batches = [
                stock_codes[index:index + batch_size]
                for index in range(0, len(stock_codes), batch_size)
            ]
            worker_count = min(max_workers, len(stock_batches))
            available_bytes = type(self)._available_memory_bytes()
            memory_text = (
                f"{(available_bytes / (1024 ** 3)):.1f}"
                if available_bytes is not None
                else "unknown"
            )
            if show_progress:
                print(
                    f"[PROGRESS] analysis phase=batch_factor stocks={len(stock_codes)} "
                    f"batches={len(stock_batches)} batch_size={batch_size} workers={worker_count} "
                    f"memory_available_gb={memory_text}"
                )
            started_at = time.time()
            total = len(stock_codes)
            completed = 0
            success_count = 0
            completed_batches = 0
            active_batches = set()
            batch_progress_counts = {
                batch_index + 1: 0
                for batch_index in range(len(stock_batches))
            }
            stock_done_lock = None

            def emit_batch_progress():
                self._emit_progress_line(
                    prefix="[PROGRESS] analysis phase=batch_factor",
                    completed=completed,
                    total=total,
                    success_count=success_count,
                    started_at=started_at,
                    extra_fields=[
                        ("batches_done", f"{completed_batches}/{len(stock_batches)}"),
                        ("active_batches", len(active_batches)),
                    ],
                )

            if show_progress:
                import threading

                stock_done_lock = threading.Lock()

            def make_progress_callback(batch_no):
                if not show_progress:
                    return None

                def _progress_callback(_stock_code):
                    nonlocal completed
                    with stock_done_lock:
                        completed += 1
                        batch_progress_counts[batch_no] = batch_progress_counts.get(batch_no, 0) + 1
                        emit_batch_progress()

                return _progress_callback

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {}
                for batch_index, batch in enumerate(stock_batches):
                    batch_kwargs = {
                        "days": days,
                        "factor_set": factor_set,
                        "factor_score_config": factor_score_config,
                        "persist_features": persist_features,
                        "ridge_factors": ridge_factors,
                        "progress_callback": make_progress_callback(batch_index + 1),
                        "batch_index": batch_index + 1,
                        "total_batches": len(stock_batches),
                    }
                    if signal_recipes is not None:
                        batch_kwargs["signal_recipes"] = signal_recipes
                    future_map[
                        executor.submit(
                            self._analyze_factor_batch,
                            batch,
                            **batch_kwargs,
                        )
                    ] = (batch_index + 1, batch)
                if show_progress:
                    active_batches = {batch_index for batch_index, _batch in future_map.values()}

                for future in as_completed(future_map):
                    batch_index, batch = future_map[future]
                    try:
                        result_batch = future.result()
                    except Exception as exc:
                        if show_progress:
                            with stock_done_lock:
                                missing_count = max(len(batch) - batch_progress_counts.get(batch_index, 0), 0)
                                completed += missing_count
                                batch_progress_counts[batch_index] = len(batch)
                                active_batches.discard(batch_index)
                                completed_batches += 1
                                emit_batch_progress()
                        print(f"\n[ERROR] 批量因子分析失败 batch={batch_index}/{len(stock_batches)} batch_size={len(batch)}: {exc}")
                        continue
                    if show_progress:
                        with stock_done_lock:
                            missing_count = max(len(batch) - batch_progress_counts.get(batch_index, 0), 0)
                            completed += missing_count
                            batch_progress_counts[batch_index] = len(batch)
                            active_batches.discard(batch_index)
                            completed_batches += 1
                    if result_batch:
                        pool_results.extend(result_batch)
                        success_count += len(result_batch)
                    if show_progress:
                        emit_batch_progress()
            if show_progress:
                print(file=sys.stderr)
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
                        f"\r[PROGRESS] {completed}/{total} "
                        f"({completed / total:.1%}) success={success_count} "
                        f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                    , end="", flush=True, file=sys.stderr)
            if show_progress:
                print(file=sys.stderr)
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
                        print(f"\n[ERROR] 并行分析股票 {stock_code} 失败: {exc}")
                        completed += 1
                        if show_progress:
                            elapsed = max(time.time() - started_at, 1e-9)
                            rate = completed / elapsed
                            remaining = total - completed
                            eta = remaining / rate if rate > 0 else 0.0
                            print(
                                f"\r[PROGRESS] {completed}/{total} "
                                f"({completed / total:.1%}) success={success_count} "
                                f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                            , end="", flush=True, file=sys.stderr)
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
                            f"\r[PROGRESS] {completed}/{total} "
                            f"({completed / total:.1%}) success={success_count} "
                            f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                        , end="", flush=True, file=sys.stderr)
            if show_progress:
                print(file=sys.stderr)

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
        ridge_factors=None,
        signal_recipes=None,
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
            ridge_factors=ridge_factors,
            signal_recipes=signal_recipes,
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


StockAnalyzer._default_analyze_stock_factors = StockAnalyzer.analyze_stock_factors
