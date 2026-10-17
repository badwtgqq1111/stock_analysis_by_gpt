#!/usr/bin/env python3
"""P2.71 PIT data fetcher (resumable, coverage-tracked).

Downloads the point-in-time datasets the factor-mining program needs beyond
the local qfq bars:

* ``stock_basic_delisted``  delisting dates (official/relay stock_basic, list_status=D)
* ``index_weight``          historical index constituents for 000905.SH / 000300.SH
* ``suspend_d``             exchange suspension records (type S/R)
* code-scoped (research universe):
    - ``daily_raw``         raw (unadjusted) OHLC + pre_close for limit detection
    - ``stk_limit``         exchange up/down limit prices
    - ``adj_factor``        daily adjustment factors (skips codes already in
                            assets/data/raw/adjustment_factor_snapshots)

Cache layout (default root ``assets/data/raw/pit/market=CN``)::

    stock_basic_delisted/bulk.parquet
    index_weight/index_code=000905.SH/bulk.parquet
    suspend_d/year=2024/bulk.parquet
    code_data/code=000001.SZ/{daily_raw,stk_limit,adj_factor}.parquet
    universe.json
    coverage.json

Every unit is recorded in ``coverage.json`` (rows, fetched_at, gateway).  The
fetcher is resumable: existing non-empty units are skipped unless ``--refresh``
is passed.  Network failures on a unit are recorded and skipped so a later run
can continue; a non-zero exit code is returned if any unit failed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient  # noqa: E402
from data.store.scheduler_env import load_scheduler_env  # noqa: E402

PIT_ROOT = ROOT / 'assets/data/raw/pit/market=CN'
ADJ_SNAPSHOTS = ROOT / 'assets/data/raw/adjustment_factor_snapshots/market=CN/source=tushare_relay'
DELISTED_META = ROOT / 'assets/data/meta/cn_delisted_daily.parquet'
INDEX_CODES = ('000905.SH', '000300.SH')
INDEX_START = '2016-01-01'
CODE_START = '2013-01-01'
CODE_DATASETS = ('daily_raw', 'stk_limit', 'adj_factor')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _yyyymmdd(value: str) -> str:
    return pd.Timestamp(value).strftime('%Y%m%d')


def _load_coverage(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {'units': {}, 'failures': {}}


def _save_coverage(path: Path, coverage: dict) -> None:
    path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2, default=str) + '\n')


def _write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp.parquet')
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:  # custom --root outside the repo (tests/smoke)
        return str(path)


def _paged_range(client: CNTushareRelayClient, api: str, *, start: str, end: str,
                 params: dict, limit: int = 6000, sleep: float = 0.05,
                 max_calls: int = 400) -> tuple[pd.DataFrame, str]:
    """Fetch [start, end]; split a chunk when it returns >= limit rows."""
    frames: list[pd.DataFrame] = []
    gateways: set[str] = set()
    stack = [(_yyyymmdd(start), _yyyymmdd(end))]
    calls = 0
    while stack:
        s, e = stack.pop()
        frame, gateway = client.get(api, start_date=s, end_date=e, limit=limit, **params)
        calls += 1
        gateways.add(gateway)
        if calls > max_calls:
            raise RuntimeError(f'{api}: exceeded {max_calls} calls')
        if len(frame) >= limit and s < e:
            mid = pd.Timestamp(s) + (pd.Timestamp(e) - pd.Timestamp(s)) / 2
            stack.append((s, mid.strftime('%Y%m%d')))
            stack.append(((mid + pd.Timedelta(days=1)).strftime('%Y%m%d'), e))
            continue
        if len(frame):
            frames.append(frame)
        if sleep:
            time.sleep(sleep)
    if not frames:
        return pd.DataFrame(), ','.join(sorted(gateways)) or 'none'
    out = pd.concat(frames, ignore_index=True)
    keys = [key for key in ('ts_code', 'trade_date', 'index_code', 'con_code') if key in out.columns]
    if keys:
        out = out.drop_duplicates(keys, keep='last')
        if 'trade_date' in keys:
            out = out.sort_values(keys).reset_index(drop=True)
    return out, ','.join(sorted(gateways))


def _unit(client, coverage: dict, *, root: Path, dataset: str, key: str, path: Path,
          api: str, start: str, end: str, params: dict, refresh: bool,
          sleep: float) -> bool:
    unit_id = f'{dataset}/{key}'
    if path.exists() and not refresh:
        rows = int(coverage['units'].get(unit_id, {}).get('rows', -1))
        if rows != 0:
            print(f'skip {unit_id} (cached)', flush=True)
            return True
    try:
        frame, gateway = _paged_range(client, api, start=start, end=end,
                                      params=params, sleep=sleep)
        _write_parquet(frame, path)
        coverage['units'][unit_id] = {
            'rows': int(len(frame)), 'fetched_at': _now(), 'gateway': gateway,
            'api': api, 'start': start, 'end': end, 'path': _rel(path),
        }
        coverage['failures'].pop(unit_id, None)
        print(f'ok   {unit_id}: {len(frame)} rows ({gateway})', flush=True)
        return True
    except Exception as exc:  # record and continue; rerun later resumes here
        coverage['failures'][unit_id] = {
            'error': f'{type(exc).__name__}: {exc}'[:400], 'at': _now(),
        }
        print(f'FAIL {unit_id}: {type(exc).__name__}: {exc}', flush=True)
        return False


def _stock_codes() -> set[str]:
    """``cn_stock_basic`` membership (raw + T-normalized spellings)."""
    basic_path = ROOT / 'assets/data/meta/cn_stock_basic.parquet'
    if not basic_path.exists():
        return set()
    raw = {str(code) for code in
           pd.read_parquet(basic_path, columns=['ts_code'])['ts_code'].dropna()}
    normalized = {code[1:] if code.startswith('T') and code[1:2].isdigit() else code
                  for code in raw}
    return raw | normalized


def resolve_universe(root: Path, mode: str, n_panel: int, *, force: bool = False,
                     explicit: str | None = None) -> list[str]:
    universe_path = root / 'universe.json'
    if explicit:
        return [code.strip().upper() for code in explicit.split(',') if code.strip()]
    codes: set[str] = set()
    if universe_path.exists() and not force:
        codes.update(json.loads(universe_path.read_text())['codes'])
    else:
        if 'panel' in mode:
            from scripts.p2_71_alphaqcm_pilot import select_panel
            codes.update(select_panel(n_panel, with_fundamentals=False).stock_code.unique().tolist())
        if 'delisted' in mode:
            delisted = pd.read_parquet(DELISTED_META, columns=['ts_code'])
            codes.update(delisted.ts_code.dropna().unique().tolist())
            if ADJ_SNAPSHOTS.exists():
                snapshot_codes = {p.stem.replace('_', '.') for p in ADJ_SNAPSHOTS.glob('*.parquet')}
                codes.update(snapshot_codes)
    stocks = _stock_codes()
    if stocks:
        filtered = sorted(code for code in codes if code in stocks)
    else:
        filtered = sorted(codes)
    universe_path.write_text(json.dumps(
        {'created_at': _now(), 'mode': mode, 'n_panel': n_panel,
         'codes': filtered, 'count': len(filtered), 'stock_only': True,
         'dropped_non_stock': sorted(codes - set(filtered))},
        ensure_ascii=False, indent=2) + '\n')
    return filtered


def run(root: Path, *, mode: str, n_panel: int, refresh: bool, sleep: float,
        skip_adj_snapshot: bool, codes_arg: str | None = None,
        bulk_only: bool = False, skip_bulk: bool = False) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    coverage = _load_coverage(root / 'coverage.json')
    client = CNTushareRelayClient(timeout=40)
    ok_units = fail_units = 0

    def unit(**kwargs):
        nonlocal ok_units, fail_units
        good = _unit(client, coverage, root=root, refresh=refresh, sleep=sleep, **kwargs)
        ok_units += good
        fail_units += (not good)
        _save_coverage(root / 'coverage.json', coverage)

    # 1) delisted list with delist_date
    if not skip_bulk:
        unit(dataset='stock_basic_delisted', key='bulk',
             path=root / 'stock_basic_delisted' / 'bulk.parquet',
             api='stock_basic', start='1990-01-01', end='2026-12-31',
             params={'list_status': 'D',
                     'fields': 'ts_code,name,list_date,delist_date'})

        # 2) index weight history (quarter chunks via the pager)
        for index_code in INDEX_CODES:
            unit(dataset='index_weight', key=index_code,
                 path=root / 'index_weight' / f'index_code={index_code}' / 'bulk.parquet',
                 api='index_weight', start=INDEX_START, end='2026-12-31',
                 params={'index_code': index_code,
                         'fields': 'index_code,con_code,trade_date,weight'})

        # 3) suspensions year by year
        for year in range(2013, 2027):
            unit(dataset='suspend_d', key=str(year),
                 path=root / 'suspend_d' / f'year={year}' / 'bulk.parquet',
                 api='suspend_d', start=f'{year}-01-01', end=f'{year}-12-31',
                 params={'fields': 'ts_code,trade_date,suspend_type,suspend_timing'})

    # 4) code-scoped datasets for the research universe
    codes = [] if bulk_only else resolve_universe(root, mode, n_panel,
                                                  force=refresh, explicit=codes_arg)
    print(f'universe codes: {len(codes)}', flush=True)
    if not codes_arg:
        # The pre-2026-10-17 panel did not restrict to stocks, and the clean
        # OHLCV layer also carries index series, so the universe accidentally
        # included index codes (e.g. 000001.SH, which tushare daily rejects).
        # Purge their cached units so coverage reflects the stock universe.
        stocks = _stock_codes()
        junk_codes: set[str] = set()
        for unit_id in list(coverage['units']) + list(coverage['failures']):
            dataset, _, key = unit_id.partition('/')
            if dataset in CODE_DATASETS and key and stocks and key not in stocks:
                coverage['units'].pop(unit_id, None)
                coverage['failures'].pop(unit_id, None)
                junk_codes.add(key)
        for code in sorted(junk_codes):
            junk_dir = root / 'code_data' / f'code={code}'
            if junk_dir.exists():
                shutil.rmtree(junk_dir)
        if junk_codes:
            _save_coverage(root / 'coverage.json', coverage)
            print(f'purged {len(junk_codes)} non-stock codes from cache', flush=True)
    snapshot_codes = set()
    if skip_adj_snapshot and ADJ_SNAPSHOTS.exists():
        snapshot_codes = {p.stem.replace('_', '.') for p in ADJ_SNAPSHOTS.glob('*.parquet')}
    for code in codes:
        for dataset in CODE_DATASETS:
            if dataset == 'adj_factor' and code in snapshot_codes:
                continue
            api = {'daily_raw': 'daily', 'stk_limit': 'stk_limit',
                   'adj_factor': 'adj_factor'}[dataset]
            fields = {'daily_raw': 'ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount',
                      'stk_limit': 'ts_code,trade_date,up_limit,down_limit',
                      'adj_factor': 'ts_code,trade_date,adj_factor'}[dataset]
            unit(dataset=dataset, key=code,
                 path=root / 'code_data' / f'code={code}' / f'{dataset}.parquet',
                 api=api, start=CODE_START, end='2026-12-31',
                 params={'ts_code': code, 'fields': fields})

    summary = {
        'status': 'PIT_FETCH_COMPLETED' if fail_units == 0 else 'PIT_FETCH_PARTIAL',
        'updated_at': _now(), 'units_ok': ok_units, 'units_failed': fail_units,
        'coverage_units': len(coverage['units']),
        'failures': coverage['failures'],
    }
    _save_coverage(root / 'coverage.json', coverage)
    (root / 'fetch_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=PIT_ROOT)
    parser.add_argument('--universe', default='panel_top+delisted',
                        help="'panel_top+delisted' | 'delisted' | 'none'")
    parser.add_argument('--n-panel', type=int, default=300)
    parser.add_argument('--codes', default=None,
                        help='显式代码清单（逗号分隔），用于冒烟/补充抓取')
    parser.add_argument('--bulk-only', action='store_true',
                        help='只抓取全市章数据集（退市/指数成分/停牌），跳过代码级')
    parser.add_argument('--skip-bulk', action='store_true',
                        help='跳过全市章数据集，仅抓代码级（冒烟/补抓）')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--sleep', type=float, default=0.05)
    parser.add_argument('--skip-adj-snapshot', action='store_true', default=True)
    args = parser.parse_args()
    load_scheduler_env()
    summary = run(args.root.resolve(), mode=args.universe, n_panel=args.n_panel,
                  refresh=args.refresh, sleep=args.sleep,
                  skip_adj_snapshot=args.skip_adj_snapshot,
                  codes_arg=args.codes, bulk_only=args.bulk_only,
                  skip_bulk=args.skip_bulk)
    raise SystemExit(0 if summary['units_failed'] == 0 else 1)


if __name__ == '__main__':
    main()
