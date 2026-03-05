#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
工具函数模块 - 提供通用辅助函数
"""

import os
from datetime import datetime


def setup_output_dir(output_dir):
    """
    设置并创建输出目录
    
    Args:
        output_dir (str): 输出目录路径
        
    Returns:
        str: 输出目录绝对路径，创建失败返回 None
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        abs_path = os.path.abspath(output_dir)
        print(f"[OK] 输出目录已设置：{abs_path}")
        return abs_path
    except Exception as e:
        print(f"[ERROR] 创建输出目录失败：{e}")
        return None


def print_section(title):
    """
    打印分隔线和标题
    
    Args:
        title (str): 标题文本
    """
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def print_summary(output_path, json_path=None, chart_path=None):
    """
    打印总结信息
    
    Args:
        output_path (str): 输出目录
        json_path (str): JSON 文件路径
        chart_path (str): 图表文件路径
    """
    print()
    print_section("[SUMMARY] 完成")
    
    if output_path:
        print(f"[INFO] 输出目录：{output_path}")
    
    if json_path:
        print(f"       JSON 文件：{os.path.basename(json_path)}")
    
    if chart_path:
        print(f"       K 线图表：{os.path.basename(chart_path)}")
    
    print()
    print("[OK] 程序执行完成!")
