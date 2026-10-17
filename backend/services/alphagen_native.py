"""Safe native AlphaGen expression validation and pandas evaluation.

AlphaQCM exports expressions in the AlphaGen grammar (``Mean($close,10)``,
``Corr($close,$volume,20)``, ...).  The front-end factor pool needs to list,
validate, edit and preview those expressions without pretending they are the
smaller P2.71 DSL.  This module is the bridge:

* parsing is delegated to the vendored safe AST parser;
* validation returns the same variable/operator/parameter shape as the
  front-end factor pool;
* evaluation is a pandas implementation of the vendored operators, with
  positive rolling windows only (no future ``Ref`` in a factor).
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from backend.services.alphagen_expression import parse_expression
from backend.services.alphaqcm_data import prepare_vendor_path
from backend.services.factor_dsl import VARIABLES
from backend.services.fundamental_data import NATIVE_FEATURE_METADATA

PRICE_FEATURES = {
    'open': {'label': '复权开盘价', 'unit': '元/股(qfq)', 'window': 't',
             'availability': 't开盘后', 'definition': 'AlphaGen开盘特征'},
    'high': {'label': '复权最高价', 'unit': '元/股(qfq)', 'window': 't',
             'availability': 't收盘后', 'definition': 'AlphaGen最高价特征'},
    'low': {'label': '复权最低价', 'unit': '元/股(qfq)', 'window': 't',
            'availability': 't收盘后', 'definition': 'AlphaGen最低价特征'},
    'close': {'label': '复权收盘价', 'unit': '元/股(qfq)', 'window': 't',
              'availability': 't收盘后', 'definition': 'AlphaGen收盘价特征'},
    'volume': {'label': '成交量', 'unit': '原始日线单位', 'window': 't',
               'availability': 't收盘后', 'definition': 'AlphaGen成交量特征'},
    'vwap': {'label': 'VWAP', 'unit': '元/股(qfq代理)', 'window': 't',
             'availability': 't收盘后', 'definition': 'OHLC3代理VWAP'},
}
FEATURE_METADATA = {**PRICE_FEATURES, **NATIVE_FEATURE_METADATA}

ROLLING = {'Ref', 'Mean', 'Sum', 'Std', 'Var', 'Max', 'Min', 'Med', 'Mad',
           'Rank', 'Delta', 'WMA', 'EMA'}
PAIR = {'Cov', 'Corr'}
BINARY = {'Add', 'Sub', 'Mul', 'Div', 'Pow', 'Greater', 'Less'}
UNARY = {'Abs', 'Log', 'Sign'}
OPERATOR_DESCRIPTIONS = {
    'Add': '逐元素加法', 'Sub': '逐元素减法', 'Mul': '逐元素乘法',
    'Div': '逐元素除法；零分母转缺失', 'Pow': '逐元素幂',
    'Greater': '逐元素取较大值（AlphaGen Greater=max）',
    'Less': '逐元素取较小值（AlphaGen Less=min）',
    'Abs': '绝对值', 'Log': '自然对数；非正数转缺失', 'Sign': '符号',
    'Ref': '按股票向后滞后n个交易日', 'Mean': '按股票滚动均值',
    'Sum': '按股票滚动求和', 'Std': '按股票滚动样本标准差',
    'Var': '按股票滚动样本方差', 'Max': '滚动最大值', 'Min': '滚动最小值',
    'Med': '滚动中位数', 'Mad': '滚动平均绝对偏差',
    'Rank': '滚动窗口内最后一个值的时序百分位秩',
    'Delta': '当前值减n日前值', 'WMA': '线性加权滚动均值',
    'EMA': '指数加权滚动均值', 'Cov': '滚动样本协方差',
    'Corr': '滚动相关系数',
}


def _feature_name(node) -> str:
    return node._feature.name.lower()


def _constant_value(node) -> float:
    return float(getattr(node, '_value', getattr(node, 'value', 0.0)))


def validate_native(formula: str) -> dict:
    """Validate a native AlphaGen expression and return front-end semantics."""
    prepare_vendor_path()
    expr = parse_expression(formula)
    variables: set[str] = set()
    operators: set[str] = set()
    parameters: list[dict] = []

    def walk(node):
        name = type(node).__name__
        if name == 'Feature':
            feature = _feature_name(node)
            if feature not in FEATURE_METADATA:
                raise ValueError(f'未知特征: {feature}')
            variables.add(feature)
        elif name == 'Constant':
            value = _constant_value(node)
            if not np.isfinite(value) or abs(value) > 1e12:
                raise ValueError('常量为非有限数或过大')
            parameters.append({'value': value, 'role': 'AlphaGen常量'})
        elif name in BINARY:
            operators.add(name)
            walk(node._lhs)
            walk(node._rhs)
        elif name in UNARY:
            operators.add(name)
            walk(node._operand)
        elif name in ROLLING:
            dt = int(node._delta_time)
            if dt <= 0:
                raise ValueError('因子表达式不允许非正窗口或未来Ref')
            operators.add(name)
            parameters.append({'value': dt, 'role': f'{name}滚动窗口（交易日）'})
            walk(node._operand)
        elif name in PAIR:
            dt = int(node._delta_time)
            if dt <= 0:
                raise ValueError('pair rolling窗口必须为正')
            operators.add(name)
            parameters.append({'value': dt, 'role': f'{name}滚动窗口（交易日）'})
            walk(node._lhs)
            walk(node._rhs)
        else:
            raise ValueError(f'未准入的AlphaGen节点: {name}')

    walk(expr)
    if not variables:
        raise ValueError('因子至少引用一个特征')
    return {
        'dsl_version': 'alphagen_v1',
        'result_type': 'number',
        'variables': [{'name': k, **FEATURE_METADATA[k]} for k in sorted(variables)],
        'operators': [{'name': k, 'description': OPERATOR_DESCRIPTIONS.get(k, k)}
                      for k in sorted(operators)],
        'parameters': parameters,
    }


def _broadcast(value, index, name='value') -> pd.Series:
    if isinstance(value, pd.Series):
        return value.reindex(index)
    return pd.Series(float(value), index=index, name=name)


def _rolling(series: pd.Series, codes: pd.Series, n: int, kind: str) -> pd.Series:
    def apply(s: pd.Series):
        if kind == 'Std':
            return s.rolling(n, min_periods=n).std(ddof=1)
        if kind == 'Var':
            return s.rolling(n, min_periods=n).var(ddof=1)
        if kind == 'Med':
            return s.rolling(n, min_periods=n).median()
        if kind == 'Mad':
            return s.rolling(n, min_periods=n).apply(
                lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
        if kind == 'Rank':
            return s.rolling(n, min_periods=n).apply(_rank_last, raw=True)
        if kind == 'WMA':
            return s.rolling(n, min_periods=n).apply(
                lambda x: float(np.dot(x, np.arange(1, n + 1) / np.arange(1, n + 1).sum())),
                raw=True)
        if kind == 'EMA':
            alpha = 1 - 2 / (1 + n)
            weights = alpha ** np.arange(n, 0, -1)
            weights = weights / weights.sum()
            return s.rolling(n, min_periods=n).apply(
                lambda x: float(np.dot(x, weights)), raw=True)
        return getattr(s.rolling(n, min_periods=n), kind.lower())()

    return series.groupby(codes, sort=False, group_keys=False).transform(apply)


def _rank_last(x: np.ndarray) -> float:
    if np.isnan(x).any():
        return np.nan
    n = len(x)
    last = x[-1]
    left = int((last < x).sum())
    right = int((last <= x).sum())
    return (right + left + (right > left)) / (2 * n)


def _pair_rolling(lhs: pd.Series, rhs: pd.Series, codes: pd.Series, n: int,
                  kind: str) -> pd.Series:
    out = pd.Series(np.nan, index=lhs.index, dtype=float)
    for _, idx in codes.groupby(codes, sort=False).groups.items():
        x = lhs.loc[idx]
        y = rhs.loc[idx]
        if kind == 'Cov':
            out.loc[idx] = x.rolling(n, min_periods=n).cov(y).to_numpy()
        else:
            out.loc[idx] = x.rolling(n, min_periods=n).corr(y).to_numpy()
    return out


def evaluate_native(formula: str, panel: pd.DataFrame) -> pd.Series:
    """Evaluate a native expression on a long stock/date panel."""
    prepare_vendor_path()
    expr = parse_expression(formula)
    frame = panel.copy()
    frame['stock_code'] = frame.stock_code.astype(str)
    frame['trade_date'] = pd.to_datetime(frame.trade_date).dt.normalize()
    if frame.duplicated(['stock_code', 'trade_date']).any():
        raise ValueError('面板键重复')
    frame = frame.sort_values(['stock_code', 'trade_date'], kind='stable').reset_index(drop=True)
    codes = frame['stock_code']

    def ev(node):
        name = type(node).__name__
        if name == 'Feature':
            field = _feature_name(node)
            if field not in frame.columns:
                raise ValueError(f'面板缺特征: {field}')
            return pd.to_numeric(frame[field], errors='coerce').replace([np.inf, -np.inf], np.nan)
        if name == 'Constant':
            return _constant_value(node)
        if name in UNARY:
            x = _broadcast(ev(node._operand), frame.index)
            if name == 'Abs':
                return x.abs()
            if name == 'Log':
                out = np.log(x.where(x > 0))
                return pd.Series(out, index=frame.index).replace([np.inf, -np.inf], np.nan)
            return np.sign(x)
        if name in BINARY:
            left = _broadcast(ev(node._lhs), frame.index)
            right = _broadcast(ev(node._rhs), frame.index)
            if name == 'Add':
                out = left + right
            elif name == 'Sub':
                out = left - right
            elif name == 'Mul':
                out = left * right
            elif name == 'Div':
                out = left / right.replace(0, np.nan)
            elif name == 'Pow':
                out = np.power(left, right)
            elif name == 'Greater':
                out = np.maximum(left, right)
            else:
                out = np.minimum(left, right)
            return pd.Series(out, index=frame.index).replace([np.inf, -np.inf], np.nan)
        if name in ROLLING:
            x = _broadcast(ev(node._operand), frame.index)
            n = int(node._delta_time)
            if name == 'Ref':
                return x.groupby(codes, sort=False, group_keys=False).shift(n)
            if name == 'Delta':
                return x.groupby(codes, sort=False, group_keys=False).diff(n)
            return _rolling(x, codes, n, name)
        if name in PAIR:
            lhs = _broadcast(ev(node._lhs), frame.index)
            rhs = _broadcast(ev(node._rhs), frame.index)
            return _pair_rolling(lhs, rhs, codes, int(node._delta_time), name)
        raise ValueError(f'未准入的AlphaGen节点: {name}')

    result = ev(expr)
    if not isinstance(result, pd.Series):
        result = pd.Series(float(result), index=frame.index)
    result.index = pd.MultiIndex.from_frame(frame[['trade_date', 'stock_code']])
    result.index.names = ['trade_date', 'stock_code']
    return result.replace([np.inf, -np.inf], np.nan)
