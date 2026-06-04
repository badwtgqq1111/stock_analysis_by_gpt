#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""SearXNG local search provider for company tag evidence."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime

import requests

from data.ingest.providers.hk_common import normalize_hk_stock_code


DEFAULT_SEARXNG_URL = "http://127.0.0.1:8888"
SEARCH_SOURCE = "searxng_search"
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


def normalize_searxng_search_result(
    stock_code,
    query,
    result_rank,
    title,
    url,
    content,
    engine="",
    score="",
    raw_text="",
    fetched_at=None,
):
    code = normalize_hk_stock_code(stock_code)
    query = _clean(query)
    title = _clean(title)
    url = _clean(url)
    content = _clean(content)
    raw_text = _clean(raw_text)
    digest = _content_hash(code, query, url, title, content, engine, score, raw_text)
    return {
        "stock_code": code,
        "market": "HK",
        "source": SEARCH_SOURCE,
        "title": f"query={query}; rank={int(result_rank)}; title={title}",
        "summary": (
            f"rank={int(result_rank)}; engine={_clean(engine)}; score={_clean(score)}; "
            f"url={url}; hash={digest}; snippet={content[:360]}"
        ),
        "url": url,
        "raw_text": raw_text or content,
        "fetched_at": fetched_at or datetime.utcnow().isoformat(),
    }


class SearxngCompanySearchFetcher:
    """Fetch SearXNG evidence for one HK stock."""

    def __init__(
        self,
        stock_code,
        company_name="",
        *,
        searxng_url=None,
        max_results_per_query=5,
        max_queries_per_stock=3,
        engines=None,
        language="zh-CN",
        categories="general",
    ):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.company_name = _clean(company_name)
        self.searxng_url = (searxng_url or os.environ.get("SEARXNG_URL") or DEFAULT_SEARXNG_URL).rstrip("/")
        self.max_results_per_query = int(max_results_per_query)
        self.max_queries_per_stock = int(max_queries_per_stock)
        self.engines = _clean(engines)
        self.language = _clean(language) or "zh-CN"
        self.categories = _clean(categories) or "general"

    def build_queries(self):
        name = self.company_name or self.stock_code
        queries = [
            f"{self.stock_code} {name} 港股 主营业务 年报 收入分部",
            f"{self.stock_code}.HK {name} company profile business segments annual report",
            f"{self.stock_code} {name} AI 云服务 游戏 铜矿 铁矿 稳定币 业务",
        ]
        return queries[: max(0, self.max_queries_per_stock)]

    def fetch(self):
        rows = []
        for query in self.build_queries():
            try:
                data = self._search(query)
            except Exception as exc:
                rows.append(
                    normalize_searxng_search_result(
                        stock_code=self.stock_code,
                        query=query,
                        result_rank=0,
                        title="search_error",
                        url=f"{self.searxng_url}/search",
                        content=str(exc)[:500],
                        raw_text=str(exc),
                    )
                )
                continue

            results = data.get("results") or []
            if not results:
                rows.append(
                    normalize_searxng_search_result(
                        stock_code=self.stock_code,
                        query=query,
                        result_rank=0,
                        title="no_results",
                        url=f"{self.searxng_url}/search",
                        content="SearXNG returned no results",
                    )
                )
                continue

            for rank, result in enumerate(results[: self.max_results_per_query], start=1):
                rows.append(
                    normalize_searxng_search_result(
                        stock_code=self.stock_code,
                        query=query,
                        result_rank=rank,
                        title=result.get("title"),
                        url=result.get("url"),
                        content=result.get("content"),
                        engine=result.get("engine") or ";".join(result.get("engines") or []),
                        score=result.get("score"),
                        raw_text=result.get("content"),
                    )
                )
        return rows

    def _search(self, query):
        params = {
            "q": query,
            "format": "json",
            "language": self.language,
            "categories": self.categories,
        }
        if self.engines:
            params["engines"] = self.engines
        response = requests.get(
            f"{self.searxng_url}/search",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise RuntimeError(f"SearXNG API error {response.status_code}: {response.text[:500]}")
        return response.json()
