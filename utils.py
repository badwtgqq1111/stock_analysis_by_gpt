#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
工具函数模块 - 提供通用辅助功能
"""

import os
import shutil
from datetime import datetime


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


def print_section(title):
    """
    打印分节标题

    Args:
        title (str): 标题文本
    """
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def print_summary(output_path, json_path=None, chart_path=None):
    """
    打印总结信息

    Args:
        output_path (str): 输出目录路径
        json_path (str): JSON 文件路径（可选）
        chart_path (str): 图表文件路径（可选）
    """
    print()
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
