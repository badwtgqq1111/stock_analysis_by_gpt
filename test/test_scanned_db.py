#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试已扫描股票数据库功能"""

from data.store import DatabaseManager

def test_scanned_stocks():
    """测试已扫描股票数据库功能"""
    print('='*80)
    print('测试已扫描股票数据库功能')
    print('='*80)

    db = DatabaseManager()

    # 测试保存已扫描股票
    print('\n1. 测试保存已扫描股票')
    test_stocks = [
        ('00001', '香港交易所'),
        ('00005', '汇丰控股'),
        ('03633', '中裕能源'),
        ('03690', '美团'),
    ]

    for code, name in test_stocks:
        success = db.save_scanned_stock(code, name, 'active')
        print(f'   保存 {code} - {name}: {"✓" if success else "✗"}')

    # 测试获取已扫描股票
    print('\n2. 测试获取已扫描股票')
    scanned = db.get_scanned_stocks()
    print(f'   总共已扫描: {len(scanned)} 只股票')
    for stock in scanned[:5]:  # 显示前5个
        print(f'   {stock["code"]}: {stock["name"]} ({stock["status"]})')

    # 测试检查特定股票是否已扫描
    print('\n3. 测试检查特定股票扫描状态')
    test_codes = ['00001', '03633', '99999']
    for code in test_codes:
        is_scanned = db.is_stock_scanned(code)
        print(f'   {code} 已扫描: {"✓" if is_scanned else "✗"}')

    # 测试重复保存（应该更新）
    print('\n4. 测试重复保存（更新）')
    success = db.save_scanned_stock('00001', '香港交易所（更新）', 'active')
    updated_stock = db.get_scanned_stocks()
    updated_name = next((s['name'] for s in updated_stock if s['code'] == '00001'), None)
    print(f'   更新后的名称: {updated_name}')

    # 测试不同状态
    print('\n5. 测试不同状态过滤')
    db.save_scanned_stock('00002', '中电控股', 'inactive')
    db.save_scanned_stock('00003', '香港中华煤气', 'error')

    active_stocks = db.get_scanned_stocks('active')
    inactive_stocks = db.get_scanned_stocks('inactive')
    error_stocks = db.get_scanned_stocks('error')

    print(f'   活跃状态: {len(active_stocks)} 只')
    print(f'   非活跃状态: {len(inactive_stocks)} 只')
    print(f'   错误状态: {len(error_stocks)} 只')

    # 统计信息
    print('\n6. 统计信息')
    total_scanned = db.get_scanned_stock_count()
    print(f'   已扫描股票总数: {total_scanned}')

    db.close()

    print('\n' + '='*80)
    print('测试完成！')
    print('='*80)

if __name__ == '__main__':
    test_scanned_stocks()
