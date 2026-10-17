"""Qlib-free panel adapter for the vendored AlphaQCM/AlphaGen fork."""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import torch

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / 'vendor/alphaqcm'


def prepare_vendor_path() -> None:
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))


def _build_tensor(panel: pd.DataFrame, features: list, start_time: str, end_time: str,
                  max_backtrack_days: int, max_future_days: int,
                  device: torch.device) -> tuple[torch.Tensor, pd.DatetimeIndex, pd.Index]:
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(panel.trade_date).unique()))
    start_index = int(dates.searchsorted(pd.Timestamp(start_time)))
    end_index = int(dates.searchsorted(pd.Timestamp(end_time)))
    if end_index == len(dates) or dates[end_index] != pd.Timestamp(end_time):
        end_index -= 1
    if start_index - max_backtrack_days < 0:
        raise ValueError(f'{start_time} 前缺少 {max_backtrack_days} 个交易日')
    if end_index + max_future_days >= len(dates):
        raise ValueError(f'{end_time} 后缺少 {max_future_days} 个交易日')
    window_dates = dates[start_index - max_backtrack_days:end_index + max_future_days + 1]
    window = panel[pd.to_datetime(panel.trade_date).isin(window_dates)]
    assets = pd.Index(sorted(window.stock_code.astype(str).unique()))
    columns = [f.name.lower() for f in features]
    wide = {}
    for col in columns:
        if col in window.columns:
            wide[col] = (window.pivot(index='trade_date', columns='stock_code', values=col)
                         .reindex(index=window_dates, columns=assets))
        else:
            # Keep the action space stable across panels: a missing fundamental
            # feature is an all-NaN tensor column, never an out-of-bounds index.
            wide[col] = pd.DataFrame(np.nan, index=window_dates, columns=assets)
    values = np.stack([wide[col].to_numpy(dtype=np.float64) for col in columns], axis=1)
    alive = ~np.isnan(values).all(axis=(0, 1))
    values = values[:, :, alive]
    return (torch.tensor(values, dtype=torch.float, device=device),
            pd.DatetimeIndex(window_dates), assets[alive])


def stock_data_class():
    prepare_vendor_path()
    from alphagen_qlib.stock_data import FeatureType, StockData

    class PanelStockData(StockData):
        def __init__(self, panel: pd.DataFrame, start_time: str, end_time: str,
                     max_backtrack_days: int = 100, max_future_days: int = 30,
                     features: list | None = None,
                     device: torch.device = torch.device('cpu'), market: str = 'cn'):
            self._panel = panel
            if features is None:
                # The vendored env action space is defined over the full
                # FeatureType enum.  Missing columns are represented as NaN in
                # _build_tensor so the tensor width and action space agree.
                features = list(FeatureType)
            super().__init__(instrument=market, start_time=start_time, end_time=end_time,
                             max_backtrack_days=max_backtrack_days,
                             max_future_days=max_future_days,
                             features=features, device=device)

        @classmethod
        def _init_qlib(cls) -> None:
            return

        def _get_data(self):
            return _build_tensor(self._panel, self._features, self._start_time, self._end_time,
                                 self.max_backtrack_days, self.max_future_days, self.device)

    return PanelStockData
