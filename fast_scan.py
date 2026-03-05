#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""简化的多线程扫描测试 - 直接计数"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import threading
import time

print('='*80)
print('多线程并发扫描全市场港股（00001-09999）')
print('='*80)

start = time.time()

stock_codes = []
lock = threading.Lock()

stats = {
    'tested': 0,
    'found': 0,
    'lock': threading.Lock()
}

def query_stock(code_num):
    """查询单个股票"""
    try:
        code = str(code_num).zfill(5)
        ticker = f"hk{code}"
        url = f"http://qt.gtimg.cn/q={ticker}"

        response = requests.get(url, timeout=2)

        if response.status_code == 200 and '~' in response.text:
            try:
                content = response.content.decode('gb2312')
            except:
                content = response.text

            parts = content.split('~')

            if len(parts) > 1 and parts[1] and parts[1] != 'N/A':
                name = parts[1].strip()
                if name:
                    return {'code': code, 'name': name}
    except:
        pass

    return None

def worker_batch(code_range):
    """线程工作函数"""
    local_stocks = []

    for code_num in code_range:
        stock = query_stock(code_num)

        if stock:
            local_stocks.append(stock)
            with stats['lock']:
                stats['found'] += 1
                if stats['found'] % 100 == 0:
                    elapsed = time.time() - start
                    print(f"[PROGRESS] 已发现 {stats['found']} 只 | 已扫描 {stats['tested']} 个 | 耗时 {elapsed:.0f}s |速率 {stats['tested']/elapsed:.0f} req/s")

        with stats['lock']:
            stats['tested'] += 1
            if stats['tested'] % 1000 == 0:
                elapsed = time.time() - start
                print(f"[MILESTONE] 已扫描 {stats['tested']}/9999 | 速率 {stats['tested']/elapsed:.0f} req/s")

    return local_stocks

# 使用20个线程，每线程500个代码
num_threads = 20
batch_size = 500
total_codes = 9999

print(f"使用 {num_threads} 个线程，每线程 {batch_size} 个代码")
print()

all_stocks = []
with ThreadPoolExecutor(max_workers=num_threads) as executor:
    futures = []

    for i in range(0, total_codes, batch_size):
        batch_start = i + 1
        batch_end = min(i + batch_size, total_codes)
        code_range = range(batch_start, batch_end + 1)
        future = executor.submit(worker_batch, code_range)
        futures.append(future)

    for future in as_completed(futures):
        try:
            batch_stocks = future.result()
            all_stocks.extend(batch_stocks)
        except Exception as e:
            print(f"[ERROR] {e}")

# 去重排序
seen = set()
unique_stocks = []
for stock in all_stocks:
    if stock['code'] not in seen:
        seen.add(stock['code'])
        unique_stocks.append(stock)

unique_stocks.sort(key=lambda x: x['code'])

elapsed = time.time() - start

print()
print('='*80)
print(f'[DONE] 扫描完成！')
print(f'       总耗时：{elapsed:.1f}秒')
print(f'       共发现：{len(unique_stocks)} 只港股')
print(f'       扫描　：{stats["tested"]} 个代码')
print(f'       发现率：{(len(unique_stocks)/stats["tested"]*100):.2f}%')
print(f'       速率　：{stats["tested"]/elapsed:.0f} req/s')
print('='*80)
print()

print('前50只港股示例：')
print('-'*80)
for i, stock in enumerate(unique_stocks[:50], 1):
    print(f'  {i:4d}. {stock["code"]}: {stock["name"]}')

if len(unique_stocks) > 50:
    print(f'  ... (还有 {len(unique_stocks)-50} 只港股)')
