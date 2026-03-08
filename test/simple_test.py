#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""简单测试扫描优化数据库功能"""

from db_manager import DatabaseManager

def simple_test():
    """简单测试数据库功能"""
    print('测试扫描优化数据库功能')

    db = DatabaseManager()

    # 清理
    db.execute_query("DELETE FROM scanned_stocks WHERE stock_code IN ('TEST001', 'TEST002')")

    # 测试保存
    print('1. 保存测试股票...')
    db.save_scanned_stock('TEST001', '测试股票1', 'active')
    db.save_scanned_stock('TEST002', '测试股票2', 'active')
    print('   ✓ 保存成功')

    # 测试查询
    print('2. 查询已扫描股票...')
    count = db.get_scanned_stock_count()
    print(f'   数据库中有 {count} 只已扫描股票')

    # 测试检查
    print('3. 检查股票是否已扫描...')
    is_scanned1 = db.is_stock_scanned('TEST001')
    is_scanned2 = db.is_stock_scanned('TEST999')
    print(f'   TEST001 已扫描: {is_scanned1}')
    print(f'   TEST999 已扫描: {is_scanned2}')

    # 测试获取列表
    print('4. 获取所有已扫描股票...')
    stocks = db.get_scanned_stocks()
    for stock in stocks:
        if stock['code'].startswith('TEST'):
            print(f'   {stock["code"]}: {stock["name"]}')

    db.close()
    print('✓ 所有测试通过！')

if __name__ == '__main__':
    simple_test()