"""Factor classification, feature name translation, and explanation builders."""

import re

import numpy as np
import pandas as pd

from core.constants import FACTOR_CLASSIFICATION_RULES


def classify_factor(factor_name):
    """将 Alpha158 因子名分类到 trend/quality/risk/validated 组件。

    Args:
        factor_name: e.g. "MA30", "CNTD20", "KMID", "VOLUME5"

    Returns:
        str: one of "trend", "quality", "risk", "validated"
    """
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


# ---------------------------------------------------------------------------
# Feature name translation for Chinese summaries
# ---------------------------------------------------------------------------

_FEATURE_NAME_MAP = {
    "KLEN": "K线实体长度",
    "KMID": "K线中位",
    "KUP": "上影线",
    "KLOW": "下影线",
    "OPEN0": "开盘价",
    "HIGH0": "最高价",
    "LOW0": "最低价",
    "CLOSE0": "收盘价",
    "VWAP0": "成交均价",
    "MA5": "5日均线",
    "MA10": "10日均线",
    "MA20": "20日均线",
    "MA30": "30日均线",
    "MA60": "60日均线",
    "STD5": "5日波动率",
    "STD10": "10日波动率",
    "STD20": "20日波动率",
    "STD30": "30日波动率",
    "STD60": "60日波动率",
    "MAX5": "5日最高",
    "MAX10": "10日最高",
    "MAX20": "20日最高",
    "MAX30": "30日最高",
    "MAX60": "60日最高",
    "MIN5": "5日最低",
    "MIN10": "10日最低",
    "MIN20": "20日最低",
    "MIN30": "30日最低",
    "MIN60": "60日最低",
    "ROC5": "5日动量",
    "ROC10": "10日动量",
    "ROC20": "20日动量",
    "ROC30": "30日动量",
    "ROC60": "60日动量",
    "BETA5": "5日趋势斜率",
    "BETA10": "10日趋势斜率",
    "BETA20": "20日趋势斜率",
    "BETA30": "30日趋势斜率",
    "BETA60": "60日趋势斜率",
    "RSQR5": "5日趋势拟合度",
    "RSQR20": "20日趋势拟合度",
    "RSQR60": "60日趋势拟合度",
    "RSV5": "5日随机指标",
    "RSV10": "10日随机指标",
    "RSV20": "20日随机指标",
    "RSV60": "60日随机指标",
    "CORR5": "5日量价相关",
    "CORR10": "10日量价相关",
    "CORR20": "20日量价相关",
    "CORR60": "60日量价相关",
    "CNTP5": "5日上涨占比",
    "CNTP10": "10日上涨占比",
    "CNTP20": "20日上涨占比",
    "CNTP60": "60日上涨占比",
    "CNTN5": "5日下跌占比",
    "CNTN10": "10日下跌占比",
    "CNTN20": "20日下跌占比",
    "CNTN60": "60日下跌占比",
    "CNTD5": "5日涨跌差",
    "CNTD10": "10日涨跌差",
    "CNTD20": "20日涨跌差",
    "CNTD60": "60日涨跌差",
    "VMA5": "5日均量",
    "VMA10": "10日均量",
    "VMA20": "20日均量",
    "VMA60": "60日均量",
    "VSTD5": "5日量波动",
    "VSTD20": "20日量波动",
    "VSTD60": "60日量波动",
    "WVMA5": "5日加权量波动",
    "WVMA20": "20日加权量波动",
    "WVMA60": "60日加权量波动",
    "SUMP5": "5日涨幅累计",
    "SUMP20": "20日涨幅累计",
    "SUMP60": "60日涨幅累计",
    "SUMN5": "5日跌幅累计",
    "SUMN20": "20日跌幅累计",
    "SUMN60": "60日跌幅累计",
    "SUMD5": "5日涨跌幅差",
    "SUMD20": "20日涨跌幅差",
    "SUMD60": "60日涨跌幅差",
    "QTLU5": "5日上分位",
    "QTLU20": "20日上分位",
    "QTLU60": "60日上分位",
    "QTLD5": "5日下分位",
    "QTLD20": "20日下分位",
    "QTLD60": "60日下分位",
    "IMAX5": "5日最高位置",
    "IMAX20": "20日最高位置",
    "IMAX60": "60日最高位置",
    "IMIN5": "5日最低位置",
    "IMIN20": "20日最低位置",
    "IMIN60": "60日最低位置",
    "IMXD5": "5日高低位差",
    "IMXD20": "20日高低位差",
    "IMXD60": "60日高低位差",
}


def _translate_feature_names(names: list[str]) -> str:
    """Translate Alpha158 feature names to Chinese descriptions."""
    translated = []
    for name in names:
        cn = _FEATURE_NAME_MAP.get(name)
        if cn:
            translated.append(f"{cn}({name})")
        else:
            translated.append(name)
    return "、".join(translated)


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


def _build_lightgbm_factor_explanation(
    model_metadata,
    latest_model_score,
    risk_adjusted_score,
    drawdown_penalty_score,
    recent_drawdown,
    risk_score,
):
    latest_model_score_value = float(latest_model_score) if pd.notna(latest_model_score) else np.nan
    risk_adjusted_score_value = float(risk_adjusted_score) if pd.notna(risk_adjusted_score) else np.nan
    drawdown_penalty_value = float(drawdown_penalty_score) if pd.notna(drawdown_penalty_score) else np.nan
    recent_drawdown_value = float(recent_drawdown) if pd.notna(recent_drawdown) else np.nan
    risk_score_value = float(risk_score) if pd.notna(risk_score) else np.nan
    top_positive = []
    top_feature_names = []
    for item in (model_metadata or {}).get("top_features", [])[:5]:
        feature_name = item.get("feature_name", "")
        top_positive.append(
            {
                "factor": feature_name,
                "weight": float(item.get("importance_weight", 0.0) or 0.0),
                "score": latest_model_score_value,
                "weighted_contribution": float(item.get("importance", 0.0) or 0.0),
            }
        )
        top_feature_names.append(feature_name)

    # Build Chinese text summary
    oos_metrics = (model_metadata or {}).get("oos_metrics", {})
    ic_mean = oos_metrics.get("ic_mean")
    rank_ic_mean = oos_metrics.get("rank_ic_mean")
    label_horizon = (model_metadata or {}).get("label_horizon", 20)
    rolling_windows = (model_metadata or {}).get("rolling_windows", 0)

    summary_parts = []
    if pd.notna(latest_model_score_value) and np.isfinite(latest_model_score_value):
        if latest_model_score_value >= 90:
            summary_parts.append(f"模型排名前10%（得分{latest_model_score_value:.0f}/100）")
        elif latest_model_score_value >= 75:
            summary_parts.append(f"模型排名前25%（得分{latest_model_score_value:.0f}/100）")
        else:
            summary_parts.append(f"模型得分{latest_model_score_value:.0f}/100")

    if top_feature_names:
        feature_desc = _translate_feature_names(top_feature_names[:3])
        summary_parts.append(f"主要驱动因子: {feature_desc}")

    if pd.notna(recent_drawdown_value) and recent_drawdown_value < -0.05:
        summary_parts.append(f"近期回撤{recent_drawdown_value:.1%}，注意风险")
    elif pd.notna(recent_drawdown_value) and recent_drawdown_value > -0.02:
        summary_parts.append("近期走势平稳")

    if rolling_windows > 0 and ic_mean and np.isfinite(ic_mean):
        summary_parts.append(f"模型{label_horizon}日预测IC={ic_mean:.3f}")

    text_summary = "；".join(summary_parts) if summary_parts else "暂无摘要"

    return {
        "model_type": "lightgbm_ranker",
        "text_summary": text_summary,
        "component_scores": {
            "composite_score": risk_adjusted_score_value,
            "trend_score": latest_model_score_value,
            "quality_score": np.nan,
            "risk_score": risk_score_value,
        },
        "component_weights": {
            "trend_score": 0.70,
            "quality_score": 0.0,
            "risk_score": 0.30,
        },
        "top_positive_factors": top_positive,
        "top_features": list((model_metadata or {}).get("top_features", [])),
        "model_metadata": {
            "label_horizon": (model_metadata or {}).get("label_horizon"),
            "rolling_windows": (model_metadata or {}).get("rolling_windows"),
            "execution_delay": (model_metadata or {}).get("execution_delay"),
            "oos_metrics": oos_metrics,
            "train_rows": (model_metadata or {}).get("train_rows"),
            "valid_rows": (model_metadata or {}).get("valid_rows"),
            "feature_count": (model_metadata or {}).get("feature_count"),
            "latest_model_score": latest_model_score_value,
            "risk_adjusted_score": risk_adjusted_score_value,
            "drawdown_penalty_score": drawdown_penalty_value,
            "recent_drawdown": recent_drawdown_value,
            "risk_score": risk_score_value,
        },
    }
