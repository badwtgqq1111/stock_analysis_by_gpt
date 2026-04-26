#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试港股企业行为数据获取
默认测试腾讯控股(00700)，覆盖直接抓取 / 时间范围过滤 / 落库读取
"""

from pathlib import Path

from data.ingest import HKCorporateActionsFetcher, MarketDataService


def _print_preview(title, data):
    print(f"\n{title}")
    if data is None or data.empty:
        print("❌ 获取数据失败")
        return False

    print(f"✅ 成功获取 {len(data)} 条数据")
    if "event_date" in data.columns and not data["event_date"].isnull().all():
        min_date = data["event_date"].min()
        max_date = data["event_date"].max()
        print(f"📅 事件时间范围：{min_date.strftime('%Y-%m-%d')} 至 {max_date.strftime('%Y-%m-%d')}")

    if "action_type" in data.columns:
        print(f"🧩 事件类型：{sorted(data['action_type'].dropna().unique().tolist())}")

    print("\n📊 前5行数据预览：")
    print(data.head())

    missing = data.isnull().sum()
    if missing.any():
        missing = missing[missing > 0]
        print(f"\n⚠️  缺失数据统计：{missing.to_dict()}")
    else:
        print("\n✅ 数据完整，无缺失值")
    return True


def test_hk_corporate_actions(stock_code="00700", db_dir="./assets"):
    """
    测试港股企业行为数据获取
    :param stock_code: 港股代码，5位数字，比如腾讯控股是 00700
    :param db_dir: 资产根目录，默认 ./assets
    """
    print(f"=== 测试 {stock_code} 港股企业行为数据获取 ===")

    fetcher = HKCorporateActionsFetcher(stock_code)
    success = True

    print("\n1. 尝试抓取全部可用企业行为数据...")
    all_actions = fetcher.fetch()
    success &= _print_preview("企业行为结果", all_actions)
    if all_actions is not None and not all_actions.empty:
        print(f"\n📡 成功来源：{fetcher.last_successful_source}")

    print("\n\n2. 尝试指定时间范围抓取（2024-01-01 至 2026-12-31）...")
    ranged_actions = fetcher.fetch(start_date="2024-01-01", end_date="2026-12-31")
    if ranged_actions is not None and not ranged_actions.empty:
        print(f"✅ 成功获取 {len(ranged_actions)} 条数据")
        min_date = ranged_actions["event_date"].min().strftime("%Y-%m-%d")
        max_date = ranged_actions["event_date"].max().strftime("%Y-%m-%d")
        print(f"📅 时间范围：{min_date} 至 {max_date}")
    else:
        print("❌ 时间范围获取失败")
        success = False

    print("\n\n3. 尝试抓取最近10条企业行为数据...")
    latest_actions = fetcher.fetch(num_records=10)
    success &= _print_preview("最近10条结果", latest_actions)

    print("\n\n4. 尝试同步到本地数据层并回读...")
    base_dir = Path(db_dir).resolve() / "data"
    service = MarketDataService(base_dir=str(base_dir))
    try:
        sync_result = service.sync_hk_corporate_actions(stock_code, persist_raw=True)
        print(f"✅ 同步结果：{sync_result}")

        loaded = service.get_hk_corporate_actions(stock_code=stock_code)
        success &= _print_preview("落库回读结果", loaded)
    finally:
        service.close()

    return success


if __name__ == "__main__":
    test_hk_corporate_actions(stock_code="00700", db_dir="./assets")
