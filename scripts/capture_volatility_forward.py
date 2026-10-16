#!/usr/bin/env python
"""Immutable, observed-at forward log; historical imports never earn forward credit."""
from __future__ import annotations
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factor_engine.portfolio.volatility_promotion import PAIRS, evaluate_gate
from factor_engine.portfolio.paper_account import run_paper_account
from factor_engine.portfolio.execution_assumptions import load_assumptions

OUT = ROOT / 'output/research/volatility_shadow'


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def protocol_id():
    paths = ['config/execution_assumptions_cn.json', 'scripts/run_volatility_shadow.py',
             'factor_engine/portfolio/paper_account.py', 'factor_engine/portfolio/volatility.py',
             'factor_engine/portfolio/volatility_sizing.py', 'factor_engine/portfolio/volatility_promotion.py',
             'scripts/capture_volatility_forward.py']
    return digest({p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths})


def eligible_capture(decision_date, captured_at):
    observed = pd.Timestamp(captured_at)
    if observed.tzinfo is None:
        raise ValueError('capture time needs timezone')
    local = observed.tz_convert('Asia/Shanghai')
    # Conservative same-day close only. Next-morning imports are not forward signals.
    return str(pd.Timestamp(decision_date).date()) == str(local.date()) and local.hour >= 15


def write_capture(folder: Path, payload: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{payload['captured_at'].replace(':','').replace('+','_')}_{uuid.uuid4().hex[:8]}.json"
    envelope = {'sha256': digest(payload), 'payload': payload}
    with target.open('x', encoding='utf-8') as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2, allow_nan=False)
    return target


def read_captures(folder):
    result = []
    for path in sorted(folder.glob('*.json')):
        envelope = json.loads(path.read_text())
        if digest(envelope['payload']) != envelope['sha256']:
            raise ValueError(f'archive hash mismatch: {path.name}')
        result.append(envelope['payload'])
    return sorted(result, key=lambda r: r['captured_at'])


def evaluate_archive(captures, calendar, out):
    histories, prices = {}, {}
    for capture in captures:
        for row in capture['signals']:
            if row['eligible']:
                histories.setdefault(row['variant'], {}).setdefault(row['trade_date'], row['weights'])
        if capture['prices_eligible']:
            prices.setdefault(capture['observed_date'], capture['prices'])
    protocol_ok = all(c.get('protocol_id') == protocol_id() and c.get('execution_assumptions') == load_assumptions() for c in captures if any(s['eligible'] for s in c['signals']) or c['prices_eligible'])
    decisions = {v: len(rows) for v, rows in histories.items()}
    eligible_dates = sorted({d for rows in histories.values() for d in rows})
    gates, navs = {}, {}
    complete = False
    if eligible_dates and prices and protocol_ok:
        start, end = pd.Timestamp(eligible_dates[0]), pd.Timestamp(max(prices))
        expected = set(calendar[(calendar >= start) & (calendar <= end)].strftime('%Y-%m-%d'))
        complete = bool(expected) and expected <= set(prices)
        bars = pd.DataFrame([r for day, rows in prices.items() if pd.Timestamp(day) >= start for r in rows])
        for variant, dates in histories.items():
            signals = pd.DataFrame([{'trade_date': day, **r} for day, weights in dates.items() for r in weights])
            if len(bars) and len(signals):
                result = run_paper_account(signals, bars, initial_capital=45000, causal_execution=True)
                nav = result['nav']; navs[variant] = nav.set_index('asof_date').nav
                (out / 'accounts' / variant).mkdir(parents=True, exist_ok=True)
                for kind, frame in result.items():
                    frame.to_csv(out / 'accounts' / variant / f'{kind}.csv', index=False)
    # Gate stays closed until explicit independent provenance/PIT review.
    review = out / 'provenance_review.json'
    provenance_ok = False
    if review.exists():
        r = json.loads(review.read_text())
        provenance_ok = r.get('approved') is True and bool(r.get('reviewer')) and bool(r.get('evidence')) and r.get('protocol_id') == protocol_id()
    for candidate, baseline in PAIRS.items():
        gates[candidate] = evaluate_gate(navs.get(candidate, pd.Series(dtype=float, index=pd.DatetimeIndex([]))),
                                        navs.get(baseline, pd.Series(dtype=float, index=pd.DatetimeIndex([]))),
                                        decisions=len(set(histories.get(candidate, {})) & set(histories.get(baseline, {}))),
                                        provenance_ok=provenance_ok, coverage_ok=complete, protocol_ok=protocol_ok)
    return {'status': 'accumulating', 'eligible_decisions': len(eligible_dates), 'gates': gates,
            'archives': len(captures), 'protocol_id': protocol_id(), 'automatic_promotion': False,
            'note': 'Only same-day post-close captured signals/prices; earliest valid capture wins; no backfill credit. Missing market sessions block promotion. QFQ/PIT review is mandatory.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=OUT)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if args.refresh:
        subprocess.run([sys.executable, str(ROOT / 'scripts/run_volatility_shadow.py'), '--output', str(args.output)], check=True, timeout=240)
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    today = now.date().isoformat()
    folder = args.output / 'forward'
    captures = read_captures(folder / 'captures')
    hashes, signals, codes = {}, [], set()
    for candidate in set(PAIRS) | set(PAIRS.values()) | {'arm_select_histvol'}:
        p = args.output / f'{candidate}_latest.csv'
        if not p.exists():
            continue
        hashes[str(p.relative_to(args.output))] = hashlib.sha256(p.read_bytes()).hexdigest()
        frame = pd.read_csv(p)
        dates = pd.to_datetime(frame.trade_date).dt.strftime('%Y-%m-%d').unique()
        if len(dates) != 1 or frame.stock_code.duplicated().any():
            raise ValueError('ambiguous signal snapshot')
        rows = json.loads(frame[['stock_code', 'target_weight']].to_json(orient='records'))
        weights = pd.to_numeric(frame.target_weight, errors='raise')
        if weights.isna().any() or not weights.between(0, 1).all() or weights.sum() > 1.000001:
            raise ValueError('invalid long-only archived weights')
        codes.update(frame.stock_code)
        signals.append({'variant': candidate, 'trade_date': dates[0], 'weights': rows,
                        'eligible': eligible_capture(dates[0], now)})
    for capture in captures:
        for signal in capture['signals']:
            if signal['eligible']:
                codes.update(r['stock_code'] for r in signal['weights'])
    files = [str(p) for p in (ROOT / 'assets/data/clean/ohlcv/market=CN').glob(
        f'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={now.year}/*.parquet')]
    bars = ds.dataset(files, format='parquet').to_table(columns=['stock_code','trade_date','open','close'],
                        filter=ds.field('stock_code').isin(sorted(codes))).to_pandas()
    bars['trade_date'] = pd.to_datetime(bars.trade_date).dt.strftime('%Y-%m-%d')
    current = bars[bars.trade_date == today]
    if current.duplicated(['trade_date','stock_code']).any():
        raise ValueError('duplicate observed bar')
    # Partial/stale data cannot silently become a valid prospective decision.
    all_ready = bool(codes) and set(current.stock_code) == codes and not current[['open','close']].isna().any().any()
    audits = pd.read_csv(args.output / 'risk_audit.csv')
    for signal in signals:
        a = audits[(audits.variant == signal['variant']) & (pd.to_datetime(audits.trade_date).dt.strftime('%Y-%m-%d') == signal['trade_date'])]
        signal['eligible'] = signal['eligible'] and all_ready and len(a) == 1 and a.iloc[0].status == 'ready'
    for name in ('risk_audit.csv','methodology.json'):
        hashes[name] = hashlib.sha256((args.output / name).read_bytes()).hexdigest()
    for path in [ROOT / 'config/execution_assumptions_cn.json', Path(__file__), ROOT / 'scripts/run_volatility_shadow.py', ROOT / 'factor_engine/portfolio/paper_account.py']:
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {'captured_at': now.isoformat(), 'observed_date': today,
               'signals': signals, 'prices': json.loads(current.to_json(orient='records')),
               'prices_eligible': bool(all_ready and now.hour >= 15), 'input_sha256': hashes,
               'execution_assumptions': load_assumptions(), 'protocol_id': protocol_id()}
    path = write_capture(folder / 'captures', payload)
    bench = pd.read_parquet(ROOT / 'assets/data/meta/cn_benchmark_daily.parquet')
    calendar = pd.DatetimeIndex(pd.to_datetime(bench[bench.ts_code == '000905.SH'].trade_date)).sort_values()
    result = evaluate_archive(read_captures(folder / 'captures'), calendar, folder)
    result['stale_signal_dates'] = {s['variant']: s['trade_date'] for s in signals if not s['eligible']}
    result.update(captured_at=now.isoformat(), latest_capture=str(path.relative_to(ROOT)))
    temp = folder / f'.latest.{uuid.uuid4().hex}.json'
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)); temp.replace(folder / 'latest.json')
    print(f"forward: archives={result['archives']}; eligible_decisions={result['eligible_decisions']}; status={result['status']}; automatic_promotion=False")
    for name, gate in result['gates'].items():
        print(f"{name}: {gate['status']}; returns={gate['observations']}; decisions={gate['decisions']}")
    print(f'capture={path}')


if __name__ == '__main__':
    main()
