#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Tavily Search API provider for company tag evidence."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime

import requests

from data.ingest.providers.hk_common import normalize_hk_stock_code


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SEARCH_SOURCE = "tavily_search"
REQUEST_TIMEOUT = 30


def _clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _content_hash(*parts):
    text = "\n".join(_clean(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_tavily_search_result(
    stock_code,
    query,
    result_rank,
    title,
    url,
    content,
    raw_content="",
    score="",
    request_id="",
    fetched_at=None,
):
    code = normalize_hk_stock_code(stock_code)
    query = _clean(query)
    title = _clean(title)
    url = _clean(url)
    content = _clean(content)
    raw_content = _clean(raw_content)
    digest = _content_hash(code, query, url, title, content, raw_content, request_id)
    return {
        "stock_code": code,
        "market": "HK",
        "source": SEARCH_SOURCE,
        "title": f"query={query}; rank={int(result_rank)}; title={title}",
        "summary": (
            f"rank={int(result_rank)}; score={_clean(score)}; request_id={_clean(request_id)}; "
            f"url={url}; hash={digest}; snippet={content[:360]}"
        ),
        "url": url,
        "raw_text": raw_content or content,
        "fetched_at": fetched_at or datetime.utcnow().isoformat(),
    }


class TavilyCompanySearchFetcher:
    """Fetch Tavily search evidence for one HK stock."""

    def __init__(
        self,
        stock_code,
        company_name="",
        *,
        api_key=None,
        max_results_per_query=5,
        max_queries_per_stock=3,
        search_depth="basic",
        topic="finance",
        include_raw_content=False,
    ):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.company_name = _clean(company_name)
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.max_results_per_query = int(max_results_per_query)
        self.max_queries_per_stock = int(max_queries_per_stock)
        self.search_depth = _clean(search_depth) or "basic"
        self.topic = _clean(topic) or "finance"
        self.include_raw_content = bool(include_raw_content)

    def build_queries(self):
        name = self.company_name or self.stock_code
        queries = [
            f"{self.stock_code} {name} 主营业务 年报 收入分部",
            f"{self.stock_code} {name} company profile business segments annual report",
            f"{self.stock_code} {name} AI 云服务 游戏 铜矿 铁矿 稳定币 业务",
        ]
        return queries[: max(0, self.max_queries_per_stock)]

    def fetch(self):
        if not self.api_key:
            raise RuntimeError("TAVILY_API_KEY is required for Tavily search")

        rows = []
        for query in self.build_queries():
            try:
                data = self._search(query)
            except Exception as exc:
                rows.append(
                    normalize_tavily_search_result(
                        stock_code=self.stock_code,
                        query=query,
                        result_rank=0,
                        title="search_error",
                        url=TAVILY_SEARCH_URL,
                        content=str(exc)[:500],
                        raw_content=str(exc),
                    )
                )
                continue

            request_id = data.get("request_id") or ""
            results = data.get("results") or []
            if not results:
                rows.append(
                    normalize_tavily_search_result(
                        stock_code=self.stock_code,
                        query=query,
                        result_rank=0,
                        title="no_results",
                        url=TAVILY_SEARCH_URL,
                        content="Tavily returned no results",
                        request_id=request_id,
                    )
                )
                continue

            for rank, result in enumerate(results[: self.max_results_per_query], start=1):
                rows.append(
                    normalize_tavily_search_result(
                        stock_code=self.stock_code,
                        query=query,
                        result_rank=rank,
                        title=result.get("title"),
                        url=result.get("url"),
                        content=result.get("content"),
                        raw_content=result.get("raw_content"),
                        score=result.get("score"),
                        request_id=request_id,
                    )
                )
        return rows

    def _search(self, query):
        payload = {
            "query": query,
            "search_depth": self.search_depth,
            "topic": self.topic,
            "max_results": self.max_results_per_query,
            "include_answer": False,
            "include_raw_content": self.include_raw_content,
            "include_images": False,
            "include_favicon": False,
            "include_usage": True,
        }
        response = requests.post(
            TAVILY_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Tavily API error {response.status_code}: {response.text[:500]}")
        return response.json()
