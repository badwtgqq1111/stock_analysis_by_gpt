"""Persisted-panel training and inference for LightGBM and a temporal Transformer."""

from __future__ import annotations

import json
import hashlib
import re
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

from factor_engine.ml.preprocessing import preprocess_features_by_date


@dataclass(frozen=True)
class TrainingArtifact:
    model_path: str
    manifest_path: str
    model_type: str
    train_rows: int
    validation_rows: int
    feature_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def train_lightgbm_panel(
    panel: pd.DataFrame,
    feature_columns: list[str],
    *,
    model_dir,
    label_column="forward_return_20d",
    validation_days=60,
    cleaning_version="p0.2.v1",
    factor_set=None,
    warm_start_path=None,
    embargo_days=None,
    min_feature_coverage=0.05,
    drop_constant_features=True,
    n_estimators=500,
    learning_rate=0.05,
    num_leaves=64,
    max_depth=8,
    min_child_samples=30,
    reg_lambda=10.0,
    early_stopping_rounds=0,
    min_trees=100,
    eval_metric="daily_ic",
    show_progress=False,
) -> dict:
    """Fit and save a cross-sectional LightGBM model from a clean panel.

    The label is a within-date cross-sectional rank, so plain L2 is nearly flat
    for any model (its floor is the label variance).  Early stopping on L2
    therefore trims the booster to a handful of trees and collapses the score
    range, which destroys the ranking the selection stage depends on.  The
    default metric is therefore the mean per-date cross-sectional IC, with a
    ``min_trees`` floor so a noisy validation fold can never shrink the model to
    a near-constant predictor.

    Early stopping is disabled by default: LightGBM trims the saved booster to
    the best iteration, so on a flat metric it silently reduces the model to a
    handful of trees (measured: 3 of 500) and the score range collapses.  Set
    ``early_stopping_rounds`` above zero only when the metric curve is known to
    peak late.
    """
    import lightgbm as lgb

    progress = tqdm(total=4, desc="LightGBM preparation", unit="step") if show_progress else None
    prepared, features, feature_quality = _prepare_labeled_panel(
        panel,
        feature_columns,
        label_column,
        min_feature_coverage=min_feature_coverage,
        drop_constant_features=drop_constant_features,
    )
    if progress is not None:
        progress.set_postfix_str(f"rows={len(prepared):,} features={len(features)}")
        progress.update(1)
    if prepared.empty:
        raise ValueError("no labeled rows available for LightGBM training")
    prepared, preprocessing = preprocess_features_by_date(prepared, features)
    train, valid, split = _purged_time_split(
        prepared, validation_days=validation_days,
        embargo_days=_resolve_embargo_days(label_column, embargo_days),
    )
    if progress is not None:
        progress.set_postfix_str(f"train={len(train):,} valid={len(valid):,}")
        progress.update(1)
    if train.empty:
        raise ValueError("not enough dates for LightGBM train/validation split")
    params = {
        "objective": "regression", "learning_rate": float(learning_rate), "n_estimators": int(n_estimators),
        "num_leaves": int(num_leaves), "max_depth": int(max_depth), "min_child_samples": int(min_child_samples),
        "subsample": 0.9, "colsample_bytree": 0.9, "reg_lambda": float(reg_lambda),
        "random_state": 42, "n_jobs": -1, "verbosity": -1,
    }
    model = lgb.LGBMRegressor(**params)
    fit_kwargs = {}
    validation_metric = None
    metric_history = {}
    if not valid.empty:
        fit_kwargs["eval_set"] = [(valid[features], valid["label"])]
        validation_metric = _build_validation_metric(eval_metric, valid["trade_date"].to_numpy())
        if validation_metric is not None:
            fit_kwargs["eval_metric"] = validation_metric["function"]
        # The per-iteration metric curve is always recorded: it is the only
        # signal that distinguishes "the model learned a ranking" from "the
        # model is a constant predictor", and L2 cannot make that distinction.
        callbacks = [lgb.record_evaluation(metric_history)]
        stopping_rounds = int(early_stopping_rounds or 0)
        if stopping_rounds > 0:
            callbacks.append(_floor_early_stopping(stopping_rounds, int(min_trees or 0)))
        fit_kwargs["callbacks"] = callbacks
    if warm_start_path and Path(warm_start_path).is_file():
        fit_kwargs["init_model"] = str(warm_start_path)
    fit_progress = tqdm(total=int(params["n_estimators"]), desc="LightGBM boosting", unit="tree") if show_progress else None
    if fit_progress is not None:
        def _fit_progress_callback(env):
            current = int(getattr(env, "iteration", 0)) + 1
            fit_progress.update(max(0, current - fit_progress.n))
            fit_progress.set_postfix_str(f"valid={len(valid):,}")
        fit_kwargs.setdefault("callbacks", []).append(_fit_progress_callback)
    try:
        model.fit(train[features], train["label"], **fit_kwargs)
    finally:
        if fit_progress is not None:
            fit_progress.close()
    if progress is not None:
        progress.set_postfix_str("booster fitted")
        progress.update(1)
    trees_kept = int(model.booster_.num_trees())
    refit_without_early_stopping = False
    if not valid.empty and trees_kept < int(min_trees or 0):
        # LightGBM trims the saved booster to the best iteration, so a flat
        # metric can silently reduce the published model to a near-constant
        # predictor.  Refit deterministically at full length instead.
        for key in ("eval_set", "eval_metric", "callbacks"):
            fit_kwargs.pop(key, None)
        model = lgb.LGBMRegressor(**params)
        model.fit(train[features], train["label"], **fit_kwargs)
        trees_kept = int(model.booster_.num_trees())
        refit_without_early_stopping = True
    validation_ic = None
    if validation_metric is not None and not valid.empty:
        name, value, _ = validation_metric["function"](valid["label"].to_numpy(), model.predict(valid[features]))
        curve = list((metric_history.get("valid_0") or {}).get(name) or [])
        best_iteration = int(np.argmax(curve)) + 1 if curve else None
        validation_ic = {
            "name": name, "value": float(value), "trees_kept": trees_kept,
            "best": float(np.max(curve)) if curve else None,
            "best_iteration": best_iteration,
            "curve_first": [round(float(v), 6) for v in curve[:5]],
            "curve_last": [round(float(v), 6) for v in curve[-5:]],
            "rounds_ran": int(len(curve)),
            "below_min_trees": bool(trees_kept < int(min_trees or 0)),
        }
    directory = Path(model_dir)
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.txt"
    model.booster_.save_model(str(model_path))
    manifest = _manifest(
        model_type="lightgbm", features=features, label_column=label_column,
        cleaning_version=cleaning_version, factor_set=factor_set,
        train_dates=train["trade_date"], validation_dates=valid["trade_date"],
        preprocessing=preprocessing,
        extra={
            "params": params,
            "warm_start_path": warm_start_path,
            "split": split,
            "feature_quality": feature_quality,
            "validation_ic": validation_ic,
            "trees_kept": trees_kept,
            "best_iteration": int(getattr(model, "best_iteration_", -1) or -1),
            "early_stopping_rounds": int(early_stopping_rounds or 0),
            "min_trees": int(min_trees or 0),
            "refit_without_early_stopping": refit_without_early_stopping,
        },
    )
    manifest_path = directory / "model_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if progress is not None:
        progress.set_postfix_str(f"saved={model_path}")
        progress.update(1)
        progress.close()
    return {
        "artifact": TrainingArtifact(str(model_path), str(manifest_path), "lightgbm", len(train), len(valid), len(features)).to_dict(),
        "validation_mse": float(np.mean((model.predict(valid[features]) - valid["label"]) ** 2)) if not valid.empty else None,
        "trees_kept": trees_kept,
        "validation_ic": validation_ic,
        "refit_without_early_stopping": refit_without_early_stopping,
    }


def _build_validation_metric(eval_metric, dates):
    """Return the LightGBM custom metric used for early stopping.

    ``daily_ic`` is the mean per-date cross-sectional correlation between the
    prediction and the (already cross-sectionally ranked) label.  It is the
    quantity the selection stage actually consumes: plain L2 is dominated by
    label variance and cannot tell a useful ranking from a constant one.
    """
    metric = str(eval_metric or "").strip().lower()
    if metric in {"", "none"}:
        return None
    if metric in {"l2", "mse", "regression"}:
        return {"name": "l2", "function": None}
    date_codes, _ = pd.factorize(pd.to_datetime(pd.Series(dates)), sort=False)
    frame_template = pd.DataFrame({"date_code": date_codes})
    min_rows = 5

    def _daily_ic(y_true, y_pred):
        frame = frame_template.copy()
        frame["y"] = np.asarray(y_true, dtype=float)
        frame["p"] = np.asarray(y_pred, dtype=float)
        grouped = frame.groupby("date_code", sort=False)
        sizes = grouped["p"].transform("size")
        usable = frame[sizes >= min_rows]
        if usable.empty:
            return "daily_ic", 0.0, True
        values = usable.groupby("date_code", sort=False)[["y", "p"]].corr().unstack().iloc[:, 1]
        values = values.replace([np.inf, -np.inf], np.nan).dropna()
        if values.empty:
            return "daily_ic", 0.0, True
        return "daily_ic", float(values.mean()), True

    return {"name": "daily_ic", "function": _daily_ic}


def _floor_early_stopping(stopping_rounds, min_trees):
    """Early stopping that delays its stop until ``min_trees`` boosting rounds.

    Note: LightGBM trims the *saved* booster to the best iteration regardless of
    when the stop fires, so this callback alone cannot guarantee a tree floor.
    ``train_lightgbm_panel`` enforces the floor by refitting without early
    stopping when the trimmed model comes back below ``min_trees``.
    """
    import lightgbm as lgb

    callback = lgb.early_stopping(int(stopping_rounds), verbose=False)
    floor = max(0, int(min_trees))

    def _callback(environment):
        # The stopping callback keeps internal per-metric state and must see
        # every iteration; only the raised stop is suppressed before the floor.
        try:
            return callback(environment)
        except lgb.callback.EarlyStopException:
            if floor and int(environment.iteration) + 1 < floor:
                return
            raise

    return _callback


def predict_lightgbm_panel(panel: pd.DataFrame, *, model_path, manifest_path) -> pd.DataFrame:
    """Load a persisted Booster and score the latest rows without retraining."""
    import lightgbm as lgb

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    features = list(manifest["feature_columns"])
    _validate_feature_schema(panel, features)
    _validate_manifest_version(manifest)
    working = panel.copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"])
    working, _ = preprocess_features_by_date(working, features)
    booster = lgb.Booster(model_file=str(model_path))
    raw_scores = np.asarray(booster.predict(working[features]), dtype=np.float64)
    # Build score columns in one concat operation.  Assigning two columns to
    # a wide, already fragmented feature frame triggers pandas' fragmentation
    # warning and creates unnecessary block copies during every OOS fold.
    score_frame = working.loc[:, ["trade_date", "stock_code"]].copy()
    score_frame = pd.concat(
        [score_frame.reset_index(drop=True), pd.DataFrame({"model_score_raw": raw_scores})],
        axis=1,
    )
    score_frame["model_score"] = score_frame.groupby("trade_date")["model_score_raw"].rank(pct=True) * 100.0
    return score_frame


def train_transformer_panel(
    panel: pd.DataFrame,
    feature_columns: list[str],
    *,
    model_dir,
    label_column="forward_return_20d",
    lookback=60,
    validation_days=60,
    epochs=10,
    batch_size=256,
    d_model=64,
    nhead=4,
    num_layers=2,
    learning_rate=1e-3,
    max_samples=12_000,
    cleaning_version="p0.2.v1",
    factor_set=None,
    warm_start_path=None,
    warm_start_manifest_path=None,
    device="auto",
    embargo_days=None,
    min_feature_coverage=0.05,
    drop_constant_features=True,
    max_feature_pairs=128,
    seeds=None,
    checkpoint_metric="ic",
    seed_ensemble="average",
    show_progress=False,
) -> dict:
    """Fit an encoder-only temporal Transformer using the same clean panel."""
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, Dataset

    started_at = time.perf_counter()
    if show_progress:
        print(
            f"[TRANSFORMER] preparing labels rows={len(panel):,} "
            f"candidate_features={len(feature_columns):,}",
            flush=True,
        )
    prepared, features, feature_quality = _prepare_labeled_panel(
        panel,
        feature_columns,
        label_column,
        min_feature_coverage=min_feature_coverage,
        drop_constant_features=drop_constant_features,
    )
    prepared["trade_date"] = pd.to_datetime(prepared["trade_date"])
    features, temporal_feature_quality = _select_temporal_feature_pairs(
        prepared, features, max_feature_pairs=max_feature_pairs,
    )
    feature_quality["temporal_feature_selection"] = temporal_feature_quality
    # Drop the unselected wide feature columns before cross-sectional work and
    # sequence construction.  Retaining them as passthrough data can otherwise
    # dominate memory even though the temporal model never consumes them.
    prepared = prepared.loc[:, list(dict.fromkeys(["trade_date", "stock_code", "label", *features]))].copy()
    if show_progress:
        print(
            f"[TRANSFORMER] labels ready rows={len(prepared):,} features={len(features):,} "
            f"elapsed={time.perf_counter() - started_at:.1f}s; cross-sectional preprocessing",
            flush=True,
        )
    prepared, missing_columns, cross_section_preprocessing = _preprocess_transformer_panel(
        prepared, features
    )
    if show_progress:
        print(
            f"[TRANSFORMER] preprocessing ready elapsed={time.perf_counter() - started_at:.1f}s; "
            "splitting and constructing sequences",
            flush=True,
        )
    embargo_days = _resolve_embargo_days(label_column, embargo_days)
    train_rows, validation_rows, split = _purged_time_split(
        prepared, validation_days=validation_days, embargo_days=embargo_days
    )
    if train_rows["trade_date"].nunique() < int(lookback):
        raise ValueError("not enough dates for Transformer train/validation split")
    scaler = _fit_sequence_scaler(
        train_rows, features, preserve_binary_features=_missing_indicator_features(features)
    )
    if show_progress:
        print(
            f"[TRANSFORMER] sequence source train_rows={len(train_rows):,} "
            f"valid_rows={len(validation_rows):,} lookback={int(lookback)}",
            flush=True,
        )
    sequences = _build_sequences(
        prepared, features, lookback, scaler, missing_columns=missing_columns,
        max_samples=max_samples, show_progress=show_progress,
        progress_label="Transformer sequence windows",
    )
    train_dates = set(pd.to_datetime(train_rows["trade_date"]).to_numpy())
    validation_dates = set(pd.to_datetime(validation_rows["trade_date"]).to_numpy())
    train_items = [item for item in sequences if item[2] in train_dates]
    valid_items = [item for item in sequences if item[2] in validation_dates]
    if not train_items or not valid_items:
        raise ValueError("not enough complete sequences for Transformer train/validation split")
    if show_progress:
        print(
            f"[TRANSFORMER] sequences ready train={len(train_items):,} valid={len(valid_items):,} "
            f"elapsed={time.perf_counter() - started_at:.1f}s; training",
            flush=True,
        )
    input_dim = len(features) * 2
    torch_device = resolve_torch_device(device)
    loss_fn = nn.HuberLoss()
    if show_progress:
        print(
            f"[TRANSFORMER] creating lazy batch loaders input_shape="
            f"({int(lookback)}, {input_dim}) batch_size={int(batch_size)}",
            flush=True,
        )
    train_loader = _sequence_loader(train_items, batch_size, DataLoader, Dataset)
    valid_loader = _sequence_loader(valid_items, batch_size, DataLoader, Dataset, shuffle=False)
    valid_dates = np.asarray([item[2] for item in valid_items])
    seed_list = [int(seed) for seed in (seeds or [0])] or [0]
    metric = str(checkpoint_metric or "ic").lower()

    def _evaluate(model) -> tuple[float, float]:
        """Validation Huber loss and mean per-date rank IC of the current weights."""
        model.eval()
        losses, predictions, targets = [], [], []
        with torch.no_grad():
            for values, labels in valid_loader:
                output = model(values.to(torch_device))
                losses.append(float(loss_fn(output, labels.to(torch_device)).cpu()))
                predictions.append(output.detach().cpu().numpy().reshape(-1))
                targets.append(labels.numpy().reshape(-1))
        current_loss = float(np.mean(losses)) if losses else float("inf")
        if not predictions:
            return current_loss, float("nan")
        current_ic = mean_daily_rank_ic(np.concatenate(predictions), np.concatenate(targets), valid_dates)
        return current_loss, current_ic

    def _better(loss: float, ic: float, best_loss: float, best_ic: float) -> bool:
        """Checkpoint rule: validation rank IC when available, Huber as tie-break."""
        if metric != "ic" or not np.isfinite(ic):
            return loss < best_loss
        if not np.isfinite(best_ic):
            return True
        if ic > best_ic + 1e-6:
            return True
        return abs(ic - best_ic) <= 1e-6 and loss < best_loss

    best_state, best_loss, best_ic, best_seed = None, float("inf"), float("nan"), seed_list[0]
    seed_results = []
    seed_states: list[dict] = []
    for seed in seed_list:
        torch.manual_seed(seed)
        np.random.seed(seed)
        model = TemporalTransformer(input_dim, d_model=d_model, nhead=nhead, num_layers=num_layers)
        warm_start = _load_transformer_warm_start(
            model,
            warm_start_path=warm_start_path,
            warm_start_manifest_path=warm_start_manifest_path,
            features=features,
            cleaning_version=cleaning_version,
            factor_set=factor_set,
            input_dim=input_dim,
            d_model=d_model,
            nhead=nhead,
            num_layers=num_layers,
        )
        model.to(torch_device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=float(learning_rate), weight_decay=1e-4)
        seed_loss, seed_ic, seed_state = float("inf"), float("nan"), None
        epoch_iter = (tqdm(range(max(1, int(epochs))), desc=f"Transformer epochs seed={seed}", unit="epoch")
                      if show_progress else range(max(1, int(epochs))))
        for _epoch in epoch_iter:
            model.train()
            for values, labels in train_loader:
                optimizer.zero_grad()
                prediction = model(values.to(torch_device))
                loss = loss_fn(prediction, labels.to(torch_device))
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
            current_loss, current_ic = _evaluate(model)
            if _better(current_loss, current_ic, seed_loss, seed_ic):
                seed_loss, seed_ic = current_loss, current_ic
                seed_state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
            if show_progress:
                epoch_iter.set_postfix_str(
                    f"valid_loss={current_loss:.6f} valid_ic={current_ic:.4f} best_ic={seed_ic:.4f}")
        if show_progress and hasattr(epoch_iter, "close"):
            epoch_iter.close()
        seed_results.append({"seed": seed, "best_validation_huber_loss": seed_loss,
                             "best_validation_rank_ic": None if not np.isfinite(seed_ic) else float(seed_ic),
                             "warm_start": bool(warm_start.get("used"))})
        if seed_state is not None:
            seed_states.append(seed_state)
        if _better(seed_loss, seed_ic, best_loss, best_ic):
            best_state, best_loss, best_ic, best_seed = seed_state, seed_loss, seed_ic, seed
        if show_progress:
            print(f"[TRANSFORMER] seed={seed} best_ic={seed_ic:.4f} best_loss={seed_loss:.6f}", flush=True)
    if best_state is None:            # never trained a single epoch
        best_state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
    directory = Path(model_dir)
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.pt"
    # Saving every seed's best weights lets inference average the seeds instead of
    # picking one: with per-seed validation IC spread of ~0.05 and a single-day
    # scoring spread of ~0.09, averaging is the variance-reducing choice.
    ensemble_mode = str(seed_ensemble or "average").lower()
    torch.save({
        "state_dict": best_state,
        "state_dicts": seed_states or [best_state],
        "ensemble_mode": ensemble_mode,
        "input_dim": input_dim, "d_model": d_model, "nhead": nhead, "num_layers": num_layers,
    }, model_path)
    manifest = _manifest(
        model_type="temporal_transformer", features=features, label_column=label_column,
        cleaning_version=cleaning_version, factor_set=factor_set,
        train_dates=train_rows["trade_date"], validation_dates=validation_rows["trade_date"],
        preprocessing={
            "cross_section": cross_section_preprocessing,
            "scaler": scaler,
            "lookback": int(lookback),
            "mask": "raw_feature_missing_mask",
        },
        extra={
            "epochs": int(epochs), "batch_size": int(batch_size),
            "best_validation_huber_loss": best_loss, "warm_start": warm_start,
            "seeds": seed_list, "selected_seed": best_seed, "seed_results": seed_results,
            "checkpoint_metric": metric, "seed_ensemble": ensemble_mode,
            "saved_seed_states": len(seed_states or [best_state]),
            "best_validation_rank_ic": None if not np.isfinite(best_ic) else float(best_ic),
            "device": str(torch_device), "split": split,
            "feature_quality": feature_quality,
        },
    )
    manifest_path = directory / "model_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "artifact": TrainingArtifact(str(model_path), str(manifest_path), "temporal_transformer", len(train_items), len(valid_items), len(features)).to_dict(),
        "validation_huber_loss": best_loss,
    }


def predict_transformer_panel(
    panel: pd.DataFrame,
    *,
    model_path,
    manifest_path,
    device="auto",
    show_progress=False,
    inference_batch_size=128,
    target_dates=None,
) -> pd.DataFrame:
    """Load a Transformer checkpoint and score complete windows in batches.

    ``target_dates`` permits a whole OOS test block to share one causal context
    panel and one cross-sectional preprocessing pass.  With no target supplied,
    the latest complete window per stock is scored for production inference.
    """
    import torch

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    features = list(manifest["feature_columns"])
    _validate_feature_schema(panel, features)
    _validate_manifest_version(manifest)
    preprocessing = manifest["preprocessing"]
    scaler = preprocessing["scaler"]
    lookback = int(preprocessing["lookback"])
    # Build dense inference inputs directly.  Calling
    # _preprocess_transformer_panel() is appropriate during training, but it
    # constructs another 1K+ column DataFrame.  A 60-day full-universe panel
    # can otherwise exceed unified memory before inference begins.
    working = panel
    order_columns = working[["stock_code", "trade_date"]]
    if not order_columns.equals(order_columns.sort_values(["stock_code", "trade_date"], kind="stable")):
        working = working.sort_values(["stock_code", "trade_date"], kind="stable")
    stock_codes = working["stock_code"].to_numpy(copy=True)
    trade_dates = pd.to_datetime(working["trade_date"], errors="coerce").to_numpy(copy=True)
    # Some raw Alpha formulas can emit values outside float32 range before
    # cross-sectional winsorization. Treat non-finite values as missing and
    # bound finite extremes before the conversion so inference is stable.
    feature_values = working.loc[:, features].to_numpy(dtype=np.float64, copy=True)
    feature_values[~np.isfinite(feature_values)] = np.nan
    np.clip(feature_values, -1e20, 1e20, out=feature_values)
    feature_values = feature_values.astype(np.float32, copy=False)
    missing_values = ~np.isfinite(feature_values)
    feature_values[missing_values] = np.nan
    if preprocessing.get("cross_section"):
        config = preprocessing["cross_section"].get("config") or {}
        mode = config.get("mode", "qlib_robust")
        winsorize_lo = float(config.get("winsorize_lo", 0.01))
        winsorize_hi = float(config.get("winsorize_hi", 0.99))
        clip_range = float(config.get("clip_range", 5.0))
        min_samples = int(config.get("min_samples_for_winsorize", 20))
        missing_indicator_indices = [
            index for index, feature in enumerate(features)
            if feature in _missing_indicator_features(features)
        ]
        missing_indicator_index_set = set(missing_indicator_indices)
        continuous_indices = [
            index for index in range(len(features)) if index not in missing_indicator_index_set
        ]
        if continuous_indices and mode not in {"none", "raw", "off"}:
            for trade_date in pd.unique(trade_dates):
                if pd.isna(trade_date):
                    continue
                indexes = np.flatnonzero(trade_dates == trade_date)
                values = feature_values[np.ix_(indexes, continuous_indices)].astype(np.float64, copy=False)
                finite = np.isfinite(values)
                valid_counts = finite.sum(axis=0)
                if mode in {"qlib_robust", "robust"} and len(indexes) >= min_samples:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        lo = np.nanquantile(values, winsorize_lo, axis=0)
                        hi = np.nanquantile(values, winsorize_hi, axis=0)
                    can_clip = (valid_counts > 2) & np.isfinite(lo) & np.isfinite(hi) & (hi > lo)
                    if can_clip.any():
                        values[:, can_clip] = np.clip(values[:, can_clip], lo[can_clip], hi[can_clip])
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    median = np.nanmedian(values, axis=0)
                    mad = np.nanmedian(np.abs(values - median), axis=0)
                    std = np.nanstd(values, axis=0)
                median = np.where(np.isfinite(median), median, 0.0)
                scale = mad * 1.4826
                scale = np.where(np.isfinite(scale) & (scale >= 1e-12), scale, std)
                scale = np.where(np.isfinite(scale) & (scale >= 1e-12), scale, np.nan)
                normalized = np.clip((values - median) / scale, -clip_range, clip_range)
                feature_values[np.ix_(indexes, continuous_indices)] = np.where(
                    np.isfinite(normalized), normalized, 0.0
                ).astype(np.float32, copy=False)
        if missing_indicator_indices:
            indicators = np.nan_to_num(
                feature_values[:, missing_indicator_indices], nan=1.0, posinf=1.0, neginf=0.0,
            )
            feature_values[:, missing_indicator_indices] = np.clip(indicators, 0.0, 1.0)
    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)
    torch_device = resolve_torch_device(device)
    state_dicts = list(checkpoint.get("state_dicts") or [])
    if str(checkpoint.get("ensemble_mode", "best")).lower() != "average" or not state_dicts:
        state_dicts = [checkpoint["state_dict"]]
    models = []
    for state in state_dicts:
        member = TemporalTransformer(
            int(checkpoint["input_dim"]), d_model=int(checkpoint["d_model"]),
            nhead=int(checkpoint["nhead"]), num_layers=int(checkpoint["num_layers"]),
        )
        member.load_state_dict(state)
        member.to(torch_device)
        member.eval()
        models.append(member)
    model = models[0]
    if show_progress and len(models) > 1:
        print(f"[TRANSFORMER] seed ensemble: averaging {len(models)} checkpoints at inference", flush=True)
    center = np.asarray([scaler["center"].get(feature, 0.0) for feature in features], dtype=np.float32)
    scale = np.asarray([scaler["scale"].get(feature, 1.0) for feature in features], dtype=np.float32)
    scale[~np.isfinite(scale) | (scale == 0)] = 1.0
    feature_values -= center
    feature_values /= scale
    np.nan_to_num(feature_values, copy=False, nan=0.0, posinf=8.0, neginf=-8.0)
    np.clip(feature_values, -8.0, 8.0, out=feature_values)
    boundaries = np.r_[0, np.flatnonzero(stock_codes[1:] != stock_codes[:-1]) + 1, len(stock_codes)]
    stock_count = len(boundaries) - 1
    target_values = None
    target_mask = None
    if target_dates is not None:
        target_values = pd.to_datetime(pd.Series(list(target_dates)), errors="coerce").dropna().to_numpy()
        if len(target_values) == 0:
            return pd.DataFrame(columns=["trade_date", "stock_code", "model_score_raw", "model_score"])
        target_mask = np.isin(trade_dates, target_values)
        candidate_count = sum(
            int(target_mask[start + int(lookback) - 1:end].sum())
            for start, end in zip(boundaries[:-1], boundaries[1:])
            if end - start >= lookback
        )
    else:
        candidate_count = stock_count
    del working
    batch_size = max(1, int(inference_batch_size))
    rows = []
    batch = []
    batch_count = 0

    def score_batch(items):
        nonlocal batch_count
        if not items:
            return
        batch_inputs = np.stack([item[2] for item in items]).astype(np.float32, copy=False)
        if show_progress and batch_count == 0:
            print(
                f"[TRANSFORMER] scoring device={torch_device} batch_shape={batch_inputs.shape} "
                f"batches={int(np.ceil(candidate_count / batch_size))}",
                flush=True,
            )
        with torch.no_grad():
            tensor = torch.from_numpy(batch_inputs).to(torch_device)
            if len(models) > 1:
                predictions = np.mean(
                    [member(tensor).detach().cpu().numpy() for member in models], axis=0,
                )
            else:
                predictions = model(tensor).detach().cpu().numpy()
        batch_count += 1
        rows.extend(
            {
                "trade_date": trade_date,
                "stock_code": stock_code,
                "model_score_raw": float(score),
            }
            for (stock_code, trade_date, _), score in zip(items, predictions)
        )
        if progress is not None:
            progress.update(len(items))

    progress_unit = "sequence" if target_mask is not None else "stock"
    progress = tqdm(total=candidate_count, desc="Transformer scoring", unit=progress_unit) if show_progress else None
    try:
        for start, end in zip(boundaries[:-1], boundaries[1:]):
            if end - start < lookback:
                if progress is not None and target_mask is None:
                    progress.update(1)
                continue
            if target_mask is None:
                endpoints = (end - 1,)
            else:
                endpoints = np.flatnonzero(target_mask[start:end]) + start
                endpoints = endpoints[endpoints >= start + int(lookback) - 1]
            for endpoint in endpoints:
                sequence = np.concatenate(
                    [
                        feature_values[endpoint - lookback + 1:endpoint + 1],
                        missing_values[endpoint - lookback + 1:endpoint + 1],
                    ],
                    axis=1,
                )
                batch.append((stock_codes[endpoint], pd.Timestamp(trade_dates[endpoint]), sequence))
                if len(batch) >= batch_size:
                    score_batch(batch)
                    batch.clear()
        score_batch(batch)
    finally:
        if progress is not None:
            progress.close()
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["model_score"] = result.groupby("trade_date")["model_score_raw"].rank(pct=True) * 100.0
    return result


def train_cnn_panel(
    panel: pd.DataFrame,
    feature_columns: list[str],
    *,
    model_dir,
    label_column="forward_return_20d",
    lookback=60,
    validation_days=60,
    embargo_days=None,
    epochs=10,
    batch_size=256,
    channels=64,
    kernel_size=3,
    num_layers=3,
    learning_rate=1e-3,
    max_samples=12_000,
    cleaning_version="p0.2.v1",
    factor_set=None,
    device="auto",
    min_feature_coverage=0.05,
    drop_constant_features=True,
    max_feature_pairs=128,
    show_progress=False,
) -> dict:
    """Fit a 1D temporal CNN from the same clean-panel sequence contract."""
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, Dataset

    prepared, features, feature_quality = _prepare_labeled_panel(
        panel,
        feature_columns,
        label_column,
        min_feature_coverage=min_feature_coverage,
        drop_constant_features=drop_constant_features,
    )
    prepared["trade_date"] = pd.to_datetime(prepared["trade_date"])
    features, temporal_feature_quality = _select_temporal_feature_pairs(
        prepared, features, max_feature_pairs=max_feature_pairs,
    )
    feature_quality["temporal_feature_selection"] = temporal_feature_quality
    prepared = prepared.loc[:, list(dict.fromkeys(["trade_date", "stock_code", "label", *features]))].copy()
    prepared, missing_columns, cross_section_preprocessing = _preprocess_transformer_panel(prepared, features)
    embargo_days = _resolve_embargo_days(label_column, embargo_days)
    train_rows, validation_rows, split = _purged_time_split(
        prepared, validation_days=validation_days, embargo_days=embargo_days
    )
    if train_rows["trade_date"].nunique() < int(lookback):
        raise ValueError("not enough dates for CNN train/validation split")
    scaler = _fit_sequence_scaler(
        train_rows, features, preserve_binary_features=_missing_indicator_features(features)
    )
    sequences = _build_sequences(
        prepared, features, lookback, scaler, missing_columns=missing_columns,
        max_samples=max_samples, show_progress=show_progress, progress_label="CNN sequence windows",
    )
    train_dates = set(pd.to_datetime(train_rows["trade_date"]).to_numpy())
    validation_dates = set(pd.to_datetime(validation_rows["trade_date"]).to_numpy())
    train_items = [item for item in sequences if item[2] in train_dates]
    valid_items = [item for item in sequences if item[2] in validation_dates]
    if not train_items or not valid_items:
        raise ValueError("not enough complete sequences for CNN train/validation split")
    input_dim = len(features) * 2
    model = TemporalCNN(input_dim, channels=channels, kernel_size=kernel_size, num_layers=num_layers)
    torch_device = resolve_torch_device(device)
    model.to(torch_device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(learning_rate), weight_decay=1e-4)
    loss_fn = nn.HuberLoss()
    if show_progress:
        print(
            f"[CNN] creating lazy batch loaders input_shape="
            f"({int(lookback)}, {input_dim}) batch_size={int(batch_size)}",
            flush=True,
        )
    train_loader = _sequence_loader(train_items, batch_size, DataLoader, Dataset)
    valid_loader = _sequence_loader(valid_items, batch_size, DataLoader, Dataset, shuffle=False)
    best_state, best_loss = None, float("inf")
    epoch_iter = tqdm(range(max(1, int(epochs))), desc="CNN epochs", unit="epoch") if show_progress else range(max(1, int(epochs)))
    for _epoch in epoch_iter:
        model.train()
        for values, labels in train_loader:
            optimizer.zero_grad()
            loss = loss_fn(model(values.to(torch_device)), labels.to(torch_device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
        model.eval()
        losses = []
        with torch.no_grad():
            for values, labels in valid_loader:
                losses.append(float(loss_fn(model(values.to(torch_device)), labels.to(torch_device)).cpu()))
        current_loss = float(np.mean(losses)) if losses else float("inf")
        if current_loss < best_loss:
            best_loss = current_loss
            best_state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
        if show_progress:
            epoch_iter.set_postfix_str(f"train_batches={len(train_loader)} valid_loss={current_loss:.6f}")
    if show_progress:
        epoch_iter.close()
    directory = Path(model_dir)
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.pt"
    torch.save(
        {
            "state_dict": best_state, "input_dim": input_dim, "channels": int(channels),
            "kernel_size": int(kernel_size), "num_layers": int(num_layers),
        },
        model_path,
    )
    manifest = _manifest(
        model_type="temporal_cnn", features=features, label_column=label_column,
        cleaning_version=cleaning_version, factor_set=factor_set,
        train_dates=train_rows["trade_date"], validation_dates=validation_rows["trade_date"],
        preprocessing={
            "cross_section": cross_section_preprocessing,
            "scaler": scaler, "lookback": int(lookback), "mask": "raw_feature_missing_mask",
        },
        extra={
            "epochs": int(epochs), "batch_size": int(batch_size), "best_validation_huber_loss": best_loss,
            "device": str(torch_device), "split": split,
            "feature_quality": feature_quality,
        },
    )
    manifest_path = directory / "model_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "artifact": TrainingArtifact(str(model_path), str(manifest_path), "temporal_cnn", len(train_items), len(valid_items), len(features)).to_dict(),
        "validation_huber_loss": best_loss,
    }


def predict_cnn_panel(panel: pd.DataFrame, *, model_path, manifest_path, device="auto", show_progress=False) -> pd.DataFrame:
    """Load a persisted temporal CNN and score the latest complete sequence per stock."""
    import torch

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    features = list(manifest["feature_columns"])
    _validate_feature_schema(panel, features)
    _validate_manifest_version(manifest)
    if manifest.get("model_type") != "temporal_cnn":
        raise ValueError("CNN manifest has an incompatible model type")
    preprocessing = manifest["preprocessing"]
    working, missing_columns, _ = _preprocess_transformer_panel(
        panel, features, config=preprocessing.get("cross_section", {}).get("config")
    )
    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)
    model = TemporalCNN(
        int(checkpoint["input_dim"]), channels=int(checkpoint["channels"]),
        kernel_size=int(checkpoint["kernel_size"]), num_layers=int(checkpoint["num_layers"]),
    )
    model.load_state_dict(checkpoint["state_dict"])
    torch_device = resolve_torch_device(device)
    model.to(torch_device)
    model.eval()
    lookback = int(preprocessing["lookback"])
    center = pd.Series(preprocessing["scaler"]["center"])
    scale = pd.Series(preprocessing["scaler"]["scale"]).replace(0, 1.0)
    rows = []
    groups = working.sort_values(["stock_code", "trade_date"]).groupby("stock_code", sort=False)
    iterator = tqdm(groups, total=working["stock_code"].nunique(), desc="CNN scoring", unit="stock") if show_progress else groups
    for stock_code, group in iterator:
        if len(group) < lookback:
            continue
        values = group[features].apply(pd.to_numeric, errors="coerce").tail(lookback)
        missing = group[missing_columns].tail(lookback).to_numpy(dtype=np.float32)
        normalized = ((values.fillna(center) - center) / scale).clip(-8, 8).to_numpy(dtype=np.float32)
        inputs = np.concatenate([normalized, missing], axis=1)
        with torch.no_grad():
            score = float(model(torch.tensor(inputs).unsqueeze(0).to(torch_device))[0].cpu())
        rows.append({"trade_date": pd.Timestamp(group["trade_date"].iloc[-1]), "stock_code": stock_code, "model_score_raw": score})
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["model_score"] = result.groupby("trade_date")["model_score_raw"].rank(pct=True) * 100.0
    return result


def select_top_model_scores(
    score_frames: dict[str, pd.DataFrame], *, model="ensemble", top_n=10,
    model_weights: dict[str, float] | None = None,
    metadata: dict | None = None,
    as_of_date=None,
) -> pd.DataFrame:
    """Combine persisted percentile scores and return the Top-N for one date.

    ``as_of_date`` replays a historical decision date: the cross section used is
    the latest one common to every required model file that is not newer than
    the requested date.  Without it the newest common cross section is used.
    """
    requested = str(model or "ensemble").strip().lower()
    valid = {
        name: frame[["trade_date", "stock_code", "model_score"]].copy()
        for name, frame in score_frames.items()
        if frame is not None and not frame.empty
    }
    if requested not in {"lightgbm", "transformer", "cnn", "ensemble"}:
        raise ValueError("selection model must be lightgbm, transformer, cnn, or ensemble")
    required = [requested] if requested != "ensemble" else ["lightgbm", "transformer"]
    missing = [name for name in required if name not in valid]
    if missing:
        raise ValueError(f"persisted score files missing for: {','.join(missing)}")
    if as_of_date is not None:
        cutoff = pd.to_datetime(as_of_date).normalize()
        common: set | None = None
        for name in required:
            dates = set(pd.to_datetime(valid[name]["trade_date"]).dt.normalize().unique())
            common = dates if common is None else (common & dates)
        eligible = sorted(date for date in (common or set()) if date <= cutoff)
        if not eligible:
            raise ValueError(f"no persisted model scores on or before {cutoff.strftime('%Y-%m-%d')}")
        latest_date = eligible[-1]
    else:
        latest_date = min(pd.to_datetime(valid[name]["trade_date"]).max() for name in required)
    merged = None
    for name in required:
        frame = valid[name].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame[frame["trade_date"] == latest_date].rename(columns={"model_score": f"{name}_score"})
        merged = frame if merged is None else merged.merge(frame, on=["trade_date", "stock_code"], how="inner")
    if merged is None or merged.empty:
        raise ValueError("no common latest-date persisted model scores available for selection")
    score_columns = [f"{name}_score" for name in required]
    effective_weights = None
    if requested == "ensemble" and model_weights:
        weights = {name: max(0.0, float(model_weights.get(name, 0.0))) for name in required}
        total = sum(weights.values()) or 1.0
        effective_weights = {name: value / total for name, value in weights.items()}
        merged["ensemble_score"] = sum(merged[f"{name}_score"] * weights[name] for name in required) / total
    else:
        merged["ensemble_score"] = merged[score_columns].mean(axis=1)
    merged["model_score"] = merged[score_columns[0]] if requested != "ensemble" else merged["ensemble_score"]
    merged = merged.sort_values(["model_score", "stock_code"], ascending=[False, True]).reset_index(drop=True)
    merged["rank"] = np.arange(1, len(merged) + 1)
    selected = merged.head(max(1, int(top_n)))
    for key, value in (metadata or {}).items():
        selected[key] = value
    if effective_weights is not None:
        selected["effective_model_weights"] = json.dumps(effective_weights, ensure_ascii=False)
    return selected


def _prepare_labeled_panel(
    panel,
    feature_columns,
    label_column,
    *,
    min_feature_coverage=0.05,
    drop_constant_features=True,
    filter_features=True,
):
    features = [column for column in feature_columns if column in panel.columns]
    if not features:
        raise ValueError("no model feature columns")
    # Assigning 1,000+ columns one at a time fragments the DataFrame and turns
    # a label assignment into a costly operation. Convert the whole block once.
    numeric_features = panel.loc[:, features].apply(pd.to_numeric, errors="coerce")
    working = pd.concat([panel.drop(columns=features).copy(), numeric_features], axis=1)
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working = working.dropna(subset=["trade_date", "stock_code", label_column])
    # Labels are normalized only within their decision-date cross section.
    working["label"] = working.groupby("trade_date")[label_column].rank(pct=True) - 0.5
    working = working.dropna(subset=["label"])
    if not filter_features:
        return working, features, _empty_feature_quality(features, min_feature_coverage, drop_constant_features)
    features, feature_quality = _select_model_features(
        working,
        features,
        min_feature_coverage=min_feature_coverage,
        drop_constant_features=drop_constant_features,
    )
    if not features:
        raise ValueError("no informative model feature columns after quality filtering")
    return working, features, feature_quality


def _select_model_features(frame, feature_columns, *, min_feature_coverage=0.05, drop_constant_features=True):
    """Remove feature pairs that cannot contribute signal in this training fold.

    A ``*_clean`` value and its ``*_is_missing`` mask are treated as one source
    feature for coverage purposes. This prevents a low-coverage value from
    leaving behind a mask that only describes a data-source outage.
    """
    threshold = min(1.0, max(0.0, float(min_feature_coverage)))
    columns = [column for column in feature_columns if column in frame.columns]
    numeric = frame.loc[:, columns].replace([np.inf, -np.inf], np.nan)
    coverage = numeric.notna().mean()
    dropped_low_coverage: list[str] = []
    dropped_constant: list[str] = []
    dropped = set()

    clean_columns = [column for column in columns if column.endswith("_clean")]
    for clean_column in clean_columns:
        base = clean_column[:-len("_clean")]
        paired_columns = [clean_column]
        missing_column = f"{base}_is_missing"
        if missing_column in numeric.columns:
            paired_columns.append(missing_column)
        if float(coverage[clean_column]) < threshold:
            dropped.update(paired_columns)
            dropped_low_coverage.extend(paired_columns)

    if drop_constant_features:
        for column in columns:
            if column in dropped:
                continue
            values = numeric[column].dropna()
            if values.empty or values.nunique(dropna=True) <= 1:
                dropped.add(column)
                dropped_constant.append(column)

    selected = [column for column in columns if column not in dropped]
    return selected, {
        "input_feature_count": len(columns),
        "output_feature_count": len(selected),
        "min_feature_coverage": threshold,
        "drop_constant_features": bool(drop_constant_features),
        "dropped_low_coverage": sorted(set(dropped_low_coverage)),
        "dropped_constant": sorted(set(dropped_constant)),
    }


def _empty_feature_quality(feature_columns, min_feature_coverage, drop_constant_features):
    return {
        "input_feature_count": len(feature_columns),
        "output_feature_count": len(feature_columns),
        "min_feature_coverage": min(1.0, max(0.0, float(min_feature_coverage))),
        "drop_constant_features": bool(drop_constant_features),
        "dropped_low_coverage": [],
        "dropped_constant": [],
        "filter_applied": False,
    }


def _select_temporal_feature_pairs(frame, feature_columns, *, max_feature_pairs=128):
    """Select a bounded, auditable temporal feature set before sequence expansion.

    A temporal input doubles each feature with its raw-missingness mask.  Passing
    every Alpha feature therefore turns a 60-day sequence into a multi-megabyte
    object and makes a moderate training sample consume tens of gigabytes.  Rank
    clean values using only the supplied training panel's ranked label, coverage
    and variance, then retain the corresponding missingness mask when available.
    """
    limit = max(0, int(max_feature_pairs or 0))
    columns = [column for column in feature_columns if column in frame.columns]
    clean_columns = [column for column in columns if str(column).endswith("_clean")]
    if not limit or len(clean_columns) <= limit:
        return columns, {
            "max_feature_pairs": limit,
            "input_clean_feature_count": len(clean_columns),
            "selected_clean_feature_count": len(clean_columns),
            "selection_applied": False,
        }
    numeric = frame.loc[:, clean_columns].apply(pd.to_numeric, errors="coerce")
    # Feature ranking needs a stable scale estimate only. Raw Alpha formulas
    # can yield finite values outside a dtype's safe variance range before the
    # later cross-sectional winsorization. Normalize this ranking-only copy to
    # float64 and cap extremes rather than allowing pandas nanops to overflow.
    ranking_values = numeric.to_numpy(dtype=np.float64, copy=True)
    ranking_values[~np.isfinite(ranking_values)] = np.nan
    extreme_limit = 1e12
    extreme_count = int(np.count_nonzero(np.abs(ranking_values) > extreme_limit))
    np.clip(ranking_values, -extreme_limit, extreme_limit, out=ranking_values)
    numeric = pd.DataFrame(ranking_values, columns=clean_columns, index=frame.index)
    coverage = numeric.notna().mean()
    variability = numeric.std(ddof=0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    label = pd.to_numeric(frame["label"], errors="coerce")
    correlation = numeric.corrwith(label).abs().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # Coverage is capped at one and variance is log-compressed so one
    # large-scale factor cannot crowd out all other predictive candidates.
    score = correlation * coverage * np.log1p(variability.clip(lower=0.0))
    ranked_clean = sorted(clean_columns, key=lambda column: (-float(score[column]), str(column)))[:limit]
    selected = set(ranked_clean)
    for clean_column in ranked_clean:
        missing_column = f"{clean_column[:-len('_clean')]}_is_missing"
        if missing_column in columns:
            selected.add(missing_column)
    return [column for column in columns if column in selected], {
        "max_feature_pairs": limit,
        "input_clean_feature_count": len(clean_columns),
        "selected_clean_feature_count": len(ranked_clean),
        "selected_feature_count": len(selected),
        "selection_applied": True,
        "method": "abs_rank_label_correlation_x_coverage_x_log_variance",
        "ranking_extreme_clip_value": extreme_limit,
        "ranking_extreme_value_count": extreme_count,
    }


def _resolve_embargo_days(label_column, embargo_days):
    if embargo_days is not None:
        return max(0, int(embargo_days))
    match = re.search(r"_(\d+)d(?:$|_)", str(label_column))
    return int(match.group(1)) if match else 0


def _purged_time_split(frame, *, validation_days, embargo_days):
    """Split by decision date and embargo labels that overlap validation outcomes."""
    working = frame.copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    dates = sorted(working["trade_date"].dropna().unique())
    validation_count = max(1, min(int(validation_days), max(1, len(dates) - 1)))
    validation_start_index = len(dates) - validation_count
    train_end_index = max(0, validation_start_index - max(0, int(embargo_days)))
    train_dates = set(dates[:train_end_index])
    validation_dates = set(dates[validation_start_index:])
    if not train_dates or not validation_dates:
        raise ValueError("not enough dates after purged train/validation split")
    train = working[working["trade_date"].isin(train_dates)].copy()
    valid = working[working["trade_date"].isin(validation_dates)].copy()
    purged = dates[train_end_index:validation_start_index]
    return train, valid, {
        "method": "purged_time_split",
        "validation_days": validation_count,
        "embargo_days": int(embargo_days),
        "purged_date_count": len(purged),
        "train_end": str(pd.Timestamp(max(train_dates)).date()),
        "validation_start": str(pd.Timestamp(min(validation_dates)).date()),
    }


def _manifest(*, model_type, features, label_column, cleaning_version, factor_set, train_dates, validation_dates, preprocessing, extra):
    train_dates = pd.to_datetime(train_dates)
    validation_dates = pd.to_datetime(validation_dates)
    return {
        "model_type": model_type, "feature_columns": list(features), "feature_count": len(features),
        "label_column": label_column, "cleaning_version": cleaning_version, "factor_set": factor_set,
        "train_start": str(train_dates.min().date()), "train_end": str(train_dates.max().date()),
        "validation_start": str(validation_dates.min().date()) if len(validation_dates) else None,
        "validation_end": str(validation_dates.max().date()) if len(validation_dates) else None,
        "preprocessing": preprocessing, "extra": extra,
        "feature_schema_hash": _feature_schema_hash(features),
    }


def _validate_feature_schema(panel, features):
    missing = [column for column in features if column not in panel.columns]
    if missing:
        raise ValueError(f"model feature schema mismatch; missing={','.join(missing[:10])}")


def _feature_schema_hash(features):
    payload = "\n".join(str(feature) for feature in features).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _validate_manifest_version(manifest):
    expected = manifest.get("feature_schema_hash")
    features = manifest.get("feature_columns") or []
    if expected and expected != _feature_schema_hash(features):
        raise ValueError("model manifest feature schema hash is invalid")


def _load_transformer_warm_start(
    model,
    *,
    warm_start_path,
    warm_start_manifest_path,
    features,
    cleaning_version,
    factor_set,
    input_dim,
    d_model,
    nhead,
    num_layers,
):
    """Load a compatible checkpoint for fine-tuning, never silently partially load."""
    if not warm_start_path:
        return {"used": False}
    import torch

    checkpoint_path = Path(warm_start_path)
    if not checkpoint_path.is_file():
        raise ValueError(f"Transformer warm-start checkpoint does not exist: {checkpoint_path}")
    manifest_path = Path(warm_start_manifest_path) if warm_start_manifest_path else checkpoint_path.with_name("model_manifest.json")
    if not manifest_path.is_file():
        raise ValueError(f"Transformer warm-start manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_manifest_version(manifest)
    if manifest.get("model_type") != "temporal_transformer":
        raise ValueError("Transformer warm-start manifest has an incompatible model type")
    if manifest.get("feature_schema_hash") != _feature_schema_hash(features):
        raise ValueError("Transformer warm-start feature schema does not match the current panel")
    if manifest.get("cleaning_version") != cleaning_version:
        raise ValueError("Transformer warm-start cleaning version does not match the current panel")
    if factor_set is not None and manifest.get("factor_set") != factor_set:
        raise ValueError("Transformer warm-start factor set does not match the current panel")
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    expected = {"input_dim": input_dim, "d_model": d_model, "nhead": nhead, "num_layers": num_layers}
    incompatible = {key: (checkpoint.get(key), value) for key, value in expected.items() if checkpoint.get(key) != value}
    if incompatible:
        raise ValueError(f"Transformer warm-start architecture mismatch: {incompatible}")
    model.load_state_dict(checkpoint["state_dict"], strict=True)
    return {"used": True, "checkpoint_path": str(checkpoint_path), "manifest_path": str(manifest_path)}


def resolve_torch_device(requested="auto"):
    """Select CUDA, Apple Metal Performance Shaders, or CPU deterministically."""
    import torch

    choice = str(requested or "auto").strip().lower()
    available = {
        "cuda": bool(torch.cuda.is_available()),
        "mps": bool(torch.backends.mps.is_built() and torch.backends.mps.is_available()),
        "cpu": True,
    }
    if choice == "auto":
        choice = next(name for name in ("cuda", "mps", "cpu") if available[name])
    if choice not in available:
        raise ValueError("unsupported Transformer device; expected auto, cuda, mps, or cpu")
    if not available[choice]:
        raise ValueError(f"requested Transformer device is unavailable: {choice}")
    return torch.device(choice)


def _preprocess_transformer_panel(panel, features, *, config=None):
    """Apply same-day cross-sectional transforms while retaining raw missingness."""
    features = list(features)
    missing_columns = [f"__transformer_missing_{index}" for index in range(len(features))]
    missing_indicator_features = set(_missing_indicator_features(features))
    continuous_features = [feature for feature in features if feature not in missing_indicator_features]
    parameters = dict(config or {})
    mode = parameters.get("mode", "qlib_robust")
    winsorize_lo = float(parameters.get("winsorize_lo", 0.01))
    winsorize_hi = float(parameters.get("winsorize_hi", 0.99))
    clip_range = float(parameters.get("clip_range", 5.0))
    min_samples = int(parameters.get("min_samples_for_winsorize", 20))

    # Do not call the generic DataFrame group transformer here. A production
    # inference window has ~300K rows and >1K columns; copying every complete
    # group (including masks) can exceed unified memory before scoring starts.
    feature_values = panel.loc[:, features].apply(pd.to_numeric, errors="coerce")
    feature_values = feature_values.replace([np.inf, -np.inf], np.nan).clip(-1e20, 1e20).astype(np.float32)
    missing_values = feature_values.isna().to_numpy(dtype=bool, copy=True)
    continuous_indices = [features.index(feature) for feature in continuous_features]
    missing_indicator_indices = [
        index for index, feature in enumerate(features)
        if feature in missing_indicator_features
    ]
    all_values = feature_values.to_numpy(dtype=np.float32, copy=True)
    clipped_total = 0
    if continuous_indices and mode not in {"none", "raw", "off"}:
        dates = pd.to_datetime(panel["trade_date"], errors="coerce").to_numpy()
        for trade_date in pd.unique(dates):
            if pd.isna(trade_date):
                continue
            indexes = np.flatnonzero(dates == trade_date)
            if not len(indexes):
                continue
            values = all_values[np.ix_(indexes, continuous_indices)].astype(np.float64, copy=False)
            finite = np.isfinite(values)
            valid_counts = finite.sum(axis=0)
            if mode in {"qlib_robust", "robust"} and len(indexes) >= min_samples:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    lo = np.nanquantile(values, winsorize_lo, axis=0)
                    hi = np.nanquantile(values, winsorize_hi, axis=0)
                can_clip = (valid_counts > 2) & np.isfinite(lo) & np.isfinite(hi) & (hi > lo)
                if can_clip.any():
                    clipped_total += int((((values < lo) | (values > hi)) & finite & can_clip).sum())
                    values[:, can_clip] = np.clip(values[:, can_clip], lo[can_clip], hi[can_clip])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                median = np.nanmedian(values, axis=0)
                mad = np.nanmedian(np.abs(values - median), axis=0)
                std = np.nanstd(values, axis=0)
            median = np.where(np.isfinite(median), median, 0.0)
            scale = mad * 1.4826
            scale = np.where(np.isfinite(scale) & (scale >= 1e-12), scale, std)
            scale = np.where(np.isfinite(scale) & (scale >= 1e-12), scale, np.nan)
            normalized = np.clip((values - median) / scale, -clip_range, clip_range)
            all_values[np.ix_(indexes, continuous_indices)] = np.where(
                np.isfinite(normalized), normalized, 0.0
            ).astype(np.float32, copy=False)
    if missing_indicator_indices:
        indicator_values = np.nan_to_num(
            all_values[:, missing_indicator_indices], nan=1.0, posinf=1.0, neginf=0.0,
        )
        all_values[:, missing_indicator_indices] = np.clip(indicator_values, 0.0, 1.0)
    feature_values = pd.DataFrame(all_values, columns=features, index=panel.index)
    missing_frame = pd.DataFrame(missing_values, columns=missing_columns, index=panel.index)
    feature_set = set(features)
    passthrough_columns = [column for column in panel.columns if column not in feature_set]
    transformed = pd.concat(
        [panel.loc[:, passthrough_columns].copy(), feature_values, missing_frame], axis=1,
    )
    transformed["trade_date"] = pd.to_datetime(transformed["trade_date"], errors="coerce")
    metadata = {
        "mode": mode,
        "stages_applied": ["per_date_winsorize_robust_zscore_clip_fill"],
        "feature_count": len(continuous_features),
        "missing_rate_before": round(float(missing_values[:, continuous_indices].mean()), 6) if continuous_indices else 0.0,
        "total_clipped_values": int(clipped_total),
        "total_nan_filled": int(missing_values[:, continuous_indices].sum()) if continuous_indices else 0,
        "preprocess_config_hash": hashlib.sha256(
            json.dumps({
                "mode": mode, "winsorize_lo": winsorize_lo, "winsorize_hi": winsorize_hi,
                "clip_range": clip_range, "min_samples_for_winsorize": min_samples,
            }, sort_keys=True).encode()
        ).hexdigest()[:16],
        "config": {
            "mode": mode, "winsorize_lo": winsorize_lo, "winsorize_hi": winsorize_hi,
            "clip_range": clip_range, "min_samples_for_winsorize": min_samples,
        },
    }
    return transformed, missing_columns, metadata


def _missing_indicator_features(features):
    return [feature for feature in features if str(feature).endswith("_is_missing")]


def _fit_sequence_scaler(frame, features, *, preserve_binary_features=()):
    values = frame[features].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    center = values.median().fillna(0.0)
    scale = values.std(ddof=0).replace(0, np.nan).fillna(1.0)
    for feature in preserve_binary_features:
        if feature in center.index:
            center.loc[feature] = 0.0
            scale.loc[feature] = 1.0
    return {"center": center.to_dict(), "scale": scale.to_dict()}


def mean_daily_rank_ic(predictions, targets, dates) -> float:
    """Mean per-date Spearman correlation between predictions and labels.

    The training label is a within-date cross-sectional rank, so the honest
    validation statistic is the rank IC *inside each date*, not a pooled
    correlation that mixes dates with different market moves.
    """
    series = pd.DataFrame({"prediction": np.asarray(predictions, dtype=float),
                           "target": np.asarray(targets, dtype=float),
                           "trade_date": np.asarray(dates)})
    values = []
    for _, group in series.groupby("trade_date"):
        if len(group) < 5:
            continue
        ic = group["prediction"].corr(group["target"], method="spearman")
        if np.isfinite(ic):
            values.append(float(ic))
    return float(np.mean(values)) if values else float("nan")


def _build_sequences(
    panel,
    features,
    lookback,
    scaler,
    *,
    missing_columns=None,
    max_samples,
    show_progress=False,
    progress_label="sequence windows",
):
    """Build a balanced bounded sample of stock-level temporal windows.

    The former implementation appended every window for the first instruments
    until ``max_samples`` and copied each one through a Python loop.  It both
    biased training toward early stock codes and could allocate >100 GB.  This
    version allocates the finite budget evenly across stocks and selects evenly
    spaced endpoints, preserving both historical and recent validation windows.
    """
    max_samples = max(1, int(max_samples))
    center = np.asarray([scaler["center"].get(feature, 0.0) for feature in features], dtype=np.float32)
    scale = np.asarray([scaler["scale"].get(feature, 1.0) for feature in features], dtype=np.float32)
    scale[~np.isfinite(scale) | (scale == 0)] = 1.0
    ordered = panel.sort_values(["stock_code", "trade_date"], kind="stable")
    group_count = int(ordered["stock_code"].nunique())
    base_per_stock, remainder = divmod(max_samples, max(1, group_count))
    items = []
    groups = ordered.groupby("stock_code", sort=False)
    iterator = tqdm(groups, total=group_count, desc=progress_label, unit="stock") if show_progress else groups
    for group_index, (_code, group) in enumerate(iterator):
        sample_count = base_per_stock + int(group_index < remainder)
        if sample_count <= 0 or len(group) < int(lookback):
            continue
        values = group.loc[:, features].to_numpy(dtype=np.float32, copy=True)
        if missing_columns:
            missing = group[missing_columns].to_numpy(dtype=np.float32)
        else:
            missing = (~np.isfinite(values)).astype(np.float32, copy=False)
        values[~np.isfinite(values)] = np.nan
        normalized = (values - center) / scale
        np.nan_to_num(normalized, copy=False, nan=0.0, posinf=8.0, neginf=-8.0)
        np.clip(normalized, -8.0, 8.0, out=normalized)
        inputs = np.concatenate([normalized, missing], axis=1)
        labels = group["label"].to_numpy(dtype=np.float32)
        dates = group["trade_date"].to_numpy()
        endpoints = np.arange(int(lookback) - 1, len(group), dtype=int)
        if len(endpoints) > sample_count:
            endpoints = endpoints[np.linspace(0, len(endpoints) - 1, num=sample_count, dtype=int)]
        for index in endpoints:
            items.append((inputs[index - int(lookback) + 1:index + 1], labels[index], dates[index]))
    return items


def _sequence_loader(items, batch_size, DataLoader, Dataset, *, shuffle=True):
    """Yield sequence tensors lazily instead of copying the full sample set twice."""
    import torch

    class _SequenceWindowDataset(Dataset):
        def __init__(self, sequence_items):
            self._items = sequence_items

        def __len__(self):
            return len(self._items)

        def __getitem__(self, index):
            values, label, _date = self._items[index]
            return torch.from_numpy(values), torch.tensor(label, dtype=torch.float32)

    return DataLoader(
        _SequenceWindowDataset(items), batch_size=max(1, int(batch_size)), shuffle=bool(shuffle),
    )


class TemporalTransformer:
    """Small encoder-only temporal model; torch is imported lazily by training."""

    def __new__(cls, input_dim, *, d_model, nhead, num_layers):
        import torch
        from torch import nn

        class _TemporalTransformer(nn.Module):
            def __init__(self):
                super().__init__()
                self.projection = nn.Linear(input_dim, d_model)
                self.position = nn.Parameter(torch.zeros(1, 512, d_model))
                layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead, batch_first=True, dropout=0.1)
                self.encoder = nn.TransformerEncoder(layer, num_layers=num_layers)
                self.head = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, 1))

            def forward(self, values):
                encoded = self.projection(values) + self.position[:, :values.shape[1]]
                return self.head(self.encoder(encoded)[:, -1]).squeeze(-1)

        return _TemporalTransformer()


class TemporalCNN:
    """Compact 1D CNN over the same [time, feature] inputs as Transformer."""

    def __new__(cls, input_dim, *, channels, kernel_size, num_layers):
        import torch
        from torch import nn

        class _TemporalCNN(nn.Module):
            def __init__(self):
                super().__init__()
                layers = []
                in_channels = input_dim
                padding = max(0, int(kernel_size) // 2)
                for _ in range(max(1, int(num_layers))):
                    layers.extend([
                        nn.Conv1d(in_channels, int(channels), kernel_size=int(kernel_size), padding=padding),
                        nn.GELU(),
                    ])
                    in_channels = int(channels)
                self.features = nn.Sequential(*layers)
                self.head = nn.Sequential(nn.AdaptiveAvgPool1d(1), nn.Flatten(), nn.Linear(int(channels), 1))

            def forward(self, values):
                return self.head(self.features(values.transpose(1, 2))).squeeze(-1)

        return _TemporalCNN()
