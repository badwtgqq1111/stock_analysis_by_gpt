#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Playwright browser search provider for company tag evidence."""

from __future__ import annotations

import hashlib
from datetime import datetime
from urllib.parse import quote_plus

from data.ingest.providers.hk_common import normalize_hk_stock_code


SEARCH_SOURCE = "playwright_search"
SUPPORTED_SEARCH_ENGINES = {"bing", "google"}


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


def normalize_browser_search_result(
    stock_code,
    query,
    result_rank,
    title,
    url,
    snippet,
    raw_text,
    fetched_at=None,
):
    code = normalize_hk_stock_code(stock_code)
    query = _clean(query)
    title = _clean(title)
    url = _clean(url)
    snippet = _clean(snippet)
    raw_text = _clean(raw_text)
    digest = _content_hash(code, query, url, title, snippet, raw_text)
    return {
        "stock_code": code,
        "market": "HK",
        "source": SEARCH_SOURCE,
        "title": f"query={query}; rank={int(result_rank)}; title={title}",
        "summary": f"rank={int(result_rank)}; url={url}; hash={digest}; snippet={snippet[:360]}",
        "url": url,
        "raw_text": raw_text or snippet,
        "fetched_at": fetched_at or datetime.utcnow().isoformat(),
    }


class BrowserCompanySearchFetcher:
    """Fetch browser-search evidence for one HK stock."""

    def __init__(
        self,
        stock_code,
        company_name="",
        *,
        max_results_per_query=5,
        max_pages_per_stock=8,
        per_page_timeout=12,
        search_engine="bing",
    ):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.company_name = _clean(company_name)
        self.max_results_per_query = int(max_results_per_query)
        self.max_pages_per_stock = int(max_pages_per_stock)
        self.per_page_timeout = int(per_page_timeout)
        self.search_engine = _clean(search_engine).lower() or "bing"
        if self.search_engine not in SUPPORTED_SEARCH_ENGINES:
            supported = ", ".join(sorted(SUPPORTED_SEARCH_ENGINES))
            raise ValueError(f"unsupported search_engine={search_engine!r}; supported: {supported}")

    def build_queries(self):
        name = self.company_name or self.stock_code
        return [
            f"{self.stock_code} {name} 主营业务 年报",
            f"{self.stock_code} {name} company profile business segments",
            f"{self.stock_code} {name} 港交所 公告 年报",
            f"{self.stock_code} {name} AI 云服务 游戏 铜矿 铁矿 稳定币",
            f"{self.stock_code} {name} 业务 风险 地区 收入",
        ]

    def fetch(self):
        from playwright.sync_api import sync_playwright

        rows = []
        pages_seen = 0
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(self.per_page_timeout * 1000)
            try:
                for query in self.build_queries():
                    if pages_seen >= self.max_pages_per_stock:
                        break
                    url = self._build_search_url(query)
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                    except Exception as exc:
                        rows.append(
                            normalize_browser_search_result(
                                stock_code=self.stock_code,
                                query=query,
                                result_rank=0,
                                title="search_error",
                                url=url,
                                snippet=str(exc)[:500],
                                raw_text=str(exc),
                            )
                        )
                        pages_seen += 1
                        continue
                    results = page.locator("a").all()[: self.max_results_per_query * 3]
                    rank = 0
                    for link in results:
                        href = link.get_attribute("href") or ""
                        text = link.inner_text(timeout=1000) or ""
                        if not href.startswith("http"):
                            continue
                        rank += 1
                        rows.append(
                            normalize_browser_search_result(
                                stock_code=self.stock_code,
                                query=query,
                                result_rank=rank,
                                title=text[:160],
                                url=href,
                                snippet=text[:500],
                                raw_text=text[:2000],
                            )
                        )
                        pages_seen += 1
                        if rank >= self.max_results_per_query or pages_seen >= self.max_pages_per_stock:
                            break
                    if rank == 0:
                        text = page.locator("body").inner_text(timeout=1000)[:2000]
                        rows.append(
                            normalize_browser_search_result(
                                stock_code=self.stock_code,
                                query=query,
                                result_rank=0,
                                title="search_page_snapshot",
                                url=url,
                                snippet=text[:500],
                                raw_text=text,
                            )
                        )
                        pages_seen += 1
            finally:
                browser.close()
        return rows

    def _build_search_url(self, query):
        encoded = quote_plus(query)
        if self.search_engine == "google":
            return f"https://www.google.com/search?q={encoded}"
        return f"https://www.bing.com/search?q={encoded}"
