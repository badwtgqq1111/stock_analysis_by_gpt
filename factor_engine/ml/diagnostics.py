"""Comprehensive LightGBM model diagnostics.

Feature family importance, momentum trap detection, SHAP drift, KS statistics,
industry/size exposure, and prediction distribution monitoring.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Feature family classification
# ---------------------------------------------------------------------------

_FEATURE_FAMILY_MAP: dict[str, str] = {}

MOMENTUM_KEYWORDS = [
    "ROC", "MOM", "RSI", "CCI", "BIAS", "MACD", "MFI", "KST", "TRIX",
    "PPO", "WILLR", "ADX", "PLRC", "chande",
    "startup_ret", "startup_ma", "startup_price_position", "startup_breakout",
    "price_return_", "ma_gap", "ret20", "ret60",
]
REVERSAL_KEYWORDS = ["REVS", "BETA", "CMO", "OBV", "minus", "price_delta"]
VOLUME_KEYWORDS = [
    "VOLUME", "volume", "VEMA", "VWMA", "turnover", "VWAP", "vwap",
    "turn", "amount", "OBV", "VROC", "MFI", "MoneyFlow",
    "startup_volume", "volume_ratio",
]
VOLATILITY_KEYWORDS = ["STD", "VAR", "ATR", "BB", "boll", "HLV", "vola", "std", "sqrt", "kurt", "skew", "dispersion"]
VALUE_KEYWORDS = ["PE", "PB", "PS", "EP", "BP", "DP", "ROE", "ROA", "EY", "BV", "CFP", "OCFP", "EBITDA", "SP"]
QUALITY_KEYWORDS = [
    "quality", "roe", "roa", "gross_margin", "net_margin", "debt", "accrual",
    "sector_mean_quality", "f_score", "z_score",
    "current_ratio", "quick_ratio", "interest_coverage",
]
THEME_KEYWORDS = ["theme_", "attention_score", "bottleneck_score", "evidence_quality"]
INDUSTRY_KEYWORDS = ["industry_", "sector_", "cluster_", "peer_", "ind_"]
GTJA_KEYWORDS = ["GTJA", "gtja_"]
ALT_KEYWORDS = ["alt_", "sentiment", "news_count"]
HONGKONG_KEYWORDS = ["HK_"]
MOMENTUM_TRAP_KEYWORDS = [
    "startup_candidate_score", "overheat_penalty", "downtrend_penalty",
    "startup_candidate", "high_chase",
]


def classify_feature(feature_name: str) -> str:
    name_upper = str(feature_name).upper()
    if name_upper in _FEATURE_FAMILY_MAP:
        return _FEATURE_FAMILY_MAP[name_upper]
    for kw in MOMENTUM_TRAP_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "momentum_trap"
    for kw in THEME_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "theme"
    for kw in INDUSTRY_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "industry"
    for kw in ALT_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "alt_sentiment"
    for kw in GTJA_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "gtja_proxy"
    for kw in HONGKONG_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "hk_custom"
    for kw in MOMENTUM_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "momentum"
    for kw in REVERSAL_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "reversal"
    for kw in VOLUME_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "volume"
    for kw in VOLATILITY_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "volatility"
    for kw in VALUE_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "value"
    for kw in QUALITY_KEYWORDS:
        if kw.lower() in str(feature_name).lower():
            return "quality"
    return "other"


def compute_lightgbm_model_diagnostics(
    ranking: pd.DataFrame,
    selected: pd.DataFrame,
    feature_importance: pd.DataFrame | None = None,
    high_chase_threshold: float = 80.0,
    multibagger_60d_threshold: float = 100.0,
    multibagger_120d_threshold: float = 180.0,
) -> dict:
    diagnostics: dict = {
        "ranking_rows": int(len(ranking)),
        "selected_rows": int(len(selected)),
        "feature_family_importance": {},
        "momentum_columns_available": [],
        "production_gate_pass": True,
        "production_gate_failures": [],
        "ranking_high_chase_rate": 0.0,
        "selected_high_chase_rate": 0.0,
        "selected_60d_multibagger_rate": 0.0,
        "selected_120d_multibagger_rate": 0.0,
        "selected_near_52w_high_rate": 0.0,
        "selected_momentum": {},
        "selected_high_chase_stocks": [],
        "red_flags": [],
        "recommendations": [],
    }

    for frame in (ranking, selected):
        if frame is not None and not frame.empty and "stock_code" in frame.columns:
            frame["stock_code"] = frame["stock_code"].astype(str)

    if feature_importance is not None and not feature_importance.empty:
        if "feature_name" in feature_importance.columns and "importance" in feature_importance.columns:
            fi = feature_importance.copy()
            fi["importance"] = pd.to_numeric(fi["importance"], errors="coerce").fillna(0.0)
            fi["family"] = fi["feature_name"].apply(classify_feature)
            family_importance = fi.groupby("family")["importance"].sum()
            total = family_importance.sum()
            if total > 0:
                diagnostics["feature_family_importance"] = {
                    fam: round(float(val / total), 4)
                    for fam, val in family_importance.sort_values(ascending=False).items()
                }
            diagnostics["top_20_features"] = fi.nlargest(20, "importance")[
                ["feature_name", "family", "importance"]
            ].to_dict(orient="records")

    momentum_cols = _find_momentum_columns(ranking)
    diagnostics["momentum_columns_available"] = momentum_cols

    if not ranking.empty:
        rankings_with_momentum = _compute_momentum_metrics(ranking, momentum_cols)
        diagnostics["ranking_momentum_stats"] = rankings_with_momentum
        chase_col = _find_high_chase_column(ranking)
        if chase_col:
            chase_vals = pd.to_numeric(ranking[chase_col], errors="coerce")
            diagnostics["ranking_high_chase_rate"] = round(
                float((chase_vals >= high_chase_threshold).mean()), 4
            )

    if not selected.empty:
        selected_stats = _compute_momentum_metrics(selected, momentum_cols)
        diagnostics["selected_momentum"] = selected_stats
        chase_col = _find_high_chase_column(selected)
        if chase_col:
            chase_vals = pd.to_numeric(selected[chase_col], errors="coerce")
            diagnostics["selected_high_chase_rate"] = round(
                float((chase_vals >= high_chase_threshold).mean()), 4
            )
        for col, key in [("price_return_60d_pct", "selected_60d_multibagger_rate"),
                         ("price_return_120d_pct", "selected_120d_multibagger_rate")]:
            if col in selected.columns:
                values = pd.to_numeric(selected[col], errors="coerce")
                threshold = multibagger_60d_threshold if "60" in col else multibagger_120d_threshold
                diagnostics[key] = round(float((values >= threshold).mean()), 4)
        if "price_position_52w_high" in selected.columns:
            pos = pd.to_numeric(selected["price_position_52w_high"], errors="coerce")
            diagnostics["selected_near_52w_high_rate"] = round(
                float((pos >= 95).mean()), 4
            )
        flagged = _flag_high_chase_stocks(selected, high_chase_threshold)
        diagnostics["selected_high_chase_stocks"] = flagged

    red_flags = diagnostics["red_flags"]
    gate_failures = diagnostics["production_gate_failures"]
    fam_imp = diagnostics.get("feature_family_importance", {})
    momentum_pct = fam_imp.get("momentum", 0)
    if momentum_pct > 0.35:
        red_flags.append(f"momentum_feature_family_dominates_gain={momentum_pct:.1%}_exceeds_35pct")
    if diagnostics["selected_high_chase_rate"] > 0.20:
        red_flags.append(f"high_chase_concentration={diagnostics['selected_high_chase_rate']:.1%}_selected_exceeds_20pct")
        gate_failures.append("selected_high_chase_rate_exceeds_20pct")
    elif diagnostics["selected_high_chase_rate"] > 0:
        red_flags.append(f"high_chase_present={diagnostics['selected_high_chase_rate']:.1%}_selected")
        gate_failures.append("selected_contains_high_chase")
    if diagnostics["selected_60d_multibagger_rate"] > 0:
        red_flags.append(f"multibagger_60d_present={diagnostics['selected_60d_multibagger_rate']:.1%}")
        gate_failures.append("selected_contains_60d_multibagger")
    if diagnostics["selected_120d_multibagger_rate"] > 0:
        red_flags.append(f"multibagger_120d_present={diagnostics['selected_120d_multibagger_rate']:.1%}")
        gate_failures.append("selected_contains_120d_multibagger")
    theme_pct = fam_imp.get("theme", 0)
    if theme_pct > 0.15:
        red_flags.append(f"theme_features_high_importance={theme_pct:.1%}_review_for_data_leakage")
    diagnostics["production_gate_failures"] = list(dict.fromkeys(gate_failures))
    diagnostics["production_gate_pass"] = not diagnostics["production_gate_failures"]

    recs = diagnostics["recommendations"]
    if "high_chase_score" not in selected.columns and "high_chase_score" not in ranking.columns:
        recs.append("rerun_select_to_export_high_chase_score")
    if momentum_pct > 0.35:
        recs.append("reduce_momentum_overreliance: 增加中性化或纳入组合惩罚")
    if diagnostics["selected_high_chase_rate"] > 0:
        recs.append("block_high_chase_selected: selected 中仍含追高票")
    if diagnostics["selected_near_52w_high_rate"] > 0.50:
        recs.append("cap_52w_high_exposure: 限制接近 52 周高点的持仓比例")
    if fam_imp and theme_pct < 0.01 and theme_pct >= 0:
        recs.append("theme_features_zero_contribution: 画像特征无贡献")
    if not recs:
        recs.append("ok")

    return diagnostics


def _find_momentum_columns(frame: pd.DataFrame) -> list[str]:
    candidates = [
        "price_return_5d_pct", "price_return_20d_pct", "price_return_60d_pct",
        "price_return_120d_pct", "price_return_252d_pct",
        "price_position_52w_high", "ma60_gap_pct", "high_chase_score",
    ]
    return [col for col in candidates if col in frame.columns]


def _find_high_chase_column(frame: pd.DataFrame) -> str | None:
    if "high_chase_score" in frame.columns:
        return "high_chase_score"
    return None


def _compute_momentum_metrics(frame: pd.DataFrame, momentum_cols: list[str]) -> dict:
    metrics = {}
    for col in momentum_cols:
        if col not in frame.columns:
            continue
        series = pd.to_numeric(frame[col], errors="coerce").dropna()
        if series.empty:
            continue
        metrics[col] = {
            "median": round(float(series.median()), 2),
            "mean": round(float(series.mean()), 2),
            "p90": round(float(series.quantile(0.90)), 2),
            "max": round(float(series.max()), 2),
            "min": round(float(series.min()), 2),
            "count": int(len(series)),
        }
    return metrics


def _flag_high_chase_stocks(selected: pd.DataFrame, threshold: float) -> list[dict]:
    flagged = []
    chase_col = _find_high_chase_column(selected)
    if not chase_col:
        return flagged
    chase_vals = pd.to_numeric(selected[chase_col], errors="coerce")
    high_mask = chase_vals >= threshold
    for _, row in selected[high_mask].iterrows():
        item = {"stock_code": row.get("stock_code"), "high_chase_score": row.get(chase_col)}
        for extra in ["price_return_20d_pct", "price_return_60d_pct",
                       "price_return_120d_pct", "price_position_52w_high",
                       "ma60_gap_pct", "eligibility_reasons"]:
            if extra in selected.columns:
                item[extra] = row.get(extra)
        flagged.append(item)
    return flagged


def compute_shap_drift(
    shap_history: list[dict],
    sign_change_threshold: int = 2,
    contribution_double_threshold: float = 2.0,
) -> dict:
    if len(shap_history) < 2:
        return {"drift_flags": [], "sign_changes": [], "family_spikes": []}
    df = pd.DataFrame(shap_history)
    if "feature_name" not in df.columns or "mean_shap" not in df.columns:
        return {"drift_flags": [], "sign_changes": [], "family_spikes": []}
    df["mean_shap"] = pd.to_numeric(df["mean_shap"], errors="coerce")
    df["family"] = df["feature_name"].apply(classify_feature)
    windows = sorted(df["window_label"].unique()) if "window_label" in df.columns else []
    if len(windows) < 2:
        return {"drift_flags": [], "sign_changes": [], "family_spikes": []}
    recent_w, prev_w = windows[-1], windows[-2]
    recent = df[df["window_label"] == recent_w].set_index("feature_name")["mean_shap"]
    prev = df[df["window_label"] == prev_w].set_index("feature_name")["mean_shap"]
    common = recent.index.intersection(prev.index)
    sign_changes = []
    for feat in common:
        if (recent[feat] > 0 and prev[feat] < 0) or (recent[feat] < 0 and prev[feat] > 0):
            sign_changes.append({
                "feature_name": feat, "family": classify_feature(feat),
                "prev_shap": round(float(prev[feat]), 6),
                "recent_shap": round(float(recent[feat]), 6),
            })
    family_spikes = []
    for fam, group in df.groupby("family"):
        if len(group["window_label"].unique()) < 2:
            continue
        fam_prev = group[group["window_label"] == prev_w]["mean_shap"].abs().mean()
        fam_recent = group[group["window_label"] == recent_w]["mean_shap"].abs().mean()
        if fam_prev > 0 and fam_recent / fam_prev >= contribution_double_threshold:
            family_spikes.append({
                "family": fam, "prev_abs_mean": round(float(fam_prev), 6),
                "recent_abs_mean": round(float(fam_recent), 6),
                "ratio": round(float(fam_recent / fam_prev), 2),
            })
    drift_flags = []
    if len(sign_changes) >= sign_change_threshold:
        drift_flags.append(f"shap_sign_changes={len(sign_changes)}_top_features")
    if family_spikes:
        drift_flags.append(f"feature_family_spikes={len(family_spikes)}_families")
    return {"drift_flags": drift_flags or ["ok"], "sign_changes": sign_changes, "family_spikes": family_spikes}


def compute_prediction_distribution_metrics(train_scores: pd.Series, oos_scores: pd.Series) -> dict:
    train = pd.to_numeric(train_scores, errors="coerce").dropna()
    oos = pd.to_numeric(oos_scores, errors="coerce").dropna()
    if train.empty or oos.empty:
        return {"ks_stat": None, "train_mean": None, "train_std": None,
                "oos_mean": None, "oos_std": None, "ks_flag": "insufficient_data"}
    combined = pd.concat([train, oos])
    bins = np.linspace(combined.min(), combined.max(), min(100, len(combined) // 2))
    train_hist, _ = np.histogram(train, bins=bins, density=True)
    oos_hist, _ = np.histogram(oos, bins=bins, density=True)
    train_cdf = np.cumsum(train_hist) / max(1, train_hist.sum())
    oos_cdf = np.cumsum(oos_hist) / max(1, oos_hist.sum())
    ks = float(np.max(np.abs(train_cdf - oos_cdf))) if len(train_cdf) > 0 else 0.0
    ks_flag = "ok" if ks <= 0.10 else "warning" if ks <= 0.15 else "critical"
    return {
        "ks_stat": round(ks, 4),
        "train_mean": round(float(train.mean()), 4), "train_std": round(float(train.std()), 4),
        "oos_mean": round(float(oos.mean()), 4), "oos_std": round(float(oos.std()), 4),
        "ks_flag": ks_flag,
    }


def compute_ic_decay(
    predictions: pd.DataFrame, full_frame: pd.DataFrame,
    target_col: str, horizons: list[int] | None = None,
) -> dict:
    if horizons is None:
        horizons = [1, 5, 10, 20, 40, 60]
    eval_frame = predictions.merge(
        full_frame[["trade_date", "stock_code", target_col]],
        on=["trade_date", "stock_code"], how="inner",
    )
    eval_frame = eval_frame.dropna(subset=["model_score_raw", target_col])
    decay = {}
    base_h = int(target_col.split("_")[-1]) if "_" in target_col else 20
    for h in horizons:
        target_h = f"forward_return_{h}" if h != base_h else target_col
        if target_h not in full_frame.columns:
            continue
        tmp = predictions.merge(
            full_frame[["trade_date", "stock_code", target_h]],
            on=["trade_date", "stock_code"], how="inner",
        )
        daily_ics = []
        for _date, group in tmp.groupby("trade_date"):
            if len(group) < 5:
                continue
            ic = group["model_score_raw"].corr(group[target_h])
            if np.isfinite(ic):
                daily_ics.append(ic)
        ic_series = pd.Series(daily_ics)
        decay[f"horizon_{h}d"] = {
            "ic_mean": round(float(ic_series.mean()), 6) if not ic_series.empty else None,
            "ic_std": round(float(ic_series.std()), 6) if len(ic_series) > 1 else None,
            "icir": round(float(ic_series.mean() / ic_series.std()), 4) if len(ic_series) > 1 and ic_series.std() > 1e-12 else None,
            "eval_dates": len(ic_series),
            "ic_positive_rate": round(float((ic_series > 0).mean()), 4) if not ic_series.empty else None,
        }
    return decay


def compute_theme_ablation(
    with_theme_results: dict | None, without_theme_results: dict | None,
    overlay_weights: list[float] | None = None,
) -> dict:
    if overlay_weights is None:
        overlay_weights = [0.0, 0.05, 0.10]
    result = {
        "with_theme": _safe_metrics(with_theme_results),
        "without_theme": _safe_metrics(without_theme_results),
        "overlay_weights_tested": overlay_weights,
        "delta_ic": None, "delta_rank_ic": None,
        "recommendation": "insufficient_data",
    }
    w, wo = result["with_theme"], result["without_theme"]
    if w.get("ic_mean") is not None and wo.get("ic_mean") is not None:
        result["delta_ic"] = round(w["ic_mean"] - wo["ic_mean"], 6)
        result["delta_rank_ic"] = round((w.get("rank_ic_mean") or 0) - (wo.get("rank_ic_mean") or 0), 6)
        if result["delta_ic"] > 0.002:
            result["recommendation"] = "theme_features_add_value"
        elif result["delta_ic"] > -0.002:
            result["recommendation"] = "theme_features_neutral"
        else:
            result["recommendation"] = "theme_features_harmful"
    return result


def _safe_metrics(metadata: dict | None) -> dict:
    if not metadata:
        return {}
    oos = metadata.get("oos_metrics", {})
    return {
        "ic_mean": oos.get("ic_mean"), "icir": oos.get("icir"),
        "rank_ic_mean": oos.get("rank_ic_mean"), "rank_icir": oos.get("rank_icir"),
        "ic_positive_rate": oos.get("ic_positive_rate"),
        "feature_count": metadata.get("feature_count"),
    }


def compute_neutralization_ab(modes: dict[str, dict]) -> dict:
    comparison = {}
    for mode, metadata in modes.items():
        oos = metadata.get("oos_metrics", {})
        comparison[mode] = {
            "ic_mean": oos.get("ic_mean"), "rank_ic_mean": oos.get("rank_ic_mean"),
            "icir": oos.get("icir"), "rank_icir": oos.get("rank_icir"),
            "feature_count": metadata.get("feature_count"),
        }
    best_mode, best_ic = None, -999
    for mode, metrics in comparison.items():
        ric = metrics.get("rank_ic_mean") or -999
        if ric > best_ic:
            best_ic, best_mode = ric, mode
    return {"modes": comparison, "best_by_rank_ic": best_mode,
            "recommendation": f"prefer_{best_mode}" if best_mode else "insufficient_data"}
