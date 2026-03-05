#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据保存模块 - 支持保存到 JSON 文件和 SQLite 数据库
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
        将数据保存到 SQLite 数据库（支持增量更新）

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

    def verify_db_data(self, stock_code):
        """
        验证指定股票的数据是否成功保存到数据库

        Args:
            stock_code (str): 股票代码

        Returns:
            dict: 验证结果，包含是否存在、记录数、日期范围等信息
        """
        try:
            # 检查股票信息
            stock_info = self.db_manager.get_stock_info(stock_code)

            # 获取统计信息
            stats = self.db_manager.get_statistics(stock_code)

            if stats is None:
                return {
                    'stock_code': stock_code,
                    'exists': False,
                    'total_records': 0,
                    'date_range': None,
                    'has_stock_info': False
                }

            total_records = stats['total_records']
            is_valid = total_records > 0

            return {
                'stock_code': stock_code,
                'exists': is_valid,
                'total_records': total_records,
                'date_range': stats['date_range'] if is_valid else None,
                'has_stock_info': stock_info is not None,
                'db_file_size': stats['db_file_size'],
                'db_path': stats['db_path']
            }

        except Exception as e:
            print(f"[ERROR] 验证数据错误: {e}")
            return {
                'stock_code': stock_code,
                'exists': False,
                'error': str(e)
            }

    def batch_verify_data(self, stock_codes):
        """
        批量验证多个股票的数据是否成功保存到数据库

        Args:
            stock_codes (list): 股票代码列表

        Returns:
            dict: 验证汇总结果
        """
        print("[INFO] 开始批量验证数据...")

        verification_results = []
        total_records = 0
        all_exist = True

        for stock_code in stock_codes:
            result = self.verify_db_data(stock_code)
            verification_results.append(result)

            if result['exists']:
                total_records += result['total_records']
            else:
                all_exist = False

        # 生成汇总报告
        summary = {
            'total_stocks': len(stock_codes),
            'verified_stocks': sum(1 for r in verification_results if r['exists']),
            'failed_stocks': sum(1 for r in verification_results if not r['exists']),
            'total_records': total_records,
            'all_verified': all_exist,
            'details': verification_results
        }

        return summary

    def print_verification_report(self, verification_result):
        """
        打印验证报告

        Args:
            verification_result (dict): 验证结果
        """
        print("\n" + "="*80)
        print("[VERIFY] 数据验证报告")
        print("="*80)

        if 'details' in verification_result:
            # 批量验证报告
            summary = verification_result
            print(f"\n[SUMMARY] 批量验证汇总")
            print(f"  总股票数：{summary['total_stocks']} 只")
            print(f"  验证成功：{summary['verified_stocks']} 只")
            print(f"  验证失败：{summary['failed_stocks']} 只")
            print(f"  总记录数：{summary['total_records']} 条")
            status_text = "[OK] 全部验证成功" if summary['all_verified'] else "[WARN] 部分验证失败"
            print(f"  验证状态：{status_text}")

            print(f"\n[DETAILS] 各股票详情")
            for detail in summary['details']:
                status = "[OK]" if detail['exists'] else "[FAIL]"
                stock_code = detail['stock_code']

                if detail['exists']:
                    date_range = detail['date_range']
                    records = detail['total_records']
                    print(f"  {status} {stock_code}: {records} 条记录，时间范围: {date_range[0]} 至 {date_range[1]}")
                else:
                    error = detail.get('error', '数据不存在')
                    print(f"  {status} {stock_code}: 验证失败 - {error}")
        else:
            # 单个验证报告
            stock_code = verification_result['stock_code']
            exists = verification_result['exists']

            print(f"\n[STOCK] 股票代码：{stock_code}")
            status_text = "[OK] 数据已保存" if exists else "[FAIL] 数据未保存"
            print(f"[STATUS] {status_text}")

            if exists:
                print(f"[DATA] 记录数：{verification_result['total_records']} 条")
                date_range = verification_result['date_range']
                print(f"[DATE] 时间范围：{date_range[0]} 至 {date_range[1]}")
                info_text = "已保存" if verification_result['has_stock_info'] else "未保存"
                print(f"[INFO] 股票信息：{info_text}")
                print(f"[SIZE] 数据库大小：{verification_result['db_file_size']}")

        print("="*80 + "\n")
