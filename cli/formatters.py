"""Formatting and cleanup utilities shared across CLI commands."""

import pandas as pd


def _safe_close_analyzer(analyzer):
    close_method = getattr(analyzer, "close", None)
    if callable(close_method):
        close_method()


def _format_factor_reason_lines(item):
    explanation = (item or {}).get("factor_explanation") or {}
    component_scores = explanation.get("component_scores") or {}
    component_weights = explanation.get("component_weights") or {}
    top_positive = explanation.get("top_positive_factors") or []
    if not explanation:
        return []

    lines = []
    if explanation.get("model_type") == "lightgbm_ranker":
        model_metadata = explanation.get("model_metadata") or {}
        text_summary = explanation.get("text_summary", "")
        if text_summary:
            lines.append(f"  选股理由: {text_summary}")
        lines.append(
            "  模型分解: "
            f"risk_adjusted={model_metadata.get('risk_adjusted_score', float('nan')):.1f}, "
            f"model={model_metadata.get('latest_model_score', float('nan')):.1f}, "
            f"risk={model_metadata.get('risk_score', float('nan')):.1f}"
        )
        lines.append(
            "  风险约束: "
            f"drawdown_penalty={model_metadata.get('drawdown_penalty_score', float('nan')):.1f}, "
            f"recent_drawdown={model_metadata.get('recent_drawdown', float('nan')):.2%}, "
            f"label_horizon={model_metadata.get('label_horizon')}, "
            f"rolling_windows={model_metadata.get('rolling_windows')}"
        )
        lines.append(
            "  战术状态: "
            f"startup={item.get('startup_score', float('nan')):.1f}, "
            f"startup_candidate={item.get('startup_candidate')}, "
            f"startup_candidate_score={item.get('startup_candidate_score', float('nan')):.1f}, "
            f"overheat_penalty={item.get('overheat_penalty_score', float('nan')):.1f}, "
            f"downtrend_penalty={item.get('downtrend_penalty_score', float('nan')):.1f}, "
            f"trend_state={item.get('trend_state')}"
        )
        if top_positive:
            feature_parts = []
            for factor in top_positive[:5]:
                feature_parts.append(
                    f"{factor.get('factor')}("
                    f"w={factor.get('weight', 0):.2f}, "
                    f"importance={factor.get('weighted_contribution', float('nan')):.2f})"
                )
            lines.append("  全局重要特征: " + ", ".join(feature_parts))
        return lines

    lines.append(
        "  因子总分: "
        f"composite={component_scores.get('composite_score', float('nan')):.1f}, "
        f"trend={component_scores.get('trend_score', float('nan')):.1f}, "
        f"quality={component_scores.get('quality_score', float('nan')):.1f}, "
        f"risk={component_scores.get('risk_score', float('nan')):.1f}"
    )
    lines.append(
        "  组件权重: "
        f"trend={component_weights.get('trend_score', 0):.2f}, "
        f"quality={component_weights.get('quality_score', 0):.2f}, "
        f"risk={component_weights.get('risk_score', 0):.2f}"
    )
    if top_positive:
        factor_parts = []
        for factor in top_positive[:3]:
            factor_parts.append(
                f"{factor.get('factor')}("
                f"w={factor.get('weight', 0):.2f}, "
                f"score={factor.get('score', float('nan')):.1f}, "
                f"contrib={factor.get('weighted_contribution', float('nan')):.2f})"
            )
        lines.append("  主要因子: " + ", ".join(factor_parts))
    return lines
