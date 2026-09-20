"""Meta-labeling gate for a primary cross-sectional signal (P1.17 W2).

The primary model answers "which names rank best".  It cannot answer "will this
specific entry ever pay", because its target is a within-date cross-sectional
rank.  A meta model adds that second decision on the *already selected* subset:
its features are the primary score plus the candidate's own path context, and
its label is the realized triple-barrier outcome produced by
``factor_engine.ml.strategy_labels``.

The design follows Lopez de Prado's meta-labeling, applied as a *gate* rather
than a re-ranker: a re-ranker would let an all-universe negative-alpha event
field pollute the cross-sectional ordering, while a gate only decides whether
to act on names the primary model already chose and how much to size them.

Every statistic used here is fitted inside the training fold.  The gate is
therefore never calibrated on the outcomes it is evaluated against.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from factor_engine.ml.model_training import _purged_time_split
from factor_engine.ml.preprocessing import preprocess_features_by_date

# Cost defaults are the A-share convention used by the CN paper account plus the
# sell-side stamp duty.  They are configuration, never hard-coded in callers.
DEFAULT_COMMISSION_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_STAMP_DUTY_BPS = 5.0

# Candidate-level context for the gate.  Names may be given with or without the
# ``_clean`` suffix; ``resolve_meta_feature_columns`` maps them onto the panel.
DEFAULT_META_FEATURES = (
    "model_score",
    "model_score_raw",
    "tr_volatility_20",
    "vol_atr_pct_14",
    "pv_close_to_ma20",
    "pv_return_20d",
    "pv_volume_ratio_20d",
    "turnover_rate",
    "price_position_52w_high",
    "dist_from_120d_low",
    "dist_from_60d_high",
    "return_60d",
    "flow_second_wave_flag",
    "flow_second_wave_source_count",
    "flow_second_wave_max_z5",
    "flow_second_wave_pullback_pct",
    "moneyflow_net_z_5d",
)

META_LABEL_COLUMNS = (
    "label_tb_class",
    "label_tb_ret",
    "label_tb_hold_days",
    "label_mfe_20d",
    "label_mae_20d",
    "startup_price_eligible",
)

# Candidate context that lives on the label frame rather than the feature panel:
# the startup-eligible opportunity geometry is a price label-side view of the
# same close, so the gate reads it from labels instead of recomputing it.
META_CONTEXT_COLUMNS = (
    "dist_from_120d_low",
    "dist_from_60d_high",
    "return_60d",
    "startup_price_eligible",
)


def resolve_meta_feature_columns(
    available_columns,
    requested=DEFAULT_META_FEATURES,
    *,
    include_missing_masks: bool = True,
) -> tuple[list[str], list[str]]:
    """Map requested meta features onto the panel and report what is absent.

    Returns ``(resolved, missing)``.  Missing names are reported instead of
    being silently dropped: a gate trained on a different feature set than the
    manifest claims is not auditable.
    """
    available = {str(column) for column in available_columns}
    resolved: list[str] = []
    missing: list[str] = []
    for name in requested or ():
        name = str(name)
        hit = next((column for column in _panel_candidates(name) if column in available), None)
        if hit is None:
            missing.append(name)
            continue
        resolved.append(hit)
        mask = f"{name}_is_missing"
        if include_missing_masks and mask in available:
            resolved.append(mask)
    return list(dict.fromkeys(resolved)), missing


def _panel_candidates(name: str) -> list[str]:
    """Resolve a logical feature name against the compact panel naming scheme."""
    name = str(name)
    if name in {"model_score", "model_score_raw"} or name.endswith(("_clean", "_is_missing")):
        return [name]
    return [f"{name}_clean", name]


def select_signal_candidates(
    scores: pd.DataFrame,
    *,
    score_column: str = "model_score",
    candidate_quantile: float = 0.10,
    min_candidates: int = 5,
) -> pd.DataFrame:
    """Keep the top ``candidate_quantile`` share of each date's primary signal."""
    if scores is None or scores.empty:
        return pd.DataFrame()
    frame = scores.loc[scores[score_column].notna()].copy()
    if frame.empty:
        return frame
    quantile = float(min(max(candidate_quantile, 0.0), 1.0))
    frame["signal_rank"] = frame.groupby("trade_date")[score_column].rank(ascending=False, method="first")
    counts = frame.groupby("trade_date")[score_column].transform("size")
    keep = np.maximum(int(min_candidates), np.ceil(counts * quantile).astype(int))
    frame = frame.loc[frame["signal_rank"] <= keep].copy()
    frame["candidate_count"] = frame.groupby("trade_date")[score_column].transform("size")
    return frame


def attach_meta_inputs(
    candidates: pd.DataFrame,
    *,
    panel: pd.DataFrame | None = None,
    labels: pd.DataFrame | None = None,
    feature_columns=(),
    context_columns=META_CONTEXT_COLUMNS,
) -> pd.DataFrame:
    """Attach gate inputs to primary candidates.

    The primary predictor only returns scores, so the gate's own features must
    be joined back from the feature panel, and the startup-geometry context from
    the label frame.  Outcome columns are refused outright: a gate that can see
    ``label_*`` or ``forward_*`` would be reading its own answer.
    """
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    forbidden = [
        str(column)
        for column in feature_columns
        if str(column).startswith(("label_", "forward_"))
    ]
    if forbidden:
        raise ValueError(f"meta gate features must not contain outcome columns: {','.join(sorted(forbidden))}")
    join_columns = ["stock_code", "trade_date"]
    working = candidates
    requested = [str(column) for column in (feature_columns or ())]
    if panel is not None and not panel.empty and requested:
        available: list[str] = []
        for name in requested:
            hit = next((candidate for candidate in _panel_candidates(name) if candidate in panel.columns), None)
            if hit is not None:
                available.append(hit)
            mask = f"{name}_is_missing"
            if mask in panel.columns:
                available.append(mask)
        available = [column for column in dict.fromkeys(available) if column not in working.columns]
        if available:
            working = working.merge(
                panel.loc[:, join_columns + available].drop_duplicates(join_columns),
                on=join_columns,
                how="left",
                suffixes=("", "_panel"),
            )
    if labels is not None and not labels.empty:
        context = [
            column
            for column in (context_columns or ())
            if column in labels.columns and column not in working.columns
        ]
        if context:
            working = working.merge(
                labels.loc[:, join_columns + context].drop_duplicates(join_columns),
                on=join_columns,
                how="left",
            )
    return working.reset_index(drop=True)


def build_meta_labels(
    candidates: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    meta_label_column: str = "label_tb_class",
    positive_value: float = 1.0,
    label_columns=META_LABEL_COLUMNS,
    context_columns=META_CONTEXT_COLUMNS,
    drop_unlabeled: bool = True,
) -> pd.DataFrame:
    """Attach the realized path outcome to each primary candidate.

    ``meta_label = 1`` means the candidate touched the take-profit barrier
    before its stop, i.e. the primary signal was worth acting on.
    """
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    join_columns = ["stock_code", "trade_date"]
    wanted = [
        column
        for column in (meta_label_column, *label_columns, *(context_columns or ()))
        if column in labels.columns and column not in candidates.columns
    ]
    wanted = list(dict.fromkeys(wanted))
    working = candidates.merge(labels[join_columns + wanted], on=join_columns, how="left")
    if meta_label_column not in working.columns:
        raise ValueError(f"meta label column {meta_label_column!r} is not available")
    outcome = pd.to_numeric(working[meta_label_column], errors="coerce")
    working["meta_label"] = np.where(
        outcome.isna(), np.nan, (outcome == float(positive_value)).astype(float)
    )
    if drop_unlabeled:
        working = working.loc[working["meta_label"].notna()].copy()
    return working


def train_meta_label_panel(
    frame: pd.DataFrame,
    feature_columns: list[str],
    *,
    model_dir,
    label_column: str = "meta_label",
    validation_share: float = 0.30,
    purge_days: int = 20,
    n_estimators: int = 300,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    max_depth: int = 5,
    min_child_samples: int = 50,
    reg_lambda: float = 10.0,
    random_state: int = 42,
    extra_manifest: dict | None = None,
    show_progress: bool = False,
) -> dict:
    """Fit the second-stage binary classifier on primary candidates only."""
    import lightgbm as lgb

    if frame is None or frame.empty:
        raise ValueError("no candidate rows available for meta-label training")
    features = [column for column in feature_columns if column in frame.columns]
    if not features:
        raise ValueError("no meta feature columns available in the candidate frame")
    working = frame.copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working = working.dropna(subset=["trade_date", "stock_code", label_column])
    if working.empty:
        raise ValueError("no labeled candidate rows available for meta-label training")
    working, preprocessing = preprocess_features_by_date(working, features)
    dates = sorted(working["trade_date"].unique())
    validation_share = float(min(max(validation_share, 0.05), 0.9))
    validation_days = max(1, int(round(len(dates) * validation_share)))
    train, valid, split = _purged_time_split(
        working, validation_days=validation_days, embargo_days=int(purge_days)
    )
    if train.empty or valid.empty:
        raise ValueError("not enough candidate dates for a purged meta train/validation split")
    base_rate = float(pd.to_numeric(train[label_column], errors="coerce").mean())
    positive_weight = float((1.0 - base_rate) / base_rate) if 0.0 < base_rate < 1.0 else 1.0
    params = {
        "objective": "binary",
        "learning_rate": float(learning_rate),
        "n_estimators": int(n_estimators),
        "num_leaves": int(num_leaves),
        "max_depth": int(max_depth),
        "min_child_samples": int(min_child_samples),
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "reg_lambda": float(reg_lambda),
        "random_state": int(random_state),
        "n_jobs": -1,
        "verbosity": -1,
        "is_unbalance": True if 0.2 <= base_rate <= 0.8 else False,
        "scale_pos_weight": positive_weight if base_rate < 0.2 else 1.0,
    }
    model = lgb.LGBMClassifier(**params)
    model.fit(train[features], train[label_column].astype(int))
    valid_probability = model.predict_proba(valid[features])[:, 1]
    valid_target = valid[label_column].astype(int).to_numpy()
    valid_frame = valid[["trade_date", label_column]].copy()
    valid_frame["probability"] = valid_probability
    validation_auc = _binary_auc(valid_target, valid_probability)
    per_date_auc = [
        value
        for value in (
            _binary_auc(group[label_column].astype(int).to_numpy(), group["probability"].to_numpy())
            for _, group in valid_frame.groupby("trade_date")
        )
        if value is not None and not np.isnan(value)
    ]
    train_top_decile_rate = _top_decile_positive_rate(
        model.predict_proba(train[features])[:, 1], train[label_column].astype(int).to_numpy()
    )
    directory = Path(model_dir)
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "meta_model.txt"
    model.booster_.save_model(str(model_path))
    manifest = {
        "model_type": "meta_label_gate",
        "label_column": label_column,
        "meta_feature_columns": features,
        "meta_feature_count": len(features),
        "params": params,
        "preprocessing": preprocessing,
        "split": split,
        "train_rows": int(len(train)),
        "validation_rows": int(len(valid)),
        "train_start": str(pd.Timestamp(train["trade_date"].min()).date()),
        "train_end": str(pd.Timestamp(train["trade_date"].max()).date()),
        "validation_start": str(pd.Timestamp(valid["trade_date"].min()).date()),
        "validation_end": str(pd.Timestamp(valid["trade_date"].max()).date()),
        "train_base_rate": base_rate,
        "validation_base_rate": float(pd.to_numeric(valid[label_column], errors="coerce").mean()),
        "validation_auc": validation_auc,
        "validation_per_date_auc_mean": float(np.mean(per_date_auc)) if per_date_auc else None,
        "validation_per_date_auc_count": len(per_date_auc),
        "selection_rate": float((valid_probability >= 0.5).mean()),
        "train_top_decile_positive_rate": train_top_decile_rate,
        "extra": dict(extra_manifest or {}),
    }
    manifest_path = directory / "meta_model_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if show_progress:
        print(
            f"[META] rows={len(train)}/{len(valid)} features={len(features)} "
            f"base_rate={base_rate:.4f} auc={validation_auc}",
            flush=True,
        )
    return {
        "model_path": str(model_path),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "validation_auc": validation_auc,
    }


def _binary_auc(target: np.ndarray, probability: np.ndarray) -> float | None:
    target = np.asarray(target, dtype=float)
    probability = np.asarray(probability, dtype=float)
    valid = np.isfinite(target) & np.isfinite(probability)
    target, probability = target[valid], probability[valid]
    positives = target == 1.0
    negatives = target == 0.0
    if positives.sum() == 0 or negatives.sum() == 0:
        return None
    order = np.argsort(probability, kind="stable")
    ranks = np.empty(len(probability), dtype=float)
    ranks[order] = np.arange(1, len(probability) + 1, dtype=float)
    positive_rank_sum = ranks[positives].sum()
    count_positive = float(positives.sum())
    count_negative = float(negatives.sum())
    return float(
        (positive_rank_sum - count_positive * (count_positive + 1) / 2.0)
        / (count_positive * count_negative)
    )


def _top_decile_positive_rate(probability: np.ndarray, target: np.ndarray, *, quantile: float = 0.9) -> float | None:
    probability = np.asarray(probability, dtype=float)
    target = np.asarray(target, dtype=float)
    if probability.size == 0:
        return None
    cutoff = float(np.quantile(probability, float(quantile)))
    selected = probability >= cutoff
    if not selected.any():
        return None
    return float(target[selected].mean())


def score_meta_label_panel(frame: pd.DataFrame, *, model_path, manifest_path) -> pd.DataFrame:
    """Score candidate rows with a persisted meta model."""
    import lightgbm as lgb

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    features = list(manifest["meta_feature_columns"])
    missing = [column for column in features if column not in frame.columns]
    if missing:
        raise ValueError(f"meta scoring frame is missing feature columns: {','.join(sorted(missing))}")
    working = frame.copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working, _ = preprocess_features_by_date(working, features)
    booster = lgb.Booster(model_file=str(model_path))
    probability = np.asarray(booster.predict(working[features]), dtype=float)
    out = working.loc[:, ["trade_date", "stock_code"]].copy()
    out["meta_probability"] = probability
    return out


def apply_rule_gate(
    candidates: pd.DataFrame,
    *,
    column: str,
    threshold: float,
    direction: str = "le",
    act_quantile: float | None = None,
    probability_column: str = "meta_probability",
    score_column: str = "model_score",
) -> pd.DataFrame:
    """Gate candidates with an explicit, auditable threshold rule.

    The learned gate is rejected on the P1.17 OOS sample (see the plan's W2
    section); the surviving evidence is a single decision rule, so the pipeline
    can run the rule and keep the same downstream columns (``meta_act`` and
    ``meta_weight``) that the learned gate produces.

    ``direction`` is ``le`` (keep values at or below the threshold) or ``ge``.
    ``act_quantile`` optionally caps the approved share per decision date.
    """
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    if column not in candidates.columns:
        raise ValueError(f"rule gate column {column!r} is not available")
    direction = str(direction or "le").strip().lower()
    if direction not in {"le", "ge", ">=", "<="}:
        raise ValueError(f"unsupported rule gate direction: {direction!r}")
    working = candidates.copy()
    value = pd.to_numeric(working[column], errors="coerce")
    if direction == "le":
        working["meta_act"] = (value <= float(threshold)).fillna(False)
    else:
        working["meta_act"] = (value >= float(threshold)).fillna(False)
    if act_quantile is not None and float(act_quantile) < 1.0:
        quantile = float(min(max(act_quantile, 0.0), 1.0))
        rank = working.groupby("trade_date")[score_column].rank(ascending=False, method="first")
        keep = np.maximum(
            1.0, np.ceil(working.groupby("trade_date")[score_column].transform("size") * quantile)
        )
        working["meta_act"] &= rank <= keep
    # Rule gates carry no probability, but downstream sizing expects a weight:
    # approved names share the book equally within each decision date.
    approved = working["meta_act"].astype(float)
    date_total = approved.groupby(working["trade_date"]).transform("sum")
    working[probability_column] = approved
    working["meta_weight"] = np.where(date_total > 0, approved / date_total.replace(0, np.nan), 0.0)
    return working.reset_index(drop=True)


def apply_meta_gate(
    candidates: pd.DataFrame,
    probabilities: pd.DataFrame | None = None,
    *,
    probability_column: str = "meta_probability",
    score_column: str = "model_score",
    act_quantile: float | None = 0.50,
    min_probability: float | None = None,
    weight_floor: float = 0.0,
) -> pd.DataFrame:
    """Attach the gate decision and a probability-scaled position weight.

    ``act_quantile`` keeps the best share of each date's candidates by gate
    probability.  ``min_probability`` is an absolute alternative.  Both are
    computed within the decision date, so the gate cannot shift with the market
    regime.
    """
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    working = candidates.copy()
    if probabilities is not None and not probabilities.empty:
        working = working.merge(
            probabilities, on=["stock_code", "trade_date"], how="left", suffixes=("", "_scored")
        )
    if probability_column not in working.columns:
        raise ValueError(f"meta probabilities are not available under {probability_column!r}")
    working["meta_act"] = True
    probability = pd.to_numeric(working[probability_column], errors="coerce")
    if min_probability is not None:
        working["meta_act"] &= probability >= float(min_probability)
    if act_quantile is not None:
        quantile = float(min(max(act_quantile, 0.0), 1.0))
        if quantile < 1.0:
            within_date_rank = working.groupby("trade_date")[probability_column].rank(
                ascending=False, method="first"
            )
            keep = np.maximum(1.0, np.ceil(working.groupby("trade_date")[score_column].transform("size") * quantile))
            working["meta_act"] &= within_date_rank <= keep
    probability = probability.fillna(0.0).clip(lower=0.0)
    working["meta_probability"] = probability
    if float(weight_floor) > 0.0:
        probability = probability.clip(lower=float(weight_floor))
    date_total = probability.groupby(working["trade_date"]).transform("sum")
    working["meta_weight"] = np.where(date_total > 0, probability / date_total.replace(0, np.nan), 0.0)
    return working.reset_index(drop=True)


def _round_trip_cost_bps(
    *,
    commission_bps: float = DEFAULT_COMMISSION_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    stamp_duty_bps: float = DEFAULT_STAMP_DUTY_BPS,
) -> float:
    """Buy + sell cost in bps: two commissions, two slippages, one sell tax."""
    return 2.0 * (float(commission_bps) + float(slippage_bps)) + float(stamp_duty_bps)


def evaluate_meta_gate(
    decisions: pd.DataFrame,
    *,
    top_k: int = 20,
    score_column: str = "model_score",
    act_column: str = "meta_act",
    gross_return_column: str = "forward_exec_return_20d",
    excess_return_column: str = "forward_excess_return_20d",
    gross_return_column_fallbacks=("forward_exec_return_20d", "forward_return_20d"),
    commission_bps: float = DEFAULT_COMMISSION_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    stamp_duty_bps: float = DEFAULT_STAMP_DUTY_BPS,
) -> dict:
    """Compare the gated and ungated Top-K arms on realized, executable returns.

    Both arms use the same holding horizon and the same universe, so the
    difference is attributable to the gate alone.  Costs are charged on the
    fraction of the book that is replaced between consecutive decision dates.
    """
    if decisions is None or decisions.empty:
        raise ValueError("no decision rows available for meta-gate evaluation")
    working = decisions.copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    gross_column = excess_return_column if excess_return_column in working.columns else None
    if gross_column is None:
        gross_column = next(
            (column for column in gross_return_column_fallbacks if column in working.columns), None
        )
    if gross_column is None:
        raise ValueError("no realized forward-return column available for meta-gate evaluation")
    working = working.loc[working[gross_column].notna()].copy()
    if working.empty:
        raise ValueError("no realized outcomes available for meta-gate evaluation")
    cost_bps = _round_trip_cost_bps(
        commission_bps=commission_bps, slippage_bps=slippage_bps, stamp_duty_bps=stamp_duty_bps
    )
    cost_rate = cost_bps / 10_000.0
    size = max(1, int(top_k))

    def _arm(frame: pd.DataFrame, *, gated: bool) -> tuple[pd.DataFrame, dict]:
        selected = frame.loc[frame[act_column].fillna(False)] if gated else frame
        rows = []
        previous_names: dict[pd.Timestamp, set[str]] = {}
        for trade_date, group in selected.groupby("trade_date", sort=True):
            ordered = group.sort_values(score_column, ascending=False).head(size)
            if ordered.empty:
                rows.append(
                    {"trade_date": trade_date, "held": 0, "gross": 0.0, "turnover": 0.0, "names": set()}
                )
                continue
            names = set(ordered["stock_code"].astype(str))
            previous = previous_names.get(trade_date)
            turnover = 1.0 if previous is None else 1.0 - len(names & previous) / max(len(names), 1)
            rows.append(
                {
                    "trade_date": trade_date,
                    "held": int(len(ordered)),
                    "gross": float(ordered[gross_column].mean()),
                    "turnover": float(turnover),
                    "names": names,
                }
            )
        arm = pd.DataFrame(rows)
        if arm.empty:
            return arm, {}
        arm["net"] = arm["gross"] - arm["turnover"] * cost_rate
        # Deployment-diluted variant: each approved name gets 1/K of the book and
        # the rest stays in cash, which is what an under-filled shortlist does.
        arm["deployed"] = arm["held"] / size
        arm["net_diluted"] = arm["net"] * arm["deployed"]
        return arm, {
            "dates": int(len(arm)),
            "average_holdings": float(arm["held"].mean()),
            "mean_gross": float(arm["gross"].mean()),
            "mean_net": float(arm["net"].mean()),
            "mean_net_diluted": float(arm["net_diluted"].mean()),
            "mean_turnover": float(arm["turnover"].mean()),
            "hit_rate": float((arm["gross"] > 0).mean()),
            "stdev_net": float(arm["net"].std(ddof=1)) if len(arm) > 1 else None,
            "t_stat_net": _one_sample_t(arm["net"].to_numpy()),
        }

    ungated_arm, ungated = _arm(working, gated=False)
    gated_arm, gated = _arm(working, gated=True)
    paired = ungated_arm.merge(
        gated_arm, on="trade_date", how="inner", suffixes=("_ungated", "_gated")
    )
    paired["net_diff"] = paired["net_gated"] - paired["net_ungated"]
    approved = working.loc[working[act_column].fillna(False), gross_column]
    rejected = working.loc[~working[act_column].fillna(False), gross_column]
    return {
        "return_column": gross_column,
        "top_k": size,
        "cost_bps_round_trip": cost_bps,
        "commission_bps": float(commission_bps),
        "slippage_bps": float(slippage_bps),
        "stamp_duty_bps": float(stamp_duty_bps),
        "ungated": ungated,
        "gated": gated,
        "net_mean_difference": float(paired["net_diff"].mean()) if not paired.empty else None,
        "net_mean_difference_t": _one_sample_t(paired["net_diff"].to_numpy()) if not paired.empty else None,
        "approved_mean": float(approved.mean()) if not approved.empty else None,
        "approved_count": int(approved.size),
        "rejected_mean": float(rejected.mean()) if not rejected.empty else None,
        "rejected_count": int(rejected.size),
        "approved_minus_rejected": (
            float(approved.mean() - rejected.mean()) if not approved.empty and not rejected.empty else None
        ),
        "approved_minus_rejected_t": _two_sample_t(rejected.to_numpy(), approved.to_numpy())
        if not approved.empty and not rejected.empty
        else None,
        "approved_hit_rate": float((approved > 0).mean()) if not approved.empty else None,
        "rejected_hit_rate": float((rejected > 0).mean()) if not rejected.empty else None,
    }


def _one_sample_t(values: np.ndarray) -> float | None:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return None
    stderr = values.std(ddof=1) / np.sqrt(values.size)
    if not np.isfinite(stderr) or stderr == 0:
        return None
    return float(values.mean() / stderr)


def _two_sample_t(left: np.ndarray, right: np.ndarray) -> float | None:
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    left = left[np.isfinite(left)]
    right = right[np.isfinite(right)]
    if left.size < 2 or right.size < 2:
        return None
    difference = float(right.mean() - left.mean())
    stderr = float(np.sqrt(left.var(ddof=1) / left.size + right.var(ddof=1) / right.size))
    if not np.isfinite(stderr) or stderr == 0:
        return None
    return difference / stderr


def write_meta_gate_report(metrics: dict, *, output_dir, prefix: str = "cn_meta_gate") -> dict:
    """Persist the gate comparison as markdown + json."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{prefix}.json"
    markdown_path = directory / f"{prefix}.md"
    json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        f"# Meta-label gate evaluation: {prefix}",
        "",
        f"Realized return column: `{metrics.get('return_column')}`; Top-K = {metrics.get('top_k')}; "
        f"round-trip cost = {metrics.get('cost_bps_round_trip')} bps "
        f"(commission {metrics.get('commission_bps')}/side, slippage {metrics.get('slippage_bps')}/side, "
        f"stamp duty {metrics.get('stamp_duty_bps')} on the sell leg).",
        "",
        "| Metric | Ungated | Gated |",
        "|---|---:|---:|",
    ]
    for key in ("dates", "average_holdings", "mean_gross", "mean_net", "mean_net_diluted", "mean_turnover", "hit_rate", "t_stat_net"):
        lines.append(
            f"| {key} | {_format(metrics.get('ungated', {}).get(key))} | {_format(metrics.get('gated', {}).get(key))} |"
        )
    lines.extend(
        [
            "",
            f"| Gated minus ungated net (per date) | {_format(metrics.get('net_mean_difference'))} "
            f"(t={_format(metrics.get('net_mean_difference_t'))}) |",
            f"| Approved mean / count | {_format(metrics.get('approved_mean'))} / {metrics.get('approved_count')} |",
            f"| Rejected mean / count | {_format(metrics.get('rejected_mean'))} / {metrics.get('rejected_count')} |",
            f"| Approved minus rejected | {_format(metrics.get('approved_minus_rejected'))} "
            f"(t={_format(metrics.get('approved_minus_rejected_t'))}) |",
            "",
            f"JSON: `{json_path}`",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _format(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "DEFAULT_META_FEATURES",
    "apply_rule_gate",
    "META_LABEL_COLUMNS",
    "apply_meta_gate",
    "attach_meta_inputs",
    "build_meta_labels",
    "evaluate_meta_gate",
    "resolve_meta_feature_columns",
    "score_meta_label_panel",
    "select_signal_candidates",
    "train_meta_label_panel",
    "write_meta_gate_report",
]
