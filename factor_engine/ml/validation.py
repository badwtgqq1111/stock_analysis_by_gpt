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
