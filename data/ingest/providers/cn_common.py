#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A 股 provider 公共工具。"""

from data.model import normalize_stock_code

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None


# 新浪日线解码会创建 py-mini-racer/V8 context。macOS 上多个 context 同时
# 初始化会让原生库直接终止解释器，异常处理无法恢复。因此默认批量链路只使用
# 不依赖 V8 的源；新浪仍可通过 data_source="sina" 显式启用。
DEFAULT_SOURCE_PRIORITY = ["tencent", "baostock", "akshare_eastmoney"]


def normalize_cn_stock_code(stock_code):
    """统一为 A 股标准代码。"""
    return normalize_stock_code(stock_code, market="CN")


def normalize_cn_symbol(stock_code):
    """转换为 akshare 使用的 6 位数字代码。"""
    normalized = normalize_cn_stock_code(stock_code)
    return normalized.split(".", 1)[0]


def to_sina_symbol(stock_code):
    """转换为新浪/腾讯使用的带市场前缀代码。"""
    normalized = normalize_cn_stock_code(stock_code)
    symbol = normalized.split(".", 1)[0]
    suffix = normalized.split(".", 1)[1] if "." in normalized else ""
    if suffix == "SH":
        return f"sh{symbol}"
    if suffix in {"SZ", "BJ"}:
        return f"sz{symbol}"
    if symbol.startswith("6"):
        return f"sh{symbol}"
    return f"sz{symbol}"


def safe_float(value):
    """尽量将值转为 float，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_source_priority(data_source=None, source_priority=None):
    """根据用户指定的数据源构造抓取优先级。"""
    if source_priority:
        return list(source_priority)

    if not data_source:
        return list(DEFAULT_SOURCE_PRIORITY)

    normalized = str(data_source).strip().lower()
    if normalized == "akshare":
        return list(DEFAULT_SOURCE_PRIORITY)
    if normalized == "baostock":
        return ["baostock", "tencent", "akshare_eastmoney"]
    if normalized in {"sina", "akshare_sina"}:
        return ["akshare_sina", "tencent", "baostock", "akshare_eastmoney"]
    if normalized in {"eastmoney", "akshare_eastmoney", "em"}:
        return ["akshare_eastmoney", "tencent", "baostock"]
    if normalized == "tencent":
        return ["tencent", "baostock", "akshare_eastmoney"]

    return list(DEFAULT_SOURCE_PRIORITY)
