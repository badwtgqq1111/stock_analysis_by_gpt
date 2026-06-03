#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Build stock tag registries from industry data and research evidence."""

import pandas as pd

from data.model import (
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    TAG_DICTIONARY_FIELDS,
    normalize_stock_tag_entry,
    normalize_tag_dictionary_entry,
    split_semicolon_tags,
)


def build_default_tag_dictionary():
    from data.ingest.tag_taxonomy import build_expanded_tag_dictionary

    return build_expanded_tag_dictionary()


def _clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _is_truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _add_tag(rows, row, tag, tag_type, confidence, source, evidence, is_primary=False):
    if not tag or tag in {"港股", "待人工确认", "未分类"}:
        return
    rows.append(
        normalize_stock_tag_entry(
            {
                "stock_code": row.get("stock_code"),
                "market": row.get("market") or "HK",
                "tag": tag,
                "tag_type": tag_type,
                "confidence": confidence,
                "is_primary": is_primary,
                "source": source,
                "evidence": evidence,
            }
        )
    )


def build_stock_tags_from_industry_registry(industry_frame):
    formal_rows = []
    candidate_rows = []
    for _, row in industry_frame.fillna("").iterrows():
        l1 = _clean(row.get("industry_l1"))
        l2 = _clean(row.get("industry_l2"))
        source = _clean(row.get("industry_source")) or "industry_registry"
        instrument_type = _clean(row.get("instrument_type"))
        is_fund_like = _is_truthy(row.get("is_fund_like"))
        if l1 == "未分类" or l2 == "待人工确认":
            candidate_rows.append(
                normalize_stock_tag_entry(
                    {
                        "stock_code": row.get("stock_code"),
                        "market": row.get("market") or "HK",
                        "tag": "待人工确认",
                        "tag_type": "industry",
                        "confidence": 0.2,
                        "source": source,
                        "evidence": "industry registry marked as unclassified",
                        "review_status": "pending",
                    },
                    candidate=True,
                )
            )
            continue
        _add_tag(formal_rows, row, l1, "industry", 0.90, source, "industry_l1", is_primary=True)
        _add_tag(formal_rows, row, l2, "industry", 0.92, source, "industry_l2", is_primary=True)
        for tag in split_semicolon_tags(row.get("theme_tags")):
            _add_tag(formal_rows, row, tag, "theme", 0.75, source, "theme_tags")
        if is_fund_like or instrument_type == "fund_like":
            if l2 == "地产投资信托基金":
                _add_tag(formal_rows, row, "REIT", "instrument", 0.95, source, "fund-like instrument inference", True)
            elif "ETF" in l2 or "交易所买卖" in l2:
                _add_tag(formal_rows, row, "ETF", "instrument", 0.95, source, "fund-like instrument inference", True)
    formal = pd.DataFrame(formal_rows, columns=STOCK_TAG_FIELDS)
    if not formal.empty:
        formal = formal.drop_duplicates(
            subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
        )
    candidates = pd.DataFrame(candidate_rows, columns=STOCK_TAG_CANDIDATE_FIELDS)
    return formal.reset_index(drop=True), candidates.reset_index(drop=True)


def merge_research_tags(formal, candidates, evidence_frame):
    """Merge company research-derived tags into formal and candidate tag frames."""
    from data.ingest.providers.hk_company_research import extract_tags_from_research_evidence

    research_formal, research_candidates = extract_tags_from_research_evidence(evidence_frame)
    merged_formal = pd.concat([formal, research_formal], ignore_index=True) if formal is not None else research_formal
    merged_candidates = (
        pd.concat([candidates, research_candidates], ignore_index=True)
        if candidates is not None
        else research_candidates
    )
    if not merged_formal.empty:
        merged_formal = merged_formal.drop_duplicates(
            subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
        ).reset_index(drop=True)
    if not merged_candidates.empty:
        merged_candidates = merged_candidates.drop_duplicates(
            subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
        ).reset_index(drop=True)
    return merged_formal, merged_candidates
