"""Factor scoring mixin for StockAnalyzer."""

import re

import numpy as np
import pandas as pd

from core.constants import DEFAULT_FACTOR_SET, DEFAULT_FACTOR_SCORE_CONFIG
from core.formatting import classify_factor


class FactorScoringMixin:
    """Methods for computing factor scores."""

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
