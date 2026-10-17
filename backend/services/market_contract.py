"""P2.71 data/label contract for the independent research path.

This module is deliberately independent of the legacy model, selector and
replay code.  It names the panel schema, feature availability, both labels and
the purge rule used by the Alpha101 / AlphaQCM research track.

The source files are the local CN qfq daily bars.  They contain an upstream
``vwap`` column, but an audit shows that it is on the raw-price scale while
``open/high/low/close`` are qfq.  The default research panel therefore carries
an explicit OHLC3 VWAP proxy; the native column is retained separately and is
never silently mixed with qfq prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[2]
OHLCV_ROOT = ROOT / 'assets/data/clean/ohlcv/market=CN'
STOCK_BASIC = ROOT / 'assets/data/meta/cn_stock_basic.parquet'
BENCHMARK = ROOT / 'assets/data/meta/cn_benchmark_daily.parquet'

FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount', 'turnover', 'vwap')
DERIVED_FIELDS = ('vwap_proxy', 'return_1d', 'adv20')


@dataclass(frozen=True)
class DataContract:
    """Named, auditable assumptions attached to every research manifest."""

    panel_schema: tuple[str, ...] = ('trade_date', 'stock_code', *FIELDS, *DERIVED_FIELDS)
    frequency: str = 'daily'
    timezone: str = 'Asia/Shanghai'
    price_adjustment: str = 'qfq (provider-adjusted; no independent factor audit)'
    feature_availability: str = 'after the t close (15:00 Asia/Shanghai)'
    execution: str = 'next observed market session open'
    native_vwap: str = 'raw-scale upstream amount/volume; excluded from qfq formulas by default'
    vwap_proxy: str = '(high + low + close) / 3, same qfq scale as OHLC'
    paper_label: str = 'close[t+20] / close[t] - 1'
    executable_label: str = 'open[t+21] / open[t+1] - 1'
    missing_policy: str = 'NaN is preserved; no forward fill in labels or factor values'
    universe: str = 'bars present on date + listed on/before date; no historical index membership'

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _year_files(start: str | pd.Timestamp, end: str | pd.Timestamp) -> list[Path]:
    files = sorted(OHLCV_ROOT.glob(
        'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet'))
    years = range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1)
    chosen = [p for p in files if int(p.parent.name.split('=')[1]) in years]
    if not chosen:
        raise ValueError('缺少日线 parquet')
    return chosen


def load_bars(start: str, end: str, *, warmup_days: int = 0,
              columns: Iterable[str] = FIELDS) -> pd.DataFrame:
    """Load qfq bars and build the canonical panel.

    ``warmup_days`` is a number of *observed dates* prepended before ``start``.
    It is resolved on the provider calendar, not by calendar-day arithmetic.
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if end_ts <= start_ts:
        raise ValueError('end 必须晚于 start')
    files = _year_files(start_ts, end_ts)
    wanted = ['stock_code', 'trade_date', *columns]
    table = ds.dataset([str(p) for p in files], format='parquet').to_table(
        columns=list(dict.fromkeys(wanted))).to_pandas()
    table['trade_date'] = pd.to_datetime(table['trade_date'])
    dates = pd.DatetimeIndex(sorted(table.trade_date.unique()))
    first = int(dates.searchsorted(start_ts))
    last = int(dates.searchsorted(end_ts, side='right'))
    if first >= len(dates) or last <= 0:
        raise ValueError('请求区间不在日线覆盖内')
    lo = max(0, first - int(warmup_days))
    keep = dates[lo:last]
    table = table[table.trade_date.isin(keep)].copy()
    return normalize_panel(table)


def normalize_panel(frame: pd.DataFrame) -> pd.DataFrame:
    required = {'trade_date', 'stock_code', 'open', 'high', 'low', 'close', 'volume'}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f'面板缺列: {sorted(missing)}')
    out = frame.copy()
    out['stock_code'] = out['stock_code'].astype(str)
    out['trade_date'] = pd.to_datetime(out['trade_date'], errors='raise').dt.normalize()
    if out[['stock_code', 'trade_date']].isna().any().any():
        raise ValueError('面板键含缺失')
    if out.duplicated(['stock_code', 'trade_date']).any():
        raise ValueError('面板键重复')
    for col in FIELDS:
        if col not in out:
            continue
        out[col] = pd.to_numeric(out[col], errors='coerce')
        out[col] = out[col].replace([np.inf, -np.inf], np.nan)
    # Provider suspension placeholders use 0 for all OHLC.  Treat them as
    # missing, not as a tradable zero price and not as a data-contract failure.
    ohlc_cols = ['open', 'high', 'low', 'close']
    out.loc[(out[ohlc_cols] <= 0).any(axis=1), ohlc_cols] = np.nan
    out = out.sort_values(['stock_code', 'trade_date'], kind='stable').reset_index(drop=True)
    out['vwap_proxy'] = (out.high + out.low + out.close) / 3.0
    grouped = out.groupby('stock_code', sort=False)
    out['return_1d'] = grouped.close.pct_change(fill_method=None)
    out['adv20'] = grouped.volume.transform(lambda s: s.rolling(20, min_periods=1).mean())
    if 'amount' in out:
        # Explicit record of the unit audit, not a silent conversion.
        out['amount_per_close_volume'] = out.amount / (out.close * out.volume)
    return out


def validate_panel(frame: pd.DataFrame, *, strict_prices: bool = True) -> dict:
    """Validate keys, ordering, price bounds and record missingness."""
    required = {'trade_date', 'stock_code', 'open', 'high', 'low', 'close', 'volume',
                'vwap_proxy', 'return_1d'}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f'面板缺列: {sorted(missing)}')
    if frame.duplicated(['stock_code', 'trade_date']).any():
        raise ValueError('面板键重复')
    if not frame.equals(frame.sort_values(['stock_code', 'trade_date'])):
        raise ValueError('面板未按股票和日期排序')
    ohlc = frame[['open', 'high', 'low', 'close']]
    present = ohlc.notna().all(axis=1)
    bad = present & ((frame.high + 1e-9 < frame[['open', 'close', 'low']].max(axis=1))
                     | (frame.low - 1e-9 > frame[['open', 'close', 'high']].min(axis=1)))
    if strict_prices and bad.any():
        raise ValueError(f'OHLC 边界/正数校验失败: {int(bad.sum())} 行')
    return {
        'rows': int(len(frame)),
        'assets': int(frame.stock_code.nunique()),
        'dates': int(frame.trade_date.nunique()),
        'first_date': str(frame.trade_date.min().date()),
        'last_date': str(frame.trade_date.max().date()),
        'missing_fraction': {c: float(frame[c].isna().mean()) for c in required},
        'invalid_price_rows': int(bad.sum()),
        'zero_ohlc_rows': int((frame[['open', 'high', 'low', 'close']] == 0).any(axis=1).sum()),
    }


def unit_audit(frame: pd.DataFrame) -> dict:
    """Describe the native VWAP and amount/volume scale without converting it."""
    year = frame.trade_date.dt.year
    ratio = frame.amount / (frame.close * frame.volume)
    vwap_ratio = frame.vwap / frame.close if 'vwap' in frame else pd.Series(dtype=float)
    by_year = pd.DataFrame({'year': year, 'amount_ratio': ratio, 'vwap_ratio': vwap_ratio})
    med = by_year.groupby('year').median(numeric_only=True)
    stable = bool(
        len(med) >= 2
        and (med.amount_ratio.max() - med.amount_ratio.min()) < 0.02
        and (med.vwap_ratio.max() - med.vwap_ratio.min()) < 0.02
    )
    return {
        'status': 'NATIVE_VWAP_NOT_QFQ_CONSISTENT' if not stable else 'NATIVE_VWAP_SCALE_STABLE',
        'amount_over_close_times_volume_median': float(ratio.median()),
        'vwap_over_close_median': float(vwap_ratio.median()),
        'year_medians': {int(k): {c: float(v) for c, v in row.items()}
                         for k, row in med.iterrows()},
        'conclusion': ('volume and amount appear to be on the provider raw scale while OHLC is qfq; '
                       'OHLC3 proxy is used for qfq-consistent formulas'),
    }


def forward_return(panel: pd.DataFrame, horizon: int = 20, *, kind: str = 'paper') -> pd.Series:
    """Return the named label on the panel's (date, asset) index.

    paper: close[t+horizon]/close[t]-1, matching FactorBench/AlphaGen.
    executable: open[t+horizon+1]/open[t+1]-1, the next-open entry and exit
    label used by the local causal portfolio path.
    """
    if kind not in ('paper', 'executable'):
        raise ValueError('label kind 必须是 paper 或 executable')
    if not isinstance(horizon, int) or horizon <= 0:
        raise ValueError('horizon 必须是正整数')
    wide = panel.pivot(index='trade_date', columns='stock_code', values='close' if kind == 'paper' else 'open')
    if kind == 'paper':
        out = wide.shift(-horizon) / wide - 1.0
    else:
        out = wide.shift(-(horizon + 1)) / wide.shift(-1) - 1.0
    stacked = out.stack(future_stack=True)
    stacked.name = f'forward_return_{horizon}d_{kind}'
    return stacked


def forward_return_wide(panel: pd.DataFrame, horizon: int = 20, *,
                        kind: str = 'paper') -> pd.DataFrame:
    """Wide label matrix; cheaper than stacking when the panel is large."""
    if kind not in ('paper', 'executable'):
        raise ValueError('label kind 必须是 paper 或 executable')
    field = 'close' if kind == 'paper' else 'open'
    wide = panel.pivot(index='trade_date', columns='stock_code', values=field)
    if kind == 'paper':
        return wide.shift(-horizon) / wide - 1.0
    return wide.shift(-(horizon + 1)) / wide.shift(-1) - 1.0


def attach_labels(panel: pd.DataFrame, horizon: int = 20) -> pd.DataFrame:
    out = panel.copy().set_index(['trade_date', 'stock_code']).sort_index()
    out['label_paper'] = forward_return(panel, horizon, kind='paper')
    out['label_executable'] = forward_return(panel, horizon, kind='executable')
    return out.reset_index()


def purged_split(panel: pd.DataFrame, split: dict[str, tuple[str, str]],
                 horizon: int = 20) -> pd.DataFrame:
    """Assign segments and purge the final ``horizon`` sessions of train/valid.

    A label observed at t uses prices through t+horizon.  Keeping a date near
    the end of a segment would leak across the boundary, so the label-end date
    determines membership.
    """
    if not {'trade_date', 'stock_code'} <= set(panel.columns):
        raise ValueError('面板缺键')
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(panel.trade_date).unique()))
    labels = pd.Series(index=dates, dtype=object)
    for name in ('train', 'validation', 'test'):
        if name not in split:
            continue
        start, end = map(pd.Timestamp, split[name])
        seg = dates[(dates >= start) & (dates <= end)]
        if seg.empty:
            raise ValueError(f'{name} 区间没有交易日')
        labels.loc[seg] = name
    for name in ('train', 'validation'):
        if name not in split:
            continue
        end = pd.Timestamp(split[name][1])
        seg = dates[(dates <= end)]
        if len(seg) > horizon:
            labels.loc[seg[-horizon:]] = 'purged'
    out = panel.copy()
    out['segment'] = pd.to_datetime(out.trade_date).map(labels)
    return out


def split_date_map(split: dict[str, tuple[str, str]], horizon: int = 20,
                   dates: Iterable[str | pd.Timestamp] | None = None) -> pd.Series:
    """Return a date -> segment map with the last ``horizon`` labels purged."""
    if dates is None:
        all_dates = []
        for name in ('train', 'validation', 'test'):
            if name in split:
                all_dates.append(pd.date_range(split[name][0], split[name][1], freq='D'))
        if not all_dates:
            raise ValueError('split 为空')
        dates = pd.DatetimeIndex(sorted(set().union(*[set(x) for x in all_dates])))
    else:
        dates = pd.DatetimeIndex(sorted(pd.to_datetime(list(dates))))
    labels = pd.Series(index=dates, dtype=object)
    for name in ('train', 'validation', 'test'):
        if name not in split:
            continue
        start, end = map(pd.Timestamp, split[name])
        labels.loc[(dates >= start) & (dates <= end)] = name
    for name in ('train', 'validation'):
        if name not in split:
            continue
        end = pd.Timestamp(split[name][1])
        seg = dates[dates <= end]
        if len(seg) > horizon:
            labels.loc[seg[-horizon:]] = 'purged'
    return labels


def pit_universe(as_of: str | pd.Timestamp, basic: pd.DataFrame | None = None) -> set[str]:
    """Listed-before-date universe.

    The local basic table has listing dates but no delisting dates.  Bar
    presence remains the second gate.  This is *not* historical index
    membership and is labelled as such in every report.
    """
    basic = pd.read_parquet(STOCK_BASIC) if basic is None else basic
    date = pd.Timestamp(as_of)
    listed = pd.to_datetime(basic.list_date, errors='coerce')
    return set(basic.loc[listed.notna() & (listed <= date), 'ts_code'].astype(str))


def benchmark_series(code: str, start: str, end: str) -> pd.Series:
    bench = pd.read_parquet(BENCHMARK)
    bench['trade_date'] = pd.to_datetime(bench.trade_date)
    out = bench[(bench.ts_code == code) & bench.trade_date.between(start, end)]
    out = out.sort_values('trade_date')
    if out.empty:
        raise ValueError('基准为空')
    return out.set_index('trade_date').close


def wide_features(panel: pd.DataFrame, fields: Iterable[str] = ('open', 'high', 'low', 'close',
                                                               'volume', 'vwap_proxy', 'return_1d')) -> dict[str, pd.DataFrame]:
    missing = set(fields) - set(panel.columns)
    if missing:
        raise ValueError(f'面板缺特征: {sorted(missing)}')
    return {field: panel.pivot(index='trade_date', columns='stock_code', values=field)
            for field in fields}


CONTRACT = DataContract()
