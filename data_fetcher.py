#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据获取模块 - 从腾讯财经 API 获取港股数据，支持增量更新
"""

import requests
import pandas as pd
from datetime import datetime
from db_manager import DatabaseManager


class StockInfoFetcher:
    """获取股票基本信息"""

    def __init__(self, stock_code):
        """
        初始化股票代码

        Args:
            stock_code (str): 股票代码，如 '03633'
        """
        self.stock_code = stock_code
        self.ticker_symbol = f"hk{stock_code}"
        self.info = None

    def fetch(self):
        """
        获取股票基本信息

        Returns:
            dict: 股票基本信息，失败返回 None
        """
        print(f"[INFO] 正在获取 {self.ticker_symbol} 的基本信息...")

        try:
            # 腾讯财经实时行情 API
            url = f"http://qt.gtimg.cn/q={self.ticker_symbol}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()

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
                print("[WARNING] 数据不完整")
                return None

            # 提取关键信息
            self.info = {
                'name': parts[1] if len(parts) > 1 else 'N/A',
                'code': self.ticker_symbol,
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

            return self.info

        except requests.exceptions.RequestException as e:
            print(f"[ERROR] 网络请求错误：{e}")
            return None
        except Exception as e:
            print(f"[ERROR] 获取基本信息时发生错误：{e}")
            return None

    def get_info(self):
        """获取缓存的信息"""
        return self.info


class HistoryDataFetcher:
    """获取股票历史数据，支持增量更新"""

    def __init__(self, stock_code, db_dir="./assets"):
        """
        初始化股票代码

        Args:
            stock_code (str): 股票代码，如 '03633'
            db_dir (str): 数据库目录
        """
        self.stock_code = stock_code
        self.ticker_symbol = f"hk{stock_code}"
        self.data = None
        self.db_manager = DatabaseManager(db_dir)

    def fetch(self):
        """
        获取股票历史数据

        Returns:
            DataFrame: 股票历史数据，失败返回 None
        """
        print(f"[INFO] 正在从腾讯财经获取 {self.ticker_symbol} 的股票数据...")

        try:
            # 腾讯财经历史数据 API
            url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={self.ticker_symbol},day,,,1000,qfq"

            response = requests.get(url, timeout=10)
            response.raise_for_status()

            # 解析 JSON 数据
            data = response.json()

            # 检查数据是否存在
            if 'data' not in data or self.ticker_symbol not in data['data']:
                print(f"[ERROR] 未找到 {self.ticker_symbol} 的数据")
                return None

            stock_data = data['data'][self.ticker_symbol]

            # 获取日线数据 (day)
            if 'day' not in stock_data:
                print(f"[ERROR] 无 {self.ticker_symbol} 的日线数据")
                return None

            klines = stock_data['day']

            if not klines:
                print(f"[ERROR] 未获取到 {self.ticker_symbol} 的数据")
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

            self.data = df
            return df

        except requests.exceptions.RequestException as e:
            print(f"[ERROR] 网络请求错误：{e}")
            return None
        except Exception as e:
            print(f"[ERROR] 获取数据时发生错误：{e}")
            return None

    def get_data(self):
        """获取缓存的数据"""
        return self.data

    def check_update_from_db(self):
        """
        检查数据库中是否已有数据，用于增量更新判断

        Returns:
            dict: 包含最新日期、现有记录数等信息
        """
        latest_date = self.db_manager.get_latest_date(self.stock_code)

        if latest_date:
            # 从数据库获取存储的数据
            db_data = self.db_manager.get_kline_data(self.stock_code)
            stats = self.db_manager.get_statistics(self.stock_code)

            return {
                'has_data': True,
                'latest_date': latest_date,
                'total_records': stats['total_records'] if stats else 0,
                'date_range': stats['date_range'] if stats else None
            }
        else:
            return {
                'has_data': False,
                'latest_date': None,
                'total_records': 0,
                'date_range': None
            }

    def load_from_db(self):
        """
        从数据库加载已保存的 K 线数据

        Returns:
            DataFrame: K 线数据，无数据返回 None
        """
        data = self.db_manager.get_kline_data(self.stock_code)

        if data is not None and not data.empty:
            print(f"[INFO] 从数据库加载数据: {len(data)} 条记录")
            self.data = data

        return data
