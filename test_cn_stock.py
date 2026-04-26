#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 A 股历史数据获取
默认测试协创数据(300857)，覆盖日线 / 1min / 5min / 60min
"""

from data.ingest import CNHistoryDataFetcher


def _print_preview(title, data):
    print(f"\n{title}")
    if data is None or data.empty:
        print("❌ 获取数据失败")
        return False

    print(f"✅ 成功获取 {len(data)} 条数据")
    print(f"📅 时间范围：{data.index[0].strftime('%Y-%m-%d %H:%M:%S')} 至 {data.index[-1].strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n📊 前5行数据预览：")
    print(data.head())

    missing = data.isnull().sum()
    if missing.any():
        print(f"\n⚠️  缺失数据统计：{missing.to_dict()}")
    else:
        print("\n✅ 数据完整，无缺失值")
    return True


def test_cn_stock(stock_code="300857", data_source="akshare"):
    """
    测试 A 股数据获取
    :param stock_code: A 股代码，协创数据为 300857
    :param data_source: 数据源，当前使用 akshare/eastmoney
    """
    print(f"=== 测试 {stock_code} A股数据获取，数据源：{data_source} ===")

    fetcher = CNHistoryDataFetcher(stock_code, data_source=data_source)
    success = True

    print("\n1. 尝试获取最近100条日线数据...")
    daily_data = fetcher.fetch(num_records=100, period="daily")
    success &= _print_preview("日线结果", daily_data)

    print("\n\n2. 尝试指定时间范围获取日线（2024-01-01 至 2024-01-31）...")
    data_range = fetcher.fetch(start_date="2024-01-01", end_date="2024-01-31", period="daily")
    if data_range is not None and not data_range.empty:
        print(f"✅ 成功获取 {len(data_range)} 条数据")
        print(f"📅 时间范围：{data_range.index[0].strftime('%Y-%m-%d')} 至 {data_range.index[-1].strftime('%Y-%m-%d')}")
    else:
        print("❌ 时间范围获取失败")
        success = False

    print("\n\n3. 尝试获取最近30条 1min 数据...")
    min1_data = fetcher.fetch(num_records=30, period="1min")
    success &= _print_preview("1min 结果", min1_data)

    print("\n\n4. 尝试获取最近60条 5min 数据...")
    min5_data = fetcher.fetch(num_records=60, period="5min")
    success &= _print_preview("5min 结果", min5_data)

    print("\n\n5. 尝试获取最近60条 60min 数据...")
    hour1_data = fetcher.fetch(num_records=60, period="60min")
    success &= _print_preview("60min 结果", hour1_data)

    return success


if __name__ == "__main__":
    test_cn_stock(stock_code="300857", data_source="akshare")
