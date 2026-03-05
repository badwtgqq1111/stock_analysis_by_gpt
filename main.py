#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
港股数据获取工具 - 主程序入口
使用腾讯财经 API 获取港股数据，保存到数据库，支持增量更新
支持单只股票处理和全市场批量处理
"""

import sys
import argparse
from data_fetcher import StockInfoFetcher, HistoryDataFetcher, HKMarketListFetcher
from data_display import StockInfoDisplay, HistoryDataDisplay
from data_saver import DataSaver
from chart_plotter import KLineChartPlotter
from utils import setup_output_dir, print_section, print_summary


def process_single_stock(stock_code, output_path, db_dir, show_chart=True):
    """
    处理单只股票
    
    Args:
        stock_code (str): 股票代码
        output_path: 输出目录路径
        db_dir: 数据库目录
        show_chart (bool): 是否显示图表
    """
    print_section(f"[SINGLE] 处理股票：{stock_code}")
    
    # ========== 检查数据库 ==========
    print_section("[MODULE] 检查增量更新")
    
    saver = DataSaver(db_dir)
    data_fetcher = HistoryDataFetcher(stock_code, db_dir)
    update_info = data_fetcher.check_update_from_db()
    
    if update_info['has_data']:
        print(f"[INFO] 数据库中已有数据")
        print(f"       最新日期：{update_info['latest_date']}")
        print(f"       总记录数：{update_info['total_records']}")
        print(f"[INFO] 即将获取最新数据并进行增量更新...")
    else:
        print(f"[INFO] 数据库为空，即将首次下载完整数据...")
    
    print()
    
    # ========== 下载数据模块 ==========
    print_section("[MODULE] 下载数据")
    
    info_fetcher = StockInfoFetcher(stock_code)
    stock_info = info_fetcher.fetch()
    
    print()
    
    hist_data = data_fetcher.fetch()
    
    if hist_data is None or hist_data.empty:
        print("[ERROR] 未能获取数据，跳过此股票")
        return False
    
    print()
    
    # ========== 显示数据模块 ==========
    print_section("[MODULE] 显示数据")
    
    StockInfoDisplay.display(stock_info)
    print()
    
    HistoryDataDisplay.display(hist_data)
    print()
    
    # ========== 数据库保存模块 ==========
    print_section("[MODULE] 保存到数据库")
    
    saver.save_stock_info_to_db(stock_info, stock_code)
    db_stats = saver.save_to_db(hist_data, stock_code)
    
    if db_stats:
        print(f"[INFO] 增量更新统计:")
        print(f"       新增记录：{db_stats['new_records']}")
        print(f"       更新记录：{db_stats['updated_records']}")
    
    print()
    
    # ========== 导出 JSON 模块 ==========
    print_section("[MODULE] 导出 JSON 文件")
    
    json_path = saver.save_json(hist_data, stock_code, output_path)
    
    print()
    
    # ========== 数据库统计信息 ==========
    print_section("[MODULE] 数据库统计")
    
    db_info = saver.get_db_statistics(stock_code)
    if db_info:
        print(f"[INFO] 数据库统计信息:")
        print(f"       总记录数：{db_info['total_records']}")
        print(f"       日期范围：{db_info['date_range'][0]} 到 {db_info['date_range'][1]}")
        print(f"       数据库大小：{db_info['db_file_size']}")
        print(f"       存储位置：{db_info['db_path']}")
    
    print()
    
    # ========== 绘制图表模块 ==========
    if show_chart:
        print_section("[MODULE] 绘制图表")
        
        plotter = KLineChartPlotter(stock_code)
        chart_path = plotter.plot(hist_data, output_path, months=12)
        print()
    else:
        chart_path = None
    
    return True


def process_all_stocks(output_path, db_dir, limit=None):
    """
    批量处理全市场所有股票
    
    Args:
        output_path: 输出目录路径
        db_dir: 数据库目录
        limit (int): 限制处理的股票数量，None 表示不限制
    """
    print_section("[BATCH] 批量处理港股全市场")
    
    # ========== 获取股票列表 ==========
    print_section("[MODULE] 获取港股全市场股票列表")
    
    market_fetcher = HKMarketListFetcher()
    stocks = market_fetcher.fetch()
    
    if not stocks:
        print("[ERROR] 无法获取股票列表，程序中止")
        return
    
    print(f"[INFO] 共发现 {len(stocks)} 只港股")
    
    # 保存股票列表到数据库
    saver = DataSaver(db_dir)
    market_fetcher.save_to_db(saver.db_manager)
    
    print()
    
    # ========== 批量处理 ==========
    if limit:
        stocks_to_process = stocks[:limit]
        print(f"[INFO] 将处理前 {limit} 只股票（总共 {len(stocks)} 只）")
    else:
        stocks_to_process = stocks
        print(f"[INFO] 将处理全部 {len(stocks)} 只股票")
    
    print()
    
    success_count = 0
    fail_count = 0
    
    for idx, stock in enumerate(stocks_to_process, 1):
        stock_code = stock['code']
        stock_name = stock['name']
        
        print(f"\n{'='*80}")
        print(f"[{idx}/{len(stocks_to_process)}] 处理：{stock_code} - {stock_name}")
        print('='*80)
        
        try:
            if process_single_stock(stock_code, output_path, db_dir, show_chart=False):
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            print(f"[ERROR] 处理 {stock_code} 时发生异常：{e}")
            fail_count += 1
        
        # 每处理 10 只股票显示进度统计
        if idx % 10 == 0:
            print(f"\n[PROGRESS] 已处理 {idx}/{len(stocks_to_process)} 只股票")
            print(f"           成功：{success_count}, 失败：{fail_count}")
    
    # ========== 总结 ==========
    print()
    print_section("[SUMMARY] 批量处理完成")
    print(f"[INFO] 总计：{len(stocks_to_process)} 只股票")
    print(f"       成功：{success_count} 只")
    print(f"       失败：{fail_count} 只")
    print(f"       成功率：{(success_count/len(stocks_to_process)*100):.1f}%")


def process_multiple_stocks(stock_codes, output_path, db_dir, show_chart=False, verify=True):
    """
    批量处理指定的多个股票，支持验证

    Args:
        stock_codes (list): 股票代码列表
        output_path: 输出目录路径
        db_dir: 数据库目录
        show_chart (bool): 是否显示图表
        verify (bool): 是否在完成后验证数据

    Returns:
        dict: 处理结果统计
    """
    if not stock_codes:
        print("[ERROR] 股票代码列表为空，程序中止")
        return
    
    print_section("[BATCH] 批量处理指定股票")
    print(f"[INFO] 将处理 {len(stock_codes)} 只股票")
    print(f"[INFO] 股票列表：{', '.join(stock_codes)}")
    print()
    
    saver = DataSaver(db_dir)
    
    success_count = 0
    fail_count = 0
    processed_stocks = []
    failed_stocks = []
    
    for idx, stock_code in enumerate(stock_codes, 1):
        print(f"\n{'='*80}")
        print(f"[{idx}/{len(stock_codes)}] 处理股票：{stock_code}")
        print('='*80)
        
        try:
            if process_single_stock(stock_code, output_path, db_dir, show_chart=show_chart):
                success_count += 1
                processed_stocks.append(stock_code)
            else:
                fail_count += 1
                failed_stocks.append(stock_code)
        except Exception as e:
            print(f"[ERROR] 处理 {stock_code} 时发生异常：{e}")
            fail_count += 1
            failed_stocks.append(stock_code)
    
    # ========== 汇总 ==========
    print()
    print_section("[SUMMARY] 批量处理完成")
    print(f"[INFO] 总计：{len(stock_codes)} 只股票")
    print(f"       成功：{success_count} 只")
    print(f"       失败：{fail_count} 只")
    print(f"       成功率：{(success_count/len(stock_codes)*100):.1f}%")
    
    # ========== 验证阶段 ==========
    if verify and processed_stocks:
        print()
        print_section("[VERIFY] 验证已保存的数据")
        
        verification_result = saver.batch_verify_data(processed_stocks)
        saver.print_verification_report(verification_result)
    
    return {
        'total': len(stock_codes),
        'success': success_count,
        'failed': fail_count,
        'processed': processed_stocks,
        'failed_stocks': failed_stocks
    }


def main():
    """主函数 - 支持命令行参数"""
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description='港股数据获取工具 - 支持单只股票、多只股票和批量处理',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                        # 默认处理 03633
  python main.py 03633                  # 处理单只股票 03633
  python main.py --stocks 03633,02590   # 批量处理多个指定股票
  python main.py --all                  # 批量处理全市场所有股票
  python main.py --all --limit 50       # 批量处理前 50 只股票
  python main.py --stocks 03633 --verify # 处理并验证指定股票
        """
    )
    
    parser.add_argument('stock_code', nargs='?', default='03633',
                        help='股票代码 (默认：03633)')
    parser.add_argument('--stocks', type=str, default=None,
                        help='逗号分隔的股票代码列表 (例：03633,02590,03690)')
    parser.add_argument('--all', action='store_true',
                        help='批量处理全市场所有股票')
    parser.add_argument('--limit', type=int, default=None,
                        help='限制处理的股票数量（仅与--all 一起使用）')
    parser.add_argument('--verify', action='store_true',
                        help='处理完后验证数据是否成功保存')
    parser.add_argument('--no-chart', action='store_true',
                        help='不绘制 K 线图')
    parser.add_argument('--output', default='./output',
                        help='输出目录 (默认：./output)')
    parser.add_argument('--db', default='./assets',
                        help='数据库目录 (默认：./assets)')
    
    args = parser.parse_args()
    
    # 设置输出目录
    output_path = setup_output_dir(args.output)
    if output_path is None:
        print("[ERROR] 无法创建输出目录，程序中止")
        return
    
    # 根据参数选择处理模式
    if args.all:
        # 批量处理全市场
        process_all_stocks(output_path, args.db, args.limit)
    elif args.stocks:
        # 批量处理指定的多个股票
        stock_codes = [code.strip() for code in args.stocks.split(',')]
        stock_codes = [code for code in stock_codes if code]  # 过滤空值
        
        if not stock_codes:
            print("[ERROR] 没有有效的股票代码，程序中止")
            return
        
        process_multiple_stocks(
            stock_codes,
            output_path,
            args.db,
            show_chart=not args.no_chart,
            verify=args.verify
        )
    else:
        # 处理单只股票
        saver = DataSaver(args.db)
        
        process_single_stock(
            args.stock_code, 
            output_path, 
            args.db, 
            show_chart=not args.no_chart
        )
        
        # 如果指定了验证，则验证单个股票
        if args.verify:
            print()
            print_section("[VERIFY] 验证已保存的数据")
            verification_result = saver.verify_db_data(args.stock_code)
            saver.print_verification_report(verification_result)


if __name__ == "__main__":
    main()
