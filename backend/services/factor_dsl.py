"""P2.71 causal panel DSL. AST interpreter only; no Python eval or legacy model path."""
from __future__ import annotations

import ast
import operator

import numpy as np
import pandas as pd

VARIABLES = {
    'vwap': {'label': 'VWAP', 'unit': '元/股(qfq代理)', 'window': 't', 'availability': 't收盘后', 'definition': 'OHLC3代理VWAP'},
    'close': {'label': '当日复权收盘价', 'unit': '元/股(qfq)', 'window': 't', 'availability': 't收盘后', 'definition': '同一复权版本的日线close；仅用于下一交易时点决策'},
    'open': {'label': '当日复权开盘价', 'unit': '元/股(qfq)', 'window': 't', 'availability': 't开盘后', 'definition': '日线open，不能用于当日开盘前信号'},
    'high': {'label': '当日最高价', 'unit': '元/股(qfq)', 'window': 't', 'availability': 't收盘后', 'definition': '完整当日日线high'},
    'low': {'label': '当日最低价', 'unit': '元/股(qfq)', 'window': 't', 'availability': 't收盘后', 'definition': '完整当日日线low'},
    'volume': {'label': '成交量', 'unit': '原始日线单位，导入前核查', 'window': 't', 'availability': 't收盘后', 'definition': '保留原始volume；未认证股/手换算，不据此构造VWAP'},
    'amount': {'label': '成交额', 'unit': '元(沿用基准日线契约，待上游单位审计)', 'window': 't', 'availability': 't收盘后', 'definition': '日线amount；等价版阈值沿用20,000,000，单位审计前不称执行验证完成'},
    'moneyflow_net_z_5d': {'label': '主力净流入5日强度', 'unit': '无量纲', 'window': '上游5日特征', 'availability': '数据源发布后且不早于t收盘', 'definition': '显式source=moneyflow；上游z=(当日net_mf_amount-向后5观测均值)/样本标准差(ddof=1,min_periods=2,含当日)。完整5点理论界约±4/√5，非硬截断，也非5日净额之和的z；沿用总净额字段而非仅大单。缺失/源发布时点须审计'},
    'moneyflow_positive_ratio_10d': {'label': '10日净流入正值占比', 'unit': '比例[0,1]', 'window': '上游10日特征', 'availability': '数据源发布后且不早于t收盘', 'definition': 'source=moneyflow；上游(net_mf_amount>0)的向后10观测均值(min_periods=1,含当日)。上游缺失比较为false，因此可能计作非正流入；本层缺失不填0，不能推断实时可用'},
    'roe': {'label': '净资产收益率', 'unit': '%', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：只使用ann_date<=t的最新公告值'},
    'roa': {'label': '总资产收益率', 'unit': '%', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：只使用ann_date<=t的最新公告值'},
    'grossprofit_margin': {'label': '毛利率', 'unit': '%', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：毛利率'},
    'netprofit_margin': {'label': '净利率', 'unit': '%', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：净利率'},
    'debt_to_assets': {'label': '资产负债率', 'unit': '%', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：负债/资产'},
    'netprofit_yoy': {'label': '净利润同比', 'unit': '%', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：净利润同比增速'},
    'or_yoy': {'label': '营收同比', 'unit': '%', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：营业收入同比增速'},
    'ocfps': {'label': '每股经营现金流', 'unit': '元/股', 'window': '最新已公告报告期', 'availability': 'ann_date收盘后', 'definition': 'PIT：每股经营现金流'},
    'pe_ind_pct': {'label': 'PE行业分位', 'unit': '分位[0,1]', 'window': '当日', 'availability': 't收盘后', 'definition': '材料化日频行业相对PE分位'},
    'pb_ind_pct': {'label': 'PB行业分位', 'unit': '分位[0,1]', 'window': '当日', 'availability': 't收盘后', 'definition': '材料化日频行业相对PB分位'},
    'size_ind_pct': {'label': '规模行业分位', 'unit': '分位[0,1]', 'window': '当日', 'availability': 't收盘后', 'definition': '材料化日频行业相对规模分位'},
}
OPERATORS = {
    '+': '逐元素加法', '-': '逐元素减法或取负', '*': '逐元素乘法', '/': '逐元素除法；零分母/无穷转缺失',
    '>': '严格大于', '>=': '大于等于', '<': '严格小于', '<=': '小于等于', '==': '等于', '!=': '不等于',
    'and': '所有条件成立；缺失比较为假', 'or': '至少一个条件成立；缺失比较为假',
    'mean': '每股向后n条观测滚动均值，含当日；满n条才输出',
    'delay': '每股向后n条观测滞后；n必须为1至252整数',
    'rank': '同日有效股票百分位排名(升序，平均并列)；非时序rank',
    'abs': '逐元素绝对值',
}
BIN = {ast.Add: ('+', operator.add), ast.Sub: ('-', operator.sub), ast.Mult: ('*', operator.mul), ast.Div: ('/', operator.truediv)}
CMP = {ast.Gt: ('>', operator.gt), ast.GtE: ('>=', operator.ge), ast.Lt: ('<', operator.lt), ast.LtE: ('<=', operator.le), ast.Eq: ('==', operator.eq), ast.NotEq: ('!=', operator.ne)}


def validate(formula: str) -> dict:
    if not isinstance(formula, str) or not formula.strip() or len(formula) > 2000:
        raise ValueError('公式长度须为1至2000')
    try:
        tree = ast.parse(formula, mode='eval')
    except (SyntaxError, RecursionError) as exc:
        raise ValueError('公式语法错误') from exc
    if sum(1 for _ in ast.walk(tree)) > 160:
        raise ValueError('公式过于复杂')
    variables, ops, params = set(), set(), []

    def walk(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if not np.isfinite(node.value) or abs(node.value) > 1e12:
                raise ValueError('数值超出范围')
            params.append({'value': node.value, 'role': '公式数值常量；含义由因子解释说明'})
            return 'number'
        if isinstance(node, ast.Name) and node.id in VARIABLES:
            variables.add(node.id)
            return 'number'
        if isinstance(node, ast.BinOp) and type(node.op) in BIN:
            if walk(node.left) != 'number' or walk(node.right) != 'number':
                raise ValueError('算术操作需要数值')
            ops.add(BIN[type(node.op)][0])
            return 'number'
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            if walk(node.operand) != 'number':
                raise ValueError('取负需要数值')
            ops.add('-')
            return 'number'
        if isinstance(node, ast.Compare) and all(type(x) in CMP for x in node.ops):
            for x in [node.left, *node.comparators]:
                if walk(x) != 'number':
                    raise ValueError('比较需要数值')
            ops.update(CMP[type(x)][0] for x in node.ops)
            return 'boolean'
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            if any(walk(x) != 'boolean' for x in node.values):
                raise ValueError('and/or只连接条件')
            ops.add('and' if isinstance(node.op, ast.And) else 'or')
            return 'boolean'
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            if name not in ('mean', 'delay', 'abs', 'rank'):
                raise ValueError('未知函数')
            if len(node.args) != (2 if name in ('mean', 'delay') else 1):
                raise ValueError('函数参数数量错误')
            if walk(node.args[0]) != 'number':
                raise ValueError('函数输入需要数值')
            if name in ('mean', 'delay'):
                n = node.args[1]
                if not isinstance(n, ast.Constant) or type(n.value) is not int or not 1 <= n.value <= 252:
                    raise ValueError('窗口/滞后仅允许1至252正整数；禁止未来引用')
                params.append({'value': n.value, 'role': f'{name}窗口/滞后观测数；按每股交易记录而非自然日'})
            ops.add(name)
            return 'number'
        raise ValueError('表达式含未知变量或未准入语法')

    result_type = walk(tree.body)
    if not variables:
        raise ValueError('因子至少引用一个变量')
    return {'dsl_version': 'p2_71_v1', 'result_type': result_type,
            'variables': [{'name': k, **VARIABLES[k]} for k in sorted(variables)],
            'operators': [{'name': k, 'description': OPERATORS[k]} for k in sorted(ops)], 'parameters': params}


def panel(frame: pd.DataFrame) -> pd.DataFrame:
    if not {'stock_code', 'trade_date'} <= set(frame.columns):
        raise ValueError('面板需要stock_code和trade_date')
    out = frame.copy()
    if out['stock_code'].isna().any() or out['trade_date'].isna().any():
        raise ValueError('面板键含缺失')
    out['stock_code'] = out['stock_code'].astype(str)
    out['trade_date'] = pd.to_datetime(out['trade_date'], errors='raise')
    if out.duplicated(['stock_code', 'trade_date']).any():
        raise ValueError('面板键重复')
    return out.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)


def evaluate(formula: str, frame: pd.DataFrame) -> pd.Series:
    info = validate(formula)
    df = panel(frame)
    missing = {x['name'] for x in info['variables']} - set(df.columns)
    if missing:
        raise ValueError(f'面板缺变量: {sorted(missing)}')
    tree = ast.parse(formula, mode='eval')

    def series(x):
        return x if isinstance(x, pd.Series) else pd.Series(x, index=df.index, dtype=float)

    def clean(x):
        return series(x).replace([np.inf, -np.inf], np.nan)

    def run(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return pd.to_numeric(df[node.id], errors='coerce').replace([np.inf, -np.inf], np.nan)
        if isinstance(node, ast.UnaryOp):
            return -run(node.operand)
        if isinstance(node, ast.BinOp):
            left, right = run(node.left), run(node.right)
            if isinstance(node.op, ast.Div):
                right = series(right).replace(0, np.nan)
            return clean(BIN[type(node.op)][1](left, right))
        if isinstance(node, ast.Compare):
            left, result = run(node.left), pd.Series(True, index=df.index)
            for op, rhs in zip(node.ops, node.comparators):
                right = run(rhs)
                a, b = series(left), series(right)
                result &= CMP[type(op)][1](a, b) & a.notna() & b.notna()
                left = right
            return result
        if isinstance(node, ast.BoolOp):
            values = [run(x) for x in node.values]
            out = values[0]
            for other in values[1:]:
                out = out & other if isinstance(node.op, ast.And) else out | other
            return out
        name = node.func.id
        x = series(run(node.args[0]))
        if name == 'abs':
            return x.abs()
        if name == 'rank':
            return x.groupby(df['trade_date']).rank(pct=True, method='average')
        n = node.args[1].value
        grouped = x.groupby(df['stock_code'], sort=False)
        return grouped.shift(n) if name == 'delay' else grouped.transform(lambda s: s.rolling(n, min_periods=n).mean())

    return clean(run(tree.body))
