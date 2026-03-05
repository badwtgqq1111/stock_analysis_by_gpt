#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试多线程港股扫描"""

import sys
import time
from data_fetcher import HKMarketListFetcher

print('='*80)
print('开始多线程并发扫描全市场港股...')
print('='*80)
start = time.time()

try:
    fetcher = HKMarketListFetcher()
    stocks = fetcher.fetch()

    elapsed = time.time() - start

    print()
    print('='*80)
    print(f'[DONE] 扫描耗时：{elapsed:.1f}秒')
    print(f'[FOUND] 共发现 {len(stocks)} 只港股')
    print('='*80)
    print()

    print('前30只港股示例：')
    print('-'*80)
    for i, stock in enumerate(stocks[:30], 1):
        print(f'  {i:3d}. {stock["code"]}: {stock["name"]}')

    if len(stocks) > 30:
        print(f'  ... (还有 {len(stocks)-30} 只港股)')
        print()
        print('最后10只港股示例：')
        print('-'*80)
        for i, stock in enumerate(stocks[-10:], len(stocks)-9):
            print(f'  {i:3d}. {stock["code"]}: {stock["name"]}')

except KeyboardInterrupt:
    print('\n[INFO] 用户中断扫描')
    sys.exit(0)
except Exception as e:
    print(f'[ERROR] {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
