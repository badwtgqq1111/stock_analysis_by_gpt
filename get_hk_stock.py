#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
获取港股数据的 Python 脚本 - 使用腾讯财经 API
示例：获取 03633.HK (卓朗发展) 的股票数据
"""

import requests
import pandas as pd
import json
from datetime import datetime, timedelta
import time
import os
import shutil

try:
    import matplotlib
    matplotlib.use('Agg')  # 使用非交互式后端
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Rectangle
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("警告：matplotlib 未安装，将跳过 K 线图绘制")


def setup_output_dir(output_dir="./output"):
    """
    设置输出目录，清理历史数据

    Args:
        output_dir (str): 输出目录路径，默认为 ./output

    Returns:
        str: 输出目录的绝对路径
    """
    # 转换为绝对路径
    output_path = os.path.abspath(output_dir)

    # 如果目录存在，清理它
    if os.path.exists(output_path):
        print(f"[CLEAN] 清理历史数据...")
        try:
            shutil.rmtree(output_path)
            print("[OK] 历史数据已清理")
        except Exception as e:
            print(f"⚠️  清理目录失败：{e}")

    # 创建新目录
    try:
        os.makedirs(output_path, exist_ok=True)
        print(f"[CREATE] 创建输出目录：{output_path}")
        print()
    except Exception as e:
        print(f"[ERROR] 创建目录失败：{e}")
        return None

    return output_path


def get_hk_stock_data_tencent(stock_code, start_date=None, end_date=None):
    """
    使用腾讯财经 API 获取港股股票历史数据

    Args:
        stock_code (str): 股票代码，例如 '03633'
        start_date (str): 开始日期，格式 'YYYY-MM-DD'
        end_date (str): 结束日期，格式 'YYYY-MM-DD'

    Returns:
        DataFrame: 包含股票历史的 DataFrame
    """
    # 港股代码格式：hk 前缀 + 代码
    ticker_symbol = f"hk{stock_code}"

    print(f"[INFO] 正在从腾讯财经获取 {ticker_symbol} 的股票数据...")

    try:
        # 腾讯财经历史数据 API
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={ticker_symbol},day,,,1000,qfq"

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        # 解析 JSON 数据
        data = response.json()

        # 检查数据是否存在
        if 'data' not in data or ticker_symbol not in data['data']:
            print(f"[ERROR] 未找到 {ticker_symbol} 的数据")
            return None

        stock_data = data['data'][ticker_symbol]

        # 获取日线数据 (day)
        if 'day' not in stock_data:
            print(f"[ERROR] 无 {ticker_symbol} 的日线数据")
            return None

        klines = stock_data['day']

        if not klines:
            print(f"[ERROR] 未获取到 {ticker_symbol} 的数据")
            return None

        # 解析 K 线数据 (处理可能有 6-7 列的情况)
        rows = []
        for kline in klines:
            row = {
                'date': kline[0],
                'open': kline[1],
                'close': kline[2],
                'high': kline[3],
                'low': kline[4],
                'volume': kline[5] if len(kline) > 5 else 0
            }
            rows.append(row)

        # 转换为 DataFrame
        df = pd.DataFrame(rows)

        # 转换数据类型
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['low'] = pd.to_numeric(df['low'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        df['date'] = pd.to_datetime(df['date'])

        # 设置日期为索引
        df.set_index('date', inplace=True)

        # 重命名列以匹配标准格式
        df.rename(columns={
            'open': 'Open',
            'close': 'Close',
            'high': 'High',
            'low': 'Low',
            'volume': 'Volume'
        }, inplace=True)

        # 按日期排序
        df.sort_index(inplace=True)

        print()
        print(f"[OK] 成功获取 {len(df)} 条记录")
        print(f"     时间范围：{df.index[0].strftime('%Y-%m-%d')} 至 {df.index[-1].strftime('%Y-%m-%d')}")

        return df

    except requests.exceptions.RequestException as e:
        print(f"[ERROR] 网络请求错误：{e}")
        return None
    except Exception as e:
        print(f"[ERROR] 获取数据时发生错误：{e}")
        return None


def get_stock_info_tencent(stock_code):
    """
    使用腾讯财经获取股票基本信息

    Args:
        stock_code (str): 股票代码

    Returns:
        dict: 股票基本信息
    """
    # 港股代码格式
    ticker_symbol = f"hk{stock_code}"

    print(f"[INFO] 正在获取 {ticker_symbol} 的基本信息...")

    try:
        # 腾讯财经实时行情 API
        url = f"http://qt.gtimg.cn/q={ticker_symbol}"

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        # 返回的是 JavaScript 格式的字符串，需要解析
        # 尝试不同的编码方式
        try:
            content = response.content.decode('gb2312')
        except:
            try:
                content = response.content.decode('gbk')
            except:
                content = response.content.decode('utf-8', errors='ignore')

        # 解析数据 (格式：v_hk03633="51~软体饮...~")
        if '~' not in content:
            print("[ERROR] 数据格式异常")
            return None

        parts = content.split('~')

        if len(parts) < 50:
            print("⚠️  数据不完整")
            return None

        # 提取关键信息
        info = {
            'name': parts[1] if len(parts) > 1 else 'N/A',
            'code': ticker_symbol,
            'current_price': float(parts[3]) if len(parts) > 3 and parts[3] else None,
            'close_price': float(parts[4]) if len(parts) > 4 and parts[4] else None,
            'open_price': float(parts[5]) if len(parts) > 5 and parts[5] else None,
            'high': float(parts[33]) if len(parts) > 33 and parts[33] else None,
            'low': float(parts[34]) if len(parts) > 34 and parts[34] else None,
            'volume': float(parts[6]) if len(parts) > 6 and parts[6] else None,
            'market_cap': float(parts[43]) if len(parts) > 43 and parts[43] else None,
            'pe_ratio': float(parts[39]) if len(parts) > 39 and parts[39] else None,
            '52_week_high': float(parts[47]) if len(parts) > 47 and parts[47] else None,
            '52_week_low': float(parts[48]) if len(parts) > 48 and parts[48] else None,
        }

        print()
        print("[INFO] 基本信息")
        print("-" * 70)
        print(f"  股票名称    {info['name']}")
        print(f"  股票代码    {info['code']}")
        print(f"  当前价格    {info['current_price']:.2f} HKD")
        print(f"  昨收价      {info['close_price']:.2f} HKD")
        print(f"  今开价      {info['open_price']:.2f} HKD")
        print(f"  最高价      {info['high']:.2f} HKD")
        print(f"  最低价      {info['low']:.2f} HKD")
        print(f"  成交量      {int(info['volume']):,}" if info['volume'] else "  成交量      N/A")
        print(f"  市盈率      {info['pe_ratio']:.2f}" if info['pe_ratio'] else "  市盈率      N/A")
        print(f"  52周最高    {info['52_week_high']:.2f} HKD" if info['52_week_high'] else "  52周最高    N/A")
        print(f"  52周最低    {info['52_week_low']:.2f} HKD" if info['52_week_low'] else "  52周最低    N/A")

        return info

    except requests.exceptions.RequestException as e:
        print(f"[ERROR] 网络请求错误：{e}")
        return None
    except Exception as e:
        print(f"[ERROR] 获取基本信息时发生错误：{e}")
        return None


def display_stock_data(hist):
    """
    显示股票数据摘要

    Args:
        hist (DataFrame): 股票历史数据
    """
    if hist is None or hist.empty:
        print("[ERROR] 没有数据可显示")
        return

    print("[INFO] 数据预览（最近 5 行）")
    print("-" * 70)
    print(hist.tail(5).to_string())

    print()
    print("[INFO] 数据统计信息")
    print("-" * 70)
    print(f"总记录数：{len(hist)} 条")
    print(f"时间范围：{hist.index[0].strftime('%Y-%m-%d')} 至 {hist.index[-1].strftime('%Y-%m-%d')}")
    print()
    print(hist.describe().to_string())

    print()
    print("[INFO] 列说明")
    print("-" * 70)
    print("  Open   - 开盘价")
    print("  Close  - 收盘价")
    print("  High   - 最高价")
    print("  Low    - 最低价")
    print("  Volume - 成交量")


def plot_kline_ascii(hist, stock_code, months=12):
    """
    绘制 ASCII K 线图 (文本格式)

    Args:
        hist (DataFrame): 股票历史数据
        stock_code (str): 股票代码
        months (int): 显示过去多少个月的数据，默认 12 个月（一年）
    """
    if hist is None or hist.empty:
        print("没有数据可显示")
        return

    # 计算一年前的日期
    cutoff_date = datetime.now() - timedelta(days=365)

    # 过滤数据 - 只显示过去一年的数据
    hist_one_year = hist[hist.index >= cutoff_date]

    if hist_one_year.empty:
        print("没有一年内的数据")
        return

    # 取最后 120 个交易日以便显示
    if len(hist_one_year) > 120:
        hist_display = hist_one_year.tail(120)
    else:
        hist_display = hist_one_year

    print(f"\n╔════════════════════════════════════════════════════════════════╗")
    print(f"║ 港股 {stock_code} 最近 {len(hist_display)} 个交易日 K 线图      ║")
    print(f"║ 时间范围: {hist_display.index[0].strftime('%Y-%m-%d')} 至 {hist_display.index[-1].strftime('%Y-%m-%d')}  ║")
    print(f"╚════════════════════════════════════════════════════════════════╝\n")

    # 计算最高和最低价格（用于缩放）
    all_prices = pd.concat([hist_display['High'], hist_display['Low']])
    max_price = all_prices.max()
    min_price = all_prices.min()
    price_range = max_price - min_price

    # 设置图表高度
    chart_height = 30
    chart_width = len(hist_display)

    # 创建网格
    grid = [[' ' for _ in range(chart_width)] for _ in range(chart_height)]

    # 绘制 K 线
    for idx, (date, row) in enumerate(hist_display.iterrows()):
        open_price = row['Open']
        close_price = row['Close']
        high_price = row['High']
        low_price = row['Low']

        # 计算行号（从上到下）
        high_row = int((max_price - high_price) / price_range * (chart_height - 1)) if price_range > 0 else 0
        low_row = int((max_price - low_price) / price_range * (chart_height - 1)) if price_range > 0 else chart_height - 1
        open_row = int((max_price - open_price) / price_range * (chart_height - 1)) if price_range > 0 else 0
        close_row = int((max_price - close_price) / price_range * (chart_height - 1)) if price_range > 0 else 0

        # 确定颜色符号
        if close_price >= open_price:
            wick_char = '┃'  # 上涨
            body_char = '█'
        else:
            wick_char = '┃'  # 下跌
            body_char = '▒'

        # 绘制影线
        for row_idx in range(min(high_row, low_row), max(high_row, low_row) + 1):
            if 0 <= row_idx < chart_height:
                grid[row_idx][idx] = wick_char

        # 绘制实体
        for row_idx in range(min(open_row, close_row), max(open_row, close_row) + 1):
            if 0 <= row_idx < chart_height:
                grid[row_idx][idx] = body_char

    # 打印网格
    for row_idx, row_data in enumerate(grid):
        # 计算对应的价格
        price = max_price - (row_idx / (chart_height - 1) * price_range) if chart_height > 1 else max_price
        price_str = f"{price:6.2f}"
        line = ''.join(row_data)
        print(f"{price_str} │{line}│")

    # 打印底部分隔线和日期轴
    print("       ├" + "─" * chart_width + "┤")

    # 打印日期标签（每 10 个交易日显示一次）
    date_labels = ""
    for idx in range(0, chart_width, max(1, chart_width // 10)):
        label = hist_display.index[idx].strftime('%m-%d')
        date_labels += label.ljust(max(1, chart_width // 10))
    print("       │" + date_labels[:chart_width] + "│")
    print("\n图例：█ 上升日 | ▒ 下跌日\n")


def plot_kline_chart(hist, stock_code, output_dir="./output", months=12):
    """
    绘制 K 线图 (蜡烛图) - 包含上影线和下影线

    Args:
        hist (DataFrame): 股票历史数据
        stock_code (str): 股票代码
        output_dir (str): 输出目录
        months (int): 显示过去多少个月的数据，默认 12 个月（一年）

    Returns:
        str: 图表文件路径，失败返回 None
    """
    if not HAS_MATPLOTLIB:
        # 如果没有 matplotlib，使用 ASCII 版本
        plot_kline_ascii(hist, stock_code, months)
        return None

    if hist is None or hist.empty:
        print("[ERROR] 没有数据可显示")
        return None

    # 计算一年前的日期
    cutoff_date = datetime.now() - timedelta(days=365)

    # 过滤数据 - 只显示过去一年的数据
    hist_one_year = hist[hist.index >= cutoff_date]

    if hist_one_year.empty:
        print("[ERROR] 没有一年内的数据")
        return None

    print(f"[INFO] 正在绘制 {len(hist_one_year)} 条记录的 K 线图...")

    # 创建图表
    fig, ax = plt.subplots(figsize=(16, 8))

    # 设置中文字体
    plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    width = 0.5  # K 线宽度

    for idx, (date, row) in enumerate(hist_one_year.iterrows()):
        open_price = row['Open']
        close_price = row['Close']
        high_price = row['High']
        low_price = row['Low']

        # 确定颜色 - 红色表示上升，绿色表示下跌
        if close_price >= open_price:
            color = 'red'
            body_bottom = open_price
            body_top = close_price
        else:
            color = 'green'
            body_bottom = close_price
            body_top = open_price

        # 绘制影线（高-低）- 上影线和下影线
        ax.plot([idx, idx], [low_price, high_price], color=color, linewidth=1.5,
                solid_capstyle='round', alpha=0.8)

        # 绘制实体（开盘到收盘）- 矩形
        height = body_top - body_bottom
        rectangle = Rectangle((idx - width/2, body_bottom), width, height,
                            facecolor=color, edgecolor=color, linewidth=0.8, alpha=0.9)
        ax.add_patch(rectangle)

    # 设置 X 轴范围
    ax.set_xlim(-1, len(hist_one_year))

    # 自动计算 Y 轴范围，添加边距
    all_prices = pd.concat([hist_one_year['High'], hist_one_year['Low']])
    price_min = all_prices.min()
    price_max = all_prices.max()
    price_range = price_max - price_min
    margin = price_range * 0.1
    ax.set_ylim(price_min - margin, price_max + margin)

    # 设置 Y 轴标签为价格
    ax.set_ylabel('价格 (HKD)', fontsize=12, fontweight='bold')
    ax.set_xlabel('日期', fontsize=12, fontweight='bold')

    # 设置时间轴标签 - 显示日期
    num_labels = min(15, len(hist_one_year))
    step = max(1, len(hist_one_year) // num_labels)
    x_ticks = range(0, len(hist_one_year), step)
    x_labels = [hist_one_year.index[i].strftime('%Y-%m-%d') for i in x_ticks]
    ax.set_xticks(x_ticks)
    ax.set_xticklabels(x_labels, rotation=45, ha='right')

    # 设置标题
    price_change = hist_one_year['Close'].iloc[-1] - hist_one_year['Close'].iloc[0]
    pct_change = (price_change / hist_one_year['Close'].iloc[0]) * 100

    ax.set_title(f'港股 {stock_code} 一年 K 线图\n{hist_one_year.index[0].strftime("%Y-%m-%d")} 至 {hist_one_year.index[-1].strftime("%Y-%m-%d")} | 涨跌：{price_change:+.2f} ({pct_change:+.1f}%)',
                 fontsize=14, fontweight='bold', pad=20)

    # 添加网格
    ax.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)

    # 添加图例
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor='red', edgecolor='red', label='上升日（收 > 开）'),
        Patch(facecolor='green', edgecolor='green', label='下跌日（收 < 开）')
    ]
    ax.legend(handles=legend_elements, loc='upper left', fontsize=10)

    # 调整布局
    plt.tight_layout()

    # 保存图表
    try:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chart_filename = f"kline_chart_{stock_code}_{timestamp}.png"
        chart_filepath = os.path.join(output_dir, chart_filename)

        plt.savefig(chart_filepath, dpi=100, bbox_inches='tight')
        print(f"✅ K 线图已保存到：{chart_filepath}")

        plt.close()
        return chart_filepath

    except Exception as e:
        print(f"❌ 保存图表时发生错误：{e}")
        plt.close()
        return None


def save_to_json(data, stock_code, output_dir="./output"):
    """
    将数据保存到 JSON 文件

    Args:
        data (DataFrame): 股票数据
        stock_code (str): 股票代码
        output_dir (str): 输出目录

    Returns:
        str: 保存的文件路径，失败返回 None
    """
    if data is None or data.empty:
        print("❌ 没有数据可保存")
        return None

    try:
        # 确保目录存在
        os.makedirs(output_dir, exist_ok=True)

        # 创建文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"hk_stock_{stock_code}_{timestamp}.json"
        filepath = os.path.join(output_dir, filename)

        # 重置索引以便保存日期
        data_reset = data.reset_index()
        data_reset['date'] = data_reset['date'].dt.strftime('%Y-%m-%d')

        # 转换为字典
        data_dict = {
            'stock_code': stock_code,
            'record_count': len(data),
            'date_range': f"{data_reset['date'].iloc[0]} to {data_reset['date'].iloc[-1]}",
            'update_time': datetime.now().isoformat(),
            'data': data_reset.to_dict('records')
        }

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data_dict, f, ensure_ascii=False, indent=2)

        print(f"✅ 数据已保存到：{filepath} ({len(data)} 条记录)")
        return filepath

    except Exception as e:
        print(f"❌ 保存文件时发生错误：{e}")
        return None


def main():
    """主函数 - 分模块执行"""

    # 配置
    stock_code = "03633"
    output_dir = "./output"

    # ========== 初始化模块 ==========
    print("=" * 70)
    print("[INIT] 港股数据获取工具 - 腾讯财经 API")
    print("=" * 70)
    print()

    # 设置输出目录
    output_path = setup_output_dir(output_dir)
    if output_path is None:
        print("[ERROR] 无法创建输出目录，程序中止")
        return

    # ========== 下载数据模块 ==========
    print("=" * 70)
    print("[MODULE] 下载数据")
    print("=" * 70)

    # 获取基本信息
    print()
    stock_info = get_stock_info_tencent(stock_code)

    print()
    # 获取历史数据
    hist_data = get_hk_stock_data_tencent(stock_code)

    if hist_data is None or hist_data.empty:
        print("[ERROR] 未能获取数据，程序中止")
        return
    print()

    # ========== 显示数据模块 ==========
    print("=" * 70)
    print("[MODULE] 显示数据")
    print("=" * 70)
    print()

    display_stock_data(hist_data)
    print()

    # ========== 保存数据模块 ==========
    print("=" * 70)
    print("[MODULE] 保存数据")
    print("=" * 70)
    print()

    # 保存到 JSON 文件
    json_path = save_to_json(hist_data, stock_code, output_path)
    print()

    # ========== 绘制图表模块 ==========
    print("=" * 70)
    print("[MODULE] 绘制图表")
    print("=" * 70)
    print()

    # 绘制一年 K 线图
    chart_path = plot_kline_chart(hist_data, stock_code, output_path, months=12)
    print()

    # ========== 总结模块 ==========
    print("=" * 70)
    print("[DONE] 处理完成")
    print("=" * 70)

    print()
    print(f"[OUTPUT] 输出目录：{output_path}")
    if json_path:
        print(f"[FILE] 数据文件：{os.path.basename(json_path)}")
    if chart_path:
        print(f"[CHART] 图表文件：{os.path.basename(chart_path)}")

    print()
    print(f"[TIME] 完成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


if __name__ == "__main__":
    main()
