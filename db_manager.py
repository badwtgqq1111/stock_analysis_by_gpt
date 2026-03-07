#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据库管理模块 - 使用 DuckDB 实现高性能 OLAP 数据库
支持增量更新，保存在 assets 目录
"""

import duckdb
import os
import pandas as pd
from datetime import datetime
from pathlib import Path


class DatabaseManager:
    """DuckDB 数据库管理器 - 列式存储，高性能 OLAP"""

    def __init__(self, db_dir="./assets"):
        """
        初始化数据库管理器

        Args:
            db_dir (str): 数据库存储目录
        """
        self.db_dir = db_dir
        os.makedirs(db_dir, exist_ok=True)
        self.db_path = os.path.join(db_dir, "stock_data.duckdb")
        self.conn = None
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        try:
            # 连接到 DuckDB 数据库
            self.conn = duckdb.connect(self.db_path)

            # 启用扩展
            self.conn.execute("INSTALL json")
            self.conn.execute("LOAD json")

            # 创建股票基本信息表
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_info (
                    stock_code VARCHAR PRIMARY KEY NOT NULL,
                    name VARCHAR,
                    current_price DOUBLE,
                    close_price DOUBLE,
                    open_price DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    volume DOUBLE,
                    market_cap DOUBLE,
                    pe_ratio DOUBLE,
                    week_52_high DOUBLE,
                    week_52_low DOUBLE,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 创建索引以提高查询性能
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS kline_data (
                    stock_code VARCHAR NOT NULL,
                    date DATE NOT NULL,
                    open DOUBLE NOT NULL,
                    close DOUBLE NOT NULL,
                    high DOUBLE NOT NULL,
                    low DOUBLE NOT NULL,
                    volume DOUBLE NOT NULL,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (stock_code, date),
                    FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
                )
            """)

            # 创建更新日志表（追踪增量更新）
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS update_log (
                    stock_code VARCHAR NOT NULL,
                    action VARCHAR,
                    new_records INTEGER,
                    updated_records INTEGER,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
                )
            """)

            # 创建已扫描股票代码表（避免重复扫描）
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS scanned_stocks (
                    stock_code VARCHAR PRIMARY KEY NOT NULL,
                    name VARCHAR,
                    scan_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR DEFAULT 'active'  -- active, inactive, error
                )
            """)

            # 创建索引以提高查询性能
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kline_stock_date
                ON kline_data(stock_code, date)
            """)

            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stock_code
                ON stock_info(stock_code)
            """)

            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_scanned_code
                ON scanned_stocks(stock_code)
            """)

            print(f"[OK] DuckDB 数据库已初始化: {self.db_path}")

        except Exception as e:
            print(f"[ERROR] 数据库初始化错误: {e}")

    def save_stock_info(self, stock_info, stock_code):
        """
        保存股票基本信息

        Args:
            stock_info (dict): 股票基本信息
            stock_code (str): 股票代码

        Returns:
            bool: 是否保存成功
        """
        if not stock_info:
            return False

        try:
            # 使用 INSERT OR REPLACE 实现 upsert（覆盖更新）
            self.conn.execute("""
                INSERT OR REPLACE INTO stock_info (
                    stock_code, name, current_price, close_price, open_price,
                    high, low, volume, market_cap, pe_ratio, week_52_high, week_52_low, update_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                stock_code,
                stock_info.get('name'),
                stock_info.get('current_price'),
                stock_info.get('close_price'),
                stock_info.get('open_price'),
                stock_info.get('high'),
                stock_info.get('low'),
                stock_info.get('volume'),
                stock_info.get('market_cap'),
                stock_info.get('pe_ratio'),
                stock_info.get('52_week_high'),
                stock_info.get('52_week_low'),
                datetime.now().isoformat()
            ))

            return True

        except Exception as e:
            print(f"[ERROR] 保存股票信息错误: {e}")
            return False

    def save_kline_data(self, data, stock_code):
        """
        保存 K 线数据，支持批量插入和增量更新（优化版）

        Args:
            data (DataFrame): K 线数据
            stock_code (str): 股票代码

        Returns:
            dict: 包含新增和更新记录数的统计信息
        """
        if data is None or data.empty:
            print("[ERROR] 没有数据可保存")
            return None

        try:
            # 重置索引以获取日期列
            data_reset = data.reset_index()

            # 确保日期列为字符串格式
            data_reset['date'] = pd.to_datetime(data_reset['date']).dt.strftime('%Y-%m-%d')

            # 重命名列以匹配数据库表结构
            df_for_db = data_reset.rename(columns={
                'Open': 'open',
                'Close': 'close',
                'High': 'high',
                'Low': 'low',
                'Volume': 'volume'
            })[['date', 'open', 'close', 'high', 'low', 'volume']].copy()

            # 添加 stock_code 列
            df_for_db['stock_code'] = stock_code

            # 添加时间戳列
            now = datetime.now().isoformat()
            df_for_db['create_time'] = now
            df_for_db['update_time'] = now

            # 使用 DuckDB 的批量操作进行 UPSERT
            # 1. 创建临时表
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS temp_kline AS SELECT * FROM df_for_db LIMIT 0")
            self.conn.execute("DELETE FROM temp_kline")

            # 2. 将 DataFrame 数据插入临时表
            self.conn.execute("INSERT INTO temp_kline SELECT * FROM df_for_db")

            # 3. 对现有记录执行 UPDATE
            updated = self.conn.execute("""
                UPDATE kline_data
                SET open = t.open,
                    close = t.close,
                    high = t.high,
                    low = t.low,
                    volume = t.volume,
                    update_time = t.update_time
                FROM temp_kline t
                WHERE kline_data.stock_code = t.stock_code
                  AND kline_data.date = t.date
            """).fetchall()

            # 获取更新的记录数
            updated_count = self.conn.execute("""
                SELECT COUNT(*) FROM kline_data k
                INNER JOIN temp_kline t ON k.stock_code = t.stock_code AND k.date = t.date
            """).fetchall()[0][0]

            # 4. 插入新记录（排除已存在的）
            new_count = self.conn.execute("""
                INSERT INTO kline_data (stock_code, date, open, close, high, low, volume, create_time, update_time)
                SELECT t.stock_code, t.date, t.open, t.close, t.high, t.low, t.volume, t.create_time, t.update_time
                FROM temp_kline t
                LEFT JOIN kline_data k ON t.stock_code = k.stock_code AND t.date = k.date
                WHERE k.date IS NULL
            """).fetchall()

            # 获取新增的记录数
            inserted_count = self.conn.execute("""
                SELECT COUNT(*) FROM temp_kline t
                LEFT JOIN kline_data k ON t.stock_code = k.stock_code AND t.date = k.date
                WHERE k.date IS NULL
            """).fetchall()[0][0]

            # 5. 记录到更新日志
            self.conn.execute("""
                INSERT INTO update_log (stock_code, action, new_records, updated_records, update_time)
                VALUES (?, ?, ?, ?, ?)
            """, (stock_code, 'upsert_batch', inserted_count, updated_count, now))

            # 清理临时表
            self.conn.execute("DROP TABLE IF EXISTS temp_kline")

            stats = {
                'new_records': inserted_count,
                'updated_records': updated_count,
                'total_records': inserted_count + updated_count
            }

            print(f"[OK] 数据已批量保存到数据库 (新增：{inserted_count}, 更新：{updated_count})")
            return stats

        except Exception as e:
            print(f"[ERROR] 保存 K 线数据错误：{e}")
            import traceback
            traceback.print_exc()
            return None

    def get_latest_date(self, stock_code):
        """
        获取数据库中最新的日期（用于增量更新）

        Args:
            stock_code (str): 股票代码

        Returns:
            str: 最新日期字符串，格式 'YYYY-MM-DD'，无数据返回 None
        """
        try:
            result = self.conn.execute(
                "SELECT MAX(date) FROM kline_data WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()

            if result and result[0] and result[0][0]:
                return str(result[0][0])
            return None

        except Exception as e:
            print(f"[ERROR] 查询最新日期错误: {e}")
            return None

    def get_kline_data(self, stock_code, start_date=None, end_date=None):
        """
        从数据库获取 K 线数据

        Args:
            stock_code (str): 股票代码
            start_date (str): 开始日期（可选），格式 'YYYY-MM-DD'
            end_date (str): 结束日期（可选），格式 'YYYY-MM-DD'

        Returns:
            DataFrame: K 线数据
        """
        try:
            query = "SELECT date, open, close, high, low, volume FROM kline_data WHERE stock_code = ?"
            params = [stock_code]

            if start_date:
                query += " AND date >= ?"
                params.append(start_date)

            if end_date:
                query += " AND date <= ?"
                params.append(end_date)

            query += " ORDER BY date ASC"

            df = self.conn.execute(query, params).df()

            if df.empty:
                return None

            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)

            # 重命名列以匹配标准格式
            df.rename(columns={
                'open': 'Open',
                'close': 'Close',
                'high': 'High',
                'low': 'Low',
                'volume': 'Volume'
            }, inplace=True)

            return df

        except Exception as e:
            print(f"[ERROR] 查询 K 线数据错误: {e}")
            return None

    def get_stock_info(self, stock_code):
        """
        从数据库获取股票基本信息

        Args:
            stock_code (str): 股票代码

        Returns:
            dict: 股票基本信息，不存在返回 None
        """
        try:
            result = self.conn.execute(
                "SELECT * FROM stock_info WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()

            if not result:
                return None

            columns = [
                'id', 'stock_code', 'name', 'current_price', 'close_price',
                'open_price', 'high', 'low', 'volume', 'market_cap',
                'pe_ratio', 'week_52_high', 'week_52_low', 'update_time'
            ]

            return dict(zip(columns, result[0]))

        except Exception as e:
            print(f"[ERROR] 查询股票信息错误: {e}")
            return None

    def get_update_log(self, stock_code=None, limit=10):
        """
        获取更新日志

        Args:
            stock_code (str): 股票代码（可选，为空则查询所有）
            limit (int): 返回记录数限制

        Returns:
            list: 更新日志列表
        """
        try:
            if stock_code:
                results = self.conn.execute(
                    "SELECT * FROM update_log WHERE stock_code = ? ORDER BY update_time DESC LIMIT ?",
                    (stock_code, limit)
                ).fetchall()
            else:
                results = self.conn.execute(
                    "SELECT * FROM update_log ORDER BY update_time DESC LIMIT ?",
                    (limit,)
                ).fetchall()

            return results if results else []

        except Exception as e:
            print(f"[ERROR] 查询更新日志错误: {e}")
            return []

    def get_statistics(self, stock_code):
        """
        获取数据库统计信息

        Args:
            stock_code (str): 股票代码

        Returns:
            dict: 统计信息
        """
        try:
            # 获取 K 线数据记录数
            kline_count = self.conn.execute(
                "SELECT COUNT(*) FROM kline_data WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()[0][0]

            # 获取日期范围
            date_range = self.conn.execute(
                "SELECT MIN(date), MAX(date) FROM kline_data WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()[0]

            # 获取数据库大小
            db_size = os.path.getsize(self.db_path) if os.path.exists(
                self.db_path) else 0

            return {
                'total_records': kline_count,
                'date_range': date_range,
                'db_file_size': f"{db_size / 1024 / 1024:.2f} MB",
                'db_path': self.db_path
            }

        except Exception as e:
            print(f"[ERROR] 获取统计信息错误: {e}")
            return None

    def cleanup_old_data(self, stock_code, days=365*5):
        """
        清理超过指定天数的旧数据（可选）

        Args:
            stock_code (str): 股票代码
            days (int): 保留天数

        Returns:
            int: 删除的记录数
        """
        try:
            from datetime import timedelta
            cutoff_date = (datetime.now() - timedelta(days=days)).date()

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                "DELETE FROM kline_data WHERE stock_code = ? AND date < ?",
                (stock_code, cutoff_date)
            )

            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()

            if deleted_count > 0:
                print(f"[OK] 已删除 {deleted_count} 条过期数据")

            return deleted_count

        except sqlite3.Error as e:
            print(f"[ERROR] 清理数据错误: {e}")
            return 0

    def export_to_json(self, stock_code, output_path="./output"):
        """
        将数据库数据导出为 JSON

        Args:
            stock_code (str): 股票代码
            output_path (str): 输出目录

        Returns:
            str: 导出文件路径
        """
        try:
            os.makedirs(output_path, exist_ok=True)

            # 获取股票信息和 K 线数据
            stock_info = self.get_stock_info(stock_code)
            kline_data = self.get_kline_data(stock_code)

            if kline_data is None or kline_data.empty:
                print("[ERROR] 无数据可导出")
                return None

            # 整合数据
            kline_reset = kline_data.reset_index()
            kline_reset['date'] = kline_reset['date'].dt.strftime('%Y-%m-%d')

            data_dict = {
                'stock_code': stock_code,
                'stock_info': stock_info,
                'record_count': len(kline_data),
                'date_range': f"{kline_reset['date'].iloc[0]} to {kline_reset['date'].iloc[-1]}",
                'export_time': datetime.now().isoformat(),
                'source': 'sqlite_database',
                'data': kline_reset.to_dict('records')
            }

            # 保存为 JSON
            import json
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"hk_stock_{stock_code}_{timestamp}.json"
            filepath = os.path.join(output_path, filename)

            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)

            print(f"[OK] 数据已导出到：{filepath}")
            return filepath

        except Exception as e:
            print(f"[ERROR] 导出数据错误: {e}")
            return None

    def get_all_stocks(self):
        """
        获取数据库中所有股票列表

        Returns:
            list: 股票代码列表
        """
        try:
            result = self.conn.execute(
                "SELECT DISTINCT stock_code FROM kline_data ORDER BY stock_code"
            ).fetchall()

            return [row[0] for row in result] if result else []

        except Exception as e:
            print(f"[ERROR] 获取股票列表错误：{e}")
            return []

    def save_scanned_stock(self, stock_code, name, status='active'):
        """
        保存已扫描的股票代码到数据库

        Args:
            stock_code (str): 股票代码
            name (str): 股票名称
            status (str): 状态 ('active', 'inactive', 'error')

        Returns:
            bool: 是否成功
        """
        try:
            now = datetime.now().isoformat()
            self.conn.execute("""
                INSERT INTO scanned_stocks (stock_code, name, scan_time, last_update, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (stock_code) DO UPDATE SET
                    name = excluded.name,
                    last_update = excluded.last_update,
                    status = excluded.status
            """, (stock_code, name, now, now, status))

            return True

        except Exception as e:
            print(f"[ERROR] 保存已扫描股票错误：{e}")
            return False

    def get_scanned_stocks(self, status_filter=None):
        """
        获取已扫描的股票列表

        Args:
            status_filter (str): 状态过滤 ('active', 'inactive', 'error', None表示全部)

        Returns:
            list: 已扫描的股票列表 [{'code': '00001', 'name': '香港交易所', 'status': 'active'}, ...]
        """
        try:
            if status_filter:
                result = self.conn.execute(
                    "SELECT stock_code, name, status FROM scanned_stocks WHERE status = ? ORDER BY stock_code",
                    (status_filter,)
                ).fetchall()
            else:
                result = self.conn.execute(
                    "SELECT stock_code, name, status FROM scanned_stocks ORDER BY stock_code"
                ).fetchall()

            return [{'code': row[0], 'name': row[1], 'status': row[2]} for row in result] if result else []

        except Exception as e:
            print(f"[ERROR] 获取已扫描股票列表错误：{e}")
            return []

    def is_stock_scanned(self, stock_code):
        """
        检查股票是否已被扫描

        Args:
            stock_code (str): 股票代码

        Returns:
            bool: 是否已被扫描
        """
        try:
            result = self.conn.execute(
                "SELECT COUNT(*) FROM scanned_stocks WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()[0][0]

            return result > 0

        except Exception as e:
            print(f"[ERROR] 检查股票扫描状态错误：{e}")
            return False

    def get_scanned_stock_count(self):
        """
        获取已扫描股票总数

        Returns:
            int: 已扫描股票数量
        """
        try:
            result = self.conn.execute(
                "SELECT COUNT(*) FROM scanned_stocks"
            ).fetchall()[0][0]

            return result

        except Exception as e:
            print(f"[ERROR] 获取已扫描股票数量错误：{e}")
            return 0

    def get_total_kline_records(self):
        """
        获取数据库中所有 K 线数据总记录数

        Returns:
            int: 总记录数
        """
        try:
            result = self.conn.execute(
                "SELECT COUNT(*) FROM kline_data"
            ).fetchall()[0][0]

            return result

        except Exception as e:
            print(f"[ERROR] 获取总记录数错误：{e}")
            return 0

    def sort_database(self):
        """
        按股票代码和日期排序数据库中的所有数据
        (DuckDB 中的数据本身已经有序，这里主要确保索引优化)

        Returns:
            dict: 排序统计信息
        """
        try:
            print("[INFO] 开始整理数据库索引...")

            # 获取统计信息
            stock_count = self.conn.execute(
                "SELECT COUNT(DISTINCT stock_code) FROM kline_data"
            ).fetchall()[0][0]

            total_records = self.get_total_kline_records()

            # 重建索引以优化查询性能
            self.conn.execute("VACUUM")

            stats = {
                'total_stocks': stock_count,
                'total_records': total_records,
                'status': 'success'
            }

            print(f"[OK] 数据库整理完成")
            print(f"     总股票数：{stock_count}")
            print(f"     总记录数：{total_records}")

            return stats

        except Exception as e:
            print(f"[ERROR] 排序数据库错误：{e}")
            return {'status': 'failed', 'error': str(e)}

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            print("[OK] 数据库连接已关闭")

    def __del__(self):
        """析构函数，自动关闭数据库"""
        self.close()
