"""P2.71 editable factor pool, independent of generated legacy factor library."""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import pandas as pd

from backend.services.factor_pool import FactorStore, Conflict, families
from backend.services.factor_dsl import validate, evaluate, panel, VARIABLES, OPERATORS

router = APIRouter(prefix='/api/factor-pool', tags=['factor-pool'])
ROOT = Path(__file__).resolve().parents[2]


class Source(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    locator: str = Field(min_length=1, max_length=1000)
    url: str = Field(default='', max_length=2000)
    authors: str = Field(default='', max_length=500)
    date: str = Field(default='', max_length=100)


class Parent(BaseModel):
    factor_id: str
    version: int = Field(ge=1)


class Save(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=8000)
    formula: str = Field(min_length=1, max_length=4000)
    sources: list[Source] = Field(min_length=1, max_length=12)
    change_note: str = Field(min_length=1, max_length=2000)
    action: str = 'edit'
    expression_language: str = 'p2_71_dsl'
    expected_version: int = Field(ge=0)
    parent: Parent | None = None


class Preview(BaseModel):
    formula: str = Field(min_length=1, max_length=4000)
    expression_language: str = 'p2_71_dsl'
    records: list[dict] = Field(default_factory=list, max_length=5000)


class ImportAlphaQCM(BaseModel):
    run_dir: str = Field(min_length=1, max_length=1000)
    update: bool = False


def call(fn):
    try:
        return fn()
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('')
def index():
    return {'factors': FactorStore().list(), 'families': families(), 'variables': VARIABLES, 'operators': OPERATORS}


@router.post('/validate')
def validate_formula(req: Preview):
    if req.expression_language == 'alphagen_v1':
        from backend.services.alphagen_native import validate_native
        return call(lambda: validate_native(req.formula))
    return call(lambda: validate(req.formula))


@router.post('/preview')
def preview(req: Preview):
    def run():
        if req.expression_language == 'alphagen_v1':
            from backend.services.alphagen_native import evaluate_native, validate_native
            info = validate_native(req.formula)
            frame = panel(pd.DataFrame(req.records))
            values = evaluate_native(req.formula, frame)
            rows = frame[['stock_code', 'trade_date']].copy()
            rows = rows.merge(values.rename('value').reset_index(),
                              on=['trade_date', 'stock_code'], how='left')
            rows['trade_date'] = rows['trade_date'].dt.strftime('%Y-%m-%d')
            rows['value'] = rows['value'].astype(object).where(rows['value'].notna(), None)
            return {'semantics': info, 'rows': rows.to_dict('records'),
                    'evaluation_status': 'preview_only'}
        info = validate(req.formula)
        frame = panel(pd.DataFrame(req.records))
        values = evaluate(req.formula, frame)
        rows = frame[['stock_code', 'trade_date']].copy()
        rows['trade_date'] = rows['trade_date'].dt.strftime('%Y-%m-%d')
        rows['value'] = values.astype(object).where(values.notna(), None)
        return {'semantics': info, 'rows': rows.to_dict('records'), 'evaluation_status': 'preview_only'}
    return call(run)


@router.get('/alphaqcm/performance')
def alphaqcm_performance():
    """Latest frozen AlphaQCM portfolio curve vs CSI500."""
    root = ROOT / 'output/research/p2_71/alphaqcm_portfolio_v1'
    nav_path, metrics_path = root / 'nav.csv', root / 'metrics.json'
    if not nav_path.exists() or not metrics_path.exists():
        return {'available': False,
                'reason': '还没有AlphaQCM组合回测产物；先运行 scripts/p2_71_alphaqcm_backtest.py'}
    frame = pd.read_csv(nav_path, parse_dates=['trade_date'])
    payload = json.loads(metrics_path.read_text())
    return {
        'available': True,
        'kind': 'alphaqcm_frozen_portfolio_vs_csi500',
        'dates': frame.trade_date.dt.strftime('%Y-%m-%d').tolist(),
        'nav': pd.to_numeric(frame.portfolio_nav, errors='coerce').tolist(),
        'benchmark_nav': pd.to_numeric(frame.benchmark_nav, errors='coerce').tolist(),
        'metrics': payload.get('portfolio'),
        'benchmark_metrics': payload.get('csi500'),
        'excess_return': payload.get('excess_return'),
        'observations': payload.get('observations'),
        'factor_count': payload.get('factor_count'),
        'start': payload.get('start'), 'end': payload.get('end'),
        'config': payload.get('config'),
        'run_dir': payload.get('run_dir'),
        'reason': '冻结AlphaQCM表达式的长仓研究回测；非全市场PIT、非生产或前向影子。',
    }


@router.post('/alphaqcm/import')
def import_alphaqcm(req: ImportAlphaQCM):
    """Import a frozen AlphaQCM run into the editable factor pool."""
    root = (ROOT / 'output/research/p2_71').resolve()
    run = Path(req.run_dir).resolve()
    if root not in run.parents and run != root:
        raise HTTPException(status_code=400, detail='只允许导入 output/research/p2_71 下的run')
    if not (run / 'factors.json').exists() and not (run.is_file() and run.name == 'factors.json'):
        raise HTTPException(status_code=404, detail='找不到 factors.json')
    from scripts.p2_71_import_alphaqcm_factors import import_run
    return call(lambda: import_run(run, update=req.update))


@router.get('/{factor_id}/history')
def history(factor_id: str):
    return call(lambda: {'versions': FactorStore().history(factor_id)})


@router.get('/{factor_id}')
def detail(factor_id: str, version: int | None = None):
    return call(lambda: FactorStore().get(factor_id, version))


@router.post('/{factor_id}/versions')
def save(factor_id: str, req: Save):
    return call(lambda: FactorStore().save(factor_id, req.model_dump(exclude={'parent', 'expected_version'}),
                                         req.expected_version, req.parent.model_dump() if req.parent else None))
