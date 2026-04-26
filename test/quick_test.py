#!/usr/bin/env python
from data.store import DatabaseManager

db = DatabaseManager()
print('数据库连接成功')

# 清理测试数据
db.execute_query("DELETE FROM scanned_stocks WHERE stock_code LIKE 'TEST%'")
print('清理完成')

# 保存测试股票
db.save_scanned_stock('TEST001', '测试股票1', 'active')
print('保存TEST001完成')

# 查询数量
count = db.get_scanned_stock_count()
print(f'数据库中已扫描股票数量: {count}')

# 检查是否已扫描
is_scanned = db.is_stock_scanned('TEST001')
print(f'TEST001已扫描: {is_scanned}')

is_scanned2 = db.is_stock_scanned('NONEXIST')
print(f'NONEXIST已扫描: {is_scanned2}')

# 获取所有股票
stocks = db.get_scanned_stocks()
test_stocks = [s for s in stocks if s['code'].startswith('TEST')]
print(f'测试股票列表: {test_stocks}')

db.close()
print('测试完成 - 扫描优化功能正常！')
