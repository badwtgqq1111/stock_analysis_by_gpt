#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股股票池抓取。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import requests

from data.store.database_manager import DatabaseManager


class HKMarketListFetcher:
    """获取港股全市场股票列表。"""

    def __init__(self):
        self.stocks = []

    def fetch(self):
        print("[INFO] 正在获取港股全市场股票列表...")
        try:
            hk_stocks_url = (
                "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData?page=1&num=10000&sort=symbol&asc=1&node=hk&symbol="
            )
            response = requests.get(hk_stocks_url, timeout=15)
            response.raise_for_status()
            stocks_data = response.json()

            if not stocks_data:
                print("[WARNING] 未能获取港股列表，使用备用方案...")
                return self._fetch_alternative()

            for stock in stocks_data:
                code = stock.get("code", "")
                name = stock.get("name", "")
                if code and len(code) == 5 and code.isdigit():
                    self.stocks.append({"code": code, "name": name})

            print(f"[OK] 成功获取 {len(self.stocks)} 只港股")
            return self.stocks
        except Exception as e:
            print(f"[ERROR] 获取港股列表失败：{e}")
            return self._fetch_alternative()

    def _fetch_alternative(self):
        print("[INFO] 使用多线程并发方案扫描全市场港股列表...")
        print("[INFO] 扫描范围：00001-09999，使用多线程加速...")

        db_manager = DatabaseManager()
        scanned_stocks = db_manager.get_scanned_stocks("active")
        scanned_codes = {stock["code"] for stock in scanned_stocks}
        print(f"[INFO] 数据库中已有 {len(scanned_codes)} 只已扫描股票，将跳过...")

        stats = {"tested": 0, "found": 0, "skipped": len(scanned_codes), "lock": threading.Lock()}

        def query_stock(code_num):
            try:
                code = str(code_num).zfill(5)
                if code in scanned_codes:
                    return None

                ticker = f"hk{code}"
                url = f"http://qt.gtimg.cn/q={ticker}"
                response = requests.get(url, timeout=2)

                if response.status_code == 200 and "~" in response.text:
                    try:
                        content = response.content.decode("gb2312")
                    except Exception:
                        content = response.text

                    parts = content.split("~")
                    if len(parts) > 1 and parts[1] and parts[1] != "N/A":
                        name = parts[1].strip()
                        if name:
                            db_manager.save_scanned_stock(code, name, "active")
                            return {"code": code, "name": name}
            except requests.exceptions.Timeout:
                pass
            except Exception:
                pass
            return None

        def worker_batch(code_range):
            local_stocks = []
            for code_num in code_range:
                stock = query_stock(code_num)
                if stock:
                    local_stocks.append(stock)
                    with stats["lock"]:
                        stats["found"] += 1
                        if stats["found"] % 50 == 0:
                            print(f"[PROGRESS] 已发现 {stats['found']} 只港股 (已扫描 {stats['tested']} 个代码)...")

                with stats["lock"]:
                    stats["tested"] += 1
                    if stats["tested"] % 1000 == 0:
                        print(
                            f"[MILESTONE] 已扫描 {stats['tested']}/9999 个代码，已跳过 {stats['skipped']} 只已扫描股票..."
                        )
            return local_stocks

        num_threads = 20
        batch_size = 500
        total_codes = 9999
        print(f"[INFO] 使用 {num_threads} 个线程，每线程处理 {batch_size} 个代码...")

        all_stocks = []
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = []
            for i in range(0, total_codes, batch_size):
                batch_start = i + 1
                batch_end = min(i + batch_size, total_codes)
                code_range = range(batch_start, batch_end + 1)
                futures.append(executor.submit(worker_batch, code_range))

            for future in as_completed(futures):
                try:
                    batch_stocks = future.result()
                    all_stocks.extend(batch_stocks)
                except Exception as e:
                    print(f"[ERROR] 线程异常：{e}")

        all_stocks.extend(scanned_stocks)
        seen = set()
        unique_stocks = []
        for stock in all_stocks:
            if stock["code"] not in seen:
                seen.add(stock["code"])
                unique_stocks.append(stock)

        self.stocks = sorted(unique_stocks, key=lambda x: x["code"])

        print()
        print(f"[OK] 扫描完成！共发现 {len(self.stocks)} 只港股")
        print(f"     新发现：{stats['found']} 只")
        print(f"     已存在：{stats['skipped']} 只")
        print(f"     总扫描：{stats['tested']} 个代码")
        print(f"     发现率：{(stats['found'] / stats['tested'] * 100):.2f}%")

        db_manager.close()
        return self.stocks

    def save_to_db(self, db_manager):
        if not self.stocks:
            return False
        try:
            for stock in self.stocks:
                stock_info = {
                    "name": stock["name"],
                    "code": f"hk{stock['code']}",
                    "current_price": None,
                    "close_price": None,
                    "open_price": None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "market_cap": None,
                    "pe_ratio": None,
                    "52_week_high": None,
                    "52_week_low": None,
                }
                db_manager.save_stock_info(stock_info, stock["code"])

            print(f"[OK] 已将 {len(self.stocks)} 只股票信息保存到数据库")
            return True
        except Exception as e:
            print(f"[ERROR] 保存股票列表到数据库失败：{e}")
            return False

    def get_stocks(self):
        return self.stocks
