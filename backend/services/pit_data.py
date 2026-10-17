"""PIT (point-in-time) data layer for the P2.71 factor-mining program.

Reads the resumable caches written by ``scripts/p2_71_pit_fetch.py`` plus the
local meta tables, and annotates bar panels with point-in-time flags:

* listing / delisting status (``is_listed``),
* historical ST and delisting-risk intervals (``is_st``, ``is_delist_risk``),
* exchange suspension records (``suspended_official``),
* true price-limit flags from raw prices + exchange limit prices
  (``close_at_up`` / ``close_at_down`` / ``open_at_up`` / ``open_at_down``),
* raw (unadjusted) daily prices and daily adjustment factors.

The annotate path is an as-of join on ``(stock_code, trade_date)``, so callers
can merge flags against any bar panel without a global calendar assumption.

Data sources can be inconsistent in vintage (e.g. the local qfq OHLCV keeps
its adjustment base from the last rebuild and appends raw-scale rows since);
:func:`qfq_vintage_report` quantifies that, and derived studies should prefer
raw prices + :func:`adj_factors` for adjustment-sensitive work.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PIT_ROOT = ROOT / 'assets/data/raw/pit/market=CN'
META_ROOT = ROOT / 'assets/data/meta'
ADJ_SNAPSHOTS = ROOT / 'assets/data/raw/adjustment_factor_snapshots/market=CN/source=tushare_relay'
LIMIT_TOL = 0.001  # half a tick; exchange prices live on a 0.01 grid


def normalize_code(value) -> str:
    """Map tushare/relay code spellings to the project ``stock_code`` form."""
    text = str(value).strip().upper().replace('_', '.')
    if text.startswith('T') and len(text) > 1 and text[1].isdigit():
        text = text[1:]  # tushare marks reused old codes with a leading T
    return text


def _dates(values) -> pd.Series:
    return pd.to_datetime(values, errors='coerce').dt.normalize()


def _read_bulk(root: Path | None, dataset: str, **names) -> pd.DataFrame:
    base = (root or PIT_ROOT) / dataset
    if not base.exists():
        return pd.DataFrame()
    frames = [pd.read_parquet(p) for p in sorted(base.rglob('*.parquet'))]
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    code_col = names.get('code_col', 'ts_code')
    if code_col in out.columns:
        out['stock_code'] = out[code_col].map(normalize_code)
    if 'trade_date' in out.columns:
        out['trade_date'] = _dates(out['trade_date'])
    return out


def _read_code_data(root: Path | None, dataset: str, codes: Iterable[str] | None = None) -> pd.DataFrame:
    base = (root or PIT_ROOT) / 'code_data'
    if not base.exists():
        return pd.DataFrame()
    wanted = {normalize_code(code) for code in codes} if codes is not None else None
    frames = []
    for path in sorted(base.glob(f'code=*/{dataset}.parquet')):
        code = normalize_code(path.parent.name.split('=', 1)[1])
        if wanted is not None and code not in wanted:
            continue
        frame = pd.read_parquet(path)
        if len(frame):
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out['stock_code'] = out['ts_code'].map(normalize_code)
    out['trade_date'] = _dates(out['trade_date'])
    return out


# ---------------------------------------------------------------- meta tables

def listing_dates(meta_root: Path | None = None) -> pd.DataFrame:
    path = (meta_root or META_ROOT) / 'cn_stock_basic.parquet'
    if not path.exists():
        return pd.DataFrame(columns=['stock_code', 'list_date'])
    basic = pd.read_parquet(path)
    basic['stock_code'] = basic['ts_code'].map(normalize_code)
    basic['list_date'] = _dates(basic['list_date'])
    return basic[['stock_code', 'list_date']].dropna().drop_duplicates('stock_code')


def delist_dates(root: Path | None = None, meta_root: Path | None = None) -> pd.DataFrame:
    """Delisting dates with source precedence official > delisted-last-bar."""
    official = _read_bulk(root, 'stock_basic_delisted', code_col='ts_code')
    rows: dict[str, dict] = {}
    if len(official):
        for code, group in official.groupby('stock_code'):
            date = _dates(group['delist_date']).max()
            if pd.notna(date):
                rows[code] = {'delist_date': date, 'source': 'stock_basic_D'}
    delisted = (meta_root or META_ROOT) / 'cn_delisted_daily.parquet'
    if delisted.exists():
        table = pd.read_parquet(delisted, columns=['ts_code', 'trade_date'])
        last = table.assign(stock_code=table['ts_code'].map(normalize_code),
                            trade_date=_dates(table['trade_date'])) \
                    .groupby('stock_code')['trade_date'].max()
        for code, date in last.items():
            if code not in rows:
                rows[code] = {'delist_date': date, 'source': 'delisted_last_bar'}
    if not rows:
        return pd.DataFrame(columns=['stock_code', 'delist_date', 'source'])
    out = pd.DataFrame([{**{'stock_code': code}, **payload} for code, payload in rows.items()])
    return out.sort_values('stock_code').reset_index(drop=True)


def st_intervals(meta_root: Path | None = None) -> pd.DataFrame:
    path = (meta_root or META_ROOT) / 'cn_stock_namechange.parquet'
    if not path.exists():
        return pd.DataFrame(columns=['stock_code', 'start_date', 'end_date',
                                     'is_st', 'is_delist_risk'])
    table = pd.read_parquet(path)
    out = pd.DataFrame({
        'stock_code': table['ts_code'].map(normalize_code),
        'start_date': _dates(table['start_date']),
        'end_date': _dates(table['end_date']),
        'is_st': table['is_st'].fillna(False).astype(bool),
        'is_delist_risk': table['is_delist_risk'].fillna(False).astype(bool),
    }).dropna(subset=['start_date'])
    return out.sort_values(['stock_code', 'start_date']).reset_index(drop=True)


def suspend_records(root: Path | None = None) -> pd.DataFrame:
    table = _read_bulk(root, 'suspend_d')
    if not len(table):
        return pd.DataFrame(columns=['stock_code', 'trade_date', 'suspend_type'])
    return table[['stock_code', 'trade_date', 'suspend_type']].dropna(subset=['trade_date'])


def index_weights(index_code: str, root: Path | None = None) -> pd.DataFrame:
    path = (root or PIT_ROOT) / 'index_weight' / f'index_code={index_code}' / 'bulk.parquet'
    if not path.exists():
        return pd.DataFrame(columns=['stock_code', 'trade_date', 'weight'])
    frame = pd.read_parquet(path)
    frame['stock_code'] = frame['con_code'].map(normalize_code)
    frame['trade_date'] = _dates(frame['trade_date'])
    return frame[['stock_code', 'trade_date', 'weight']].drop_duplicates(
        ['stock_code', 'trade_date'])


def raw_daily(codes: Iterable[str] | None = None, root: Path | None = None) -> pd.DataFrame:
    frame = _read_code_data(root, 'daily_raw', codes)
    if not len(frame):
        return pd.DataFrame(columns=['stock_code', 'trade_date', 'open', 'high', 'low',
                                     'close', 'pre_close'])
    return frame[['stock_code', 'trade_date', 'open', 'high', 'low', 'close',
                  'pre_close', 'pct_chg']]


def stk_limit(codes: Iterable[str] | None = None, root: Path | None = None) -> pd.DataFrame:
    frame = _read_code_data(root, 'stk_limit', codes)
    if not len(frame):
        return pd.DataFrame(columns=['stock_code', 'trade_date', 'up_limit', 'down_limit'])
    return frame[['stock_code', 'trade_date', 'up_limit', 'down_limit']]


def adj_factors(codes: Iterable[str] | None = None, root: Path | None = None,
                meta_root: Path | None = None) -> pd.DataFrame:
    """Daily adjustment factors from the freshest available local source."""
    code_data = _read_code_data(root, 'adj_factor', codes)
    frames = []
    if len(code_data):
        frames.append(code_data[['stock_code', 'trade_date', 'adj_factor']]
                      .assign(source='pit_code_data'))
    snap_root = ADJ_SNAPSHOTS
    if snap_root.exists():
        wanted = {normalize_code(code) for code in codes} if codes is not None else None
        snaps = []
        for path in sorted(snap_root.glob('*.parquet')):
            code = normalize_code(path.stem)
            if wanted is not None and code not in wanted:
                continue
            snaps.append(pd.read_parquet(path))
        if snaps:
            snap = pd.concat(snaps, ignore_index=True)
            snap['stock_code'] = snap['stock_code'].map(normalize_code)
            snap['trade_date'] = _dates(snap['trade_date'])
            frames.append(snap[['stock_code', 'trade_date', 'adj_factor']]
                          .assign(source='adjustment_snapshot'))
    delisted = (meta_root or META_ROOT) / 'cn_delisted_daily.parquet'
    if delisted.exists():
        table = pd.read_parquet(delisted, columns=['ts_code', 'trade_date', 'adj_factor'])
        table['stock_code'] = table['ts_code'].map(normalize_code)
        table['trade_date'] = _dates(table['trade_date'])
        wanted = {normalize_code(code) for code in codes} if codes is not None else None
        if wanted is not None:
            table = table[table.stock_code.isin(wanted)]
        frames.append(table[['stock_code', 'trade_date', 'adj_factor']]
                      .assign(source='delisted_daily'))
    if not frames:
        return pd.DataFrame(columns=['stock_code', 'trade_date', 'adj_factor', 'source'])
    out = pd.concat(frames, ignore_index=True).dropna(subset=['adj_factor'])
    out['_rank'] = out['source'].map({'pit_code_data': 0, 'adjustment_snapshot': 1,
                                      'delisted_daily': 2})
    out = (out.sort_values(['stock_code', 'trade_date', '_rank'])
              .drop_duplicates(['stock_code', 'trade_date'], keep='first')
              .drop(columns='_rank'))
    return out.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)


# ------------------------------------------------------------- as-of matching

def _asof_join(keys: pd.DataFrame, ref: pd.DataFrame, value_cols: list[str]) -> pd.DataFrame:
    """Backward as-of join that preserves the caller's key order."""
    if not len(ref):
        out = keys.copy()
        for column in value_cols:
            out[column] = np.nan
        return out
    left = keys.copy()
    left['_row'] = np.arange(len(left))
    left = left.sort_values('trade_date', kind='stable')
    right = ref.sort_values('trade_date', kind='stable')
    merged = pd.merge_asof(left, right, on='trade_date', by='stock_code',
                           direction='backward')
    return merged.sort_values('_row', kind='stable').drop(columns='_row').reset_index(drop=True)


def st_panel(keys: pd.DataFrame, meta_root: Path | None = None) -> pd.DataFrame:
    """Attach ``is_st`` / ``is_delist_risk`` to (stock_code, trade_date) keys."""
    intervals = st_intervals(meta_root)
    out = keys.copy()
    out['is_st'] = False
    out['is_delist_risk'] = False
    if not len(intervals):
        return out
    ref = intervals.rename(columns={'start_date': 'trade_date'})
    merged = _asof_join(out[['stock_code', 'trade_date']], ref,
                        ['end_date', 'is_st', 'is_delist_risk'])
    live = merged['end_date'].isna() | (merged['trade_date'] <= merged['end_date'])
    out['is_st'] = merged['is_st'].where(live, False).fillna(False).astype(bool).values
    out['is_delist_risk'] = (merged['is_delist_risk'].where(live, False)
                             .fillna(False).astype(bool).values)
    return out


def index_panel(keys: pd.DataFrame, index_code: str,
                root: Path | None = None) -> pd.DataFrame:
    """``in_index`` forward-filled from monthly weight snapshots to bar keys."""
    out = keys.copy()
    out['in_index'] = False
    out['index_weight'] = np.nan
    weights = index_weights(index_code, root)
    if not len(weights):
        return out
    merged = _asof_join(out[['stock_code', 'trade_date']], weights, ['weight'])
    out['in_index'] = merged['weight'].notna().values
    out['index_weight'] = merged['weight'].values
    return out


def limit_panel(keys: pd.DataFrame, root: Path | None = None,
                codes: Iterable[str] | None = None) -> pd.DataFrame:
    """True price-limit flags from exchange limit prices and raw closes."""
    out = keys.copy()
    for column in ('raw_close', 'raw_open', 'up_limit', 'down_limit',
                   'close_at_up', 'close_at_down', 'open_at_up', 'open_at_down'):
        out[column] = np.nan
    limits = stk_limit(codes, root)
    raws = raw_daily(codes, root)
    if not len(limits) or not len(raws):
        return out
    merged = out[['stock_code', 'trade_date']].merge(
        raws[['stock_code', 'trade_date', 'open', 'close']]
        .rename(columns={'open': 'raw_open', 'close': 'raw_close'}),
        on=['stock_code', 'trade_date'], how='left')
    merged = merged.merge(limits, on=['stock_code', 'trade_date'], how='left')
    close = merged['raw_close'].astype(float)
    open_ = merged['raw_open'].astype(float)
    up = merged['up_limit'].astype(float)
    down = merged['down_limit'].astype(float)
    valid = close.notna() & up.notna()
    out['raw_close'] = close.values
    out['raw_open'] = open_.values
    out['up_limit'] = up.values
    out['down_limit'] = down.values
    out['close_at_up'] = (close >= up - LIMIT_TOL).where(valid, np.nan).values
    out['close_at_down'] = ((close <= down + LIMIT_TOL)
                            .where(close.notna() & down.notna(), np.nan).values)
    out['open_at_up'] = ((open_ >= up - LIMIT_TOL)
                         .where(open_.notna() & up.notna(), np.nan).values)
    out['open_at_down'] = ((open_ <= down + LIMIT_TOL)
                           .where(open_.notna() & down.notna(), np.nan).values)
    return out


def suspension_panel(keys: pd.DataFrame, root: Path | None = None) -> pd.DataFrame:
    """Official suspension records; True means a suspension record exists."""
    out = keys.copy()
    out['suspended_official'] = np.nan
    records = suspend_records(root)
    if not len(records):
        return out
    suspended = records[records['suspend_type'].astype(str).str.upper() == 'S']
    marker = suspended.drop_duplicates(['stock_code', 'trade_date']) \
                      .assign(suspended_official=True)
    merged = out[['stock_code', 'trade_date']].merge(
        marker, on=['stock_code', 'trade_date'], how='left')
    out['suspended_official'] = merged['suspended_official'].fillna(False).astype(bool).values
    return out


def annotate_bars(bars: pd.DataFrame, *, index_code: str | None = None,
                  root: Path | None = None, meta_root: Path | None = None) -> pd.DataFrame:
    """Attach PIT flags to a bar panel keyed by (stock_code, trade_date)."""
    keys = bars[['stock_code', 'trade_date']].copy()
    keys['stock_code'] = keys['stock_code'].map(normalize_code)
    keys['trade_date'] = _dates(keys['trade_date'])
    out = bars.copy()
    out['stock_code'] = out['stock_code'].map(normalize_code)
    out['trade_date'] = _dates(out['trade_date'])

    listing = listing_dates(meta_root)
    delist = delist_dates(root, meta_root)
    out = out.merge(listing, on='stock_code', how='left')
    out = out.merge(delist, on='stock_code', how='left')
    listed_after = out['list_date'].isna() | (out['trade_date'] >= out['list_date'])
    listed_before = out['delist_date'].isna() | (out['trade_date'] <= out['delist_date'])
    out['is_listed'] = (listed_after & listed_before).values

    st = st_panel(keys, meta_root)
    out['is_st'] = st['is_st'].values
    out['is_delist_risk'] = st['is_delist_risk'].values

    limits = limit_panel(keys, root, codes=out['stock_code'].unique())
    for column in ('raw_close', 'raw_open', 'up_limit', 'down_limit',
                   'close_at_up', 'close_at_down', 'open_at_up', 'open_at_down'):
        out[column] = limits[column].values

    susp = suspension_panel(keys, root)
    out['suspended_official'] = susp['suspended_official'].values

    if index_code:
        member = index_panel(keys, index_code, root)
        out['in_index'] = member['in_index'].values
        out['index_weight'] = member['index_weight'].values

    return out


# ------------------------------------------------------------------ coverage

def qfq_vintage_report(bars: pd.DataFrame, codes: Iterable[str] | None = None,
                       root: Path | None = None) -> pd.DataFrame:
    """Implied qfq normalization base per code/year: ``factor * raw_close / close``.

    A constant base means the local qfq series is consistent for that code;
    steps flag rebuild/append vintages (see module docstring).
    """
    raw = raw_daily(codes if codes is not None else bars['stock_code'].unique(), root)
    if not len(raw):
        return pd.DataFrame(columns=['stock_code', 'year', 'base_mean', 'base_std', 'rows'])
    frames = adj_factors(codes if codes is not None else bars['stock_code'].unique(), root)
    factors = frames[['stock_code', 'trade_date', 'adj_factor']] if len(frames) else None
    if factors is None or not len(factors):
        return pd.DataFrame(columns=['stock_code', 'year', 'base_mean', 'base_std', 'rows'])
    merged = (bars[['stock_code', 'trade_date', 'close']]
              .merge(raw[['stock_code', 'trade_date', 'close']]
                     .rename(columns={'close': 'raw_close'}),
                     on=['stock_code', 'trade_date'], how='inner')
              .merge(factors, on=['stock_code', 'trade_date'], how='inner'))
    merged = merged[merged['close'] > 0]
    merged['base'] = merged['adj_factor'] * merged['raw_close'] / merged['close']
    merged['year'] = merged['trade_date'].dt.year
    grouped = merged.groupby(['stock_code', 'year'])['base'].agg(['mean', 'std', 'size'])
    grouped.columns = ['base_mean', 'base_std', 'rows']
    return grouped.reset_index()


def coverage_report(root: Path | None = None, meta_root: Path | None = None) -> dict:
    base = root or PIT_ROOT
    coverage_path = base / 'coverage.json'
    coverage = json.loads(coverage_path.read_text()) if coverage_path.exists() else {}
    units = coverage.get('units', {})
    datasets: dict[str, dict] = {}
    for unit_id, payload in units.items():
        dataset = unit_id.split('/', 1)[0]
        entry = datasets.setdefault(dataset, {'units': 0, 'rows': 0, 'failures': 0})
        entry['units'] += 1
        entry['rows'] += int(payload.get('rows', 0) or 0)
    for unit_id in coverage.get('failures', {}):
        dataset = unit_id.split('/', 1)[0]
        datasets.setdefault(dataset, {'units': 0, 'rows': 0, 'failures': 0})
        datasets[dataset]['failures'] += 1

    code_dirs = sorted((base / 'code_data').glob('code=*')) if (base / 'code_data').exists() else []
    code_files: dict[str, int] = {}
    for code_dir in code_dirs:
        for path in code_dir.glob('*.parquet'):
            code_files[path.stem] = code_files.get(path.stem, 0) + 1

    delist = delist_dates(base, meta_root)
    st = st_intervals(meta_root)
    result = {
        'root': str(base),
        'units': len(units),
        'failures': len(coverage.get('failures', {})),
        'datasets': datasets,
        'code_data_files': code_files,
        'delist_codes': int(len(delist)),
        'delist_by_source': delist['source'].value_counts().to_dict() if len(delist) else {},
        'st_intervals': int(len(st)),
        'st_codes': int(st['stock_code'].nunique()) if len(st) else 0,
    }
    return result
