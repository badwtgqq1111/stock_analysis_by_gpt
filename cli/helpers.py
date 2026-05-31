"""Shared helper functions for CLI commands."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd


def _parse_horizons(raw_value):
    if raw_value is None:
        return (1, 5, 10, 20)
    if isinstance(raw_value, (tuple, list)):
        return tuple(int(item) for item in raw_value)
    values = []
    for chunk in str(raw_value).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        values.append(int(chunk))
    return tuple(values or [1, 5, 10, 20])


def _parse_signal_recipes(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (tuple, list)):
        return tuple(str(item).strip() for item in raw_value if str(item).strip())
    values = []
    for chunk in str(raw_value).split(","):
        chunk = chunk.strip()
        if chunk:
            values.append(chunk)
    return tuple(values) if values else None


def _build_current_factor_weight_table(score_config):
    rows = []
    config = score_config or {}
    component_weights = config.get("weights", {})
    for component_name in ("trend", "quality", "risk"):
        component_weight = float(component_weights.get(f"{component_name}_score", 0.0))
        for factor_name, rule in (config.get(component_name, {}) or {}).items():
            rows.append(
                {
                    "component": component_name,
                    "factor": factor_name,
                    "configured_factor_weight": float(rule.get("weight", 0.0)),
                    "configured_component_weight": component_weight,
                    "direction": "higher_is_better" if bool(rule.get("higher_is_better", True)) else "lower_is_better",
                }
            )
    return pd.DataFrame(rows)


def _merge_recommended_factor_weights(score_config, factor_scorecard):
    base_config = deepcopy(score_config or {})
    scorecard = factor_scorecard if isinstance(factor_scorecard, pd.DataFrame) else pd.DataFrame()
    if scorecard.empty or "component" not in scorecard.columns:
        return base_config
    factor_name_column = "factor" if "factor" in scorecard.columns else ("feature_name" if "feature_name" in scorecard.columns else None)
    if factor_name_column is None:
        return base_config

    # Track existing factor -> component mapping from config
    existing_factor_component = {}  # factor_name -> component_name
    for comp_name in base_config:
        if comp_name == "weights":
            continue
        for factor_name in (base_config.get(comp_name) or {}):
            existing_factor_component[factor_name] = comp_name

    component_names = sorted(set(scorecard["component"].dropna()))
    for component_name in component_names:
        component_rows = scorecard[scorecard["component"].fillna("") == component_name].copy()
        if component_rows.empty or "recommended_factor_weight" not in component_rows.columns:
            continue
        recommended = pd.to_numeric(component_rows["recommended_factor_weight"], errors="coerce")
        if recommended.notna().any() and recommended.fillna(0).sum() > 0:
            for _, row in component_rows.iterrows():
                factor_name = row.get(factor_name_column)
                if not factor_name:
                    continue
                weight_value = row.get("recommended_factor_weight")
                if pd.isna(weight_value):
                    continue
                higher_is_better = bool(row.get("higher_is_better", True))

                if factor_name in existing_factor_component:
                    # Factor already exists in config -- update it in its ORIGINAL component
                    orig_component = existing_factor_component[factor_name]
                    base_config.setdefault(orig_component, {})
                    base_config[orig_component][factor_name]["weight"] = float(weight_value)
                else:
                    # Add new factor that Ridge identified as predictive
                    base_config.setdefault(component_name, {})
                    base_config[component_name][factor_name] = {
                        "weight": float(weight_value),
                        "higher_is_better": higher_is_better,
                    }
                    existing_factor_component[factor_name] = component_name

    # Ensure validated component has a weight entry
    if "validated" in base_config and base_config["validated"]:
        base_config.setdefault("weights", {})
        base_config["weights"].setdefault("validated_score", 0.15)

    return base_config


def _build_validation_cache_key(
    factor_set,
    validation_days,
    validation_horizons,
    validation_quantiles,
    validation_min_observations,
    validation_stock_codes,
    validation_factor_scope="all",
    validated_feature_names=None,
):
    stock_codes = list(validation_stock_codes or [])
    stock_code_hash = hashlib.sha1("\n".join(stock_codes).encode("utf-8")).hexdigest() if stock_codes else "none"
    validated_feature_names = [str(item) for item in (validated_feature_names or []) if str(item).strip()]
    identity = {
        "factor_set": factor_set,
        "validation_days": int(validation_days),
        "validation_horizons": [int(item) for item in validation_horizons],
        "validation_quantiles": int(validation_quantiles),
        "validation_min_observations": int(validation_min_observations),
        "stock_count": len(stock_codes),
        "stock_code_hash": stock_code_hash,
        "validation_factor_scope": str(validation_factor_scope),
        "validated_feature_names": validated_feature_names,
    }
    cache_key = hashlib.sha1(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return cache_key, identity


def _get_validation_cache_dir(analyzer):
    data_layout = getattr(analyzer, "data_layout", None)
    layer_path = getattr(data_layout, "layer_path", None)
    if callable(layer_path):
        return Path(layer_path("meta")) / "factor_weight_cache"
    return None


def _load_validation_weight_cache(cache_dir, cache_key):
    if cache_dir is None:
        return None
    cache_path = Path(cache_dir) / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["_cache_path"] = str(cache_path)
    return payload


def _write_validation_weight_cache(cache_dir, cache_key, payload):
    if cache_dir is None:
        return None
    cache_path = Path(cache_dir) / f"{cache_key}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return cache_path


def _get_run_manifest_dir(analyzer=None, fallback_base=None):
    data_layout = getattr(analyzer, "data_layout", None)
    layer_path = getattr(data_layout, "layer_path", None)
    if callable(layer_path):
        return Path(layer_path("meta")) / "run_manifests"
    if fallback_base is not None:
        return Path(fallback_base).expanduser().resolve().parent / "run_manifests"
    return (Path.cwd() / "output" / "run_manifests").resolve()


def _write_run_manifest(
    run_type,
    params,
    analyzer=None,
    fallback_base=None,
    artifacts=None,
    factor_materialization=None,
    upstream=None,
    status="ok",
):
    manifest_dir = _get_run_manifest_dir(analyzer=analyzer, fallback_base=fallback_base)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    run_id = f"{run_type}_{created_at.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    manifest = {
        "run_id": run_id,
        "run_type": str(run_type),
        "status": str(status or "ok"),
        "created_at": created_at.isoformat(),
        "params": dict(params or {}),
        "artifacts": dict(artifacts or {}),
        "factor_materialization": dict(factor_materialization or {}),
        "upstream": dict(upstream or {}),
    }
    manifest_path = manifest_dir / f"{run_id}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return manifest_path, manifest


def _fallback_classify_factor_name(name):
    text = str(name or "").strip().upper()
    if not text:
        return "validated"
    trend_prefixes = ("MA", "ROC", "MAX", "MIN", "RSV", "IMAX", "IMIN", "IMXD", "QTLU", "QTLD", "OPEN", "HIGH", "LOW", "CLOSE", "VWAP")
    quality_prefixes = ("VMA", "WVMA", "VSUMP", "VSUMN", "VSUMD", "CORR", "CORD", "CNTP", "CNTN", "CNTD", "SUMP", "SUMN", "SUMD", "RSQR", "RESI", "VOLUME", "KMID", "KLEN", "KUP", "KLOW", "KSFT")
    risk_prefixes = ("STD", "VSTD")
    if text.startswith(risk_prefixes):
        return "risk"
    if text.startswith(quality_prefixes):
        return "quality"
    if text.startswith(trend_prefixes):
        return "trend"
    return "validated"


def _sanitize_validation_scorecard(scorecard):
    working = scorecard.copy() if isinstance(scorecard, pd.DataFrame) else pd.DataFrame()
    if working.empty:
        return working

    for column in ("validation_score", "recommended_factor_weight", "configured_factor_weight", "ridge_coef", "abs_ridge_coef"):
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")
            working[column] = working[column].replace([np.inf, -np.inf], np.nan)

    if "component" in working.columns:
        try:
            from core import classify_factor
        except ImportError:
            classify_factor = _fallback_classify_factor_name
        missing_mask = working["component"].isna() | (working["component"].astype(str).str.strip() == "")
        if missing_mask.any() and "feature_name" in working.columns:
            working.loc[missing_mask, "component"] = working.loc[missing_mask, "feature_name"].apply(classify_factor)
    return working


def _is_usable_validation_scorecard(scorecard):
    working = _sanitize_validation_scorecard(scorecard)
    if working.empty or "validation_score" not in working.columns:
        return False
    validation_score = pd.to_numeric(working["validation_score"], errors="coerce")
    finite = validation_score[np.isfinite(validation_score)]
    if finite.empty:
        return False
    return bool((finite > 0).any())


def _build_factor_scorecard(report):
    ic_summary = report.get("ic_summary")
    long_short_summary = report.get("long_short_summary")
    turnover_summary = report.get("turnover_summary")
    decay_summary = report.get("decay_summary")
    ic_summary = ic_summary if isinstance(ic_summary, pd.DataFrame) else pd.DataFrame()
    long_short_summary = long_short_summary if isinstance(long_short_summary, pd.DataFrame) else pd.DataFrame()
    turnover_summary = turnover_summary if isinstance(turnover_summary, pd.DataFrame) else pd.DataFrame()
    decay_summary = decay_summary if isinstance(decay_summary, pd.DataFrame) else pd.DataFrame()
    configured_weights = _build_current_factor_weight_table(
        (report.get("metadata") or {}).get("factor_score_config") or {}
    )

    base = pd.DataFrame(columns=["feature_name"])
    if not ic_summary.empty:
        base = (
            ic_summary.groupby(["feature_name"], dropna=False)
            .agg(
                mean_ic=("mean_ic", "mean"),
                mean_rank_ic=("mean_rank_ic", "mean"),
                ic_positive_rate=("ic_positive_rate", "mean"),
                rank_ic_positive_rate=("rank_ic_positive_rate", "mean"),
                ic_ir=("ic_ir", "mean"),
                rank_ic_ir=("rank_ic_ir", "mean"),
                horizons=("horizon", "nunique"),
            )
            .reset_index()
        )
    if not long_short_summary.empty:
        spread = (
            long_short_summary.groupby(["feature_name"], dropna=False)
            .agg(
                mean_spread=("mean_spread", "mean"),
                spread_ir=("spread_ir", "mean"),
                spread_positive_rate=("positive_rate", "mean"),
            )
            .reset_index()
        )
        base = spread if base.empty else base.merge(spread, on="feature_name", how="outer")
    if not turnover_summary.empty:
        turnover = (
            turnover_summary.groupby(["feature_name"], dropna=False)
            .agg(
                mean_turnover=("mean_turnover", "mean"),
                max_turnover=("max_turnover", "max"),
            )
            .reset_index()
        )
        base = turnover if base.empty else base.merge(turnover, on="feature_name", how="outer")
    if not decay_summary.empty:
        decay = (
            decay_summary.groupby(["feature_name"], dropna=False)
            .agg(
                ic_decay_ratio=("ic_decay_ratio", "mean"),
                rank_ic_decay_ratio=("rank_ic_decay_ratio", "mean"),
                spread_decay_ratio=("spread_decay_ratio", "mean"),
            )
            .reset_index()
        )
        base = decay if base.empty else base.merge(decay, on="feature_name", how="outer")

    if base.empty:
        base = pd.DataFrame(columns=["feature_name"])
    if not configured_weights.empty:
        base = base.merge(configured_weights, left_on="feature_name", right_on="factor", how="left")
        if "factor" in base.columns:
            base.drop(columns=["factor"], inplace=True)

    for column in [
        "mean_ic",
        "mean_rank_ic",
        "ic_positive_rate",
        "rank_ic_positive_rate",
        "ic_ir",
        "rank_ic_ir",
        "mean_spread",
        "spread_ir",
        "spread_positive_rate",
        "mean_turnover",
        "ic_decay_ratio",
        "rank_ic_decay_ratio",
        "spread_decay_ratio",
    ]:
        if column not in base.columns:
            base[column] = pd.NA

    def _safe_value(value):
        return float(value) if pd.notna(value) else 0.0

    def _validation_score(row):
        metric_columns = [
            "mean_rank_ic",
            "mean_ic",
            "mean_spread",
            "rank_ic_positive_rate",
            "ic_positive_rate",
            "mean_turnover",
        ]
        if all(pd.isna(row.get(column)) for column in metric_columns):
            return 0.0
        turnover_bonus = (
            max(0.0, 1.0 - float(row["mean_turnover"])) * 5.0
            if pd.notna(row.get("mean_turnover"))
            else 0.0
        )
        return (
            abs(_safe_value(row["mean_rank_ic"])) * 35.0
            + abs(_safe_value(row["mean_ic"])) * 20.0
            + max(_safe_value(row["mean_spread"]), 0.0) * 100.0 * 20.0
            + _safe_value(row["rank_ic_positive_rate"]) * 15.0
            + _safe_value(row["ic_positive_rate"]) * 5.0
            + turnover_bonus
        )

    base["validation_score"] = base.apply(_validation_score, axis=1)

    # Auto-classify factors if component missing
    if "component" not in base.columns or base["component"].isna().all():
        from core import classify_factor
        base["component"] = base["feature_name"].apply(classify_factor)

    base.sort_values(["validation_score", "mean_rank_ic", "mean_spread"], ascending=False, inplace=True)
    base.reset_index(drop=True, inplace=True)

    base["recommended_factor_weight"] = pd.NA
    component_names = sorted(set(base["component"].dropna())) if "component" in base.columns else []
    if not component_names:
        component_names = ["trend", "quality", "risk"]
    for component_name in component_names:
        mask = base["component"].fillna("") == component_name
        if mask.any():
            total = pd.to_numeric(base.loc[mask, "validation_score"], errors="coerce").clip(lower=0).sum()
            if total > 0:
                base.loc[mask, "recommended_factor_weight"] = (
                    pd.to_numeric(base.loc[mask, "validation_score"], errors="coerce").clip(lower=0) / total
                )
            else:
                base.loc[mask, "recommended_factor_weight"] = pd.NA

    return base


def _build_factor_scorecard_ridge(report, ridge_alpha=1.0, target_horizon=5):
    """Use Ridge regression to estimate factor weights instead of hard-coded linear scoring.

    Small factor set (p <= n/2): cross-sectional regression per day, time-series mean of coefficients.
    Large factor set (p > n/2): stacked panel regression, single fit, faster and more stable when p>n.
    Falls back to original formula when validation_frame is unavailable.
    """
    try:
        from sklearn.linear_model import Ridge
    except ImportError:
        return _build_factor_scorecard(report)

    validation_frame = report.get("validation_frame")
    if validation_frame is None or validation_frame.empty:
        return _build_factor_scorecard(report)

    target_col = f"forward_return_{int(target_horizon)}"
    if target_col not in validation_frame.columns:
        return _build_factor_scorecard(report)

    pivot = validation_frame.pivot_table(
        index=["trade_date", "stock_code"],
        columns="feature_name",
        values="feature_value",
        aggfunc="last",
    )
    returns = validation_frame.groupby(["trade_date", "stock_code"])[target_col].last()
    common_idx = pivot.index.intersection(returns.index)
    if len(common_idx) == 0:
        return _build_factor_scorecard(report)
    pivot = pivot.loc[common_idx]
    returns = returns.loc[common_idx]

    feature_names = list(pivot.columns)
    K = len(feature_names)
    if K < 2:
        return _build_factor_scorecard(report)

    dates = list(pivot.index.get_level_values("trade_date").unique())
    avg_stocks = len(common_idx) // max(len(dates), 1)

    # Stacked panel regression: demean per day + single Ridge fit
    stacked_rows = []
    for trade_date in dates:
        X = pivot.xs(trade_date, level="trade_date").dropna()
        y = returns.xs(trade_date, level="trade_date").loc[X.index]
        common = X.index.intersection(y.dropna().index)
        if len(common) < max(5, 2):
            continue
        X = X.loc[common]
        y = y.loc[common].astype(float)
        X_mean = X.mean()
        X_std = X.std().replace(0, 1)
        y_mean = y.mean()
        X_scaled = (X - X_mean) / X_std
        y_centered = y - y_mean
        stacked_rows.append((X_scaled, y_centered))

    if not stacked_rows:
        return _build_factor_scorecard(report)

    X_all = pd.concat([r[0] for r in stacked_rows], axis=0)
    y_all = pd.concat([r[1] for r in stacked_rows], axis=0)

    model = Ridge(alpha=ridge_alpha, fit_intercept=False)
    model.fit(X_all.values.astype(np.float64), y_all.values.astype(np.float64))
    coefs = model.coef_

    rows = []
    for name, coef in zip(feature_names, coefs):
        rows.append({
            "feature_name": name,
            "ridge_coef": float(coef),
            "ridge_panel": True,
        })

    scorecard = pd.DataFrame(rows)
    scorecard["abs_ridge_coef"] = scorecard["ridge_coef"].abs()
    scorecard["higher_is_better"] = scorecard["ridge_coef"].fillna(0) > 0
    scorecard.sort_values("abs_ridge_coef", ascending=False, inplace=True)
    scorecard.reset_index(drop=True, inplace=True)

    # Auto-classify all factors so every factor gets a component assignment
    from core import classify_factor
    scorecard["component"] = scorecard["feature_name"].apply(classify_factor)

    configured_weights = _build_current_factor_weight_table(
        (report.get("metadata") or {}).get("factor_score_config") or {}
    )
    if not configured_weights.empty:
        # Preserve original component from config if it exists (override auto-classify)
        original_component_map = {}
        for _, cw_row in configured_weights.iterrows():
            fn = cw_row.get("factor")
            comp = cw_row.get("component")
            if fn and comp:
                original_component_map[fn] = comp
        if original_component_map:
            scorecard["component"] = scorecard.apply(
                lambda r: original_component_map.get(r["feature_name"], r["component"]), axis=1
            )

        # Drop overlapping columns from configured_weights to avoid _x/_y suffix
        overlap = [c for c in configured_weights.columns if c in scorecard.columns and c not in ("factor", "feature_name")]
        if overlap:
            configured_weights = configured_weights.drop(columns=overlap)
        scorecard = scorecard.merge(configured_weights, left_on="feature_name", right_on="factor", how="left")
        if "factor" in scorecard.columns:
            scorecard.drop(columns=["factor"], inplace=True)

    # --- Multi-dimensional validation_score ---
    # Default: abs Ridge coefficient
    scorecard["validation_score"] = scorecard["abs_ridge_coef"]
    scorecard["validation_score_components"] = "ridge_only"

    # Try to incorporate IC, Fama-MacBeth, monotonicity if available
    ic_summary = report.get("ic_summary")
    fm_result = report.get("fm_result")
    monotonicity = report.get("monotonicity")

    if ic_summary is not None and not ic_summary.empty:
        # Merge |mean_rank_ic| from ic_summary (shortest horizon per factor)
        ic_rank = ic_summary.copy()
        if "horizon" in ic_rank.columns:
            ic_rank = ic_rank.sort_values("horizon").groupby("feature_name").first().reset_index()
        if "mean_rank_ic" in ic_rank.columns:
            scorecard = scorecard.merge(
                ic_rank[["feature_name", "mean_rank_ic"]].rename(columns={"mean_rank_ic": "_mean_rank_ic"}),
                on="feature_name", how="left",
            )

    if fm_result is not None and not fm_result.empty and "fm_tstat" in fm_result.columns:
        scorecard = scorecard.merge(
            fm_result[["feature_name", "fm_tstat", "fm_pvalue"]].rename(
                columns={"fm_tstat": "_fm_tstat", "fm_pvalue": "_fm_pvalue"}
            ),
            on="feature_name", how="left",
        )

    if monotonicity is not None and not monotonicity.empty and "monotonicity_score" in monotonicity.columns:
        scorecard = scorecard.merge(
            monotonicity[["feature_name", "monotonicity_score"]].rename(
                columns={"monotonicity_score": "_monotonicity_score"}
            ),
            on="feature_name", how="left",
        )

    # Build composite if auxiliary metrics are available
    aux_cols_present = [c for c in ["_mean_rank_ic", "_fm_tstat", "_monotonicity_score"] if c in scorecard.columns]
    if len(aux_cols_present) >= 2:
        def _minmax_norm(s):
            clean = pd.to_numeric(s, errors="coerce")
            mn, mx = clean.min(), clean.max()
            if pd.isna(mn) or pd.isna(mx) or mx == mn:
                return pd.Series(0.5, index=s.index)
            return (clean - mn) / (mx - mn)

        score_components = {"_ridge_norm": _minmax_norm(scorecard["abs_ridge_coef"]) * 0.40}

        if "_mean_rank_ic" in scorecard.columns:
            score_components["_ic_norm"] = _minmax_norm(scorecard["_mean_rank_ic"].abs()) * 0.15
        else:
            score_components["_ridge_norm"] += 0.15

        if "_fm_tstat" in scorecard.columns:
            score_components["_fm_norm"] = _minmax_norm(scorecard["_fm_tstat"].abs().clip(upper=5)) * 0.20
        else:
            score_components["_ridge_norm"] += 0.20

        if "_monotonicity_score" in scorecard.columns:
            score_components["_mono_norm"] = _minmax_norm(scorecard["_monotonicity_score"]) * 0.15
        else:
            score_components["_ridge_norm"] += 0.15

        # rank_autocorr placeholder (0.10 redistributed to Ridge if not available)
        score_components["_ridge_norm"] += 0.10

        composite = sum(score_components.values())
        scorecard["validation_score"] = composite.clip(lower=0)
        scorecard["validation_score_components"] = "+".join(score_components.keys())

    scorecard["recommended_factor_weight"] = pd.NA
    component_names = sorted(set(scorecard["component"].dropna()))
    for component_name in component_names:
        mask = scorecard["component"].fillna("") == component_name
        if mask.any():
            total = pd.to_numeric(scorecard.loc[mask, "validation_score"], errors="coerce").clip(lower=0).sum()
            if total > 0:
                scorecard.loc[mask, "recommended_factor_weight"] = (
                    pd.to_numeric(scorecard.loc[mask, "validation_score"], errors="coerce").clip(lower=0) / total
                )

    return scorecard
