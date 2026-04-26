#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
港股数据获取工具 - 主程序入口
使用腾讯财经 API 获取港股数据，保存到数据库，支持增量更新
支持单只股票处理和全市场批量处理
"""

import sys
import argparse
import asyncio
import os
try:
    import uvloop
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False
from data.ingest import StockInfoFetcher, HistoryDataFetcher, HKMarketListFetcher, MarketDataService
from data_display import StockInfoDisplay, HistoryDataDisplay
from data.store.exporters import DataSaver
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

    # ========== Smart download strategy ==========
    # Download latest data for updates if needed
    # Always load complete historical data from database for plotting
    data_fetcher.fetch_with_strategy()

    # Always load complete historical data from database for plotting
    print("[INFO] Loading complete historical data from database...")
    hist_data = data_fetcher.db_manager.get_kline_data(stock_code)

    if hist_data is None or hist_data.empty:
        print("[ERROR] No data in database, skipping this stock")
        return False
    else:
        print(f"[OK] Loaded {len(hist_data)} historical records from database")

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


async def process_all_stocks_async(output_path, db_dir, limit=None):
    """
    异步批量处理全市场所有股票

    Args:
        output_path: 输出目录路径
        db_dir: 数据库目录
        limit (int): 限制处理的股票数量，None 表示不限制
    """
    print_section("[BATCH] 批量处理港股全市场")
    service = MarketDataService(base_dir=os.path.join(db_dir, "data"))
    try:
        summary = await asyncio.to_thread(
            service.bulk_sync_hk_history,
            start_date="2014-01-01",
            end_date=None,
            adjust="qfq",
            max_workers=None,
            flush_stock_count=64,
            flush_row_count=250000,
            limit=limit,
            stock_codes=None,
            include_stock_info=True,
            compact_after=True,
        )
        print()
        print_section("[SUMMARY] 批量处理完成")
        print(f"[INFO] 总计：{summary.get('total_stocks', 0)} 只股票")
        print(f"[INFO] 成功：{summary.get('success_count', 0)} 只")
        print(f"[INFO] 跳过：{summary.get('skipped_count', 0)} 只")
        print(f"[INFO] 失败：{summary.get('failed_count', 0)} 只")
        print(f"[INFO] 累计K线记录：{summary.get('rows_written', 0)}")
        print(f"[INFO] 数据集位置：{summary.get('dataset_path', 'N/A')}")
    finally:
        service.close()


def process_all_stocks(output_path, db_dir, limit=None):
    """
    批量处理全市场所有股票（同步包装器）

    Args:
        output_path: 输出目录路径
        db_dir: 数据库目录
        limit (int): 限制处理的股票数量，None 表示不限制
    """
    # 设置 uvloop 事件循环策略（可选，提升性能）
    if UVLOOP_AVAILABLE:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    else:
        print("[INFO] uvloop 不可用，使用标准 asyncio 事件循环")

    # 运行异步函数
    asyncio.run(process_all_stocks_async(output_path, db_dir, limit))


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
        description='港股数据获取工具 - 默认遍历全市场，传参则处理指定股票并绘制图表',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                        # 默认处理全市场所有港股（不绘制图表）
  python main.py 03633                  # 处理单只股票 03633 并绘制 K 线图
  python main.py --stocks 03633,02590   # 批量处理多个指定股票并绘制图表
  python main.py --limit 50             # 处理全市场前 50 只股票
  python main.py 03633 --no-chart       # 处理单只股票但不绘制图表
        """
    )

    parser.add_argument('stock_code', nargs='?', default=None,
                        help='股票代码（不指定则处理全市场）')
    parser.add_argument('--stocks', type=str, default=None,
                        help='逗号分隔的股票代码列表 (例：03633,02590,03690)')
    parser.add_argument('--limit', type=int, default=None,
                        help='限制处理的股票数量（不指定股票时使用）')
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
    if args.stocks:
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
            verify=False
        )
    elif args.stock_code:
        # 处理单只股票，默认显示图表
        process_single_stock(
            args.stock_code,
            output_path,
            args.db,
            show_chart=not args.no_chart
        )
    else:
        # 默认处理全市场所有股票（不显示图表）
        process_all_stocks(output_path, args.db, args.limit)


if __name__ == "__main__":
    main()
