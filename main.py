#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
港股数据获取工具 - 主程序入口
使用腾讯财经 API 获取港股数据，保存到数据库，支持增量更新
"""

from data_fetcher import StockInfoFetcher, HistoryDataFetcher
from data_display import StockInfoDisplay, HistoryDataDisplay
from data_saver import DataSaver
from chart_plotter import KLineChartPlotter
from utils import setup_output_dir, print_section, print_summary


def main():
    """主函数 - 分模块执行"""

    # 配置
    stock_code = "03633"
    output_dir = "./output"
    db_dir = "./assets"

    # ========== 初始化模块 ==========
    print_section("[INIT] 港股数据获取工具 - 腾讯财经 API (数据库增量更新版)")

    # 设置输出目录
    output_path = setup_output_dir(output_dir)
    if output_path is None:
        print("[ERROR] 无法创建输出目录，程序中止")
        return

    print()

    # ========== 检查数据库 ==========
    print_section("[MODULE] 检查增量更新")

    # 初始化数据保存器（会自动初始化数据库）
    saver = DataSaver(db_dir)

    # 检查数据库中是否已有数据
    data_fetcher = HistoryDataFetcher(stock_code, db_dir)
    update_info = data_fetcher.check_update_from_db()

    if update_info['has_data']:
        print(f"[INFO] 数据库中已有数据")
        print(f"       最新日期: {update_info['latest_date']}")
        print(f"       总记录数: {update_info['total_records']}")
        print(f"[INFO] 即将获取最新数据并进行增量更新...")
    else:
        print(f"[INFO] 数据库为空，即将首次下载完整数据...")

    print()

    # ========== 下载数据模块 ==========
    print_section("[MODULE] 下载数据")

    # 获取基本信息
    info_fetcher = StockInfoFetcher(stock_code)
    stock_info = info_fetcher.fetch()

    print()

    # 获取历史数据
    hist_data = data_fetcher.fetch()

    if hist_data is None or hist_data.empty:
        print("[ERROR] 未能获取数据，程序中止")
        return

    print()

    # ========== 显示数据模块 ==========
    print_section("[MODULE] 显示数据")

    StockInfoDisplay.display(stock_info)
    print()

    HistoryDataDisplay.display(hist_data)
    print()

    # ========== 数据库保存模块 ==========
    print_section("[MODULE] 保存到数据库")

    # 保存股票基本信息
    saver.save_stock_info_to_db(stock_info, stock_code)

    # 保存 K 线数据到数据库（支持增量更新）
    db_stats = saver.save_to_db(hist_data, stock_code)

    if db_stats:
        print(f"[INFO] 增量更新统计:")
        print(f"       新增记录: {db_stats['new_records']}")
        print(f"       更新记录: {db_stats['updated_records']}")

    print()

    # ========== 导出 JSON 模块（可选，用于外部使用）==========
    print_section("[MODULE] 导出 JSON 文件")

    # 直接保存当前数据为 JSON（快速）
    json_path = saver.save_json(hist_data, stock_code, output_path)

    # 或从数据库导出完整数据（包含历史所有数据）
    # json_path = saver.export_from_db(stock_code, output_path)

    print()

    # ========== 数据库统计信息 ==========
    print_section("[MODULE] 数据库统计")

    db_info = saver.get_db_statistics(stock_code)
    if db_info:
        print(f"[INFO] 数据库统计信息:")
        print(f"       总记录数: {db_info['total_records']}")
        print(f"       日期范围: {db_info['date_range'][0]} 到 {db_info['date_range'][1]}")
        print(f"       数据库大小: {db_info['db_file_size']}")
        print(f"       存储位置: {db_info['db_path']}")

    print()

    # ========== 绘制图表模块 ==========
    print_section("[MODULE] 绘制图表")

    # 绘制一年 K 线图
    plotter = KLineChartPlotter(stock_code)
    chart_path = plotter.plot(hist_data, output_path, months=12)
    print()

    # ========== 总结模块 ==========
    print_summary(output_path, json_path, chart_path)


if __name__ == "__main__":
    main()
