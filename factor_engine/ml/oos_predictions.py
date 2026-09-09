"""Chronological out-of-sample prediction generation for model comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

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
    for fold in folds:
        train = prepared[prepared["trade_date"].isin(set(fold.train_dates))].copy()
        test = prepared[prepared["trade_date"].isin(set(fold.test_dates))].copy()
        artifact = train_lightgbm_panel(
            train, features, model_dir=directory / f"lightgbm_fold_{fold.fold}", label_column=label_column,
            validation_days=max(1, min(20, len(fold.train_dates) // 5)), cleaning_version=cleaning_version,
            factor_set=factor_set, embargo_days=0,
            min_feature_coverage=min_feature_coverage,
            drop_constant_features=drop_constant_features,
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
    max_samples=200_000,
    cleaning_version="p0.2.v1",
    factor_set=None,
    device="auto",
    min_feature_coverage=0.05,
    drop_constant_features=True,
) -> dict:
    """Fit one Transformer per expanding fold; score each test date as-of that date."""
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
    for fold in folds:
        train = prepared[prepared["trade_date"].isin(set(fold.train_dates))].copy()
        artifact = train_transformer_panel(
            train, features, model_dir=directory / f"transformer_fold_{fold.fold}", label_column=label_column,
            lookback=lookback, validation_days=max(1, min(20, len(fold.train_dates) // 5)), epochs=epochs,
            batch_size=batch_size, max_samples=max_samples, cleaning_version=cleaning_version,
            factor_set=factor_set, device=device, embargo_days=0,
            min_feature_coverage=min_feature_coverage,
            drop_constant_features=drop_constant_features,
        )
        for test_date in fold.test_dates:
            # The inference panel ends at this decision date; later rows never enter the sequence.
            asof = prepared[prepared["trade_date"] <= test_date].drop(columns=[label_column], errors="ignore")
            scores = predict_transformer_panel(
                asof, model_path=artifact["artifact"]["model_path"], manifest_path=artifact["artifact"]["manifest_path"], device=device,
            )
            labels = prepared[prepared["trade_date"] == test_date][["trade_date", "stock_code", label_column]]
            scores = scores.merge(labels, on=["trade_date", "stock_code"], how="inner")
            scores["fold"] = fold.fold
            parts.append(scores)
    return _persist_oos(parts, directory, "transformer", label_column, folds)


def generate_cnn_oos_predictions(
    panel: pd.DataFrame, feature_columns: list[str], *, label_column: str, output_dir,
    n_splits=5, min_train_days=120, test_days=None, purge_days=20, embargo_days=0,
    lookback=60, epochs=5, batch_size=256, max_samples=200_000, channels=64,
    kernel_size=3, num_layers=3, cleaning_version="p0.2.v1", factor_set=None, device="auto",
    min_feature_coverage=0.05, drop_constant_features=True,
) -> dict:
    """Fit one 1D CNN per fold and only score sequences ending on OOS dates."""
    prepared, features, _ = _prepare_labeled_panel(
        panel, feature_columns, label_column, filter_features=False,
    )
    folds = expanding_walk_forward_splits(prepared["trade_date"].unique(), n_splits=n_splits, min_train_days=min_train_days, test_days=test_days, purge_days=purge_days, embargo_days=embargo_days)
    directory = Path(output_dir); directory.mkdir(parents=True, exist_ok=True)
    parts = []
    for fold in folds:
        train = prepared[prepared["trade_date"].isin(set(fold.train_dates))].copy()
        artifact = train_cnn_panel(
            train, features, model_dir=directory / f"cnn_fold_{fold.fold}", label_column=label_column, lookback=lookback,
            validation_days=max(1, min(20, len(fold.train_dates) // 5)), epochs=epochs, batch_size=batch_size,
            max_samples=max_samples, channels=channels, kernel_size=kernel_size, num_layers=num_layers,
            cleaning_version=cleaning_version, factor_set=factor_set, device=device, embargo_days=0,
            min_feature_coverage=min_feature_coverage,
            drop_constant_features=drop_constant_features,
        )
        for test_date in fold.test_dates:
            asof = prepared[prepared["trade_date"] <= test_date].drop(columns=[label_column], errors="ignore")
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
