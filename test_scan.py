#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试全市场港股扫描"""

from data_fetcher import HKMarketListFetcher
import time

print('开始扫描全市场港股...')
print('='*80)
start = time.time()

fetcher = HKMarketListFetcher()
stocks = fetcher.fetch()

elapsed = time.time() - start

print()
print('='*80)
print(f'[DONE] 扫描耗时：{elapsed:.1f}秒')
print(f'[FOUND] 共发现 {len(stocks)} 只港股')
print()
print('前20只港股示例：')
print('-'*80)
for i, stock in enumerate(stocks[:20], 1):
    print(f'  {i:3d}. {stock["code"]}: {stock["name"]}')

if len(stocks) > 20:
    print(f'  ... (还有 {len(stocks)-20} 只港股)')
