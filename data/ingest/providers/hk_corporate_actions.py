#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股企业行为抓取。"""

import re

import pandas as pd
import requests

from data.ingest.providers.hk_common import ak, normalize_hk_stock_code
from data.ingest.providers.history_utils import apply_date_filters, call_with_retries
from data.model import normalize_corporate_actions_frame


DEFAULT_SOURCE_PRIORITY = ["akshare_eastmoney", "eastmoney_api"]


class HKCorporateActionsFetcher:
    """获取港股分红派息等企业行为数据。"""

    def __init__(self, stock_code, source_priority=None):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.source_priority = list(source_priority or DEFAULT_SOURCE_PRIORITY)
        self.last_successful_source = None
        self.data = None

    @staticmethod
    def _extract_ratio(plan_text, keywords):
        for keyword in keywords:
            pattern = rf"每\s*(\d+(?:\.\d+)?)\s*股[^，。,;；]*?{keyword}\s*(\d+(?:\.\d+)?)"
            match = re.search(pattern, plan_text)
            if match:
                base = float(match.group(1))
                value = float(match.group(2))
                if base > 0:
                    return value / base
        return 0.0

    @staticmethod
    def _extract_cash_dividend(plan_text):
        patterns = [
            r"每股[^，。,;；]*?派\s*(\d+(?:\.\d+)?)",
            r"每\s*(\d+(?:\.\d+)?)\s*股[^，。,;；]*?派\s*(\d+(?:\.\d+)?)",
            r"股息\s*(\d+(?:\.\d+)?)",
        ]
        for pattern in patterns:
            match = re.search(pattern, plan_text)
            if not match:
                continue
            if len(match.groups()) == 1:
                return float(match.group(1))
            base = float(match.group(1))
            value = float(match.group(2))
            if base > 0:
                return value / base
        return 0.0

    @staticmethod
    def _extract_rights_issue(plan_text):
        ratio_match = re.search(
            r"每\s*(\d+(?:\.\d+)?)\s*股[^，。,;；]*?供\s*(\d+(?:\.\d+)?)\s*股",
            plan_text,
        )
        price_match = re.search(r"供股价[为:]?\s*(\d+(?:\.\d+)?)", plan_text)
        ratio = 0.0
        if ratio_match:
            base = float(ratio_match.group(1))
            value = float(ratio_match.group(2))
            if base > 0:
                ratio = value / base
        price = float(price_match.group(1)) if price_match else 0.0
        return ratio, price

    @classmethod
    def _plan_to_action_rows(cls, row):
        plan_text = str(row.get("分红方案") or "").strip()
        if not plan_text or any(token in plan_text for token in ["不派", "无", "取消"]):
            return []

        base_payload = {
            "event_date": row.get("除净日"),
            "announcement_date": row.get("最新公告日期"),
            "ex_date": row.get("除净日"),
            "record_date": row.get("截至过户日"),
            "payment_date": row.get("发放日"),
            "fiscal_year": row.get("财政年度"),
            "plan_explain": plan_text,
            "distribution_type": row.get("分配类型"),
        }

        action_rows = []
        cash_dividend = cls._extract_cash_dividend(plan_text)
        if cash_dividend > 0:
            action_rows.append(
                {
                    **base_payload,
                    "action_type": "cash_dividend",
                    "cash_dividend": cash_dividend,
                }
            )

        bonus_share_ratio = cls._extract_ratio(plan_text, ["送", "转增", "转"])
        if bonus_share_ratio > 0:
            action_rows.append(
                {
                    **base_payload,
                    "action_type": "bonus_issue",
                    "bonus_share_ratio": bonus_share_ratio,
                }
            )

        rights_issue_ratio, rights_issue_price = cls._extract_rights_issue(plan_text)
        if rights_issue_ratio > 0:
            action_rows.append(
                {
                    **base_payload,
                    "action_type": "rights_issue",
                    "rights_issue_ratio": rights_issue_ratio,
                    "rights_issue_price": rights_issue_price,
                }
            )
        return action_rows

    @classmethod
    def _normalize_dividend_frame(cls, source_frame):
        if source_frame is None or source_frame.empty:
            return pd.DataFrame()

        working = source_frame.copy()
        for column in ["最新公告日期", "除净日", "截至过户日", "发放日"]:
            if column in working.columns:
                working[column] = pd.to_datetime(working[column], errors="coerce")

        rows = []
        for _, record in working.iterrows():
            rows.extend(cls._plan_to_action_rows(record))
        return pd.DataFrame(rows)

    def _fetch_akshare_eastmoney_actions(self):
        if ak is None:
            raise ImportError("akshare 未安装")
        frame = call_with_retries(
            lambda: ak.stock_hk_dividend_payout_em(symbol=self.stock_code),
            attempts=2,
            sleep_seconds=0.5,
        )
        return self._normalize_dividend_frame(frame)

    def _fetch_eastmoney_api_actions(self):
        url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
        params = {
            "reportName": "RPT_HKF10_MAIN_DIVBASIC",
            "columns": "SECURITY_CODE,UPDATE_DATE,REPORT_TYPE,EX_DIVIDEND_DATE,DIVIDEND_DATE,"
            "TRANSFER_END_DATE,YEAR,PLAN_EXPLAIN,IS_BFP",
            "quoteColumns": "",
            "filter": f'(SECURITY_CODE="{self.stock_code}")(IS_BFP="0")',
            "pageNumber": "1",
            "pageSize": "200",
            "sortTypes": "-1,-1",
            "sortColumns": "NOTICE_DATE,EX_DIVIDEND_DATE",
            "source": "F10",
            "client": "PC",
        }
        response = call_with_retries(
            lambda: requests.get(url, params=params, timeout=15),
            attempts=3,
            sleep_seconds=1.0,
        )
        response.raise_for_status()
        data_json = response.json()
        result = (data_json or {}).get("result") or {}
        rows = result.get("data") or []
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(rows).rename(
            columns={
                "UPDATE_DATE": "最新公告日期",
                "REPORT_TYPE": "分配类型",
                "EX_DIVIDEND_DATE": "除净日",
                "DIVIDEND_DATE": "发放日",
                "TRANSFER_END_DATE": "截至过户日",
                "YEAR": "财政年度",
                "PLAN_EXPLAIN": "分红方案",
            }
        )
        keep_columns = ["最新公告日期", "财政年度", "分红方案", "分配类型", "除净日", "截至过户日", "发放日"]
        frame = frame[keep_columns]
        return self._normalize_dividend_frame(frame)

    def fetch(self, start_date=None, end_date=None, num_records=None):
        fetchers = {
            "akshare_eastmoney": self._fetch_akshare_eastmoney_actions,
            "eastmoney_api": self._fetch_eastmoney_api_actions,
        }
        for source_name in self.source_priority:
            fetcher = fetchers.get(source_name)
            if fetcher is None:
                continue
            try:
                raw_frame = fetcher()
                normalized = normalize_corporate_actions_frame(
                    raw_frame,
                    stock_code=self.stock_code,
                    market="HK",
                    exchange="HKEX",
                    asset_type="equity",
                    source=source_name,
                )
                if normalized.empty:
                    continue
                filtered = apply_date_filters(
                    normalized.rename(columns={"event_date": "date"}).set_index("date"),
                    start_date=start_date,
                    end_date=end_date,
                    num_records=num_records,
                ).reset_index().rename(columns={"date": "event_date"})
                self.last_successful_source = source_name
                self.data = filtered
                return filtered
            except Exception:
                continue
        return pd.DataFrame(columns=["event_date"])
