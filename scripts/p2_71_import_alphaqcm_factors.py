#!/usr/bin/env python3
"""Import a frozen AlphaQCM factors.json into the editable factor pool.

Native AlphaGen expressions are stored with ``expression_language=
'alphagen_v1'``; the front-end factor pool uses the native validator/evaluator
instead of pretending they are P2.71 DSL formulas.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.factor_pool import FactorStore  # noqa: E402


def _slug(value: str, limit: int = 32) -> str:
    value = re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_')
    return value[:limit] or 'run'


def _description(factor: dict, run_id: str, test_rank_ic: float | None) -> str:
    parts = [
        f'AlphaQCM原生表达式（AlphaGen v1），来自研究run `{run_id}`。',
        f'表达式：{factor["expression"]}',
        f'valid IC：{factor.get("valid_ic")}；权重：{factor.get("weight")}。',
    ]
    if test_rank_ic is not None:
        parts.append(f'冻结后test RankIC：{test_rank_ic:.6f}（仅供参考，不构成晋级）。')
    parts.append('财务/PIT口径见对应run的manifest；导入不改变原run artifact hash。')
    return '\n'.join(parts)


def import_run(run: Path, *, db: Path | None = None, update: bool = False,
               prefix: str = 'aqcm') -> dict:
    if run.is_dir():
        factors_path = run / 'factors.json'
        metrics_path = run / 'test_eval' / 'metrics.json'
    else:
        factors_path = run
        metrics_path = run.parent / 'test_eval' / 'metrics.json'
    if not factors_path.exists():
        raise FileNotFoundError(factors_path)
    doc = json.loads(factors_path.read_text())
    run_id = str(doc.get('run_id') or factors_path.parent.name)
    test_rank_ic = None
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        test_rank_ic = (metrics.get('ensemble') or {}).get('spearman', {}).get('mean')
    store = FactorStore(db)
    imported, skipped = [], []
    for i, factor in enumerate(doc.get('factors', [])):
        fid = f'{prefix}_{_slug(run_id, 28)}_{i:02d}'
        if len(fid) > 80:
            fid = fid[:80]
        data = {
            'name': f'AlphaQCM {run_id} #{i:02d}',
            'description': _description(factor, run_id, test_rank_ic),
            'formula': factor['expression'],
            'expression_language': 'alphagen_v1',
            'sources': [{
                'title': f'AlphaQCM run {run_id}',
                'locator': f'factors.json: factors[{i}] / {factor.get("name", "")}',
                'url': 'https://github.com/ZhuZhouFan/AlphaQCM',
                'authors': 'AlphaQCM / local P2.71 adapter',
                'date': '',
            }],
            'change_note': f'import frozen AlphaQCM factor from {factors_path}',
            'action': 'create',
        }
        try:
            current = store.get(fid)
        except KeyError:
            current = None
        if current and not update:
            skipped.append(fid)
            continue
        saved = store.save(fid, data, current['version'] if current else 0,
                           {'factor_id': fid, 'version': current['version']} if current else None)
        imported.append({'factor_id': fid, 'version': saved['version'],
                         'formula': saved['formula'],
                         'expression_language': saved['expression_language']})
    result = {'status': 'IMPORTED', 'run_id': run_id, 'db': str(store.path),
              'imported': imported, 'skipped': skipped,
              'test_rank_ic': test_rank_ic}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True,
                        help='run directory or factors.json')
    parser.add_argument('--db', type=Path, default=None,
                        help='factor pool SQLite path; default output/factor_pool_v2/factors.sqlite3')
    parser.add_argument('--update', action='store_true',
                        help='append a new version if the factor ID already exists')
    parser.add_argument('--prefix', default='aqcm')
    args = parser.parse_args()
    import_run(args.run.resolve(), db=args.db.resolve() if args.db else None,
               update=args.update, prefix=args.prefix)


if __name__ == '__main__':
    main()
