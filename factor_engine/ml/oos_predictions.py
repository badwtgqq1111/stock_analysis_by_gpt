"""Chronological out-of-sample prediction generation for model comparison."""

from __future__ import annotations

import json
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
    prediction_stride=1,
    show_progress=False,
) -> dict:
    """Fit one Transformer per expanding fold; score each test date as-of that date."""
    if show_progress:
        print("[TRANSFORMER OOS] preparing labeled panel and chronological folds", flush=True)
    prepared, features, _ = _prepare_labeled_panel(
        panel, feature_columns, label_column, filter_features=False,
    )
    prepared = prepared.sort_values(["stock_code", "trade_date"], kind="stable").reset_index(drop=True)
    folds = expanding_walk_forward_splits(
        prepared["trade_date"].unique(), n_splits=n_splits, min_train_days=min_train_days,
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
