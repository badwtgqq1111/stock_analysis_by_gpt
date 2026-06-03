#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Online HK company research evidence fetchers."""

from datetime import datetime

import pandas as pd

from data.ingest.providers.hk_common import ak, normalize_hk_stock_code
from data.model import (
    COMPANY_RESEARCH_EVIDENCE_FIELDS,
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    normalize_stock_tag_entry,
)


KEYWORD_TAG_RULES = [
    ("网络游戏", "游戏", "business", 0.90),
    ("游戏", "游戏", "business", 0.85),
    ("云服务", "云服务", "business", 0.85),
    ("云计算", "云服务", "business", 0.82),
    ("人工智能", "AI", "theme", 0.78),
    ("大模型", "AI", "theme", 0.78),
    ("AIGC", "AI", "theme", 0.78),
    ("算力", "算力", "value_chain", 0.82),
    ("数据中心", "算力", "value_chain", 0.80),
    ("铜矿", "铜", "resource", 0.90),
    ("铜资源", "铜", "resource", 0.88),
    ("铁矿", "铁矿", "resource", 0.90),
    ("铁矿石", "铁矿", "resource", 0.90),
    ("黄金", "黄金", "resource", 0.88),
    ("煤炭", "煤炭", "resource", 0.88),
    ("原油", "原油", "resource", 0.86),
    ("物业管理", "物业管理", "business", 0.88),
    ("稳定币", "token", "theme", 0.76),
    ("代币", "token", "theme", 0.74),
    ("RWA", "token", "theme", 0.74),
]


def _clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _flatten_profile_frame(frame):
    if frame is None or frame.empty:
        return ""
    parts = []
    if frame.shape[1] >= 2:
        key_col, value_col = frame.columns[:2]
        for _, row in frame.iterrows():
            key = _clean(row.get(key_col))
            value = _clean(row.get(value_col))
            if key and value:
                parts.append(f"{key}: {value}")
    if not parts:
        for _, row in frame.iterrows():
            values = [_clean(value) for value in row.tolist()]
            values = [value for value in values if value]
            if values:
                parts.append(" ".join(values))
    return "\n".join(parts)


class HKCompanyResearchFetcher:
    """Fetch reproducible online evidence for a HK stock."""

    def __init__(self, stock_code):
        self.stock_code = normalize_hk_stock_code(stock_code)

    def fetch(self):
        if ak is None:
            raise ImportError("akshare 未安装")
        rows = []
        fetched_at = datetime.utcnow().isoformat()
        frame = ak.stock_hk_company_profile_em(symbol=self.stock_code)
        raw_text = _flatten_profile_frame(frame)
        if raw_text:
            rows.append(
                {
                    "stock_code": self.stock_code,
                    "market": "HK",
                    "source": "akshare_eastmoney_company_profile",
                    "title": "company_profile",
                    "summary": raw_text[:500],
                    "url": "",
                    "raw_text": raw_text,
                    "fetched_at": fetched_at,
                }
            )
        return pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS).to_dict("records")


def extract_tags_from_research_evidence(evidence_frame):
    formal_rows = []
    candidate_rows = []
    if evidence_frame is None or evidence_frame.empty:
        return (
            pd.DataFrame(columns=STOCK_TAG_FIELDS),
            pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS),
        )
    for _, row in evidence_frame.fillna("").iterrows():
        text = f"{row.get('title', '')}\n{row.get('summary', '')}\n{row.get('raw_text', '')}"
        for keyword, tag, tag_type, confidence in KEYWORD_TAG_RULES:
            if keyword not in text:
                continue
            target = formal_rows if confidence >= 0.75 else candidate_rows
            target.append(
                normalize_stock_tag_entry(
                    {
                        "stock_code": row.get("stock_code"),
                        "market": row.get("market") or "HK",
                        "tag": tag,
                        "tag_type": tag_type,
                        "confidence": confidence,
                        "is_primary": confidence >= 0.85,
                        "source": row.get("source") or "company_research",
                        "evidence": f"keyword={keyword}; {str(row.get('summary', ''))[:180]}",
                        "evidence_url": row.get("url"),
                    },
                    candidate=confidence < 0.75,
                )
            )
    formal = pd.DataFrame(formal_rows, columns=STOCK_TAG_FIELDS)
    if not formal.empty:
        formal = formal.drop_duplicates(
            subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
        )
    candidates = pd.DataFrame(candidate_rows, columns=STOCK_TAG_CANDIDATE_FIELDS)
    if not candidates.empty:
        candidates = candidates.drop_duplicates(
            subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
        )
    return formal.reset_index(drop=True), candidates.reset_index(drop=True)
