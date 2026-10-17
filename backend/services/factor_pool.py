"""Independent immutable factor revisions. SQLite transactions + optimistic concurrency.

Manual edits never write generated legacy factors.json. Strategies pin revisions,
not mutable head. Restore/optimize/replace always append revisions with lineage.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.services.factor_dsl import validate

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / 'config/factor_pool_seed.json'
DB = ROOT / 'output/factor_pool_v2/factors.sqlite3'


class Conflict(ValueError):
    pass


def checked(data: dict) -> dict:
    name, desc = str(data.get('name', '')).strip(), str(data.get('description', '')).strip()
    if not name or len(name) > 120 or not desc or len(desc) > 8000:
        raise ValueError('名称和金融解释必填(最长120/8000字)')
    sources = data.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
        raise ValueError('至少填写一项出处')
    for s in sources:
        if not isinstance(s, dict) or not s.get('title') or not s.get('locator'):
            raise ValueError('出处需要title与locator(页码/公式/代码位置)')
        if s.get('url') and not str(s['url']).startswith(('https://', 'http://')):
            raise ValueError('出处URL须为http/https')
    formula = str(data.get('formula', '')).strip()
    language = str(data.get('expression_language', 'p2_71_dsl'))
    if language == 'p2_71_dsl':
        semantics = validate(formula)
    elif language == 'alphagen_v1':
        from backend.services.alphagen_native import validate_native
        semantics = validate_native(formula)
    else:
        raise ValueError(f'未知表达式语言: {language}')
    note = str(data.get('change_note', '')).strip()
    if not note or len(note) > 2000:
        raise ValueError('修改说明必填(最长2000字)')
    action = data.get('action', 'edit')
    if action not in ('seed', 'create', 'edit', 'replace', 'optimize', 'restore'):
        raise ValueError('未知修改类型')
    return {'name': name, 'description': desc, 'formula': formula,
            'expression_language': language, 'sources': sources,
            'change_note': note, 'action': action, 'semantics': semantics,
            'evaluation_status': 'unmeasured'}


class FactorStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get('FACTOR_POOL_DB', str(DB)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS revisions(
              factor_id TEXT NOT NULL, version INTEGER NOT NULL,
              content TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
              parent_factor_id TEXT, parent_version INTEGER,
              PRIMARY KEY(factor_id,version));
            CREATE TABLE IF NOT EXISTS heads(factor_id TEXT PRIMARY KEY, version INTEGER NOT NULL);
            ''')
        # Serialize seeding across workers; no partially seeded pool.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for seed in json.loads(SEED.read_text())['factors']:
                if not db.execute('SELECT 1 FROM heads WHERE factor_id=?', (seed['factor_id'],)).fetchone():
                    self._insert(db, seed['factor_id'], 1, checked(seed), None)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _insert(db, fid, version, content, parent):
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?)',
                   (fid, version, raw, digest, datetime.now(timezone.utc).isoformat(),
                    parent['factor_id'] if parent else None, parent['version'] if parent else None))
        db.execute('INSERT INTO heads VALUES(?,?) ON CONFLICT(factor_id) DO UPDATE SET version=excluded.version', (fid, version))

    @staticmethod
    def unpack(row):
        if row is None:
            raise KeyError('因子/版本不存在')
        data = dict(row)
        return {**json.loads(data.pop('content')), **data}

    def get(self, fid: str, version: int | None = None):
        with self.connect() as db:
            if version is None:
                row = db.execute('SELECT r.* FROM revisions r JOIN heads h USING(factor_id,version) WHERE factor_id=?', (fid,)).fetchone()
            else:
                row = db.execute('SELECT * FROM revisions WHERE factor_id=? AND version=?', (fid, version)).fetchone()
        return self.unpack(row)

    def list(self):
        with self.connect() as db:
            return [self.unpack(r) for r in db.execute('SELECT r.* FROM revisions r JOIN heads h USING(factor_id,version) ORDER BY factor_id')]

    def history(self, fid):
        self.get(fid)
        with self.connect() as db:
            return [self.unpack(r) for r in db.execute('SELECT * FROM revisions WHERE factor_id=? ORDER BY version DESC', (fid,))]

    def save(self, fid, data, expected_version: int, parent: dict | None = None):
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,79}', fid):
            raise ValueError('factor_id只允许2至80位小写字母数字下划线，字母开头')
        content = checked(data)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT version FROM heads WHERE factor_id=?', (fid,)).fetchone()
            current = row['version'] if row else 0
            if current != expected_version:
                raise Conflict(f'版本冲突：期望{expected_version}，当前{current}，请刷新后比较')
            if current:
                parent = {'factor_id': fid, 'version': current}
            if parent and not db.execute('SELECT 1 FROM revisions WHERE factor_id=? AND version=?', (parent['factor_id'], parent['version'])).fetchone():
                raise ValueError('父因子版本不存在')
            self._insert(db, fid, current + 1, content, parent)
        return self.get(fid, current + 1)


def families():
    return json.loads((ROOT / 'config/factor_strategy_families.json').read_text())['families']


def annotate_strategies(payload: dict) -> dict:
    known = {v['id']: {**v, 'family_id': f['id']} for f in families() for v in f['variants']}
    return {**payload, 'factor_families': families(), 'strategies': [
        {**s, 'factor_design': known.get(s['id'])} for s in payload.get('strategies', [])]}
