#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stock tag registry tests."""

import json
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import (
    COMPANY_RESEARCH_EVIDENCE_FIELDS,
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    TAG_DICTIONARY_FIELDS,
    normalize_tag_dictionary_entry,
    normalize_stock_tag_entry,
    split_semicolon_tags,
)


def test_split_semicolon_tags_deduplicates_and_rejects_comma_separator():
    assert split_semicolon_tags("港股;AI；AI; 算力,,云服务") == ["港股", "AI", "算力", "云服务"]


def test_normalize_tag_dictionary_entry_standardizes_required_fields():
    row = normalize_tag_dictionary_entry(
        {
            "tag": " 人工智能 ",
            "tag_type": "theme",
            "aliases": ["AI", "AIGC", "大模型"],
            "parent_tag": "科技",
        }
    )

    assert list(row.keys()) == TAG_DICTIONARY_FIELDS
    assert row["tag"] == "人工智能"
    assert row["canonical_tag"] == "人工智能"
    assert row["aliases"] == "AI;AIGC;大模型"
    assert row["active"] is True


def test_normalize_stock_tag_entry_clamps_confidence_and_requires_source():
    row = normalize_stock_tag_entry(
        {
            "stock_code": "700",
            "tag": "游戏",
            "tag_type": "business",
            "confidence": "1.2",
            "is_primary": "true",
            "source": "company_profile",
            "evidence": "主营业务包含网络游戏",
        }
    )

    assert list(row.keys()) == STOCK_TAG_FIELDS
    assert row["stock_code"] == "00700"
    assert row["market"] == "HK"
    assert row["confidence"] == 1.0
    assert row["is_primary"] is True


def test_normalize_stock_tag_entry_rejects_missing_tag_or_source():
    with pytest.raises(ValueError, match="tag"):
        normalize_stock_tag_entry({"stock_code": "00700", "source": "manual"})
    with pytest.raises(ValueError, match="source"):
        normalize_stock_tag_entry({"stock_code": "00700", "tag": "AI"})


from data.ingest.stock_tags import (
    build_default_tag_dictionary,
    build_stock_tags_from_industry_registry,
    merge_research_tags,
)
from data.ingest.tag_taxonomy import build_expanded_tag_dictionary


def test_build_default_tag_dictionary_contains_precise_core_tags():
    dictionary = build_default_tag_dictionary()

    tags = set(dictionary["tag"])
    assert {"AI", "算力", "铜", "铁矿", "token", "游戏", "ETF"}.issubset(tags)
    assert dictionary.loc[dictionary["tag"].eq("AI"), "aliases"].iloc[0] == "人工智能;AIGC;大模型;Agent"


def test_expanded_tag_dictionary_contains_investable_specific_tags():
    dictionary = build_expanded_tag_dictionary()
    tags = set(dictionary["tag"])

    assert {"AI", "大模型", "稳定币", "铜", "锂", "光模块", "创新药", "博彩", "航运"}.issubset(tags)
    assert len(dictionary) >= 120
    assert dictionary["aliases"].astype(str).str.contains(",").sum() == 0


def test_build_stock_tags_from_industry_registry_splits_formal_and_candidates():
    industry = pd.DataFrame(
        [
            {
                "stock_code": "00700",
                "market": "HK",
                "industry_l1": "资讯科技业",
                "industry_l2": "软件服务",
                "theme_tags": "港股;资讯科技业;软件服务;科技",
                "instrument_type": "common_stock",
                "is_fund_like": "False",
            },
            {
                "stock_code": "02800",
                "market": "HK",
                "industry_l1": "基金",
                "industry_l2": "ETF及交易所买卖产品",
                "theme_tags": "港股;ETF;基金;被动产品",
                "instrument_type": "fund_like",
                "is_fund_like": "True",
            },
            {
                "stock_code": "02926",
                "market": "HK",
                "industry_l1": "未分类",
                "industry_l2": "待人工确认",
                "theme_tags": "港股;待人工确认;未分类",
                "instrument_type": "common_stock",
                "is_fund_like": "False",
            },
        ]
    )

    formal, candidates = build_stock_tags_from_industry_registry(industry)

    formal_pairs = set(zip(formal["stock_code"], formal["tag"], formal["tag_type"]))
    assert ("00700", "软件服务", "industry") in formal_pairs
    assert ("02800", "ETF", "instrument") in formal_pairs
    assert not formal["tag"].eq("待人工确认").any()
    assert set(candidates["stock_code"]) == {"02926"}
    assert candidates.iloc[0]["review_status"] == "pending"


from data.ingest.providers.hk_company_research import (
    HKCompanyResearchFetcher,
    extract_tags_from_research_evidence,
)
from data.ingest.providers.browser_company_search import normalize_browser_search_result
from data.ingest.llm_tag_extractor import (
    llm_extractions_to_tag_frames,
    parse_llm_tag_batch_response,
    parse_llm_tag_response,
)


def test_tavily_company_search_fetcher_normalizes_results(monkeypatch):
    from data.ingest.providers.tavily_company_search import TavilyCompanySearchFetcher

    requests = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "request_id": "req-1",
                "results": [
                    {
                        "title": "Tencent annual report",
                        "url": "https://example.com/tencent",
                        "content": "Tencent business segments include games, fintech and cloud.",
                        "score": 0.92,
                    }
                ],
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("requests.post", fake_post)

    rows = TavilyCompanySearchFetcher(
        "700",
        company_name="腾讯控股",
        max_results_per_query=1,
        max_queries_per_stock=1,
    ).fetch()

    assert requests[0]["headers"]["Authorization"] == "Bearer test-key"
    assert requests[0]["json"]["query"].startswith("00700 腾讯控股")
    assert rows[0]["stock_code"] == "00700"
    assert rows[0]["source"] == "tavily_search"
    assert "games" in rows[0]["raw_text"]


def test_tavily_company_search_fetcher_records_api_errors(monkeypatch):
    from data.ingest.providers.tavily_company_search import TavilyCompanySearchFetcher

    class FakeResponse:
        status_code = 429
        text = "rate limit"

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: FakeResponse())

    rows = TavilyCompanySearchFetcher(
        "00883",
        max_results_per_query=1,
        max_queries_per_stock=1,
    ).fetch()

    assert "title=search_error" in rows[0]["title"]
    assert "429" in rows[0]["raw_text"]


def test_searxng_company_search_fetcher_normalizes_results(monkeypatch):
    from data.ingest.providers.searxng_company_search import SearxngCompanySearchFetcher

    requests = []

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "results": [
                    {
                        "title": "投资者 - Tencent 腾讯",
                        "url": "https://www.tencent.com/zh-cn/investors/financial-reports.html",
                        "content": "腾讯年报披露游戏、广告、金融科技、云服务等业务。",
                        "engine": "duckduckgo",
                        "score": 1.0,
                    }
                ],
                "unresponsive_engines": [],
            }

    def fake_get(url, params=None, timeout=None):
        requests.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888")
    monkeypatch.setattr("requests.get", fake_get)

    rows = SearxngCompanySearchFetcher(
        "700",
        company_name="腾讯控股",
        max_results_per_query=1,
        max_queries_per_stock=1,
        engines="bing,duckduckgo",
    ).fetch()

    assert requests[0]["url"] == "http://127.0.0.1:8888/search"
    assert requests[0]["params"]["format"] == "json"
    assert requests[0]["params"]["engines"] == "bing,duckduckgo"
    assert requests[0]["params"]["q"].startswith("00700 腾讯控股")
    assert rows[0]["stock_code"] == "00700"
    assert rows[0]["source"] == "searxng_search"
    assert "云服务" in rows[0]["raw_text"]


def test_searxng_company_search_fetcher_records_api_errors(monkeypatch):
    from data.ingest.providers.searxng_company_search import SearxngCompanySearchFetcher

    class FakeResponse:
        status_code = 500
        text = "Internal Server Error"

        def json(self):
            return {}

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse())

    rows = SearxngCompanySearchFetcher(
        "00883",
        max_results_per_query=1,
        max_queries_per_stock=1,
    ).fetch()

    assert "title=search_error" in rows[0]["title"]
    assert "500" in rows[0]["raw_text"]


def test_normalize_browser_search_result_keeps_query_rank_and_hash():
    row = normalize_browser_search_result(
        stock_code="700",
        query="00700 腾讯 主营业务",
        result_rank=1,
        title="腾讯控股 年报",
        url="https://example.com/annual-report",
        snippet="主营业务包括网络游戏、金融科技、云服务。",
        raw_text="主营业务包括网络游戏、金融科技、云服务。",
    )

    assert row["stock_code"] == "00700"
    assert row["source"] == "playwright_search"
    assert "query=00700 腾讯 主营业务" in row["title"]
    assert "rank=1" in row["summary"]
    assert "网络游戏" in row["raw_text"]


def test_normalize_browser_search_result_supports_search_snapshot_rank_zero():
    row = normalize_browser_search_result(
        stock_code="00700",
        query="00700 腾讯 搜索",
        result_rank=0,
        title="search_page_snapshot",
        url="https://www.google.com/search?q=00700",
        snippet="Google returned no parseable result",
        raw_text="Google returned no parseable result",
    )

    assert "rank=0" in row["summary"]
    assert row["title"].endswith("title=search_page_snapshot")


def test_browser_company_search_fetcher_builds_bing_search_url(monkeypatch):
    from data.ingest.providers.browser_company_search import BrowserCompanySearchFetcher

    visited_urls = []

    class FakeLocator:
        def all(self):
            return []

        def inner_text(self, timeout=1000):
            return "No parseable search results"

    class FakePage:
        def set_default_timeout(self, timeout):
            self.timeout = timeout

        def goto(self, url, wait_until="domcontentloaded"):
            visited_urls.append(url)

        def locator(self, selector):
            return FakeLocator()

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeChromium:
        def launch(self, headless=True):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeSyncPlaywright:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakeSyncPlaywright(),
    )

    rows = BrowserCompanySearchFetcher(
        "00700",
        company_name="腾讯控股",
        search_engine="bing",
        max_results_per_query=1,
        max_pages_per_stock=1,
    ).fetch()

    assert visited_urls[0].startswith("https://www.bing.com/search?q=")
    assert rows[0]["url"].startswith("https://www.bing.com/search?q=")


def test_browser_company_search_fetcher_records_goto_errors(monkeypatch):
    from data.ingest.providers.browser_company_search import BrowserCompanySearchFetcher

    class FakePage:
        def set_default_timeout(self, timeout):
            pass

        def goto(self, url, wait_until="domcontentloaded"):
            raise RuntimeError("Page.goto: net::ERR_ABORTED")

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeChromium:
        def launch(self, headless=True):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeSyncPlaywright:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakeSyncPlaywright(),
    )

    rows = BrowserCompanySearchFetcher(
        "00883",
        search_engine="bing",
        max_pages_per_stock=1,
    ).fetch()

    assert len(rows) == 1
    assert "title=search_error" in rows[0]["title"]
    assert "ERR_ABORTED" in rows[0]["raw_text"]


def test_parse_llm_tag_response_strips_markdown_and_validates_tags():
    text = """```json
    {"stock_code":"00700","company_name":"腾讯控股","tags":[{"tag":"游戏","tag_type":"business","confidence":0.94,"is_primary":true,"evidence":"主营业务包含网络游戏","evidence_url":"https://example.com","decision":"formal","reason":"主营明确"}],"rejected":[]}
    ```"""
    parsed = parse_llm_tag_response(text)

    assert parsed["stock_code"] == "00700"
    assert parsed["tags"][0]["tag"] == "游戏"


def test_parse_llm_tag_batch_response_accepts_stocks_wrapper():
    text = """```json
    {"stocks":[
      {"stock_code":"700","tags":[{"tag":"游戏","tag_type":"business","confidence":0.94,"decision":"formal"}]},
      {"stock_code":"3690","tags":[]}
    ]}
    ```"""

    parsed = parse_llm_tag_batch_response(text)

    assert [item["stock_code"] for item in parsed] == ["00700", "03690"]


def test_llm_extractions_to_tag_frames_splits_formal_and_candidate():
    extraction = {
        "stock_code": "00700",
        "tags": [
            {
                "tag": "游戏",
                "tag_type": "business",
                "confidence": 0.94,
                "is_primary": True,
                "evidence": "主营业务",
                "evidence_url": "",
                "decision": "formal",
            },
            {
                "tag": "AI",
                "tag_type": "theme",
                "confidence": 0.68,
                "is_primary": False,
                "evidence": "新闻提及",
                "evidence_url": "",
                "decision": "candidate",
            },
        ],
    }

    formal, candidates = llm_extractions_to_tag_frames([extraction], source="deepseek_browser_evidence")

    assert set(formal["tag"]) == {"游戏"}
    assert set(candidates["tag"]) == {"AI"}


def test_company_research_fetcher_parses_profile_frame_without_network(monkeypatch):
    frame = pd.DataFrame(
        {
            "项目": ["公司名称", "主营业务"],
            "内容": ["腾讯控股有限公司", "提供增值服务、网络游戏、金融科技、云服务及广告业务。"],
        }
    )

    class FakeAk:
        @staticmethod
        def stock_hk_company_profile_em(symbol):
            assert symbol == "00700"
            return frame

    monkeypatch.setattr("data.ingest.providers.hk_company_research.ak", FakeAk)

    rows = HKCompanyResearchFetcher("700").fetch()

    assert rows[0]["stock_code"] == "00700"
    assert rows[0]["source"] == "akshare_eastmoney_company_profile"
    assert "网络游戏" in rows[0]["raw_text"]


def test_extract_tags_from_research_evidence_finds_specific_business_tags():
    evidence = pd.DataFrame(
        [
            {
                "stock_code": "00700",
                "market": "HK",
                "source": "unit",
                "title": "主营业务",
                "summary": "网络游戏、云服务、广告、金融科技",
                "raw_text": "网络游戏、云服务、广告、金融科技",
            },
            {
                "stock_code": "01208",
                "market": "HK",
                "source": "unit",
                "title": "主营业务",
                "summary": "铜矿、铁矿及黄金资源开发",
                "raw_text": "铜矿、铁矿及黄金资源开发",
            },
        ]
    )

    formal, candidates = extract_tags_from_research_evidence(evidence)
    pairs = set(zip(formal["stock_code"], formal["tag"], formal["tag_type"]))

    assert ("00700", "游戏", "business") in pairs
    assert ("00700", "云服务", "business") in pairs
    assert ("01208", "铜", "resource") in pairs
    assert ("01208", "铁矿", "resource") in pairs
    assert candidates.empty


from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse


def test_warehouse_upserts_and_reads_stock_tags():
    with tempfile.TemporaryDirectory() as tmp_dir:
        warehouse = MarketDataWarehouse(DataLayout(base_dir=tmp_dir))
        try:
            result = warehouse.upsert_stock_tags(
                pd.DataFrame(
                    [
                        {
                            "stock_code": "00700",
                            "market": "HK",
                            "tag": "游戏",
                            "tag_type": "business",
                            "confidence": 0.95,
                            "is_primary": True,
                            "source": "unit",
                            "evidence": "unit evidence",
                            "evidence_url": "",
                            "updated_at": "2026-06-03T00:00:00",
                        }
                    ]
                )
            )
            loaded = warehouse.read_stock_tags(stock_codes=["00700"], tag_type="business")
        finally:
            warehouse.close()

    assert result["rows"] == 1
    assert loaded.iloc[0]["tag"] == "游戏"
    assert loaded.iloc[0]["confidence"] == 0.95


from data.ingest.service import MarketDataService


def test_service_builds_tag_csvs_from_industry_registry():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        dictionary_csv = base / "hk_tag_dictionary.csv"
        registry_csv = base / "hk_stock_tag_registry.csv"
        candidate_csv = base / "hk_stock_tag_candidate.csv"
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "industry_l1": "资讯科技业",
                    "industry_l2": "软件服务",
                    "theme_tags": "港股;资讯科技业;软件服务;科技",
                    "instrument_type": "common_stock",
                    "is_fund_like": "False",
                }
            ]
        ).to_csv(industry_csv, index=False, encoding="utf-8-sig")

        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.build_stock_tag_csvs(
                industry_registry_csv=industry_csv,
                tag_dictionary_csv=dictionary_csv,
                output_csv=registry_csv,
                candidate_output_csv=candidate_csv,
            )
        finally:
            service.close()

        assert summary["stock_tag_rows"] > 0
        assert dictionary_csv.exists()
        assert registry_csv.exists()
        assert candidate_csv.exists()


def test_merge_research_tags_adds_precise_tags_and_keeps_candidates_separate():
    base_formal = pd.DataFrame(columns=STOCK_TAG_FIELDS)
    base_candidates = pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS)
    evidence = pd.DataFrame(
        [
            {
                "stock_code": "00700",
                "market": "HK",
                "source": "unit",
                "title": "主营业务",
                "summary": "网络游戏、云服务、稳定币相关探索",
                "url": "",
                "raw_text": "网络游戏、云服务、稳定币相关探索",
                "fetched_at": "2026-06-03T00:00:00",
            }
        ],
        columns=COMPANY_RESEARCH_EVIDENCE_FIELDS,
    )

    formal, candidates = merge_research_tags(base_formal, base_candidates, evidence)
    pairs = set(zip(formal["stock_code"], formal["tag"], formal["tag_type"]))

    assert ("00700", "游戏", "business") in pairs
    assert ("00700", "云服务", "business") in pairs
    assert candidates.empty


def test_service_research_stock_tags_uses_cache_and_limit(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_research_evidence.csv"
        pd.DataFrame(
            [
                {"stock_code": "00700", "market": "HK"},
                {"stock_code": "01208", "market": "HK"},
            ]
        ).to_csv(industry_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "source": "cached",
                    "title": "company_profile",
                    "summary": "cached",
                    "url": "",
                    "raw_text": "cached",
                    "fetched_at": "2026-06-03T00:00:00",
                }
            ],
            columns=COMPANY_RESEARCH_EVIDENCE_FIELDS,
        ).to_csv(evidence_csv, index=False, encoding="utf-8-sig")

        fetched_codes = []

        class FakeFetcher:
            def __init__(self, stock_code):
                self.stock_code = stock_code

            def fetch(self):
                fetched_codes.append(self.stock_code)
                return [
                    {
                        "stock_code": self.stock_code,
                        "market": "HK",
                        "source": "fake",
                        "title": "company_profile",
                        "summary": "铜矿",
                        "url": "",
                        "raw_text": "铜矿",
                        "fetched_at": "2026-06-03T00:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.HKCompanyResearchFetcher", FakeFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                limit=2,
            )
        finally:
            service.close()

        assert fetched_codes == ["01208"]
        assert summary["skipped_existing"] == 1
        assert summary["fetched"] == 1
        saved = pd.read_csv(evidence_csv, dtype=str).fillna("")
        assert set(saved["stock_code"]) == {"00700", "01208"}


def test_service_reports_tag_coverage():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            service.warehouse.upsert_stock_tags(
                pd.DataFrame(
                    [
                        {
                            "stock_code": "00700",
                            "market": "HK",
                            "tag": "游戏",
                            "tag_type": "business",
                            "confidence": 0.95,
                            "is_primary": True,
                            "source": "unit",
                            "evidence": "unit",
                            "evidence_url": "",
                            "updated_at": "2026-06-03T00:00:00",
                        },
                        {
                            "stock_code": "01208",
                            "market": "HK",
                            "tag": "铜",
                            "tag_type": "resource",
                            "confidence": 0.90,
                            "is_primary": True,
                            "source": "unit",
                            "evidence": "unit",
                            "evidence_url": "",
                            "updated_at": "2026-06-03T00:00:00",
                        },
                    ]
                )
            )
            report = service.get_stock_tag_coverage(market="HK", min_confidence=0.75)
        finally:
            service.close()

        assert report["status"] == "completed"
        assert report["tagged_stock_count"] == 2
        assert report["by_tag_type"]["business"] == 1
        assert report["top_tags"]["游戏"] == 1


def test_service_import_stock_tags_replace_removes_stale_tags():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        tag_csv = base / "tags.csv"
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "tag": "旧标签",
                    "tag_type": "theme",
                    "confidence": 0.95,
                    "is_primary": False,
                    "source": "unit",
                    "evidence": "old",
                    "evidence_url": "",
                    "updated_at": "2026-06-03T00:00:00",
                }
            ],
            columns=STOCK_TAG_FIELDS,
        ).to_csv(tag_csv, index=False, encoding="utf-8-sig")

        service = MarketDataService(base_dir=str(base / "data"))
        try:
            service.import_stock_tag_csvs(stock_tag_csv=tag_csv)
            pd.DataFrame(
                [
                    {
                        "stock_code": "00700",
                        "market": "HK",
                        "tag": "游戏",
                        "tag_type": "business",
                        "confidence": 0.95,
                        "is_primary": True,
                        "source": "unit",
                        "evidence": "new",
                        "evidence_url": "",
                        "updated_at": "2026-06-03T00:00:01",
                    }
                ],
                columns=STOCK_TAG_FIELDS,
            ).to_csv(tag_csv, index=False, encoding="utf-8-sig")
            service.import_stock_tag_csvs(stock_tag_csv=tag_csv, replace=True)
            tags = service.warehouse.read_stock_tags(stock_codes=["00700"])
        finally:
            service.close()

        assert set(tags["tag"]) == {"游戏"}


def test_service_browser_research_stock_tags_writes_evidence(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_browser_evidence.csv"
        pd.DataFrame([{"stock_code": "00700", "market": "HK", "name": "腾讯控股"}]).to_csv(
            industry_csv, index=False, encoding="utf-8-sig"
        )

        class FakeBrowserFetcher:
            def __init__(self, stock_code, company_name="", **kwargs):
                self.stock_code = stock_code
                self.company_name = company_name
                self.kwargs = kwargs

            def fetch(self):
                assert self.kwargs["search_engine"] == "bing"
                return [
                    {
                        "stock_code": "00700",
                        "market": "HK",
                        "source": "playwright_search",
                        "title": "query=00700 腾讯控股 主营业务; rank=1; title=公司资料",
                        "summary": "rank=1; url=https://example.com; snippet=网络游戏 云服务",
                        "url": "https://example.com",
                        "raw_text": "网络游戏 云服务",
                        "fetched_at": "2026-06-03T00:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.BrowserCompanySearchFetcher", FakeBrowserFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.browser_research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                limit=1,
                search_engine="bing",
            )
        finally:
            service.close()

        assert summary["evidence_rows"] == 1
        assert evidence_csv.exists()


def test_service_browser_research_does_not_skip_search_error_snapshots(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_browser_evidence.csv"
        pd.DataFrame([{"stock_code": "00700", "market": "HK", "name": "腾讯控股"}]).to_csv(
            industry_csv, index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "source": "playwright_search",
                    "title": "query=00700 腾讯控股; rank=0; title=search_error",
                    "summary": "rank=0; url=https://www.bing.com/search?q=00700; snippet=ERR_ABORTED",
                    "url": "https://www.bing.com/search?q=00700",
                    "raw_text": "Page.goto: net::ERR_ABORTED",
                    "fetched_at": "2026-06-03T00:00:00",
                }
            ]
        ).to_csv(evidence_csv, index=False, encoding="utf-8-sig")

        class FakeBrowserFetcher:
            def __init__(self, stock_code, company_name="", **kwargs):
                self.stock_code = stock_code

            def fetch(self):
                return [
                    {
                        "stock_code": "00700",
                        "market": "HK",
                        "source": "playwright_search",
                        "title": "query=00700 腾讯控股 主营业务; rank=1; title=公司资料",
                        "summary": "rank=1; url=https://example.com; snippet=网络游戏 云服务",
                        "url": "https://example.com",
                        "raw_text": "网络游戏 云服务",
                        "fetched_at": "2026-06-03T01:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.BrowserCompanySearchFetcher", FakeBrowserFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.browser_research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                skip_existing=True,
            )
        finally:
            service.close()

        assert summary["fetched"] == 1
        frame = pd.read_csv(evidence_csv)
        assert "公司资料" in "\n".join(frame["title"].astype(str))


def test_service_tavily_research_stock_tags_writes_evidence(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_tavily_evidence.csv"
        pd.DataFrame([{"stock_code": "00700", "market": "HK", "name": "腾讯控股"}]).to_csv(
            industry_csv, index=False, encoding="utf-8-sig"
        )

        class FakeTavilyFetcher:
            def __init__(self, stock_code, company_name="", **kwargs):
                self.stock_code = stock_code
                self.company_name = company_name
                self.kwargs = kwargs

            def fetch(self):
                assert self.kwargs["api_key"] == "test-key"
                return [
                    {
                        "stock_code": "00700",
                        "market": "HK",
                        "source": "tavily_search",
                        "title": "query=00700 腾讯控股; rank=1; title=公司资料",
                        "summary": "rank=1; url=https://example.com; snippet=网络游戏 云服务",
                        "url": "https://example.com",
                        "raw_text": "网络游戏 云服务",
                        "fetched_at": "2026-06-03T00:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.TavilyCompanySearchFetcher", FakeTavilyFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.tavily_research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                tavily_api_key="test-key",
                limit=1,
            )
        finally:
            service.close()

        assert summary["evidence_rows"] == 1
        assert pd.read_csv(evidence_csv)["source"].iloc[0] == "tavily_search"


def test_service_tavily_research_stock_tags_can_run_parallel(monkeypatch):
    import threading
    import time

    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_tavily_evidence.csv"
        pd.DataFrame(
            [
                {"stock_code": "00700", "market": "HK", "name": "腾讯控股"},
                {"stock_code": "09988", "market": "HK", "name": "阿里巴巴"},
            ]
        ).to_csv(industry_csv, index=False, encoding="utf-8-sig")

        active = 0
        max_active = 0
        lock = threading.Lock()

        class FakeTavilyFetcher:
            def __init__(self, stock_code, company_name="", **kwargs):
                self.stock_code = stock_code

            def fetch(self):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                return [
                    {
                        "stock_code": self.stock_code,
                        "market": "HK",
                        "source": "tavily_search",
                        "title": f"query={self.stock_code}; rank=1; title=公司资料",
                        "summary": "rank=1; url=https://example.com; snippet=业务",
                        "url": "https://example.com",
                        "raw_text": "业务",
                        "fetched_at": "2026-06-03T00:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.TavilyCompanySearchFetcher", FakeTavilyFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.tavily_research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                max_workers=2,
            )
        finally:
            service.close()

        assert summary["fetched"] == 2
        assert max_active == 2


def test_service_searxng_research_stock_tags_writes_evidence(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_searxng_evidence.csv"
        pd.DataFrame([{"stock_code": "00700", "market": "HK", "name": "腾讯控股"}]).to_csv(
            industry_csv, index=False, encoding="utf-8-sig"
        )

        class FakeSearxngFetcher:
            def __init__(self, stock_code, company_name="", **kwargs):
                self.stock_code = stock_code
                self.company_name = company_name
                self.kwargs = kwargs

            def fetch(self):
                assert self.kwargs["searxng_url"] == "http://127.0.0.1:8888"
                assert self.kwargs["engines"] == "bing,duckduckgo"
                return [
                    {
                        "stock_code": "00700",
                        "market": "HK",
                        "source": "searxng_search",
                        "title": "query=00700 腾讯控股; rank=1; title=公司资料",
                        "summary": "rank=1; url=https://example.com; snippet=网络游戏 云服务",
                        "url": "https://example.com",
                        "raw_text": "网络游戏 云服务",
                        "fetched_at": "2026-06-04T00:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.SearxngCompanySearchFetcher", FakeSearxngFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.searxng_research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                searxng_url="http://127.0.0.1:8888",
                engines="bing,duckduckgo",
                limit=1,
            )
        finally:
            service.close()

        assert summary["evidence_rows"] == 1
        assert pd.read_csv(evidence_csv)["source"].iloc[0] == "searxng_search"


def test_service_searxng_research_stock_tags_can_run_parallel(monkeypatch):
    import threading
    import time

    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_searxng_evidence.csv"
        pd.DataFrame(
            [
                {"stock_code": "00700", "market": "HK", "name": "腾讯控股"},
                {"stock_code": "09988", "market": "HK", "name": "阿里巴巴"},
            ]
        ).to_csv(industry_csv, index=False, encoding="utf-8-sig")

        active = 0
        max_active = 0
        lock = threading.Lock()

        class FakeSearxngFetcher:
            def __init__(self, stock_code, company_name="", **kwargs):
                self.stock_code = stock_code

            def fetch(self):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                return [
                    {
                        "stock_code": self.stock_code,
                        "market": "HK",
                        "source": "searxng_search",
                        "title": f"query={self.stock_code}; rank=1; title=公司资料",
                        "summary": "rank=1; url=https://example.com; snippet=业务",
                        "url": "https://example.com",
                        "raw_text": "业务",
                        "fetched_at": "2026-06-04T00:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.SearxngCompanySearchFetcher", FakeSearxngFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.searxng_research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                max_workers=2,
            )
        finally:
            service.close()

        assert summary["fetched"] == 2
        assert max_active == 2


def test_service_extract_stock_tags_llm_uses_cached_evidence(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        evidence_csv = base / "evidence.csv"
        dictionary_csv = base / "dictionary.csv"
        output_csv = base / "llm_tags.csv"
        candidate_csv = base / "llm_candidates.csv"
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "source": "playwright_search",
                    "title": "company",
                    "summary": "网络游戏 云服务",
                    "url": "https://example.com",
                    "raw_text": "网络游戏 云服务",
                    "fetched_at": "2026-06-03T00:00:00",
                }
            ]
        ).to_csv(evidence_csv, index=False, encoding="utf-8-sig")
        build_default_tag_dictionary().to_csv(dictionary_csv, index=False, encoding="utf-8-sig")

        class FakeClient:
            def chat_with_retry(self, messages, **kwargs):
                return '{"stock_code":"00700","tags":[{"tag":"游戏","tag_type":"business","confidence":0.94,"is_primary":true,"evidence":"网络游戏","evidence_url":"https://example.com","decision":"formal","reason":"主营明确"}],"rejected":[]}'

        monkeypatch.setattr("data.ingest.service.LLMClient", lambda *args, **kwargs: FakeClient(), raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.extract_stock_tags_llm(
                evidence_csv=evidence_csv,
                tag_dictionary_csv=dictionary_csv,
                output_csv=output_csv,
                candidate_output_csv=candidate_csv,
            )
        finally:
            service.close()

        assert summary["formal_rows"] == 1
        assert pd.read_csv(output_csv)["tag"].iloc[0] == "游戏"


def test_service_extract_stock_tags_llm_skips_existing_outputs(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        evidence_csv = base / "evidence.csv"
        dictionary_csv = base / "dictionary.csv"
        output_csv = base / "llm.csv"
        candidate_csv = base / "candidate.csv"
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "source": "searxng_search",
                    "title": "Tencent annual report",
                    "summary": "Tencent games",
                    "url": "https://example.com/700",
                    "raw_text": "Tencent games",
                    "fetched_at": "2026-06-03T00:00:00",
                },
                {
                    "stock_code": "03690",
                    "market": "HK",
                    "source": "searxng_search",
                    "title": "Meituan annual report",
                    "summary": "Meituan local services",
                    "url": "https://example.com/3690",
                    "raw_text": "Meituan local services",
                    "fetched_at": "2026-06-03T00:00:00",
                },
            ],
            columns=COMPANY_RESEARCH_EVIDENCE_FIELDS,
        ).to_csv(evidence_csv, index=False, encoding="utf-8-sig")
        build_default_tag_dictionary().to_csv(dictionary_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "tag": "游戏",
                    "tag_type": "business",
                    "confidence": 0.94,
                    "is_primary": True,
                    "source": "deepseek_browser_evidence",
                    "evidence": "网络游戏",
                    "evidence_url": "",
                    "updated_at": "2026-06-03T00:00:00",
                }
            ],
            columns=STOCK_TAG_FIELDS,
        ).to_csv(output_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS).to_csv(
            candidate_csv, index=False, encoding="utf-8-sig"
        )

        called_codes = []

        class FakeClient:
            def chat_with_retry(self, messages, **kwargs):
                payload = json.loads(messages[1]["content"])
                called_codes.append(payload["stock_code"])
                return '{"stock_code":"03690","tags":[{"tag":"本地生活","tag_type":"business","confidence":0.94,"is_primary":true,"evidence":"本地服务","evidence_url":"https://example.com/3690","decision":"formal","reason":"主营明确"}],"rejected":[]}'

        monkeypatch.setattr("data.ingest.service.LLMClient", lambda *args, **kwargs: FakeClient(), raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.extract_stock_tags_llm(
                evidence_csv=evidence_csv,
                tag_dictionary_csv=dictionary_csv,
                output_csv=output_csv,
                candidate_output_csv=candidate_csv,
                max_workers=2,
                checkpoint_every=1,
            )
        finally:
            service.close()

        assert summary["skipped_existing"] == 1
        assert called_codes == ["03690"]
        tags = pd.read_csv(output_csv, dtype=str).fillna("")
        assert set(tags["stock_code"]) == {"00700", "03690"}


def test_service_extract_stock_tags_llm_batches_multiple_stocks(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        evidence_csv = base / "evidence.csv"
        dictionary_csv = base / "dictionary.csv"
        output_csv = base / "llm.csv"
        candidate_csv = base / "candidate.csv"
        rows = []
        for code, summary in [
            ("00700", "Tencent games"),
            ("03690", "Meituan local services"),
            ("09988", "Alibaba ecommerce"),
        ]:
            rows.append(
                {
                    "stock_code": code,
                    "market": "HK",
                    "source": "searxng_search",
                    "title": f"{code} annual report",
                    "summary": summary,
                    "url": f"https://example.com/{code}",
                    "raw_text": summary,
                    "fetched_at": "2026-06-03T00:00:00",
                }
            )
        pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS).to_csv(
            evidence_csv, index=False, encoding="utf-8-sig"
        )
        build_default_tag_dictionary().to_csv(dictionary_csv, index=False, encoding="utf-8-sig")

        batch_sizes = []

        class FakeClient:
            def chat_with_retry(self, messages, **kwargs):
                payload = json.loads(messages[1]["content"])
                batch_sizes.append(len(payload["stocks"]))
                stocks = []
                for item in payload["stocks"]:
                    stocks.append(
                        {
                            "stock_code": item["stock_code"],
                            "tags": [
                                {
                                    "tag": "平台",
                                    "tag_type": "value_chain",
                                    "confidence": 0.9,
                                    "is_primary": True,
                                    "evidence": "平台业务",
                                    "evidence_url": "",
                                    "decision": "formal",
                                }
                            ],
                        }
                    )
                return json.dumps({"stocks": stocks}, ensure_ascii=False)

        monkeypatch.setattr("data.ingest.service.LLMClient", lambda *args, **kwargs: FakeClient(), raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.extract_stock_tags_llm(
                evidence_csv=evidence_csv,
                tag_dictionary_csv=dictionary_csv,
                output_csv=output_csv,
                candidate_output_csv=candidate_csv,
                batch_size=3,
                checkpoint_every=1,
            )
        finally:
            service.close()

        assert summary["batches"] == 1
        assert batch_sizes == [3]
        tags = pd.read_csv(output_csv, dtype=str).fillna("")
        assert set(tags["stock_code"]) == {"00700", "03690", "09988"}


def test_build_stock_tag_csvs_can_merge_llm_outputs():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "industry.csv"
        llm_csv = base / "llm.csv"
        llm_candidate_csv = base / "llm_candidate.csv"
        output_csv = base / "registry.csv"
        candidate_csv = base / "candidate.csv"
        dictionary_csv = base / "dictionary.csv"
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "industry_l1": "资讯科技业",
                    "industry_l2": "软件服务",
                    "theme_tags": "港股;科技",
                    "instrument_type": "common_stock",
                    "is_fund_like": "False",
                }
            ]
        ).to_csv(industry_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "tag": "游戏",
                    "tag_type": "business",
                    "confidence": 0.94,
                    "is_primary": True,
                    "source": "deepseek_browser_evidence",
                    "evidence": "网络游戏",
                    "evidence_url": "",
                    "updated_at": "2026-06-03T00:00:00",
                }
            ],
            columns=STOCK_TAG_FIELDS,
        ).to_csv(llm_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS).to_csv(
            llm_candidate_csv, index=False, encoding="utf-8-sig"
        )

        service = MarketDataService(base_dir=str(base / "data"))
        try:
            service.build_stock_tag_csvs(
                industry_registry_csv=industry_csv,
                tag_dictionary_csv=dictionary_csv,
                output_csv=output_csv,
                candidate_output_csv=candidate_csv,
                llm_tag_csv=llm_csv,
                llm_candidate_csv=llm_candidate_csv,
            )
        finally:
            service.close()

        tags = pd.read_csv(output_csv, dtype=str).fillna("")
        assert "游戏" in set(tags["tag"])


def test_review_stock_tag_candidates_accepts_selected_rows():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        candidate_csv = base / "candidate.csv"
        accepted_csv = base / "accepted.csv"
        pd.DataFrame(
            [
                {
                    "stock_code": "00700",
                    "market": "HK",
                    "tag": "AI",
                    "tag_type": "theme",
                    "confidence": 0.68,
                    "is_primary": False,
                    "source": "deepseek_browser_evidence",
                    "evidence": "AI news",
                    "evidence_url": "",
                    "updated_at": "2026-06-03T00:00:00",
                    "review_status": "accepted",
                    "review_note": "人工确认",
                }
            ],
            columns=STOCK_TAG_CANDIDATE_FIELDS,
        ).to_csv(candidate_csv, index=False, encoding="utf-8-sig")

        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.review_stock_tag_candidates(
                candidate_csv=candidate_csv,
                accepted_output_csv=accepted_csv,
            )
        finally:
            service.close()

        assert summary["accepted_rows"] == 1
        assert pd.read_csv(accepted_csv)["tag"].iloc[0] == "AI"
