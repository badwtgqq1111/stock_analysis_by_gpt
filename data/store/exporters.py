#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""数据导出与保存工具。"""

import json
import os
from datetime import datetime

from data.ingest import MarketDataService
from data.store.database_manager import DatabaseManager


class DataSaver:
    """保存股票数据到多个存储介质。"""

    def __init__(self, db_dir="./assets"):
        self.db_manager = DatabaseManager(db_dir)
        self.market_data_service = MarketDataService(base_dir=os.path.join(db_dir, "data"))

    @staticmethod
    def save_json(data, stock_code, output_dir="./output"):
        if data is None or data.empty:
            print("[ERROR] 没有数据可保存")
            return None
        try:
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"hk_stock_{stock_code}_{timestamp}.json"
            filepath = os.path.join(output_dir, filename)
            data_reset = data.reset_index()
            data_reset["date"] = data_reset["date"].dt.strftime("%Y-%m-%d")
            data_dict = {
                "stock_code": stock_code,
                "record_count": len(data),
                "date_range": f"{data_reset['date'].iloc[0]} to {data_reset['date'].iloc[-1]}",
                "update_time": datetime.now().isoformat(),
                "source": "json_file",
                "data": data_reset.to_dict("records"),
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)
            print(f"[OK] JSON 文件已保存：{filepath} ({len(data)} 条)")
            return filepath
        except Exception as e:
            print(f"[ERROR] 保存 JSON 文件错误：{e}")
            return None

    def save_to_db(self, data, stock_code):
        if data is None or data.empty:
            print("[ERROR] 没有数据可保存到数据库")
            return None
        return self.db_manager.save_kline_data(data, stock_code)

    def sync_hk_stock_to_data_layer(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq"):
        return self.market_data_service.sync_hk_stock(
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=adjust,
        )

    def sync_cn_stock_to_data_layer(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
        return self.market_data_service.sync_cn_stock(
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=adjust,
            period=period,
        )

    def save_stock_info_to_db(self, stock_info, stock_code):
        if not stock_info:
            return False
        return self.db_manager.save_stock_info(stock_info, stock_code)

    def export_from_db(self, stock_code, output_dir="./output"):
        return self.db_manager.export_to_json(stock_code, output_dir)

    def get_db_statistics(self, stock_code):
        return self.db_manager.get_statistics(stock_code)

    def verify_db_data(self, stock_code):
        try:
            stock_info = self.db_manager.get_stock_info(stock_code)
            stats = self.db_manager.get_statistics(stock_code)
            if stats is None:
                return {
                    "stock_code": stock_code,
                    "exists": False,
                    "total_records": 0,
                    "date_range": None,
                    "has_stock_info": False,
                }
            total_records = stats["total_records"]
            is_valid = total_records > 0
            return {
                "stock_code": stock_code,
                "exists": is_valid,
                "total_records": total_records,
                "date_range": stats["date_range"] if is_valid else None,
                "has_stock_info": stock_info is not None,
                "db_file_size": stats["db_file_size"],
                "db_path": stats["db_path"],
            }
        except Exception as e:
            print(f"[ERROR] 验证数据错误: {e}")
            return {"stock_code": stock_code, "exists": False, "error": str(e)}

    def batch_verify_data(self, stock_codes):
        print("[INFO] 开始批量验证数据...")
        verification_results = []
        total_records = 0
        all_exist = True
        for stock_code in stock_codes:
            result = self.verify_db_data(stock_code)
            verification_results.append(result)
            if result["exists"]:
                total_records += result["total_records"]
            else:
                all_exist = False
        return {
            "total_stocks": len(stock_codes),
            "verified_stocks": sum(1 for r in verification_results if r["exists"]),
            "failed_stocks": sum(1 for r in verification_results if not r["exists"]),
            "total_records": total_records,
            "all_verified": all_exist,
            "details": verification_results,
        }

    def print_verification_report(self, verification_result):
        print("\n" + "=" * 80)
        print("[VERIFY] 数据验证报告")
        print("=" * 80)

        if "details" in verification_result:
            summary = verification_result
            print(f"\n[SUMMARY] 批量验证汇总")
            print(f"  总股票数：{summary['total_stocks']} 只")
            print(f"  验证成功：{summary['verified_stocks']} 只")
            print(f"  验证失败：{summary['failed_stocks']} 只")
            print(f"  总记录数：{summary['total_records']} 条")
            status_text = "[OK] 全部验证成功" if summary["all_verified"] else "[WARN] 部分验证失败"
            print(f"  验证状态：{status_text}")
            print(f"\n[DETAILS] 各股票详情")
            for detail in summary["details"]:
                status = "[OK]" if detail["exists"] else "[FAIL]"
                stock_code = detail["stock_code"]
                if detail["exists"]:
                    date_range = detail["date_range"]
                    records = detail["total_records"]
                    print(f"  {status} {stock_code}: {records} 条记录，时间范围: {date_range[0]} 至 {date_range[1]}")
                else:
                    error = detail.get("error", "数据不存在")
                    print(f"  {status} {stock_code}: 验证失败 - {error}")
        else:
            stock_code = verification_result["stock_code"]
            exists = verification_result["exists"]
            print(f"\n[STOCK] 股票代码：{stock_code}")
            status_text = "[OK] 数据已保存" if exists else "[FAIL] 数据未保存"
            print(f"[STATUS] {status_text}")
            if exists:
                print(f"[INFO] 总记录数：{verification_result['total_records']}")
