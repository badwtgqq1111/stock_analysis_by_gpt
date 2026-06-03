# Stock Tag Registry Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible pipeline that researches each HK stock, creates precise multi-tag tables from evidence, imports them into Parquet/ClickHouse, and outputs manual import commands.

**Architecture:** Keep `stock_info_registry` as the stable main industry table and add three tag-oriented datasets: `tag_dictionary`, `stock_tag_registry`, and `stock_tag_candidate`. Online research writes evidence into a cache first, rule/LLM extraction converts evidence to candidate tags, high-confidence tags go to the formal registry, and `theme_tags` is derived from accepted tags using semicolon separators.

**Tech Stack:** Python 3.12, pandas, pyarrow/Parquet, ClickHouse via `clickhouse_connect`, pytest via `uv run pytest`, existing `run.py` command router.

---

## File Structure

- Create: `data/model/tag_schemas.py`  
  Defines tag table columns, normalization helpers, confidence bounds, semicolon tag parsing, and validation.
- Modify: `data/model/__init__.py`  
  Exports tag schema constants and normalization functions.
- Create: `data/ingest/stock_tags.py`  
  Builds tag dictionaries, creates base tags from `hk_industry_registry.csv`, merges researched evidence, splits formal tags from candidates, and derives `theme_tags`.
- Create: `data/ingest/providers/hk_company_research.py`  
  Fetches online company evidence per HK code through reproducible providers, with disk cache and resumable batches.
- Modify: `data/store/clickhouse_store.py`  
  Adds ClickHouse DDL/schema for `tag_dictionary`, `stock_tag_registry`, `stock_tag_candidate`, and `company_research_evidence`.
- Modify: `data/store/warehouse.py`  
  Adds generic upsert/read methods for tag datasets and evidence datasets.
- Modify: `data/ingest/service.py`  
  Adds service methods for building, researching, importing, and summarizing stock tags.
- Modify: `run.py`  
  Adds commands: `research-stock-tags`, `build-stock-tags`, `import-stock-tags`, and `tag-coverage`.
- Create: `test/test_stock_tag_registry.py`  
  Covers tag normalization, base generation, candidate split, warehouse import, and CLI-adjacent service behavior.
- Create output files:
  - `docs/hk_tag_dictionary.csv`
  - `docs/hk_stock_tag_registry.csv`
  - `docs/hk_stock_tag_candidate.csv`
  - `docs/hk_company_research_evidence.csv`

---

### Task 1: Tag Schema And Normalization

**Files:**
- Create: `data/model/tag_schemas.py`
- Modify: `data/model/__init__.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing schema tests**

Add to `test/test_stock_tag_registry.py`:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import (
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: import errors for missing `data.model` exports.

- [ ] **Step 3: Implement schema helpers**

Create `data/model/tag_schemas.py`:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Tag registry schemas and normalization helpers."""

from datetime import datetime

from data.model.schemas import normalize_bool, normalize_stock_code


TAG_DICTIONARY_FIELDS = [
    "tag",
    "tag_type",
    "canonical_tag",
    "aliases",
    "description",
    "parent_tag",
    "active",
    "updated_at",
]

STOCK_TAG_FIELDS = [
    "stock_code",
    "market",
    "tag",
    "tag_type",
    "confidence",
    "is_primary",
    "source",
    "evidence",
    "evidence_url",
    "updated_at",
]

STOCK_TAG_CANDIDATE_FIELDS = [
    *STOCK_TAG_FIELDS,
    "review_status",
    "review_note",
]

COMPANY_RESEARCH_EVIDENCE_FIELDS = [
    "stock_code",
    "market",
    "source",
    "title",
    "summary",
    "url",
    "raw_text",
    "fetched_at",
]

VALID_TAG_TYPES = {
    "theme",
    "resource",
    "value_chain",
    "business",
    "risk",
    "geo",
    "style",
    "instrument",
    "industry",
}


def _clean_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-", "--", "n/a"}:
        return ""
    return text


def split_semicolon_tags(value):
    if isinstance(value, (list, tuple, set)):
        chunks = value
    else:
        text = _clean_text(value).replace("；", ";").replace(",", ";").replace("，", ";")
        chunks = text.split(";") if text else []
    tags = []
    seen = set()
    for chunk in chunks:
        tag = _clean_text(chunk)
        if tag and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def join_semicolon_tags(value):
    return ";".join(split_semicolon_tags(value))


def normalize_tag_type(value):
    tag_type = _clean_text(value)
    if tag_type not in VALID_TAG_TYPES:
        raise ValueError(f"invalid tag_type: {tag_type}")
    return tag_type


def normalize_confidence(value, default=0.5):
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = float(default)
    return max(0.0, min(1.0, confidence))


def normalize_tag_dictionary_entry(entry):
    payload = dict(entry or {})
    tag = _clean_text(payload.get("tag"))
    if not tag:
        raise ValueError("tag is required")
    tag_type = normalize_tag_type(payload.get("tag_type") or "theme")
    return {
        "tag": tag,
        "tag_type": tag_type,
        "canonical_tag": _clean_text(payload.get("canonical_tag")) or tag,
        "aliases": join_semicolon_tags(payload.get("aliases")),
        "description": _clean_text(payload.get("description")),
        "parent_tag": _clean_text(payload.get("parent_tag")),
        "active": normalize_bool(payload.get("active"), default=True),
        "updated_at": _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat(),
    }


def normalize_stock_tag_entry(entry, candidate=False):
    payload = dict(entry or {})
    stock_code = normalize_stock_code(payload.get("stock_code"), market=payload.get("market") or "HK")
    tag = _clean_text(payload.get("tag"))
    if not tag:
        raise ValueError("tag is required")
    source = _clean_text(payload.get("source"))
    if not source:
        raise ValueError("source is required")
    row = {
        "stock_code": stock_code,
        "market": (_clean_text(payload.get("market")) or "HK").upper(),
        "tag": tag,
        "tag_type": normalize_tag_type(payload.get("tag_type") or "theme"),
        "confidence": normalize_confidence(payload.get("confidence")),
        "is_primary": normalize_bool(payload.get("is_primary"), default=False),
        "source": source,
        "evidence": _clean_text(payload.get("evidence")),
        "evidence_url": _clean_text(payload.get("evidence_url")),
        "updated_at": _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat(),
    }
    if candidate:
        row["review_status"] = _clean_text(payload.get("review_status")) or "pending"
        row["review_note"] = _clean_text(payload.get("review_note"))
    return row
```

Modify `data/model/__init__.py` to export the constants and functions:

```python
from .tag_schemas import (
    COMPANY_RESEARCH_EVIDENCE_FIELDS,
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    TAG_DICTIONARY_FIELDS,
    join_semicolon_tags,
    normalize_confidence,
    normalize_stock_tag_entry,
    normalize_tag_dictionary_entry,
    split_semicolon_tags,
)
```

Add these names to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: 4 tests pass.

---

### Task 2: Build Base Tag CSVs From The Industry Registry

**Files:**
- Create: `data/ingest/stock_tags.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing builder tests**

Append to `test/test_stock_tag_registry.py`:

```python
import pandas as pd

from data.ingest.stock_tags import (
    build_default_tag_dictionary,
    build_stock_tags_from_industry_registry,
)


def test_build_default_tag_dictionary_contains_precise_core_tags():
    dictionary = build_default_tag_dictionary()

    tags = set(dictionary["tag"])
    assert {"AI", "算力", "铜", "铁矿", "token", "游戏", "ETF"}.issubset(tags)
    assert dictionary.loc[dictionary["tag"].eq("AI"), "aliases"].iloc[0] == "人工智能;AIGC;大模型;Agent"


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: import error for missing `data.ingest.stock_tags`.

- [ ] **Step 3: Implement base tag builder**

Create `data/ingest/stock_tags.py`:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Build stock tag registries from industry data and research evidence."""

from datetime import datetime

import pandas as pd

from data.model import (
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    TAG_DICTIONARY_FIELDS,
    normalize_stock_tag_entry,
    normalize_tag_dictionary_entry,
    split_semicolon_tags,
)


DEFAULT_TAGS = [
    {"tag": "AI", "tag_type": "theme", "aliases": "人工智能;AIGC;大模型;Agent", "parent_tag": "科技", "description": "人工智能和大模型相关主题"},
    {"tag": "算力", "tag_type": "value_chain", "aliases": "数据中心;IDC;GPU服务器", "parent_tag": "AI", "description": "AI 算力和数据中心产业链"},
    {"tag": "token", "tag_type": "theme", "aliases": "代币;稳定币;RWA;Web3", "parent_tag": "数字资产", "description": "数字资产与代币化主题"},
    {"tag": "铜", "tag_type": "resource", "aliases": "铜矿;铜金属;铜资源", "parent_tag": "资源材料", "description": "铜资源与铜价敏感"},
    {"tag": "铁矿", "tag_type": "resource", "aliases": "铁矿石;铁矿资源", "parent_tag": "资源材料", "description": "铁矿石资源与价格敏感"},
    {"tag": "黄金", "tag_type": "resource", "aliases": "金矿;贵金属", "parent_tag": "资源材料", "description": "黄金和贵金属"},
    {"tag": "煤炭", "tag_type": "resource", "aliases": "动力煤;焦煤", "parent_tag": "能源", "description": "煤炭资源"},
    {"tag": "原油", "tag_type": "resource", "aliases": "石油;油气", "parent_tag": "能源", "description": "油气资源"},
    {"tag": "游戏", "tag_type": "business", "aliases": "网络游戏;手游", "parent_tag": "互联网服务", "description": "游戏业务"},
    {"tag": "云服务", "tag_type": "business", "aliases": "云计算;云平台", "parent_tag": "科技", "description": "云服务业务"},
    {"tag": "物业管理", "tag_type": "business", "aliases": "物管;物业服务", "parent_tag": "地产业", "description": "物业管理业务"},
    {"tag": "ETF", "tag_type": "instrument", "aliases": "交易所买卖基金;交易所买卖产品", "parent_tag": "基金", "description": "ETF 和交易所买卖产品"},
    {"tag": "REIT", "tag_type": "instrument", "aliases": "房地产基金;地产投资信托基金", "parent_tag": "地产业", "description": "房地产投资信托基金"},
    {"tag": "高股息", "tag_type": "style", "aliases": "收息资产;红利", "parent_tag": "风格", "description": "高股息或收息属性"},
]


def build_default_tag_dictionary():
    rows = [normalize_tag_dictionary_entry(row) for row in DEFAULT_TAGS]
    return pd.DataFrame(rows, columns=TAG_DICTIONARY_FIELDS)


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
    formal = pd.DataFrame(formal_rows, columns=STOCK_TAG_FIELDS).drop_duplicates(
        subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
    )
    candidates = pd.DataFrame(candidate_rows, columns=STOCK_TAG_CANDIDATE_FIELDS)
    return formal.reset_index(drop=True), candidates.reset_index(drop=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: all tests pass.

---

### Task 3: Online Company Research Evidence Cache

**Files:**
- Create: `data/ingest/providers/hk_company_research.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing evidence provider tests**

Append to `test/test_stock_tag_registry.py`:

```python
from data.ingest.providers.hk_company_research import (
    HKCompanyResearchFetcher,
    extract_tags_from_research_evidence,
)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: import error for missing `hk_company_research`.

- [ ] **Step 3: Implement evidence fetcher and extractor**

Create `data/ingest/providers/hk_company_research.py`:

```python
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
    formal = pd.DataFrame(formal_rows, columns=STOCK_TAG_FIELDS).drop_duplicates(
        subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
    )
    candidates = pd.DataFrame(candidate_rows, columns=STOCK_TAG_CANDIDATE_FIELDS).drop_duplicates(
        subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
    )
    return formal.reset_index(drop=True), candidates.reset_index(drop=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: all stock tag tests pass.

---

### Task 4: Warehouse And ClickHouse Storage For Tag Tables

**Files:**
- Modify: `data/store/clickhouse_store.py`
- Modify: `data/store/warehouse.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing warehouse tests**

Append to `test/test_stock_tag_registry.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_warehouse_upserts_and_reads_stock_tags -q
```

Expected: `MarketDataWarehouse` has no `upsert_stock_tags`.

- [ ] **Step 3: Add ClickHouse schemas**

Modify `data/store/clickhouse_store.py`:

```python
_STOCK_TAG_COLUMNS = [
    "stock_code", "market", "tag", "tag_type", "confidence", "is_primary",
    "source", "evidence", "evidence_url", "updated_at",
]

_STOCK_TAG_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    stock_code LowCardinality(String),
    market LowCardinality(String),
    tag String,
    tag_type LowCardinality(String),
    confidence Float64,
    is_primary Bool,
    source String,
    evidence String,
    evidence_url String,
    updated_at DateTime
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY market
ORDER BY (market, stock_code, tag_type, tag)
"""
```

Add matching schemas for:

- `tag_dictionary`
- `stock_tag_registry`
- `stock_tag_candidate`
- `company_research_evidence`

Use `ReplacingMergeTree(updated_at)` for tag tables and `ReplacingMergeTree(fetched_at)` for evidence.

- [ ] **Step 4: Add warehouse methods**

Modify `data/store/warehouse.py` with methods:

```python
def upsert_stock_tags(self, frame, dataset_name="stock_tag_registry"):
    self._ensure_writable()
    if frame is None or frame.empty:
        return {"rows": 0, "dataset_path": str(self.layout.dataset_path(dataset_name, layer="meta"))}
    payload = frame[STOCK_TAG_FIELDS].copy()
    target = self._upsert_meta_frame(
        dataset_name=dataset_name,
        frame=payload,
        dedupe_keys=["market", "stock_code", "tag_type", "tag"],
        sort_by=["market", "stock_code", "tag_type", "tag", "updated_at"],
        date_column="updated_at",
        partition_columns=("market",),
    )
    return {"rows": len(payload), "dataset_path": str(target)}
```

Also add:

- `read_stock_tags(stock_codes=None, market=None, tag=None, tag_type=None, min_confidence=None)`
- `upsert_tag_dictionary(frame)`
- `upsert_stock_tag_candidates(frame)`
- `upsert_company_research_evidence(frame)`

Implement a private `_upsert_meta_frame()` that follows the current stock-info fallback pattern: try ClickHouse first, set `_clickhouse_disabled_reason` on failure, then fall back to Parquet.

- [ ] **Step 5: Run warehouse tests**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_warehouse_upserts_and_reads_stock_tags -q
```

Expected: pass.

---

### Task 5: Service Methods And CLI Commands

**Files:**
- Modify: `data/ingest/service.py`
- Modify: `run.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing service test**

Append to `test/test_stock_tag_registry.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_builds_tag_csvs_from_industry_registry -q
```

Expected: `MarketDataService` has no `build_stock_tag_csvs`.

- [ ] **Step 3: Add service methods**

Modify `data/ingest/service.py`:

```python
def build_stock_tag_csvs(
    self,
    industry_registry_csv,
    tag_dictionary_csv="docs/hk_tag_dictionary.csv",
    output_csv="docs/hk_stock_tag_registry.csv",
    candidate_output_csv="docs/hk_stock_tag_candidate.csv",
):
    from data.ingest.stock_tags import (
        build_default_tag_dictionary,
        build_stock_tags_from_industry_registry,
    )

    industry = pd.read_csv(industry_registry_csv, dtype=str).fillna("")
    dictionary = build_default_tag_dictionary()
    formal, candidates = build_stock_tags_from_industry_registry(industry)
    Path(tag_dictionary_csv).parent.mkdir(parents=True, exist_ok=True)
    dictionary.to_csv(tag_dictionary_csv, index=False, encoding="utf-8-sig")
    formal.to_csv(output_csv, index=False, encoding="utf-8-sig")
    candidates.to_csv(candidate_output_csv, index=False, encoding="utf-8-sig")
    return {
        "status": "completed",
        "dictionary_rows": len(dictionary),
        "stock_tag_rows": len(formal),
        "candidate_rows": len(candidates),
        "tag_dictionary_csv": str(tag_dictionary_csv),
        "stock_tag_csv": str(output_csv),
        "candidate_csv": str(candidate_output_csv),
    }
```

Add:

```python
def import_stock_tag_csvs(self, tag_dictionary_csv=None, stock_tag_csv=None, candidate_csv=None):
    summary = {"status": "completed"}
    if tag_dictionary_csv:
        frame = pd.read_csv(tag_dictionary_csv, dtype=str).fillna("")
        summary["dictionary"] = self.warehouse.upsert_tag_dictionary(frame)
    if stock_tag_csv:
        frame = pd.read_csv(stock_tag_csv, dtype=str).fillna("")
        summary["stock_tags"] = self.warehouse.upsert_stock_tags(frame)
    if candidate_csv:
        frame = pd.read_csv(candidate_csv, dtype=str).fillna("")
        summary["candidates"] = self.warehouse.upsert_stock_tag_candidates(frame)
    return summary
```

Add a separate `research_stock_tags(...)` method in Task 6.

- [ ] **Step 4: Add CLI commands**

Modify `run.py`:

```python
elif len(sys.argv) > 1 and sys.argv[1] == "build-stock-tags":
    import argparse
    parser = argparse.ArgumentParser(prog="run.py build-stock-tags")
    parser.add_argument("--base-dir", default="./assets/data")
    parser.add_argument("--industry-registry-csv", default="docs/hk_industry_registry.csv")
    parser.add_argument("--tag-dictionary-csv", default="docs/hk_tag_dictionary.csv")
    parser.add_argument("--output", default="docs/hk_stock_tag_registry.csv")
    parser.add_argument("--candidate-output", default="docs/hk_stock_tag_candidate.csv")
    args = parser.parse_args(sys.argv[2:])
    service = MarketDataService(base_dir=args.base_dir)
    try:
        print(service.build_stock_tag_csvs(
            industry_registry_csv=args.industry_registry_csv,
            tag_dictionary_csv=args.tag_dictionary_csv,
            output_csv=args.output,
            candidate_output_csv=args.candidate_output,
        ))
    finally:
        service.close()
```

Add `import-stock-tags`:

```python
elif len(sys.argv) > 1 and sys.argv[1] == "import-stock-tags":
    import argparse
    parser = argparse.ArgumentParser(prog="run.py import-stock-tags")
    parser.add_argument("--base-dir", default="./assets/data")
    parser.add_argument("--tag-dictionary-csv", default="docs/hk_tag_dictionary.csv")
    parser.add_argument("--stock-tag-csv", default="docs/hk_stock_tag_registry.csv")
    parser.add_argument("--candidate-csv", default="docs/hk_stock_tag_candidate.csv")
    args = parser.parse_args(sys.argv[2:])
    service = MarketDataService(base_dir=args.base_dir)
    try:
        print(service.import_stock_tag_csvs(
            tag_dictionary_csv=args.tag_dictionary_csv,
            stock_tag_csv=args.stock_tag_csv,
            candidate_csv=args.candidate_csv,
        ))
    finally:
        service.close()
```

- [ ] **Step 5: Run service test**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_builds_tag_csvs_from_industry_registry -q
```

Expected: pass.

---

### Task 6: Resumable Online Research Command

**Files:**
- Modify: `data/ingest/service.py`
- Modify: `run.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing resumable research test**

Append to `test/test_stock_tag_registry.py`:

```python
def test_service_research_stock_tags_uses_cache_and_limit(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_research_evidence.csv"
        pd.DataFrame(
            [
                {"stock_code": "00700", "market": "HK"},
                {"stock_code": "00005", "market": "HK"},
            ]
        ).to_csv(industry_csv, index=False, encoding="utf-8-sig")

        class FakeFetcher:
            def __init__(self, stock_code):
                self.stock_code = stock_code

            def fetch(self):
                return [
                    {
                        "stock_code": self.stock_code.zfill(5),
                        "market": "HK",
                        "source": "unit",
                        "title": "company_profile",
                        "summary": "网络游戏、云服务",
                        "url": "",
                        "raw_text": "网络游戏、云服务",
                        "fetched_at": "2026-06-03T00:00:00",
                    }
                ]

        monkeypatch.setattr("data.ingest.service.HKCompanyResearchFetcher", FakeFetcher, raising=False)

        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_output_csv=evidence_csv,
                limit=1,
            )
            cached_summary = service.research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_output_csv=evidence_csv,
                skip_existing=True,
            )
        finally:
            service.close()

    assert summary["fetched"] == 1
    assert evidence_csv.exists()
    assert cached_summary["skipped_existing"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_research_stock_tags_uses_cache_and_limit -q
```

Expected: missing `research_stock_tags`.

- [ ] **Step 3: Implement research service**

Modify `data/ingest/service.py`:

```python
def research_stock_tags(
    self,
    industry_registry_csv="docs/hk_industry_registry.csv",
    evidence_output_csv="docs/hk_company_research_evidence.csv",
    stock_codes=None,
    limit=None,
    skip_existing=True,
    show_progress=False,
):
    from data.ingest.providers.hk_company_research import HKCompanyResearchFetcher
    from data.model import COMPANY_RESEARCH_EVIDENCE_FIELDS

    industry = pd.read_csv(industry_registry_csv, dtype=str).fillna("")
    codes = [normalize_stock_code(code, market="HK") for code in (stock_codes or industry["stock_code"].tolist())]
    codes = list(dict.fromkeys(codes))
    if limit:
        codes = codes[: int(limit)]

    evidence_path = Path(evidence_output_csv)
    if evidence_path.exists():
        existing = pd.read_csv(evidence_path, dtype=str).fillna("")
    else:
        existing = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
    existing_codes = set(existing["stock_code"].tolist()) if not existing.empty and "stock_code" in existing.columns else set()

    rows = []
    fetched = 0
    skipped_existing = 0
    iterable = tqdm(codes, desc="research tags") if show_progress else codes
    for code in iterable:
        if skip_existing and code in existing_codes:
            skipped_existing += 1
            continue
        try:
            rows.extend(HKCompanyResearchFetcher(code).fetch())
            fetched += 1
        except Exception as exc:
            rows.append(
                {
                    "stock_code": code,
                    "market": "HK",
                    "source": "error",
                    "title": "fetch_error",
                    "summary": str(exc),
                    "url": "",
                    "raw_text": "",
                    "fetched_at": datetime.utcnow().isoformat(),
                }
            )
    new_frame = pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
    combined = pd.concat([existing, new_frame], ignore_index=True) if not existing.empty else new_frame
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(evidence_path, index=False, encoding="utf-8-sig")
    if rows:
        self.warehouse.upsert_company_research_evidence(new_frame)
    return {
        "status": "completed",
        "requested": len(codes),
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "evidence_rows": len(combined),
        "evidence_output_csv": str(evidence_path),
    }
```

- [ ] **Step 4: Add CLI command**

Modify `run.py`:

```python
elif len(sys.argv) > 1 and sys.argv[1] == "research-stock-tags":
    import argparse
    parser = argparse.ArgumentParser(prog="run.py research-stock-tags")
    parser.add_argument("--base-dir", default="./assets/data")
    parser.add_argument("--industry-registry-csv", default="docs/hk_industry_registry.csv")
    parser.add_argument("--evidence-output", default="docs/hk_company_research_evidence.csv")
    parser.add_argument("--stock-codes", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-skip-existing", action="store_true")
    parser.add_argument("--show-progress", action="store_true")
    args = parser.parse_args(sys.argv[2:])
    service = MarketDataService(base_dir=args.base_dir)
    try:
        print(service.research_stock_tags(
            industry_registry_csv=args.industry_registry_csv,
            evidence_output_csv=args.evidence_output,
            stock_codes=args.stock_codes,
            limit=args.limit,
            skip_existing=not args.no_skip_existing,
            show_progress=args.show_progress,
        ))
    finally:
        service.close()
```

- [ ] **Step 5: Run test**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_research_stock_tags_uses_cache_and_limit -q
```

Expected: pass.

---

### Task 7: Merge Research Evidence Into Tag CSVs

**Files:**
- Modify: `data/ingest/stock_tags.py`
- Modify: `data/ingest/service.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing merge test**

Append to `test/test_stock_tag_registry.py`:

```python
from data.ingest.stock_tags import merge_research_tags


def test_merge_research_tags_adds_precise_tags_and_keeps_candidates_separate():
    formal = pd.DataFrame(columns=STOCK_TAG_FIELDS)
    candidates = pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS)
    evidence = pd.DataFrame(
        [
            {
                "stock_code": "00700",
                "market": "HK",
                "source": "unit",
                "title": "profile",
                "summary": "网络游戏、云服务、RWA",
                "url": "",
                "raw_text": "网络游戏、云服务、RWA",
                "fetched_at": "2026-06-03T00:00:00",
            }
        ]
    )

    merged_formal, merged_candidates = merge_research_tags(formal, candidates, evidence)

    assert set(merged_formal["tag"]) >= {"游戏", "云服务"}
    assert "token" in set(merged_candidates["tag"]) or "token" in set(merged_formal["tag"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_merge_research_tags_adds_precise_tags_and_keeps_candidates_separate -q
```

Expected: missing `merge_research_tags`.

- [ ] **Step 3: Implement merge helper**

Modify `data/ingest/stock_tags.py`:

```python
def merge_research_tags(formal, candidates, evidence_frame):
    from data.ingest.providers.hk_company_research import extract_tags_from_research_evidence

    research_formal, research_candidates = extract_tags_from_research_evidence(evidence_frame)
    merged_formal = pd.concat([formal, research_formal], ignore_index=True) if formal is not None else research_formal
    merged_candidates = pd.concat([candidates, research_candidates], ignore_index=True) if candidates is not None else research_candidates
    if not merged_formal.empty:
        merged_formal = merged_formal.drop_duplicates(
            subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
        ).reset_index(drop=True)
    if not merged_candidates.empty:
        merged_candidates = merged_candidates.drop_duplicates(
            subset=["stock_code", "market", "tag", "tag_type", "source"], keep="last"
        ).reset_index(drop=True)
    return merged_formal, merged_candidates
```

Modify `MarketDataService.build_stock_tag_csvs()` to accept `evidence_csv=None`; when present, read it and call `merge_research_tags()`.

- [ ] **Step 4: Run merge test**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_merge_research_tags_adds_precise_tags_and_keeps_candidates_separate -q
```

Expected: pass.

---

### Task 8: Tag Coverage Command And Manual Import Commands

**Files:**
- Modify: `data/ingest/service.py`
- Modify: `run.py`
- Modify: `README.md`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Write failing coverage test**

Append to `test/test_stock_tag_registry.py`:

```python
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
                            "evidence": "",
                            "evidence_url": "",
                            "updated_at": "2026-06-03T00:00:00",
                        }
                    ]
                )
            )
            report = service.get_stock_tag_coverage()
        finally:
            service.close()

    assert report["tagged_stock_count"] == 1
    assert report["by_tag_type"]["business"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_reports_tag_coverage -q
```

Expected: missing `get_stock_tag_coverage`.

- [ ] **Step 3: Add coverage service and CLI**

Modify `data/ingest/service.py`:

```python
def get_stock_tag_coverage(self, market="HK", min_confidence=0.75):
    tags = self.warehouse.read_stock_tags(market=market, min_confidence=min_confidence)
    if tags is None or tags.empty:
        return {"tagged_stock_count": 0, "tag_rows": 0, "by_tag_type": {}}
    return {
        "tagged_stock_count": int(tags["stock_code"].nunique()),
        "tag_rows": int(len(tags)),
        "by_tag_type": tags.groupby("tag_type")["stock_code"].nunique().sort_values(ascending=False).to_dict(),
    }
```

Modify `run.py` with `tag-coverage`:

```python
elif len(sys.argv) > 1 and sys.argv[1] == "tag-coverage":
    import argparse
    parser = argparse.ArgumentParser(prog="run.py tag-coverage")
    parser.add_argument("--base-dir", default="./assets/data")
    parser.add_argument("--market", default="HK")
    parser.add_argument("--min-confidence", type=float, default=0.75)
    args = parser.parse_args(sys.argv[2:])
    service = MarketDataService(base_dir=args.base_dir)
    try:
        print(service.get_stock_tag_coverage(market=args.market, min_confidence=args.min_confidence))
    finally:
        service.close()
```

- [ ] **Step 4: Document manual commands**

Add to `README.md`:

```bash
# 1. 可断点续跑：在线调研股票简介/主营业务证据
uv run python run.py research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-output docs/hk_company_research_evidence.csv \
  --show-progress

# 2. 根据行业表 + 调研证据生成标签字典、正式标签、候选标签
uv run python run.py build-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_stock_tag_registry.csv \
  --candidate-output docs/hk_stock_tag_candidate.csv

# 3. 手动导入标签表到 registry 后端
uv run python run.py import-stock-tags \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --stock-tag-csv docs/hk_stock_tag_registry.csv \
  --candidate-csv docs/hk_stock_tag_candidate.csv

# 4. 检查标签覆盖
uv run python run.py tag-coverage --min-confidence 0.75
```

- [ ] **Step 5: Run coverage test**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_reports_tag_coverage -q
```

Expected: pass.

---

### Task 9: End-To-End Generation And Import

**Files:**
- Generated: `docs/hk_tag_dictionary.csv`
- Generated: `docs/hk_stock_tag_registry.csv`
- Generated: `docs/hk_stock_tag_candidate.csv`
- Generated: `docs/hk_company_research_evidence.csv`

- [ ] **Step 1: Run a small online research sample**

Run:

```bash
uv run python run.py research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-output docs/hk_company_research_evidence.csv \
  --limit 20 \
  --show-progress
```

Expected: command completes and creates `docs/hk_company_research_evidence.csv`.

- [ ] **Step 2: Build tag CSVs from sample evidence**

Run:

```bash
uv run python run.py build-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_stock_tag_registry.csv \
  --candidate-output docs/hk_stock_tag_candidate.csv
```

Expected: command prints counts for dictionary, formal tags, and candidates.

- [ ] **Step 3: Inspect generated CSV quality**

Run:

```bash
uv run python -c "import pandas as pd; f=pd.read_csv('docs/hk_stock_tag_registry.csv'); c=pd.read_csv('docs/hk_stock_tag_candidate.csv'); print(f.head(20).to_string(index=False)); print({'formal_rows':len(f),'candidate_rows':len(c),'formal_stocks':f.stock_code.nunique()})"
```

Expected: formal tags include `industry`, `theme`, and `instrument` rows; candidate rows include unclassified rows.

- [ ] **Step 4: Import generated CSVs**

Run:

```bash
uv run python run.py import-stock-tags \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --stock-tag-csv docs/hk_stock_tag_registry.csv \
  --candidate-csv docs/hk_stock_tag_candidate.csv
```

Expected: command prints inserted row counts for all provided CSVs.

- [ ] **Step 5: Check coverage**

Run:

```bash
uv run python run.py tag-coverage --min-confidence 0.75
```

Expected: output includes `tagged_stock_count`, `tag_rows`, and `by_tag_type`.

---

### Task 10: Full Verification

**Files:**
- Existing tests only.

- [ ] **Step 1: Run stock tag suite**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run data layer smoke suite**

Run:

```bash
uv run pytest test/test_data_layer_smoke.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run ClickHouse fallback tests**

Run:

```bash
uv run pytest \
  test/test_factor_engine.py::test_clickhouse_insert_frame_chunks_large_stock_info_batches \
  test/test_factor_engine.py::test_warehouse_skips_clickhouse_when_configured_endpoint_is_unreachable \
  test/test_factor_engine.py::test_warehouse_disables_clickhouse_after_feature_read_failure \
  -q
```

Expected: all tests pass.

- [ ] **Step 4: Confirm manual import commands in final answer**

Report these commands to the user:

```bash
uv run python run.py research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-output docs/hk_company_research_evidence.csv \
  --show-progress

uv run python run.py build-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_stock_tag_registry.csv \
  --candidate-output docs/hk_stock_tag_candidate.csv

uv run python run.py import-stock-tags \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --stock-tag-csv docs/hk_stock_tag_registry.csv \
  --candidate-csv docs/hk_stock_tag_candidate.csv

uv run python run.py tag-coverage --min-confidence 0.75
```

---

## Self-Review

- Spec coverage: Implements `tag_dictionary`, `stock_tag_registry`, `stock_tag_candidate`, evidence caching, manual commands, and coverage checks from `docs/TAG_REGISTRY_DESIGN.md`.
- Scope control: News/social event ingestion is intentionally not implemented here; this plan builds the reusable stock tag graph and evidence cache first.
- Data quality: Online research evidence is cached before tag extraction so every precise tag has a source and evidence string.
- Import path: Both Parquet and ClickHouse are supported through the existing warehouse fallback pattern.
- Separator rule: Human-facing CSV fields use semicolons, not commas, for multi-tags.
