#!/usr/bin/env python
import duckdb

conn = duckdb.connect('./assets/stock_data.duckdb')

# 查询表结构
print('scanned_stocks表结构:')
result = conn.execute('DESCRIBE scanned_stocks').fetchall()
for row in result:
    print(f'  {row[0]}: {row[1]}')

print(f'\n数据库中已扫描股票数量: {conn.execute("SELECT COUNT(*) FROM scanned_stocks").fetchone()[0]}')

# 查询前5只已扫描股票
print('\n前5只已扫描股票:')
result2 = conn.execute('SELECT * FROM scanned_stocks LIMIT 5').fetchall()
for row in result2:
    print(f'  {row}')

conn.close()