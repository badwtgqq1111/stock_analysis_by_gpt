#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据显示模块 - 显示和格式化股票数据
"""


class StockInfoDisplay:
    """显示股票基本信息"""

    @staticmethod
    def display(info):
        """
        显示股票基本信息

        Args:
            info (dict): 股票基本信息
        """
        if info is None:
            print("[ERROR] 没有基本信息可显示")
            return

        print()
        print("[INFO] 基本信息")
        print("-" * 70)
        print(f"  股票名称    {info['name']}")
        print(f"  股票代码    {info['code']}")
        print(f"  当前价格    {info['current_price']:.2f} HKD")
        print(f"  昨收价      {info['close_price']:.2f} HKD")
        print(f"  今开价      {info['open_price']:.2f} HKD")
        print(f"  最高价      {info['high']:.2f} HKD")
        print(f"  最低价      {info['low']:.2f} HKD")
        print(f"  成交量      {int(info['volume']):,}" if info['volume'] else "  成交量      N/A")
        print(f"  市盈率      {info['pe_ratio']:.2f}" if info['pe_ratio'] else "  市盈率      N/A")
        print(f"  52周最高    {info['52_week_high']:.2f} HKD" if info['52_week_high'] else "  52周最高    N/A")
        print(f"  52周最低    {info['52_week_low']:.2f} HKD" if info['52_week_low'] else "  52周最低    N/A")


class HistoryDataDisplay:
    """显示股票历史数据"""

    @staticmethod
    def display(hist):
        """
        显示股票数据摘要

        Args:
            hist (DataFrame): 股票历史数据
        """
        if hist is None or hist.empty:
            print("[ERROR] 没有数据可显示")
            return

        print("[INFO] 数据预览（最近 5 行）")
        print("-" * 70)
        print(hist.tail(5).to_string())

        print()
        print("[INFO] 数据统计信息")
        print("-" * 70)
        print(f"总记录数：{len(hist)} 条")
        print(f"时间范围：{hist.index[0].strftime('%Y-%m-%d')} 至 {hist.index[-1].strftime('%Y-%m-%d')}")
        print()
        print(hist.describe().to_string())

        print()
        print("[INFO] 列说明")
        print("-" * 70)
        print("  Open   - 开盘价")
        print("  Close  - 收盘价")
        print("  High   - 最高价")
        print("  Low    - 最低价")
        print("  Volume - 成交量")
