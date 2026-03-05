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

            # 解析数据 (格式：v_hk03633="51~软体饮...")
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
            print(f"[INFO] 从数据库加载数据：{len(data)} 条记录")
            self.data = data

        return data


class HKMarketListFetcher:
    """获取港股全市场股票列表"""

    def __init__(self):
        """初始化港股市场列表获取器"""
        self.stocks = []

    def fetch(self):
        """
        获取港股全市场股票列表

        Returns:
            list: 股票代码列表，失败返回空列表
        """
        print("[INFO] 正在获取港股全市场股票列表...")

        try:
            # 腾讯财经港股板块列表 API
            # 获取主板股票（代码范围 00001-99999）
            url = "http://qt.gtimg.cn/q=hk00001,hk00002,hk00003,hk00004,hk00005"

            # 使用更高效的方法：获取恒生指数成分股 + 主板股票
            # 方法 1：通过恒生指数成分股获取
            hang_seng_url = "http://qt.gtimg.cn/q=sh000001"

            # 更好的方法：直接获取港股板块
            # 使用新浪或腾讯的板块接口
            hk_stocks_url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=1&num=10000&sort=symbol&asc=1&node=hk&symbol="

            response = requests.get(hk_stocks_url, timeout=15)
            response.raise_for_status()

            stocks_data = response.json()

            if not stocks_data:
                print("[WARNING] 未能获取港股列表，使用备用方案...")
                # 备用方案：使用已知的港股代码范围
                return self._fetch_alternative()

            # 提取股票代码
            for stock in stocks_data:
                code = stock.get('code', '')
                name = stock.get('name', '')

                # 过滤出港股（通常是 5 位数字）
                if code and len(code) == 5 and code.isdigit():
                    self.stocks.append({
                        'code': code,
                        'name': name
                    })

            print(f"[OK] 成功获取 {len(self.stocks)} 只港股")
            return self.stocks

        except Exception as e:
            print(f"[ERROR] 获取港股列表失败：{e}")
            return self._fetch_alternative()

    def _fetch_alternative(self):
        """
        备用方案：多线程并发扫描全市场港股列表

        策略：
        1. 使用多线程并发扫描 00001-09999
        2. 分批请求以避免过度连接
        3. 自动去重和排序

        Returns:
            list: 股票代码列表
        """
        print("[INFO] 使用多线程并发方案扫描全市场港股列表...")
        print("[INFO] 扫描范围：00001-09999，使用多线程加速...")

        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        stock_codes = []
        lock = threading.Lock()  # 线程锁，保护 stock_codes 列表

        # 初始化数据库管理器
        from db_manager import DatabaseManager
        db_manager = DatabaseManager()
        
        # 获取已扫描的股票列表
        scanned_stocks = db_manager.get_scanned_stocks('active')
        scanned_codes = {stock['code'] for stock in scanned_stocks}
        
        print(f"[INFO] 数据库中已有 {len(scanned_codes)} 只已扫描股票，将跳过...")

        # 统计信息
        stats = {
            'tested': 0,
            'found': 0,
            'skipped': len(scanned_codes),
            'lock': threading.Lock()
        }

        def query_stock(code_num):
            """查询单个股票，返回股票信息或None"""
            try:
                code = str(code_num).zfill(5)
                
                # 如果已扫描过，跳过
                if code in scanned_codes:
                    return None
                
                ticker = f"hk{code}"
                url = f"http://qt.gtimg.cn/q={ticker}"

                response = requests.get(url, timeout=2)

                if response.status_code == 200 and '~' in response.text:
                    try:
                        content = response.content.decode('gb2312')
                    except:
                        content = response.text

                    parts = content.split('~')

                    # 检查是否有有效的股票名称
                    if len(parts) > 1 and parts[1] and parts[1] != 'N/A':
                        name = parts[1].strip()
                        if name:
                            # 立即保存到数据库
                            db_manager.save_scanned_stock(code, name, 'active')
                            return {
                                'code': code,
                                'name': name
                            }

            except requests.exceptions.Timeout:
                pass
            except Exception:
                pass

            return None

        def worker_batch(code_range):
            """线程工作函数：处理一批股票代码"""
            local_stocks = []

            for code_num in code_range:
                stock = query_stock(code_num)

                if stock:
                    local_stocks.append(stock)

                    # 更新统计信息
                    with stats['lock']:
                        stats['found'] += 1
                        if stats['found'] % 50 == 0:
                            print(f"[PROGRESS] 已发现 {stats['found']} 只港股 (已扫描 {stats['tested']} 个代码)...")

                with stats['lock']:
                    stats['tested'] += 1
                    if stats['tested'] % 1000 == 0:
                        print(f"[MILESTONE] 已扫描 {stats['tested']}/9999 个代码，已跳过 {stats['skipped']} 只已扫描股票...")

            return local_stocks

        # 使用多线程并发扫描
        # 将 00001-09999 分成 20 个批次，每个批次 500 个代码
        num_threads = 20
        batch_size = 500
        total_codes = 9999

        print(f"[INFO] 使用 {num_threads} 个线程，每线程处理 {batch_size} 个代码...")

        all_stocks = []
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = []

            # 提交任务
            for i in range(0, total_codes, batch_size):
                batch_start = i + 1
                batch_end = min(i + batch_size, total_codes)
                code_range = range(batch_start, batch_end + 1)

                future = executor.submit(worker_batch, code_range)
                futures.append(future)

            # 收集结果
            for future in as_completed(futures):
                try:
                    batch_stocks = future.result()
                    all_stocks.extend(batch_stocks)
                except Exception as e:
                    print(f"[ERROR] 线程异常：{e}")

        # 合并已扫描的股票
        all_stocks.extend(scanned_stocks)
        
        # 去重和排序
        seen = set()
        unique_stocks = []
        for stock in all_stocks:
            if stock['code'] not in seen:
                seen.add(stock['code'])
                unique_stocks.append(stock)
        
        self.stocks = sorted(unique_stocks, key=lambda x: x['code'])

        print()
        print(f"[OK] 扫描完成！共发现 {len(self.stocks)} 只港股")
        print(f"     新发现：{stats['found']} 只")
        print(f"     已存在：{stats['skipped']} 只")
        print(f"     总扫描：{stats['tested']} 个代码")
        print(f"     发现率：{(stats['found']/stats['tested']*100):.2f}%")
        
        # 关闭数据库连接
        db_manager.close()
        
        return self.stocks

    def save_to_db(self, db_manager):
        """
        将股票列表保存到数据库

        Args:
            db_manager: 数据库管理器实例

        Returns:
            bool: 是否成功
        """
        if not self.stocks:
            return False

        try:
            for stock in self.stocks:
                # 创建简单的股票信息记录
                stock_info = {
                    'name': stock['name'],
                    'code': f"hk{stock['code']}",
                    'current_price': None,
                    'close_price': None,
                    'open_price': None,
                    'high': None,
                    'low': None,
                    'volume': None,
                    'market_cap': None,
                    'pe_ratio': None,
                    '52_week_high': None,
                    '52_week_low': None,
                }

                db_manager.save_stock_info(stock_info, stock['code'])

            print(f"[OK] 已将 {len(self.stocks)} 只股票信息保存到数据库")
            return True

        except Exception as e:
            print(f"[ERROR] 保存股票列表到数据库失败：{e}")
            return False

    def get_stocks(self):
        """获取股票列表"""
        return self.stocks

