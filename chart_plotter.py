#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
图表绘制模块 - 绘制 K 线图
"""

import os
from datetime import datetime, timedelta
import pandas as pd

try:
    import matplotlib
    matplotlib.use('Agg')  # 使用非交互式后端
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Patch
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


class KLineChartPlotter:
    """绘制 K 线蜡烛图"""

    def __init__(self, stock_code):
        """
        初始化股票代码

        Args:
            stock_code (str): 股票代码
        """
        self.stock_code = stock_code

    def plot(self, hist, output_dir="./output", months=12):
        """
        绘制 K 线图 (蜡烛图) - 包含上影线和下影线

        Args:
            hist (DataFrame): 股票历史数据
            output_dir (str): 输出目录
            months (int): 显示过去多少个月的数据，默认 12 个月（一年）

        Returns:
            str: 图表文件路径，失败返回 None
        """
        if not HAS_MATPLOTLIB:
            print("[WARNING] matplotlib 未安装，跳过 K 线图绘制")
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

        try:
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

            ax.set_title(f'港股 {self.stock_code} 一年 K 线图\n{hist_one_year.index[0].strftime("%Y-%m-%d")} 至 {hist_one_year.index[-1].strftime("%Y-%m-%d")} | 涨跌：{price_change:+.2f} ({pct_change:+.1f}%)',
                         fontsize=14, fontweight='bold', pad=20)

            # 添加网格
            ax.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)

            # 添加图例
            legend_elements = [
                Patch(facecolor='red', edgecolor='red', label='上升日（收 > 开）'),
                Patch(facecolor='green', edgecolor='green', label='下跌日（收 < 开）')
            ]
            ax.legend(handles=legend_elements, loc='upper left', fontsize=10)

            # 调整布局
            plt.tight_layout()

            # 保存图表
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            chart_filename = f"kline_chart_{self.stock_code}_{timestamp}.png"
            chart_filepath = os.path.join(output_dir, chart_filename)

            plt.savefig(chart_filepath, dpi=100, bbox_inches='tight')
            print(f"[OK] K 线图已保存到：{chart_filepath}")

            plt.close()
            return chart_filepath

        except Exception as e:
            print(f"[ERROR] 保存图表时发生错误：{e}")
            plt.close()
            return None
