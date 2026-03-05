#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据保存模块 - 支持保存到 JSON 文件和 DuckDB 数据库（列式存储，高性能 OLAP）
"""

import json
import os
from datetime import datetime
from db_manager import DatabaseManager


class DataSaver:
    """保存股票数据到多个存储介质"""

    def __init__(self, db_dir="./assets"):
        """
        初始化数据保存器

        Args:
            db_dir (str): 数据库目录
        """
        self.db_manager = DatabaseManager(db_dir)

    @staticmethod
    def save_json(data, stock_code, output_dir="./output"):
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
            print("[ERROR] 没有数据可保存")
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
                'source': 'json_file',
                'data': data_reset.to_dict('records')
            }

            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)

            print(f"[OK] JSON 文件已保存：{filepath} ({len(data)} 条)")
            return filepath

        except Exception as e:
            print(f"[ERROR] 保存 JSON 文件错误：{e}")
            return None

    def save_to_db(self, data, stock_code):
        """
        将数据保存到 DuckDB 数据库（支持增量更新，列式存储）

        Args:
            data (DataFrame): K 线数据
            stock_code (str): 股票代码

        Returns:
            dict: 保存统计信息，包含新增、更新记录数
        """
        if data is None or data.empty:
            print("[ERROR] 没有数据可保存到数据库")
            return None

        return self.db_manager.save_kline_data(data, stock_code)

    def save_stock_info_to_db(self, stock_info, stock_code):
        """
        将股票基本信息保存到数据库

        Args:
            stock_info (dict): 股票基本信息
            stock_code (str): 股票代码

        Returns:
            bool: 是否保存成功
        """
        if not stock_info:
            return False

        return self.db_manager.save_stock_info(stock_info, stock_code)

    def export_from_db(self, stock_code, output_dir="./output"):
        """
        从数据库导出数据为 JSON

        Args:
            stock_code (str): 股票代码
            output_dir (str): 输出目录

        Returns:
            str: 导出文件路径
        """
        return self.db_manager.export_to_json(stock_code, output_dir)

    def get_db_statistics(self, stock_code):
        """
        获取数据库统计信息

        Args:
            stock_code (str): 股票代码

        Returns:
            dict: 统计信息
        """
        return self.db_manager.get_statistics(stock_code)
