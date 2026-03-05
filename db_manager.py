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


class DatabaseManager:
    """DuckDB 数据库管理器 - 列式存储，超高性能 OLAP"""

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

            # 创建序列用于自增 ID
            self.conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_update_log START 1")

            # 创建股票基本信息表
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_info (
                    stock_code VARCHAR PRIMARY KEY,
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

            # 创建 K 线数据表（列式存储，超高性能）
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

            # 创建更新日志表
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS update_log (
                    id INTEGER PRIMARY KEY DEFAULT nextval('seq_update_log'),
                    stock_code VARCHAR NOT NULL,
                    action VARCHAR,
                    new_records INTEGER,
                    updated_records INTEGER,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (stock_code) REFERENCES stock_info(stock_code)
                )
            """)

            # 创建索引以提高查询性能
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kline_stock_date 
                ON kline_data(stock_code, date)
            """)

            print(f"[OK] DuckDB 数据库已初始化: {self.db_path}")

        except Exception as e:
            print(f"[ERROR] 数据库初始化错误: {e}")

    def save_stock_info(self, stock_info, stock_code):
        """保存股票基本信息"""
        if not stock_info:
            return False

        try:
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
        """保存 K 线数据，支持增量更新"""
        if data is None or data.empty:
            print("[ERROR] 没有数据可保存")
            return None

        try:
            new_count = 0
            updated_count = 0

            data_reset = data.reset_index()

            for _, row in data_reset.iterrows():
                date = row['date'].strftime('%Y-%m-%d') if hasattr(
                    row['date'], 'strftime') else str(row['date'])

                try:
                    result = self.conn.execute(
                        "SELECT 1 FROM kline_data WHERE stock_code = ? AND date = ?",
                        (stock_code, date)
                    ).fetchall()
                    existing = len(result) > 0

                    if existing:
                        self.conn.execute("""
                            UPDATE kline_data SET
                                open = ?, close = ?, high = ?, low = ?, volume = ?, update_time = ?
                            WHERE stock_code = ? AND date = ?
                        """, (
                            row['Open'], row['Close'], row['High'], row['Low'],
                            row['Volume'], datetime.now().isoformat(),
                            stock_code, date
                        ))
                        updated_count += 1
                    else:
                        self.conn.execute("""
                            INSERT INTO kline_data (
                                stock_code, date, open, close, high, low, volume, create_time, update_time
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            stock_code, date, row['Open'], row['Close'], row['High'],
                            row['Low'], row['Volume'], datetime.now().isoformat(),
                            datetime.now().isoformat()
                        ))
                        new_count += 1
                except Exception:
                    pass

            # 记录到更新日志
            self.conn.execute("""
                INSERT INTO update_log (stock_code, action, new_records, updated_records, update_time)
                VALUES (?, ?, ?, ?, ?)
            """, (stock_code, 'upsert', new_count, updated_count, datetime.now().isoformat()))

            stats = {
                'new_records': new_count,
                'updated_records': updated_count,
                'total_records': new_count + updated_count
            }
            print(f"[OK] 数据已保存到数据库 (新增: {new_count}, 更新: {updated_count})")
            return stats

        except Exception as e:
            print(f"[ERROR] 保存 K 线数据错误: {e}")
            return None

    def get_latest_date(self, stock_code):
        """获取数据库中最新的日期（用于增量更新）"""
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
        """从数据库获取 K 线数据"""
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

            return df

        except Exception as e:
            print(f"[ERROR] 查询 K 线数据错误: {e}")
            return None

    def get_stock_info(self, stock_code):
        """从数据库获取股票基本信息"""
        try:
            result = self.conn.execute(
                "SELECT * FROM stock_info WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()

            if not result:
                return None

            columns = [
                'stock_code', 'name', 'current_price', 'close_price',
                'open_price', 'high', 'low', 'volume', 'market_cap',
                'pe_ratio', 'week_52_high', 'week_52_low', 'update_time'
            ]

            return dict(zip(columns, result[0]))

        except Exception as e:
            print(f"[ERROR] 查询股票信息错误: {e}")
            return None

    def get_update_log(self, stock_code=None, limit=10):
        """获取更新日志"""
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
        """获取数据库统计信息"""
        try:
            kline_count = self.conn.execute(
                "SELECT COUNT(*) FROM kline_data WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()[0][0]

            date_range = self.conn.execute(
                "SELECT MIN(date), MAX(date) FROM kline_data WHERE stock_code = ?",
                (stock_code,)
            ).fetchall()[0]

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
        """清理超过指定天数的旧数据"""
        try:
            from datetime import timedelta
            cutoff_date = (datetime.now() - timedelta(days=days)).date()

            before_count = self.conn.execute(
                "SELECT COUNT(*) FROM kline_data WHERE stock_code = ? AND date < ?",
                (stock_code, cutoff_date)
            ).fetchall()[0][0]

            self.conn.execute(
                "DELETE FROM kline_data WHERE stock_code = ? AND date < ?",
                (stock_code, cutoff_date)
            )

            if before_count > 0:
                print(f"[OK] 已删除 {before_count} 条过期数据")

            return before_count

        except Exception as e:
            print(f"[ERROR] 清理数据错误: {e}")
            return 0

    def export_to_json(self, stock_code, output_path="./output"):
        """将数据库数据导出为 JSON"""
        try:
            os.makedirs(output_path, exist_ok=True)

            stock_info = self.get_stock_info(stock_code)
            kline_data = self.get_kline_data(stock_code)

            if kline_data is None or kline_data.empty:
                print("[ERROR] 无数据可导出")
                return None

            kline_reset = kline_data.reset_index()
            kline_reset['date'] = kline_reset['date'].dt.strftime('%Y-%m-%d')

            data_dict = {
                'stock_code': stock_code,
                'stock_info': stock_info,
                'record_count': len(kline_data),
                'date_range': f"{kline_reset['date'].iloc[0]} to {kline_reset['date'].iloc[-1]}",
                'export_time': datetime.now().isoformat(),
                'source': 'duckdb_database',
                'data': kline_reset.to_dict('records')
            }

            import json
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"hk_stock_{stock_code}_{timestamp}.json"
            filepath = os.path.join(output_path, filename)

            # Convert all values to JSON-serializable types
            def make_serializable(obj):
                if isinstance(obj, dict):
                    return {k: make_serializable(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [make_serializable(item) for item in obj]
                elif isinstance(obj, (datetime, pd.Timestamp)):
                    return obj.isoformat()
                elif pd.isna(obj):
                    return None
                elif isinstance(obj, (int, float, str, bool)):
                    return obj
                else:
                    return str(obj)

            data_dict = make_serializable(data_dict)

            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)

            print(f"[OK] 数据已导出到：{filepath}")
            return filepath

        except Exception as e:
            print(f"[ERROR] 导出数据错误: {e}")
            return None

    def export_to_parquet(self, stock_code, output_path="./output"):
        """将数据导出为 Parquet 格式（DuckDB 原生支持，超快速）"""
        try:
            os.makedirs(output_path, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"hk_stock_{stock_code}_{timestamp}.parquet"
            filepath = os.path.join(output_path, filename)

            self.conn.execute(f"""
                COPY (
                    SELECT * FROM kline_data WHERE stock_code = '{stock_code}' ORDER BY date
                ) TO '{filepath}' (FORMAT PARQUET)
            """)

            print(f"[OK] 数据已导出为 Parquet：{filepath}")
            return filepath

        except Exception as e:
            print(f"[ERROR] 导出 Parquet 错误: {e}")
            return None

    def export_to_csv(self, stock_code, output_path="./output"):
        """将数据导出为 CSV 格式（DuckDB 原生支持，高效）"""
        try:
            os.makedirs(output_path, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"hk_stock_{stock_code}_{timestamp}.csv"
            filepath = os.path.join(output_path, filename)

            self.conn.execute(f"""
                COPY (
                    SELECT * FROM kline_data WHERE stock_code = '{stock_code}' ORDER BY date
                ) TO '{filepath}' (FORMAT CSV, HEADER)
            """)

            print(f"[OK] 数据已导出为 CSV：{filepath}")
            return filepath

        except Exception as e:
            print(f"[ERROR] 导出 CSV 错误: {e}")
            return None

    def get_data_quality_report(self, stock_code):
        """获取数据质量报告"""
        try:
            result = self.conn.execute(f"""
                SELECT 
                    COUNT(*) as total_records,
                    COUNT(date) as valid_dates,
                    COUNT(open) as valid_opens,
                    COUNT(close) as valid_closes,
                    COUNT(high) as valid_highs,
                    COUNT(low) as valid_lows,
                    COUNT(volume) as valid_volumes,
                    MIN(open) as min_open,
                    MAX(open) as max_open,
                    AVG(volume) as avg_volume
                FROM kline_data 
                WHERE stock_code = '{stock_code}'
            """).fetchall()

            if not result:
                return None

            data = result[0]
            return {
                'total_records': data[0],
                'valid_dates': data[1],
                'valid_opens': data[2],
                'valid_closes': data[3],
                'valid_highs': data[4],
                'valid_lows': data[5],
                'valid_volumes': data[6],
                'price_range': (data[7], data[8]),
                'avg_volume': data[9]
            }

        except Exception as e:
            print(f"[ERROR] 获取数据质量报告错误: {e}")
            return None

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            print("[OK] 数据库连接已关闭")

    def __del__(self):
        """析构函数，自动关闭数据库"""
        self.close()
