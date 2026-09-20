"""Chronological out-of-sample prediction generation for model comparison."""

from __future__ import annotations

import json
import time
import warnings
from pathlib import Path

import pandas as pd
from tqdm import tqdm

# Sparse CN panels can legitimately contain an all-NaN feature on an individual date.
# Preprocessing/training replace those statistics with safe defaults; avoid flooding
# a long OOS run with this benign NumPy warning.
warnings.filterwarnings("ignore", message="All-NaN slice encountered", category=RuntimeWarning)

from factor_engine.ml.model_training import (
    _prepare_labeled_panel,
    predict_cnn_panel,
    predict_lightgbm_panel,
    predict_transformer_panel,
    train_lightgbm_panel,
    train_cnn_panel,
    train_transformer_panel,
)
from factor_engine.ml.validation import expanding_walk_forward_splits
from factor_engine.ml.graph_temporal import build_industry_adjacency, predict_graph_temporal_panel, train_graph_temporal_panel
from factor_engine.ml.meta_labeling import (
    DEFAULT_META_FEATURES,
    META_CONTEXT_COLUMNS,
    META_LABEL_COLUMNS,
    apply_meta_gate,
    apply_rule_gate,
    attach_meta_inputs,
    build_meta_labels,
    evaluate_meta_gate,
    resolve_meta_feature_columns,
    score_meta_label_panel,
    select_signal_candidates,
    train_meta_label_panel,
    write_meta_gate_report,
)

# Declared availability rule for every OOS artifact produced here.  A decision is
# taken at the close of ``trade_date`` (so same-session price and money-flow
# fields are usable) and the simulated entry is the next session's open.  Any
# change here invalidates the labels in ``factor_engine.ml.strategy_labels``.
AVAILABILITY_RULE = {
    "decision_point": "session close of trade_date",
    "execution_point": "next session open",
    "entry_delay_sessions": 1,
    "moneyflow_available": "same session close (published after the close, before the next open)",
    "path_label_embargo_sessions": 60,
}


def _asof_sequence_window(
    panel: pd.DataFrame,
    *,
    test_date,
    lookback: int,
    label_column: str,
) -> pd.DataFrame:
    """Return only the history needed to score one as-of sequence per stock.

    Sequence predictors use the final ``lookback`` observations of each stock.
    Passing the complete history for every OOS decision date was equivalent but
    repeatedly copied an ever-growing panel.  A three-times-calendar lookback
    safely covers exchange holidays while retaining enough observations for a
    complete sequence; instruments with fewer observations remain ineligible.
    """
    cutoff = pd.Timestamp(test_date)
    calendar_days = max(90, int(lookback) * 3)
    window = panel.loc[
        panel["trade_date"].between(cutoff - pd.Timedelta(days=calendar_days), cutoff),
    ].drop(columns=[label_column], errors="ignore")
    return window.groupby("stock_code", sort=False, group_keys=False).tail(int(lookback)).copy()


def _oos_sequence_context(
    panel: pd.DataFrame,
    *,
    test_dates,
    lookback: int,
    label_column: str,
) -> pd.DataFrame:
    """Return one causal context panel for all target dates in an OOS block."""
    targets = pd.DatetimeIndex(pd.to_datetime(list(test_dates))).dropna().sort_values().unique()
    if not len(targets):
        return pd.DataFrame()
    context_days = max(90, int(lookback) * 3)
    start = targets[0] - pd.Timedelta(days=context_days)
    end = targets[-1]
    return panel.loc[
        panel["trade_date"].between(start, end),
    ].drop(columns=[label_column], errors="ignore").copy()


def _scored_test_dates(test_dates, prediction_stride: int) -> list[pd.Timestamp]:
    """Select deterministic rebalancing dates from a chronological OOS block."""
    stride = max(1, int(prediction_stride))
    return list(test_dates)[::stride]


def generate_lightgbm_oos_predictions(
    panel: pd.DataFrame,
    feature_columns: list[str],
    *,
    label_column: str,
    output_dir,
    n_splits=5,
    min_train_days=120,
    test_days=None,
    purge_days=20,
    embargo_days=0,
    cleaning_version="p0.2.v1",
    factor_set=None,
    min_feature_coverage=0.05,
    drop_constant_features=True,
    prediction_stride=1,
    show_progress=False,
) -> dict:
    """Fit one LightGBM per expanding fold and persist strictly OOS predictions."""
    prepared, features, _ = _prepare_labeled_panel(
        panel, feature_columns, label_column, filter_features=False,
    )
    folds = expanding_walk_forward_splits(
        prepared["trade_date"].unique(), n_splits=n_splits, min_train_days=min_train_days,
        test_days=test_days, purge_days=purge_days, embargo_days=embargo_days,
    )
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    parts = []
    fold_iter = tqdm(folds, desc="LightGBM OOS folds", unit="fold") if show_progress else folds
    for fold in fold_iter:
        train = prepared[prepared["trade_date"].isin(set(fold.train_dates))].copy()
        test_dates = _scored_test_dates(fold.test_dates, prediction_stride)
        if show_progress:
            print(
                f"[LIGHTGBM OOS] fold={fold.fold}/{len(folds)} "
                f"train={fold.train_dates[0]:%Y-%m-%d}..{fold.train_dates[-1]:%Y-%m-%d} "
                f"test={fold.test_start:%Y-%m-%d}..{fold.test_end:%Y-%m-%d} "
                f"scoring_dates={len(test_dates)}/{len(fold.test_dates)}",
                flush=True,
            )
        test = prepared[prepared["trade_date"].isin(set(test_dates))].copy()
        artifact = train_lightgbm_panel(
            train, features, model_dir=directory / f"lightgbm_fold_{fold.fold}", label_column=label_column,
            validation_days=max(1, min(20, len(fold.train_dates) // 5)), cleaning_version=cleaning_version,
            factor_set=factor_set, embargo_days=0,
            min_feature_coverage=min_feature_coverage,
            drop_constant_features=drop_constant_features,
            show_progress=show_progress,
        )
        scores = predict_lightgbm_panel(test, model_path=artifact["artifact"]["model_path"], manifest_path=artifact["artifact"]["manifest_path"])
        scores = scores.merge(test[["trade_date", "stock_code", label_column]], on=["trade_date", "stock_code"], how="inner")
        scores["fold"] = fold.fold
        parts.append(scores)
    return _persist_oos(parts, directory, "lightgbm", label_column, folds)


def generate_lightgbm_meta_oos_predictions(
    panel: pd.DataFrame,
    feature_columns: list[str],
    *,
    label_column: str,
    label_frame: pd.DataFrame,
    output_dir,
    meta_dir=None,
    n_splits=5,
    min_train_days=120,
    test_days=None,
    purge_days=20,
    embargo_days=0,
    cleaning_version="p0.2.v1",
    factor_set=None,
    min_feature_coverage=0.05,
    drop_constant_features=True,
    prediction_stride=1,
    show_progress=False,
    meta_features=None,
    meta_label_column="label_tb_class",
    meta_positive_value=1.0,
    candidate_quantile=0.10,
    act_quantile=0.50,
    min_probability=None,
    inner_share=0.30,
    inner_purge_days=20,
    meta_validation_share=0.30,
    meta_purge_days=20,
    meta_n_estimators=300,
    meta_learning_rate=0.05,
    meta_num_leaves=31,
    meta_max_depth=5,
    meta_min_child_samples=50,
    meta_reg_lambda=10.0,
    meta_random_state=42,
    gate_mode="learned",
    rule_column="dist_from_120d_low",
    rule_threshold=0.15,
    rule_direction="le",
    top_k=20,
    evaluation_dir="output/evaluations",
    evaluation_prefix=None,
    realized_return_column="forward_excess_return_20d",
    commission_bps=5.0,
    slippage_bps=5.0,
    stamp_duty_bps=5.0,
) -> dict:
    """Fit a primary model per fold and a strictly prior meta gate on top of it.

    The gate is trained on a *nested* inner block: the fold's training window is
    split into an inner-train block and an inner-gate block separated by
    ``inner_purge_days``, a first primary model scores the inner-gate block, and
    the gate learns which of those candidates paid.  The test block is then
    scored by a second primary model fitted on the whole training window, so the
    gate never sees a label that overlaps its own evaluation window.
    """
    prepared, features, _ = _prepare_labeled_panel(
        panel, feature_columns, label_column, filter_features=False,
    )
    prepared = prepared.drop(
        columns=[column for column in prepared.columns if str(column).startswith("label_") and column != label_column],
        errors="ignore",
    )
    folds = expanding_walk_forward_splits(
        prepared["trade_date"].unique(), n_splits=n_splits, min_train_days=min_train_days,
        test_days=test_days, purge_days=purge_days, embargo_days=embargo_days,
    )
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    gate_root = Path(meta_dir) if meta_dir else directory / "meta_gate"
    requested_meta_features = tuple(meta_features or DEFAULT_META_FEATURES)
    gate_mode = str(gate_mode or "learned").strip().lower()
    if gate_mode not in {"learned", "rule"}:
        raise ValueError(f"unsupported meta gate mode: {gate_mode!r}")
    use_learned_gate = gate_mode == "learned"

    primary_parts: list[pd.DataFrame] = []
    decision_parts: list[pd.DataFrame] = []
    fold_records: list[dict] = []
    resolved_meta_features: list[str] = []
    missing_meta_features: list[str] = []
    fold_iter = tqdm(folds, desc="LightGBM meta OOS folds", unit="fold") if show_progress else folds
    for fold in fold_iter:
        fold_started = time.time()
        train_dates = [pd.Timestamp(value) for value in fold.train_dates]
        train = prepared[prepared["trade_date"].isin(set(train_dates))].copy()
        test_dates = _scored_test_dates(fold.test_dates, prediction_stride)
        test = prepared[prepared["trade_date"].isin(set(test_dates))].copy()
        if train.empty or test.empty:
            raise ValueError(f"fold {fold.fold} has an empty train or test block")
        inner_cut = int(round(len(train_dates) * (1.0 - float(inner_share))))
        inner_cut = max(1, min(inner_cut, len(train_dates) - 1))
        inner_train_dates = train_dates[: max(1, inner_cut - max(0, int(inner_purge_days)))]
        inner_gate_dates = train_dates[inner_cut:]
        if not inner_train_dates or not inner_gate_dates:
            raise ValueError(f"fold {fold.fold} cannot be split into inner train/inner gate blocks")
        if show_progress:
            print(
                f"[META OOS] fold={fold.fold}/{len(folds)} mode={gate_mode} "
                f"inner_train={len(inner_train_dates)}d inner_gate={len(inner_gate_dates)}d test={len(test_dates)}d",
                flush=True,
            )
        meta_train = pd.DataFrame()
        meta_artifact = {}
        if use_learned_gate:
            inner_artifact = train_lightgbm_panel(
                prepared[prepared["trade_date"].isin(set(inner_train_dates))], features,
                model_dir=gate_root / f"fold_{fold.fold}_inner_primary", label_column=label_column,
                validation_days=max(1, len(inner_train_dates) // 5), cleaning_version=cleaning_version,
                factor_set=factor_set, embargo_days=0, min_feature_coverage=min_feature_coverage,
                drop_constant_features=drop_constant_features, show_progress=show_progress,
            )
            inner_scores = predict_lightgbm_panel(
                prepared[prepared["trade_date"].isin(set(inner_gate_dates))],
                model_path=inner_artifact["artifact"]["model_path"],
                manifest_path=inner_artifact["artifact"]["manifest_path"],
            )
            inner_candidates = select_signal_candidates(inner_scores, candidate_quantile=candidate_quantile)
            if not resolved_meta_features:
                probe = attach_meta_inputs(
                    inner_candidates, panel=prepared, labels=label_frame, feature_columns=requested_meta_features,
                )
                resolved_meta_features, missing_meta_features = resolve_meta_feature_columns(
                    probe.columns, requested_meta_features,
                )
            meta_train = attach_meta_inputs(
                inner_candidates, panel=prepared, labels=label_frame, feature_columns=resolved_meta_features,
            )
            meta_train = build_meta_labels(
                meta_train, label_frame, meta_label_column=meta_label_column, positive_value=meta_positive_value,
            )
            if meta_train.empty:
                raise ValueError(
                    f"fold {fold.fold} produced no meta training rows; "
                    f"{meta_label_column} may not be mature for the inner gate window"
                )
            meta_artifact = train_meta_label_panel(
                meta_train, resolved_meta_features,
                model_dir=gate_root / f"fold_{fold.fold}",
                validation_share=meta_validation_share, purge_days=meta_purge_days,
                n_estimators=meta_n_estimators, learning_rate=meta_learning_rate,
                num_leaves=meta_num_leaves, max_depth=meta_max_depth,
                min_child_samples=meta_min_child_samples, reg_lambda=meta_reg_lambda,
                random_state=meta_random_state,
                extra_manifest={
                    "fold": fold.fold,
                    "inner_train_dates": [str(value.date()) for value in inner_train_dates],
                    "inner_gate_dates": [str(value.date()) for value in inner_gate_dates],
                    "inner_train_rows": int(len(inner_train_dates)),
                    "inner_gate_rows": int(len(inner_gate_dates)),
                    "candidate_quantile": float(candidate_quantile),
                    "meta_label_column": meta_label_column,
                    "meta_positive_value": float(meta_positive_value),
                },
                show_progress=show_progress,
            )
        primary_artifact = train_lightgbm_panel(
            train, features, model_dir=directory / f"lightgbm_fold_{fold.fold}", label_column=label_column,
            validation_days=max(1, min(20, len(train_dates) // 5)), cleaning_version=cleaning_version,
            factor_set=factor_set, embargo_days=0, min_feature_coverage=min_feature_coverage,
            drop_constant_features=drop_constant_features, show_progress=show_progress,
        )
        scores = predict_lightgbm_panel(
            test, model_path=primary_artifact["artifact"]["model_path"],
            manifest_path=primary_artifact["artifact"]["manifest_path"],
        )
        scores = scores.merge(test[["trade_date", "stock_code", label_column]], on=["trade_date", "stock_code"], how="inner")
        scores["fold"] = fold.fold
        primary_parts.append(scores)
        test_candidates = select_signal_candidates(scores, candidate_quantile=candidate_quantile)
        if use_learned_gate:
            scoring_frame = attach_meta_inputs(
                test_candidates, panel=prepared, labels=label_frame, feature_columns=resolved_meta_features,
            )
            probabilities = score_meta_label_panel(
                scoring_frame, model_path=meta_artifact["model_path"], manifest_path=meta_artifact["manifest_path"],
            )
            decisions = apply_meta_gate(
                scoring_frame, probabilities, act_quantile=act_quantile, min_probability=min_probability,
            )
        else:
            scoring_frame = attach_meta_inputs(
                test_candidates, panel=prepared, labels=label_frame, feature_columns=[rule_column],
            )
            decisions = apply_rule_gate(
                scoring_frame, column=rule_column, threshold=rule_threshold, direction=rule_direction,
            )
        realized_columns = [
            column
            for column in (
                realized_return_column, "forward_exec_return_5d", "forward_exec_return_10d",
                "forward_exec_return_20d", "forward_excess_return_5d", "forward_excess_return_10d",
                "label_tb_class", "label_tb_ret", "label_tb_hold_days",
            )
            if column in label_frame.columns and column not in decisions.columns
        ]
        if realized_columns:
            decisions = decisions.merge(
                label_frame[["stock_code", "trade_date"] + realized_columns],
                on=["stock_code", "trade_date"], how="left",
            )
        decisions["fold"] = fold.fold
        decision_parts.append(decisions)
        fold_records.append(
            {
                "fold": fold.fold,
                "test_start": str(pd.Timestamp(fold.test_start).date()),
                "test_end": str(pd.Timestamp(fold.test_end).date()),
                "train_date_count": len(train_dates),
                "inner_train_date_count": len(inner_train_dates),
                "inner_gate_date_count": len(inner_gate_dates),
                "inner_train_end": str(pd.Timestamp(inner_train_dates[-1]).date()),
                "inner_gate_start": str(pd.Timestamp(inner_gate_dates[0]).date()),
                "inner_gate_end": str(pd.Timestamp(inner_gate_dates[-1]).date()),
                "purge_days": int(purge_days),
                "embargo_days": int(embargo_days),
                "inner_purge_days": int(inner_purge_days),
                "meta_train_rows": int(len(meta_train)),
                "meta_validation_auc": meta_artifact.get("validation_auc") if meta_artifact else None,
                "meta_train_base_rate": (
                    meta_artifact["manifest"]["train_base_rate"] if meta_artifact else None
                ),
                "candidate_rows": int(len(test_candidates)),
                "scored_rows": int(len(decisions)),
                "seconds": round(time.time() - fold_started, 1),
            }
        )
    primary = _persist_oos(primary_parts, directory, "lightgbm", label_column, folds)
    decisions = pd.concat(decision_parts, ignore_index=True) if decision_parts else pd.DataFrame()
    decisions_path = directory / "cn_lightgbm_meta_oos_predictions.csv"
    decisions.to_csv(decisions_path, index=False)
    metrics = evaluate_meta_gate(
        decisions, top_k=top_k, excess_return_column=realized_return_column,
        commission_bps=commission_bps, slippage_bps=slippage_bps, stamp_duty_bps=stamp_duty_bps,
    )
    prefix = evaluation_prefix or f"cn_meta_gate_{Path(str(output_dir)).name}"
    paths = write_meta_gate_report(metrics, output_dir=evaluation_dir, prefix=prefix)
    per_date = _meta_gate_per_date_frame(decisions, top_k=top_k)
    per_date_path = Path(evaluation_dir) / f"{prefix}_per_date.csv"
    per_date.to_csv(per_date_path, index=False)
    manifest = {
        "model": "lightgbm_meta_gate",
        "primary": {"label_column": label_column, "rows": int(sum(len(part) for part in primary_parts))},
        "meta_labeling": {
            "gate_mode": gate_mode,
            "rule": (
                {"column": rule_column, "threshold": float(rule_threshold), "direction": rule_direction}
                if not use_learned_gate else None
            ),
            "meta_label_column": meta_label_column,
            "meta_positive_value": float(meta_positive_value),
            "requested_meta_features": list(requested_meta_features),
            "meta_feature_columns": resolved_meta_features,
            "missing_meta_features": missing_meta_features,
            "candidate_quantile": float(candidate_quantile),
            "act_quantile": act_quantile,
            "min_probability": min_probability,
            "inner_share": float(inner_share),
            "inner_purge_days": int(inner_purge_days),
            "meta_validation_share": float(meta_validation_share),
            "meta_purge_days": int(meta_purge_days),
            "top_k": int(top_k),
            "realized_return_column": realized_return_column,
            "costs": {
                "commission_bps": float(commission_bps),
                "slippage_bps": float(slippage_bps),
                "stamp_duty_bps": float(stamp_duty_bps),
                "round_trip_bps": metrics.get("cost_bps_round_trip"),
            },
            "decision_rows": int(len(decisions)),
        },
        "availability": dict(AVAILABILITY_RULE),
        "folds": fold_records,
        "evaluation": paths,
        "per_date_csv": str(per_date_path),
        "decisions_csv": str(decisions_path),
    }
    manifest_path = directory / "cn_lightgbm_meta_oos_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "primary": primary,
        "meta_labeling": {
            "decision_rows": int(len(decisions)),
            "manifest_path": str(manifest_path),
            "decisions_csv": str(decisions_path),
            "per_date_csv": str(per_date_path),
            "evaluation": paths,
            "metrics": metrics,
            "folds": fold_records,
        },
    }


def _meta_gate_per_date_frame(decisions: pd.DataFrame, *, top_k: int, act_column: str = "meta_act",
                              score_column: str = "model_score", return_column: str = "forward_excess_return_20d") -> pd.DataFrame:
    """Per-date ungated vs gated book summary for audit."""
    if decisions is None or decisions.empty:
        return pd.DataFrame()
    returns = decisions[return_column] if return_column in decisions.columns else None
    rows = []
    for trade_date, group in decisions.groupby("trade_date", sort=True):
        ordered = group.sort_values(score_column, ascending=False)
        gated = ordered.loc[ordered[act_column].fillna(False)]
        price = lambda frame: float(frame[return_column].mean()) if returns is not None and not frame.empty else None
        rows.append(
            {
                "trade_date": trade_date,
                "fold": int(group["fold"].iloc[0]) if "fold" in group.columns else None,
                "candidates": int(len(group)),
                "approved": int(len(gated)),
                "ungated_topk_return": price(ordered.head(int(top_k))),
                "approved_topk_return": price(gated.head(int(top_k))),
                "rejected_return": price(ordered.loc[~ordered[act_column].fillna(False)]),
                "all_candidates_return": price(ordered),
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["gated_topk_return"] = frame["approved_topk_return"]
        frame["spread"] = frame["approved_topk_return"] - frame["ungated_topk_return"]
    return frame


def generate_transformer_oos_predictions(
    panel: pd.DataFrame,
    feature_columns: list[str],
    *,
    label_column: str,
    output_dir,
    n_splits=5,
    min_train_days=120,
    test_days=None,
    purge_days=20,
    embargo_days=0,
    lookback=60,
    epochs=5,
    batch_size=256,
    max_samples=12_000,
    cleaning_version="p0.2.v1",
    factor_set=None,
    device="auto",
    min_feature_coverage=0.05,
    drop_constant_features=True,
    max_feature_pairs=128,
    protected_features=(),
    preserve_unlabeled=False,
    seeds=None,
    checkpoint_metric="ic",
    seed_ensemble="average",
    prediction_stride=1,
    show_progress=False,
) -> dict:
    """Fit one Transformer per expanding fold; score each test date as-of that date."""
    if show_progress:
        print("[TRANSFORMER OOS] preparing labeled panel and chronological folds", flush=True)
    prepared, features, _ = _prepare_labeled_panel(
        panel, feature_columns, label_column, filter_features=False,
        preserve_unlabeled=bool(preserve_unlabeled),
    )
    prepared = prepared.sort_values(["stock_code", "trade_date"], kind="stable").reset_index(drop=True)
    split_dates = prepared.loc[prepared["label"].notna(), "trade_date"].unique()
    folds = expanding_walk_forward_splits(
        split_dates, n_splits=n_splits, min_train_days=min_train_days,
        test_days=test_days, purge_days=purge_days, embargo_days=embargo_days,
    )
    if show_progress:
        print(
            f"[TRANSFORMER OOS] panel ready rows={len(prepared):,} features={len(features):,} "
            f"folds={len(folds)}",
            flush=True,
        )
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    parts = []
    fold_iter = tqdm(folds, desc="Transformer OOS folds", unit="fold") if show_progress else folds
    for fold in fold_iter:
        if preserve_unlabeled:
            # Startup-only labels are sparse endpoints.  Keep every causal row
            # through the last training endpoint so the sequence model can see
            # the preceding non-startup bars as context; only labeled endpoints
            # enter the supervised split inside train_transformer_panel().
            train_cutoff = max(pd.to_datetime(fold.train_dates))
            train = prepared.loc[prepared["trade_date"] <= train_cutoff].copy()
        else:
            train = prepared[prepared["trade_date"].isin(set(fold.train_dates))].copy()
        test_dates = _scored_test_dates(fold.test_dates, prediction_stride)
        if show_progress:
            print(
                f"[TRANSFORMER OOS] fold={fold.fold}/{len(folds)} "
                f"train={fold.train_dates[0]:%Y-%m-%d}..{fold.train_dates[-1]:%Y-%m-%d} "
                f"test={fold.test_start:%Y-%m-%d}..{fold.test_end:%Y-%m-%d} "
                f"scoring_dates={len(test_dates)}/{len(fold.test_dates)} "
                f"lookback={lookback}",
                flush=True,
            )
        artifact = train_transformer_panel(
            train, features, model_dir=directory / f"transformer_fold_{fold.fold}", label_column=label_column,
            lookback=lookback, validation_days=max(1, min(20, len(fold.train_dates) // 5)), epochs=epochs,
            batch_size=batch_size, max_samples=max_samples, cleaning_version=cleaning_version,
            factor_set=factor_set, device=device, embargo_days=0,
            min_feature_coverage=min_feature_coverage,
            drop_constant_features=drop_constant_features,
            max_feature_pairs=max_feature_pairs,
            protected_features=protected_features,
            preserve_unlabeled=bool(preserve_unlabeled),
            seeds=seeds,
            checkpoint_metric=checkpoint_metric,
            seed_ensemble=seed_ensemble,
            show_progress=show_progress,
        )
        # Every target gets only its preceding context, but all targets in the
        # fold share one preprocessing pass and batched Transformer inference.
        asof = _oos_sequence_context(
            prepared, test_dates=test_dates, lookback=lookback, label_column=label_column,
        )
        if show_progress:
            print(
                f"[TRANSFORMER OOS] fold={fold.fold} batch scoring context_rows={len(asof):,} "
                f"target_dates={len(test_dates)}",
                flush=True,
            )
        scores = predict_transformer_panel(
            asof, model_path=artifact["artifact"]["model_path"],
            manifest_path=artifact["artifact"]["manifest_path"], device=device,
            target_dates=test_dates, show_progress=show_progress,
        )
        labels = prepared[prepared["trade_date"].isin(set(test_dates))][["trade_date", "stock_code", label_column]]
        scores = scores.merge(labels, on=["trade_date", "stock_code"], how="inner")
        scores["fold"] = fold.fold
        parts.append(scores)
    return _persist_oos(parts, directory, "transformer", label_column, folds)


def generate_cnn_oos_predictions(
    panel: pd.DataFrame, feature_columns: list[str], *, label_column: str, output_dir,
    n_splits=5, min_train_days=120, test_days=None, purge_days=20, embargo_days=0,
    lookback=60, epochs=5, batch_size=256, max_samples=12_000, channels=64,
    kernel_size=3, num_layers=3, cleaning_version="p0.2.v1", factor_set=None, device="auto",
    min_feature_coverage=0.05, drop_constant_features=True, max_feature_pairs=128,
    protected_features=(),
    prediction_stride=1, show_progress=False,
) -> dict:
    """Fit one 1D CNN per fold and only score sequences ending on OOS dates."""
    prepared, features, _ = _prepare_labeled_panel(
        panel, feature_columns, label_column, filter_features=False,
    )
    prepared = prepared.sort_values(["stock_code", "trade_date"], kind="stable").reset_index(drop=True)
    folds = expanding_walk_forward_splits(prepared["trade_date"].unique(), n_splits=n_splits, min_train_days=min_train_days, test_days=test_days, purge_days=purge_days, embargo_days=embargo_days)
    directory = Path(output_dir); directory.mkdir(parents=True, exist_ok=True)
    parts = []
    fold_iter = tqdm(folds, desc="CNN OOS folds", unit="fold") if show_progress else folds
    for fold in fold_iter:
        train = prepared[prepared["trade_date"].isin(set(fold.train_dates))].copy()
        test_dates = _scored_test_dates(fold.test_dates, prediction_stride)
        if show_progress:
            print(
                f"[CNN OOS] fold={fold.fold}/{len(folds)} "
                f"train={fold.train_dates[0]:%Y-%m-%d}..{fold.train_dates[-1]:%Y-%m-%d} "
                f"test={fold.test_start:%Y-%m-%d}..{fold.test_end:%Y-%m-%d} "
                f"scoring_dates={len(test_dates)}/{len(fold.test_dates)} "
                f"lookback={lookback}",
                flush=True,
            )
        artifact = train_cnn_panel(
            train, features, model_dir=directory / f"cnn_fold_{fold.fold}", label_column=label_column, lookback=lookback,
            validation_days=max(1, min(20, len(fold.train_dates) // 5)), epochs=epochs, batch_size=batch_size,
            max_samples=max_samples, channels=channels, kernel_size=kernel_size, num_layers=num_layers,
            cleaning_version=cleaning_version, factor_set=factor_set, device=device, embargo_days=0,
            min_feature_coverage=min_feature_coverage,
            drop_constant_features=drop_constant_features,
            max_feature_pairs=max_feature_pairs,
            protected_features=protected_features,
            show_progress=show_progress,
        )
        date_iter = tqdm(test_dates, desc=f"CNN fold {fold.fold} scoring", unit="date", leave=False) if show_progress else test_dates
        for test_date in date_iter:
            asof = _asof_sequence_window(
                prepared, test_date=test_date, lookback=lookback, label_column=label_column,
            )
            scores = predict_cnn_panel(asof, model_path=artifact["artifact"]["model_path"], manifest_path=artifact["artifact"]["manifest_path"], device=device)
            labels = prepared[prepared["trade_date"] == test_date][["trade_date", "stock_code", label_column]]
            scores = scores.merge(labels, on=["trade_date", "stock_code"], how="inner")
            scores["fold"] = fold.fold; parts.append(scores)
    return _persist_oos(parts, directory, "cnn", label_column, folds)


def generate_graph_temporal_oos_predictions(
    panel: pd.DataFrame, feature_columns: list[str], *, industry_mapping: pd.DataFrame,
    label_column: str, output_dir, n_splits=5, min_train_days=120, test_days=None,
    purge_days=20, embargo_days=0, lookback=20, epochs=5,
) -> dict:
    """Generate graph-model OOS scores with an industry mapping known as of each fold.

    ``industry_mapping`` must contain ``stock_code``, ``industry_l1`` and
    ``available_at``. This explicit contract prevents current industry labels
    from leaking into historical graph snapshots.
    """
    required = {"stock_code", "industry_l1", "available_at"}
    if industry_mapping is None or required - set(industry_mapping.columns):
        raise ValueError("graph OOS requires PIT industry mapping: stock_code, industry_l1, available_at")
    prepared, features, _ = _prepare_labeled_panel(
        panel, feature_columns, label_column, filter_features=False,
    )
    mapping = industry_mapping.copy(); mapping["available_at"] = pd.to_datetime(mapping["available_at"], errors="coerce")
    folds = expanding_walk_forward_splits(prepared["trade_date"].unique(), n_splits=n_splits, min_train_days=min_train_days, test_days=test_days, purge_days=purge_days, embargo_days=embargo_days)
    directory = Path(output_dir); directory.mkdir(parents=True, exist_ok=True)
    parts = []
    for fold in folds:
        train = prepared[prepared["trade_date"].isin(set(fold.train_dates))].copy()
        codes = sorted(train["stock_code"].astype(str).unique())
        adjacency, graph_meta = build_industry_adjacency(codes, mapping, asof_date=fold.test_start)
        graph_meta["available_at_contract"] = "industry_mapping.available_at <= fold.test_start"
        artifact = train_graph_temporal_panel(train, features, model_dir=directory / f"graph_temporal_fold_{fold.fold}", label_column=label_column, lookback=lookback, epochs=epochs, adjacency=adjacency, graph_metadata=graph_meta)
        for test_date in fold.test_dates:
            scores = predict_graph_temporal_panel(prepared, model_path=artifact["model_path"], manifest_path=artifact["manifest_path"], asof_date=test_date)
            labels = prepared[prepared["trade_date"] == test_date][["trade_date", "stock_code", label_column]]
            scores = scores.merge(labels, on=["trade_date", "stock_code"], how="inner")
            scores["fold"] = fold.fold; parts.append(scores)
    return _persist_oos(parts, directory, "graph_temporal", label_column, folds)


def _persist_oos(parts: list[pd.DataFrame], directory: Path, model: str, label_column: str, folds) -> dict:
    frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    path = directory / f"cn_{model}_oos_predictions.csv"
    manifest_path = directory / f"cn_{model}_oos_manifest.json"
    frame.to_csv(path, index=False)
    manifest = {
        "model": model, "label_column": label_column, "rows": int(len(frame)),
        "folds": [{"fold": item.fold, "test_start": str(item.test_start.date()), "test_end": str(item.test_end.date()),
                   "train_date_count": len(item.train_dates), "purge_days": item.purge_days, "embargo_days": item.embargo_days} for item in folds],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"model": model, "path": str(path), "manifest_path": str(manifest_path), "rows": int(len(frame)), "fold_count": len(folds)}
