#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""DeepSeek structured tag extraction from company evidence."""

from __future__ import annotations

import json
import re

import pandas as pd

from data.model import (
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    normalize_stock_code,
    normalize_stock_tag_entry,
)


PROMPT_VERSION = "stock_tag_extract_v1"


def strip_json_markdown(text):
    text = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if match:
        return match.group(1).strip()
    return text


def parse_llm_tag_response(text):
    payload = json.loads(strip_json_markdown(text))
    if "stock_code" not in payload:
        raise ValueError("LLM response missing stock_code")
    if "tags" not in payload or not isinstance(payload["tags"], list):
        raise ValueError("LLM response missing tags list")
    payload["stock_code"] = normalize_stock_code(payload["stock_code"], market="HK")
    return payload


def parse_llm_tag_batch_response(text):
    payload = json.loads(strip_json_markdown(text))
    if isinstance(payload, dict) and "stocks" in payload:
        stocks = payload["stocks"]
    elif isinstance(payload, list):
        stocks = payload
    else:
        return [parse_llm_tag_response(text)]
    if not isinstance(stocks, list):
        raise ValueError("LLM batch response stocks must be a list")
    parsed = []
    for stock_payload in stocks:
        if "stock_code" not in stock_payload:
            raise ValueError("LLM batch response stock missing stock_code")
        if "tags" not in stock_payload or not isinstance(stock_payload["tags"], list):
            raise ValueError("LLM batch response stock missing tags list")
        stock_payload["stock_code"] = normalize_stock_code(stock_payload["stock_code"], market="HK")
        parsed.append(stock_payload)
    return parsed


def llm_extractions_to_tag_frames(extractions, source="deepseek_browser_evidence"):
    formal_rows = []
    candidate_rows = []
    for extraction in extractions:
        stock_code = normalize_stock_code(extraction.get("stock_code"), market="HK")
        for tag in extraction.get("tags", []):
            confidence = float(tag.get("confidence") or 0)
            decision = str(tag.get("decision") or "").lower()
            payload = {
                "stock_code": stock_code,
                "market": "HK",
                "tag": tag.get("tag"),
                "tag_type": tag.get("tag_type"),
                "confidence": confidence,
                "is_primary": tag.get("is_primary"),
                "source": source,
                "evidence": tag.get("evidence") or tag.get("reason") or "",
                "evidence_url": tag.get("evidence_url") or "",
            }
            if decision == "formal":
                formal_rows.append(normalize_stock_tag_entry(payload))
            elif decision == "candidate":
                payload["review_status"] = "pending"
                payload["review_note"] = tag.get("reason") or "llm candidate"
                candidate_rows.append(normalize_stock_tag_entry(payload, candidate=True))
    return (
        pd.DataFrame(formal_rows, columns=STOCK_TAG_FIELDS),
        pd.DataFrame(candidate_rows, columns=STOCK_TAG_CANDIDATE_FIELDS),
    )


def build_tag_extraction_prompt(stock_code, evidence_rows, tag_dictionary_frame):
    dictionary_columns = ["tag", "tag_type", "aliases", "parent_tag", "description"]
    evidence_columns = ["source", "title", "summary", "url", "raw_text"]
    dictionary = tag_dictionary_frame.reindex(columns=dictionary_columns).fillna("").to_dict("records")
    evidence = evidence_rows.reindex(columns=evidence_columns).fillna("").head(20).to_dict("records")
    return [
        {
            "role": "system",
            "content": (
                "你是港股公司研究标签抽取器。只输出 JSON，不要输出解释。"
                "标签必须基于证据，不能把客户行业误判成公司自身暴露。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "stock_code": stock_code,
                    "allowed_tags": dictionary,
                    "evidence": evidence,
                    "prompt_version": PROMPT_VERSION,
                    "output_schema": {
                        "stock_code": "string",
                        "company_name": "string",
                        "tags": [
                            {
                                "tag": "string",
                                "tag_type": "theme|resource|value_chain|business|risk|geo|style|instrument|industry",
                                "confidence": "number 0-1",
                                "is_primary": "boolean",
                                "evidence": "string",
                                "evidence_url": "string",
                                "decision": "formal|candidate|reject",
                                "reason": "string",
                            }
                        ],
                        "rejected": [{"tag": "string", "reason": "string"}],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def build_tag_batch_extraction_prompt(stock_evidence_rows, tag_dictionary_frame):
    dictionary_columns = ["tag", "tag_type", "aliases", "parent_tag", "description"]
    evidence_columns = ["source", "title", "summary", "url", "raw_text"]
    dictionary = tag_dictionary_frame.reindex(columns=dictionary_columns).fillna("").to_dict("records")
    stocks = []
    for stock_code, evidence_rows in stock_evidence_rows:
        evidence = evidence_rows.reindex(columns=evidence_columns).fillna("").head(12).to_dict("records")
        stocks.append({"stock_code": stock_code, "evidence": evidence})
    return [
        {
            "role": "system",
            "content": (
                "你是港股公司研究标签抽取器。只输出 JSON，不要输出解释。"
                "标签必须基于证据，不能把客户行业误判成公司自身暴露。"
                "必须为输入中的每个 stock_code 返回一个结果。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "allowed_tags": dictionary,
                    "stocks": stocks,
                    "prompt_version": f"{PROMPT_VERSION}_batch",
                    "output_schema": {
                        "stocks": [
                            {
                                "stock_code": "string",
                                "company_name": "string",
                                "tags": [
                                    {
                                        "tag": "string",
                                        "tag_type": "theme|resource|value_chain|business|risk|geo|style|instrument|industry",
                                        "confidence": "number 0-1",
                                        "is_primary": "boolean",
                                        "evidence": "string",
                                        "evidence_url": "string",
                                        "decision": "formal|candidate|reject",
                                        "reason": "string",
                                    }
                                ],
                                "rejected": [{"tag": "string", "reason": "string"}],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
