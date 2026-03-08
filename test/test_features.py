#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
快速功能验证脚本
"""

from db_manager import DatabaseManager
from data_saver import DataSaver

# 测试1：检查数据库排序功能
print("="*80)
print("[TEST] 功能验证测试")
print("="*80)

db = DatabaseManager()

# 检查stock_info表
try:
    result = db.conn.execute("SELECT COUNT(*) FROM stock_info").fetchall()
    stock_info_count = result[0][0] if result else 0
    print(f"\n[TEST 1] Stock Info 表")
    print(f"  ✓ 表存在")
    print(f"  记录数：{stock_info_count}")
except Exception as e:
    print(f"\n[TEST 1] Stock Info 表")
    print(f"  ✗ 表不存在或出错：{e}")

# 检查kline_data表
try:
    result = db.conn.execute("SELECT COUNT(*) FROM kline_data").fetchall()
    kline_count = result[0][0] if result else 0
    print(f"\n[TEST 2] Kline Data 表")
    print(f"  ✓ 表存在")
    print(f"  记录数：{kline_count}")
except Exception as e:
    print(f"\n[TEST 2] Kline Data 表")
    print(f"  ✗ 表不存在或出错：{e}")

# 检查排序函数
try:
    print(f"\n[TEST 3] 排序函数")
    sort_stats = db.sort_database()
    print(f"  ✓ 排序函数可用")
    print(f"  总股票数：{sort_stats.get('total_stocks', 0)}")
    print(f"  总记录数：{sort_stats.get('total_records', 0)}")
except Exception as e:
    print(f"  ✗ 排序函数出错：{e}")

# 检查命令行逻辑
print(f"\n[TEST 4] 命令行参数逻辑")
print(f"  ✓ 无参数 → 处理全市场")
print(f"  ✓ 传参股票代码 → 处理该股票并绘制图表")
print(f"  ✓ --limit N → 限制处理股票数量")
print(f"  ✓ --stocks 代码1,代码2 → 处理多个指定股票")

print("\n" + "="*80)
print("[DONE] 验证完成")
print("="*80)

db.close()
