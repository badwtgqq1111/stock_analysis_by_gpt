#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股股票池抓取。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import threading

import requests

from data.ingest.providers.hk_common import ak
from data.store.database_manager import DatabaseManager


class HKMarketListFetcher:
    """获取港股全市场股票列表。"""

    INDEX_PRODUCT_ALLOW_KEYWORDS = (
        "指数",
        "ETF",
        "ETP",
        "TRACKER",
        "TRUST",
        "杠杆",
        "反向",
        "两倍",
        "二倍",
        "三倍",
        "2X",
        "3X",
        "XL2",
        "XL3",
        "做多",
        "做空",
        "恒指",
        "国指",
        "科指",
        "纳指",
        "纳斯达克",
        "标普",
        "道指",
        "日经",
        "罗素",
        "比特币",
        "以太币",
    )

    NON_STOCK_EXCLUDE_KEYWORDS = (
        "牛证",
        "熊证",
        "牛熊证",
        "认购证",
        "认沽证",
        "窝轮",
        "界内证",
        "权证",
        "票据",
        "债券",
        "债务",
        "房托",
        "房地产投资信托",
        "REIT",
    )

    def __init__(self):
        self.stocks = []

    def fetch(self, limit=None):
        print("[INFO] 正在获取港股全市场股票列表...")
        fetchers = [
            ("akshare_sina", self._fetch_from_akshare_sina),
            ("akshare_eastmoney", self._fetch_from_akshare_eastmoney),
        ]

        for source_name, fetcher in fetchers:
            try:
                stocks = fetcher()
                if stocks:
                    if limit:
                        stocks = stocks[:limit]
                    self.stocks = stocks
                    print(f"[OK] 成功获取 {len(self.stocks)} 只港股，来源：{source_name}")
                    return self.stocks
                print(f"[WARNING] {source_name} 未返回有效港股列表，尝试下一数据源...")
            except Exception as e:
                print(f"[WARNING] {source_name} 获取港股列表失败：{e}")

        print("[WARNING] AKShare 港股列表接口不可用，回退到腾讯扫描方案...")
        return self._fetch_alternative(limit=limit)

    @classmethod
    def _is_allowed_index_product(cls, name):
        normalized_name = str(name or "").strip()
        upper_name = normalized_name.upper()
        return any(keyword in normalized_name or keyword in upper_name for keyword in cls.INDEX_PRODUCT_ALLOW_KEYWORDS)

    @classmethod
    def _is_supported_security(cls, code, name):
        normalized_code = str(code or "").strip()
        normalized_name = str(name or "").strip()
        upper_name = normalized_name.upper()

        if not normalized_name:
            return False
        if normalized_name in {normalized_code, normalized_code.lstrip("0"), f"HK{normalized_code}", f"hk{normalized_code}"}:
            return False
        if re.fullmatch(r"0*\d{5}", normalized_name):
            return False
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", normalized_name):
            return False

        if cls._is_allowed_index_product(normalized_name):
            return True

        return not any(keyword in normalized_name or keyword in upper_name for keyword in cls.NON_STOCK_EXCLUDE_KEYWORDS)

    def _normalize_spot_frame(self, df, code_column, name_column):
        if df is None or df.empty:
            return []

        stocks = []
        seen = set()
        filtered_out = 0
        for _, row in df.iterrows():
            raw_code = str(row.get(code_column, "")).strip()
            raw_name = str(row.get(name_column, "")).strip()
            digits = "".join(ch for ch in raw_code if ch.isdigit())
            if len(digits) != 5 or not raw_name:
                continue
            if not self._is_supported_security(digits, raw_name):
                filtered_out += 1
                continue
            if digits in seen:
                continue
            seen.add(digits)
            stocks.append({"code": digits, "name": raw_name})

        if filtered_out:
            print(f"[INFO] 已过滤 {filtered_out} 只非目标港股证券")
        return sorted(stocks, key=lambda item: item["code"])

    def _fetch_from_akshare_sina(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        try:
            df = ak.stock_hk_spot()
        except AttributeError as exc:
            raise RuntimeError("当前 akshare 版本不包含 stock_hk_spot 接口") from exc

        return self._normalize_spot_frame(df, code_column="代码", name_column="中文名称")

    def _fetch_from_akshare_eastmoney(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        try:
            df = ak.stock_hk_spot_em()
        except AttributeError as exc:
            raise RuntimeError("当前 akshare 版本不包含 stock_hk_spot_em 接口") from exc

        return self._normalize_spot_frame(df, code_column="代码", name_column="名称")

    def _fetch_alternative(self, limit=None):
        print("[INFO] 使用多线程并发方案扫描全市场港股列表...")
        print("[INFO] 扫描范围：00001-09999，使用多线程加速...")

        db_manager = DatabaseManager()
        cache_available = db_manager.conn is not None
        scanned_stocks = db_manager.get_scanned_stocks("active") if cache_available else []
        scanned_codes = {stock["code"] for stock in scanned_stocks}
        print(f"[INFO] 数据库中已有 {len(scanned_codes)} 只已扫描股票，将跳过...")
        if not cache_available:
            print("[WARNING] 扫描缓存数据库不可用，本次仅做内存扫描，不写入 scanned_stocks 缓存")

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
                        if name and self._is_supported_security(code, name):
                            if cache_available:
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
                if limit and stats["found"] >= limit:
                    break
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
            if not self._is_supported_security(stock["code"], stock["name"]):
                continue
            if stock["code"] not in seen:
                seen.add(stock["code"])
                unique_stocks.append(stock)
                if limit and len(unique_stocks) >= limit:
                    break

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
