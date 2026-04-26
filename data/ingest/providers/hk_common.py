#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股 provider 公共工具。"""

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None


DEFAULT_SOURCE_PRIORITY = ["akshare_sina", "tencent", "akshare_eastmoney"]


def normalize_hk_stock_code(stock_code):
    """统一为 5 位港股代码。"""
    return str(stock_code).replace("hk", "").strip().zfill(5)


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
    if normalized in {"sina", "akshare_sina"}:
        return ["akshare_sina", "tencent", "akshare_eastmoney"]
    if normalized in {"eastmoney", "akshare_eastmoney", "em"}:
        return ["akshare_eastmoney", "tencent", "akshare_sina"]
    if normalized == "tencent":
        return ["tencent", "akshare_sina", "akshare_eastmoney"]

    return list(DEFAULT_SOURCE_PRIORITY)
