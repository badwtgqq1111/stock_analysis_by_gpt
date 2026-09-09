"""Validation split helpers for financial ML."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class PurgedFold:
    """Date-level purged CV fold."""

    fold: int
    train_dates: list[pd.Timestamp]
    test_dates: list[pd.Timestamp]
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    purge_days: int
    embargo_days: int


@dataclass(frozen=True)
class WalkForwardFold:
    """Expanding walk-forward fold with no future dates in the training set."""

    fold: int
    train_dates: list[pd.Timestamp]
    test_dates: list[pd.Timestamp]
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    purge_days: int
    embargo_days: int


def purged_time_series_splits(
    dates,
    *,
    n_splits: int = 5,
    purge_days: int = 21,
    embargo_days: int = 20,
) -> list[PurgedFold]:
    """Create date-level purged/embargoed CV folds.

    The split is intentionally date-based, not row-based.  Any train date whose
    label window can overlap the test window is removed by the purge interval;
    dates after the test window are also embargoed to avoid slow-moving feature
    leakage.
    """
    date_index = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).dropna().unique())).sort_values()
    if len(date_index) < max(2, n_splits):
        return []
    n_splits = max(2, min(int(n_splits), len(date_index)))
    purge = pd.Timedelta(days=max(int(purge_days), 0))
    embargo = pd.Timedelta(days=max(int(embargo_days), 0))

    fold_sizes = [len(date_index) // n_splits] * n_splits
    for i in range(len(date_index) % n_splits):
        fold_sizes[i] += 1

    folds: list[PurgedFold] = []
    start = 0
    for fold_idx, size in enumerate(fold_sizes):
        stop = start + size
        test_dates = list(date_index[start:stop])
        if not test_dates:
            start = stop
            continue
        test_start = test_dates[0]
        test_end = test_dates[-1]
        train_dates = [
            date
            for date in date_index
            if date < test_start - purge or date > test_end + embargo
        ]
        folds.append(
            PurgedFold(
                fold=fold_idx + 1,
                train_dates=train_dates,
                test_dates=test_dates,
                test_start=test_start,
                test_end=test_end,
                purge_days=int(purge_days),
                embargo_days=int(embargo_days),
            )
        )
        start = stop
    return folds


def expanding_walk_forward_splits(
    dates,
    *,
    n_splits: int = 5,
    min_train_days: int = 120,
    test_days: int | None = None,
    purge_days: int = 20,
    embargo_days: int = 0,
) -> list[WalkForwardFold]:
    """Create chronological expanding folds for genuine out-of-sample testing.

    Unlike generic purged CV, every train date precedes the test block. The
    purge interval is measured in available decision dates, so weekends and
    exchange holidays do not create false gaps.
    """
    date_index = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).dropna().unique())).sort_values()
    if len(date_index) < max(2, int(min_train_days) + 1):
        return []
    n_splits = max(1, int(n_splits))
    available = len(date_index) - max(0, int(min_train_days))
    block = max(1, int(test_days)) if test_days is not None else max(1, available // n_splits)
    folds: list[WalkForwardFold] = []
    test_start_index = max(0, int(min_train_days))
    for fold_index in range(n_splits):
        test_end_index = min(len(date_index), test_start_index + block)
        if test_start_index >= test_end_index:
            break
        train_end_index = max(0, test_start_index - max(0, int(purge_days)))
        train_dates = list(date_index[:train_end_index])
        test_dates = list(date_index[test_start_index:test_end_index])
        if train_dates and test_dates:
            folds.append(
                WalkForwardFold(
                    fold=fold_index + 1,
                    train_dates=train_dates,
                    test_dates=test_dates,
                    test_start=test_dates[0],
                    test_end=test_dates[-1],
                    purge_days=max(0, int(purge_days)),
                    embargo_days=max(0, int(embargo_days)),
                )
            )
        # Embargo advances the next test origin without adding future rows to
        # the training set. This keeps folds disjoint and deterministic.
        test_start_index = test_end_index + max(0, int(embargo_days))
    return folds
