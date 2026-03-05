#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试协程版本的股票处理"""

import asyncio

try:
    import uvloop
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False

async def test_async_stock_processing():
    """测试异步股票处理"""
    print("开始测试协程股票处理...")

    # 测试数据
    test_stocks = [
        {'code': '00001', 'name': '香港交易所'},
        {'code': '00005', 'name': '汇丰控股'},
        {'code': '03633', 'name': '中裕能源'}
    ]

    async def process_single_test_stock(stock):
        stock_code = stock['code']
        stock_name = stock['name']

        print(f"开始处理 {stock_code} - {stock_name}")

        # 模拟异步操作
        await asyncio.sleep(0.1)  # 模拟网络请求

        print(f"完成处理 {stock_code} - {stock_name}")
        return True

    # 并发处理测试股票
    semaphore = asyncio.Semaphore(2)  # 限制并发数量

    async def limited_process(stock):
        async with semaphore:
            return await process_single_test_stock(stock)

    print("并发处理开始...")
    tasks = [limited_process(stock) for stock in test_stocks]
    results = await asyncio.gather(*tasks)

    success_count = sum(1 for r in results if r)
    print(f"\n测试完成: {success_count}/{len(test_stocks)} 成功")

def main():
    if UVLOOP_AVAILABLE:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        print("使用 uvloop 事件循环")
    else:
        print("使用标准 asyncio 事件循环")

    asyncio.run(test_async_stock_processing())

if __name__ == '__main__':
    main()