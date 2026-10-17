"""Small safe parser for AlphaGen expression strings.

AlphaQCM's vendored fork predates the public ``alphagen.data.parser`` helper,
so this module parses the exported grammar with Python's AST and constructs
only classes from ``alphagen.data.expression``.  Unknown names, attributes,
subscripts and arbitrary calls are rejected.
"""
from __future__ import annotations

import ast


def _expression_module():
    try:
        from alphagen.data import expression as ex
    except ImportError as exc:
        raise RuntimeError('需要将 vendor/alphaqcm 加入 PYTHONPATH') from exc
    return ex


def _constructor_map(ex):
    names = [
        'Constant',
        'Add', 'Sub', 'Mul', 'Div', 'Pow', 'Greater', 'Less',
        'Abs', 'Log', 'Sign',
        'Ref', 'Mean', 'Sum', 'Std', 'Var', 'Max', 'Min',
        'Med', 'Mad', 'Rank', 'Delta', 'WMA', 'EMA', 'Cov', 'Corr',
    ]
    return {name: getattr(ex, name) for name in names if hasattr(ex, name)}


def parse_expression(source: str):
    ex = _expression_module()
    ctors = _constructor_map(ex)
    features = {f.name.lower(): f for f in ex.FeatureType}
    if not isinstance(source, str) or len(source) > 4000:
        raise ValueError('表达式为空或过长')
    source = source.replace('$', '')
    try:
        tree = ast.parse(source, mode='eval')
    except SyntaxError as exc:
        raise ValueError('表达式语法错误') from exc

    def build(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            key = node.id.lower()
            if key in features:
                return ex.Feature(features[key])
            if node.id in ctors:
                return ctors[node.id]
            raise ValueError(f'未知名称: {node.id}')
        if isinstance(node, ast.BinOp):
            left, right = build(node.left), build(node.right)
            op = {ast.Add: 'Add', ast.Sub: 'Sub', ast.Mult: 'Mul',
                  ast.Div: 'Div', ast.Pow: 'Pow'}.get(type(node.op))
            if op is None:
                raise ValueError('未准入的二元运算')
            return ctors[op](left, right)
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)):
                    return -float(node.operand.value)
                value = build(node.operand)
                return ctors['Sub'](0.0, value)
            if isinstance(node.op, ast.UAdd):
                return build(node.operand)
            raise ValueError('未准入的一元运算')
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ctors:
                raise ValueError('未准入函数')
            if node.keywords:
                raise ValueError('不允许关键字参数')
            args = [build(x) for x in node.args]
            if node.func.id in {'Ref', 'Mean', 'Sum', 'Std', 'Var', 'Max', 'Min',
                                'Med', 'Mad', 'Rank', 'Delta', 'WMA', 'EMA'} and len(args) >= 2:
                if not float(args[-1]).is_integer():
                    raise ValueError('滚动窗口必须为整数')
                if float(args[-1]) <= 0:
                    raise ValueError('因子表达式不允许非正窗口或未来引用')
                args[-1] = int(args[-1])
            if node.func.id in {'Cov', 'Corr'} and len(args) >= 3:
                if not float(args[-1]).is_integer():
                    raise ValueError('滚动窗口必须为整数')
                if float(args[-1]) <= 0:
                    raise ValueError('因子表达式不允许非正窗口')
                args[-1] = int(args[-1])
            try:
                return ctors[node.func.id](*args)
            except TypeError as exc:
                raise ValueError(f'{node.func.id} 参数非法') from exc
        raise ValueError('表达式含属性、下标或其他未准入语法')

    return build(tree.body)
