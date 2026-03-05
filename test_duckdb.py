#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DuckDB 数据库测试脚本
验证所有数据库功能是否正常工作
"""

import os
import sys
from datetime import datetime
import pandas as pd

# 导入模块
from db_manager import DatabaseManager
from data_fetcher import StockInfoFetcher, HistoryDataFetcher
from data_saver import DataSaver


def test_database_initialization():
    """测试 1: 数据库初始化"""
    print("=" * 70)
    print("[TEST 1] 数据库初始化测试")
    print("=" * 70)
    
    try:
        db_manager = DatabaseManager("./test_assets")
        print("✅ 数据库初始化成功")
        print(f"   数据库路径：{db_manager.db_path}")
        return db_manager
    except Exception as e:
        print(f"❌ 数据库初始化失败：{e}")
        return None


def test_stock_info_save_and_load(db_manager):
    """测试 2: 股票基本信息保存和加载"""
    print()
    print("=" * 70)
    print("[TEST 2] 股票基本信息保存/加载测试")
    print("=" * 70)
    
    stock_code = "03633"
    test_info = {
        'name': '测试股票',
        'current_price': 10.5,
        'close_price': 10.2,
        'open_price': 10.0,
        'high': 11.0,
        'low': 9.8,
        'volume': 1000000,
        'market_cap': 5000000000,
        'pe_ratio': 15.5,
        '52_week_high': 12.0,
        '52_week_low': 8.0
    }
    
    try:
        # 保存数据
        success = db_manager.save_stock_info(test_info, stock_code)
        if success:
            print("✅ 股票信息保存成功")
        else:
            print("❌ 股票信息保存失败")
            return False
        
        # 加载数据
        loaded_info = db_manager.get_stock_info(stock_code)
        if loaded_info:
            print("✅ 股票信息加载成功")
            print(f"   股票名称：{loaded_info['name']}")
            print(f"   当前价格：{loaded_info['current_price']}")
            return True
        else:
            print("❌ 股票信息加载失败")
            return False
            
    except Exception as e:
        print(f"❌ 测试失败：{e}")
        return False


def test_kline_data_save_and_load(db_manager):
    """测试 3: K 线数据保存和加载"""
    print()
    print("=" * 70)
    print("[TEST 3] K 线数据保存/加载测试")
    print("=" * 70)
    
    stock_code = "03633"
    
    try:
        # 创建测试数据
        dates = pd.date_range(start='2024-01-01', periods=10, freq='D')
        test_data = pd.DataFrame({
            'Open': [10.0 + i*0.1 for i in range(10)],
            'Close': [10.2 + i*0.1 for i in range(10)],
            'High': [10.5 + i*0.1 for i in range(10)],
            'Low': [9.8 + i*0.1 for i in range(10)],
            'Volume': [1000000 + i*10000 for i in range(10)]
        }, index=dates)
        
        # 保存数据
        stats = db_manager.save_kline_data(test_data, stock_code)
        if stats:
            print("✅ K 线数据保存成功")
            print(f"   新增记录：{stats['new_records']}")
            print(f"   更新记录：{stats['updated_records']}")
        else:
            print("❌ K 线数据保存失败")
            return False
        
        # 加载数据
        loaded_data = db_manager.get_kline_data(stock_code)
        if loaded_data is not None and not loaded_data.empty:
            print("✅ K 线数据加载成功")
            print(f"   记录数：{len(loaded_data)}")
            print(f"   时间范围：{loaded_data.index[0]} 至 {loaded_data.index[-1]}")
            return True
        else:
            print("❌ K 线数据加载失败")
            return False
            
    except Exception as e:
        print(f"❌ 测试失败：{e}")
        return False


def test_incremental_update(db_manager):
    """测试 4: 增量更新测试"""
    print()
    print("=" * 70)
    print("[TEST 4] 增量更新测试")
    print("=" * 70)
    
    stock_code = "03633"
    
    try:
        # 获取最新日期
        latest_date = db_manager.get_latest_date(stock_code)
        print(f"   数据库中最新日期：{latest_date}")
        
        # 创建新数据（包含部分重叠）
        if latest_date:
            start_date = pd.to_datetime(latest_date) - pd.Timedelta(days=2)
        else:
            start_date = pd.Timestamp('2024-01-09')
        
        new_dates = pd.date_range(start=start_date, periods=5, freq='D')
        new_data = pd.DataFrame({
            'Open': [11.0 + i*0.1 for i in range(5)],
            'Close': [11.2 + i*0.1 for i in range(5)],
            'High': [11.5 + i*0.1 for i in range(5)],
            'Low': [10.8 + i*0.1 for i in range(5)],
            'Volume': [2000000 + i*10000 for i in range(5)]
        }, index=new_dates)
        
        # 保存增量数据
        stats = db_manager.save_kline_data(new_data, stock_code)
        if stats:
            print("✅ 增量更新成功")
            print(f"   新增记录：{stats['new_records']}")
            print(f"   更新记录：{stats['updated_records']}")
            return True
        else:
            print("❌ 增量更新失败")
            return False
            
    except Exception as e:
        print(f"❌ 测试失败：{e}")
        return False


def test_statistics(db_manager):
    """测试 5: 统计信息查询"""
    print()
    print("=" * 70)
    print("[TEST 5] 统计信息查询测试")
    print("=" * 70)
    
    stock_code = "03633"
    
    try:
        stats = db_manager.get_statistics(stock_code)
        if stats:
            print("✅ 统计信息查询成功")
            print(f"   总记录数：{stats['total_records']}")
            print(f"   日期范围：{stats['date_range'][0]} 至 {stats['date_range'][1]}")
            print(f"   数据库大小：{stats['db_file_size']}")
            return True
        else:
            print("❌ 统计信息查询失败")
            return False
            
    except Exception as e:
        print(f"❌ 测试失败：{e}")
        return False


def test_update_log(db_manager):
    """测试 6: 更新日志查询"""
    print()
    print("=" * 70)
    print("[TEST 6] 更新日志查询测试")
    print("=" * 70)
    
    stock_code = "03633"
    
    try:
        logs = db_manager.get_update_log(stock_code, limit=5)
        if logs:
            print("✅ 更新日志查询成功")
            print(f"   最近 {len(logs)} 条更新记录:")
            for log in logs[:3]:
                print(f"      - {log}")
            return True
        else:
            print("⚠️  无更新日志")
            return True
            
    except Exception as e:
        print(f"❌ 测试失败：{e}")
        return False


def test_cleanup(db_manager):
    """测试 7: 数据清理测试"""
    print()
    print("=" * 70)
    print("[TEST 7] 数据清理测试")
    print("=" * 70)
    
    stock_code = "03633"
    
    try:
        deleted = db_manager.cleanup_old_data(stock_code, days=5)
        print("✅ 数据清理完成")
        print(f"   删除记录数：{deleted}")
        return True
        
    except Exception as e:
        print(f"❌ 测试失败：{e}")
        return False


def test_export_json(db_manager):
    """测试 8: JSON 导出测试"""
    print()
    print("=" * 70)
    print("[TEST 8] JSON 导出测试")
    print("=" * 70)
    
    stock_code = "03633"
    
    try:
        export_path = db_manager.export_to_json(stock_code, "./test_output")
        if export_path:
            print("✅ JSON 导出成功")
            print(f"   导出路径：{export_path}")
            return True
        else:
            print("❌ JSON 导出失败")
            return False
            
    except Exception as e:
        print(f"❌ 测试失败：{e}")
        return False


def cleanup_test_files():
    """清理测试文件"""
    import shutil
    
    test_dirs = ["./test_assets", "./test_output"]
    
    for dir_path in test_dirs:
        if os.path.exists(dir_path):
            try:
                shutil.rmtree(dir_path)
                print(f"[CLEANUP] 已清理测试目录：{dir_path}")
            except Exception as e:
                print(f"[CLEANUP] 清理失败 {dir_path}: {e}")


def main():
    """运行所有测试"""
    print()
    print("=" * 70)
    print("🧪 DuckDB 数据库功能测试套件")
    print("=" * 70)
    print(f"开始时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    results = []
    
    # 测试 1: 数据库初始化
    db_manager = test_database_initialization()
    results.append(("数据库初始化", db_manager is not None))
    
    if db_manager:
        # 测试 2: 股票信息保存/加载
        result = test_stock_info_save_and_load(db_manager)
        results.append(("股票信息保存/加载", result))
        
        # 测试 3: K 线数据保存/加载
        result = test_kline_data_save_and_load(db_manager)
        results.append(("K 线数据保存/加载", result))
        
        # 测试 4: 增量更新
        result = test_incremental_update(db_manager)
        results.append(("增量更新", result))
        
        # 测试 5: 统计信息
        result = test_statistics(db_manager)
        results.append(("统计信息查询", result))
        
        # 测试 6: 更新日志
        result = test_update_log(db_manager)
        results.append(("更新日志查询", result))
        
        # 测试 7: 数据清理
        result = test_cleanup(db_manager)
        results.append(("数据清理", result))
        
        # 测试 8: JSON 导出
        result = test_export_json(db_manager)
        results.append(("JSON 导出", result))
    
    # 清理测试文件
    print()
    print("=" * 70)
    print("[CLEANUP] 清理测试文件")
    print("=" * 70)
    cleanup_test_files()
    
    # 打印测试结果汇总
    print()
    print("=" * 70)
    print("📊 测试结果汇总")
    print("=" * 70)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")
    
    print()
    print(f"总计：{passed}/{total} 通过")
    print(f"完成率：{(passed/total*100):.1f}%")
    print()
    
    if passed == total:
        print("🎉 所有测试通过！DuckDB 版本工作正常！")
    else:
        print("⚠️  部分测试失败，请检查错误信息")
    
    print()
    print(f"结束时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


if __name__ == "__main__":
    main()
