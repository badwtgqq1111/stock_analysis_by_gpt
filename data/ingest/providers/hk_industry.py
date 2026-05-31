#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股行业分类补全。"""

from datetime import datetime

import pandas as pd

from data.ingest.providers.hk_common import ak, build_source_priority, normalize_hk_stock_code


class HKIndustryFetcher:
    """Fetch HK stock industry metadata from F10-style public sources."""

    INDUSTRY_L1_KEYS = (
        "一级行业",
        "行业类别",
        "板块",
        "所属板块",
        "sector",
    )
    INDUSTRY_L2_KEYS = (
        "行业",
        "所属行业",
        "BELONG_INDUSTRY",
        "二级行业",
        "细分行业",
        "主营行业",
        "业务分类",
        "industry",
        "sub_industry",
    )
    INDUSTRY_L3_KEYS = (
        "三级行业",
        "子行业",
        "sub_sector",
    )
    THEME_KEYS = (
        "概念题材",
        "题材",
        "概念",
        "主题",
        "theme_tags",
    )
    L2_TO_L1 = {
        # 金融业
        "银行": "金融业",
        "保险": "金融业",
        "证券": "金融业",
        "其他金融": "金融业",
        "投资及资产管理": "金融业",
        "地产投资": "金融业",
        "金融服务": "金融业",
        # 资讯科技业
        "软件服务": "资讯科技业",
        "资讯科技器材": "资讯科技业",
        "半导体": "资讯科技业",
        "电子元器件": "资讯科技业",
        "互联网服务": "资讯科技业",
        # 电讯业
        "电讯": "电讯业",
        "电讯服务": "电讯业",
        # 医疗保健业
        "药品及生物科技": "医疗保健业",
        "医疗保健设备和服务": "医疗保健业",
        "医疗服务": "医疗保健业",
        "其他医疗保健": "医疗保健业",
        # 非必需性消费
        "汽车": "非必需性消费",
        "汽车及零部件": "非必需性消费",
        "旅游及消闲设施": "非必需性消费",
        "酒店及消闲": "非必需性消费",
        "媒体及娱乐": "非必需性消费",
        "纺织及服饰": "非必需性消费",
        "家庭电器及用品": "非必需性消费",
        "零售": "非必需性消费",
        "支援服务": "非必需性消费",
        # 必需性消费
        "食物饮品": "必需性消费",
        "食品饮料": "必需性消费",
        "农业产品": "必需性消费",
        "超市及便利店": "必需性消费",
        "个人护理": "必需性消费",
        # 地产业
        "地产": "地产业",
        "物业": "地产业",
        "地产发展商": "地产业",
        "地产投资信托基金": "地产业",
        # 工业
        "工业工程": "工业",
        "工业制造": "工业",
        "工用支援": "工业",
        "运输": "工业",
        "航空": "工业",
        "航运及港口": "工业",
        "建筑": "工业",
        "建筑材料": "工业",
        "环保": "工业",
        # 原材料业
        "原材料": "原材料业",
        "一般金属及矿石": "原材料业",
        "金属及采矿": "原材料业",
        "黄金及贵金属": "原材料业",
        "化工": "原材料业",
        "林木及纸制品": "原材料业",
        # 能源业
        "能源": "能源业",
        "石油及天然气": "能源业",
        "煤炭": "能源业",
        # 公用事业
        "公用事业": "公用事业",
        "电力": "公用事业",
        "燃气": "公用事业",
        "水务": "公用事业",
        # 综合企业
        "综合企业": "综合企业",
    }

    def __init__(self, stock_code, data_source=None, source_priority=None):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.source_priority = build_source_priority(data_source, source_priority)
        self.last_successful_source = None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return None
        if pd.isna(value):
            return None
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null", "-", "--", "n/a"}:
            return None
        return text

    @classmethod
    def _flatten_frame(cls, frame):
        if frame is None or frame.empty:
            return {}

        working = frame.copy()
        flattened = {}

        if working.shape[1] >= 2:
            key_col = working.columns[0]
            value_col = working.columns[1]
            for _, row in working.iterrows():
                key = cls._clean_text(row.get(key_col))
                value = cls._clean_text(row.get(value_col))
                if key and value:
                    flattened[key] = value

        if len(working) == 1:
            row = working.iloc[0]
            for column in working.columns:
                value = cls._clean_text(row.get(column))
                if value:
                    flattened[str(column)] = value

        for column in working.columns:
            for value in working[column].dropna().head(20):
                text = cls._clean_text(value)
                if text and "：" in text:
                    key, raw_value = text.split("：", 1)
                    parsed_value = cls._clean_text(raw_value)
                    if key.strip() and parsed_value:
                        flattened[key.strip()] = parsed_value

        return flattened

    @classmethod
    def _pick_first(cls, mapping, keys):
        if not mapping:
            return None
        normalized = {str(key).strip().lower(): value for key, value in mapping.items()}
        for key in keys:
            value = mapping.get(key)
            if cls._clean_text(value):
                return cls._clean_text(value)
            value = normalized.get(str(key).strip().lower())
            if cls._clean_text(value):
                return cls._clean_text(value)
        for key, value in mapping.items():
            key_text = str(key).strip().lower()
            if any(str(candidate).strip().lower() in key_text for candidate in keys):
                if cls._clean_text(value):
                    return cls._clean_text(value)
        return None

    @classmethod
    def _infer_l1_from_l2(cls, industry_l2):
        text = cls._clean_text(industry_l2)
        if not text:
            return None
        if text in cls.L2_TO_L1:
            return cls.L2_TO_L1[text]
        for keyword, industry_l1 in cls.L2_TO_L1.items():
            if keyword and keyword in text:
                return industry_l1
        return None

    @classmethod
    def _normalize_industry_levels(cls, industry_l1, industry_l2, source_name):
        l1 = cls._clean_text(industry_l1)
        l2 = cls._clean_text(industry_l2)

        inferred_from_l2 = cls._infer_l1_from_l2(l2)
        if inferred_from_l2:
            return inferred_from_l2, l2

        # Eastmoney's BELONG_INDUSTRY is usually a fine-grained industry name.
        # Treat a lone value such as "软件服务" or "银行" as L2 and infer L1.
        inferred_from_l1_text = cls._infer_l1_from_l2(l1)
        if inferred_from_l1_text and (not l2 or l2 == l1):
            return inferred_from_l1_text, l1

        return l1, l2

    @classmethod
    def _parse_industry_payload(cls, frame, source_name, stock_code):
        mapping = cls._flatten_frame(frame)
        industry_l1 = cls._pick_first(mapping, cls.INDUSTRY_L1_KEYS)
        industry_l2 = cls._pick_first(mapping, cls.INDUSTRY_L2_KEYS)
        industry_l3 = cls._pick_first(mapping, cls.INDUSTRY_L3_KEYS)
        theme_tags = cls._pick_first(mapping, cls.THEME_KEYS)
        industry_l1, industry_l2 = cls._normalize_industry_levels(industry_l1, industry_l2, source_name)

        if not any([industry_l1, industry_l2, industry_l3, theme_tags]):
            return None

        return {
            "stock_code": normalize_hk_stock_code(stock_code),
            "industry_l1": industry_l1,
            "industry_l2": industry_l2,
            "industry_l3": industry_l3,
            "theme_tags": theme_tags,
            "industry_source": source_name,
            "industry_updated_at": datetime.utcnow().isoformat(),
        }

    def _fetch_eastmoney_company_profile(self):
        if ak is None:
            raise ImportError("akshare 未安装")
        frame = ak.stock_hk_company_profile_em(symbol=self.stock_code)
        return self._parse_industry_payload(frame, "akshare_eastmoney_company_profile", self.stock_code)

    def _fetch_eastmoney_security_profile(self):
        if ak is None:
            raise ImportError("akshare 未安装")
        frame = ak.stock_hk_security_profile_em(symbol=self.stock_code)
        return self._parse_industry_payload(frame, "akshare_eastmoney_security_profile", self.stock_code)

    def fetch(self):
        fetchers = {
            "akshare_eastmoney": (
                self._fetch_eastmoney_company_profile,
                self._fetch_eastmoney_security_profile,
            ),
            "eastmoney": (
                self._fetch_eastmoney_company_profile,
                self._fetch_eastmoney_security_profile,
            ),
        }

        tried = set()
        for source_name in self.source_priority:
            for fetcher in fetchers.get(source_name, ()):
                fetcher_name = getattr(fetcher, "__name__", str(fetcher))
                if fetcher_name in tried:
                    continue
                tried.add(fetcher_name)
                try:
                    payload = fetcher()
                    if payload:
                        self.last_successful_source = payload.get("industry_source")
                        return payload
                except Exception:
                    continue
        return None
