#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Stock profile, deep tags, and graph index helpers."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests
from tqdm import tqdm

from data.model import (
    COMPANY_RESEARCH_EVIDENCE_FIELDS,
    ENTITY_ALIAS_FIELDS,
    STOCK_DEEP_TAG_FIELDS,
    STOCK_GRAPH_EDGE_FIELDS,
    STOCK_GRAPH_NODE_FIELDS,
    STOCK_PROFILE_FIELDS,
    THEME_OPPORTUNITY_SCORE_FIELDS,
    normalize_entity_alias_entry,
    normalize_attention_signal_entry,
    normalize_stock_code,
    normalize_stock_deep_tag_entry,
    normalize_stock_graph_edge_entry,
    normalize_stock_graph_node_entry,
    normalize_stock_profile_entry,
    normalize_theme_opportunity_score_entry,
)


DEEP_EVIDENCE_SOURCE = "source_aware_search"
PROFILE_LLM_SOURCE = "deep_profile_llm"
LIGHTRAG_PROFILE_SOURCE = "lightrag_graph"

INVESTABLE_PROFILE_DIMENSIONS = {
    "business": ["主营", "业务", "收入", "营收", "商业化", "客户", "毛利", "亏损", "研发投入"],
    "product": ["产品", "平台", "模型", "GLM", "ChatGLM", "CodeGeeX", "CogVLM", "CogView", "MaaS"],
    "technology": ["技术", "MoE", "推理", "多模态", "智能体", "Agent", "Coding", "上下文", "benchmark"],
    "value_chain": ["上游", "下游", "供应", "算力", "芯片", "云", "客户", "生态"],
    "catalyst": ["催化", "发布", "合作", "上市", "融资", "订单", "备案", "开源"],
    "risk": ["风险", "亏损", "竞争", "监管", "成本", "裁员", "商业化", "估值"],
    "attention": ["GitHub", "HuggingFace", "arxiv", "论文", "benchmark", "公众号", "新闻", "热度"],
}

REPORT_SECTION_KEYWORDS = {
    "products": ["GLM", "ChatGLM", "CodeGeeX", "CogVLM", "CogView", "MaaS", "产品", "模型", "平台", "智谱清言"],
    "technology": ["MoE", "DSA", "ARC", "Agentic", "Vibe Coding", "多模态", "推理", "长上下文", "技术", "架构"],
    "business": ["营收", "收入", "毛利", "客户", "商业化", "MaaS", "ARR", "企业收入", "研发投入"],
    "value_chain": ["产业链", "上游", "下游", "算力", "芯片", "云", "英特尔", "合作伙伴", "生态"],
    "catalysts": ["催化", "发布", "合作", "开源", "上市", "备案", "融资", "增长", "英特尔"],
    "risks": ["风险", "亏损", "竞争", "监管", "成本", "裁员", "商业化", "估值", "经调整净亏损"],
    "attention": ["GitHub", "HuggingFace", "Hugging Face", "arXiv", "论文", "benchmark", "新闻", "热度"],
}

SEMANTIC_EDGE_KEYWORDS = [
    ("has_metric", ["营收", "收入", "毛利", "亏损", "研发投入", "ARR", "同比", "复合年增长率", "每股亏损"]),
    ("has_risk", ["风险", "亏损", "竞争", "监管", "成本", "裁员", "商业化", "估值"]),
    ("uses_technology", ["MoE", "DSA", "ARC", "多模态", "推理", "长上下文", "智能体", "Agentic", "Vibe Coding", "技术", "架构"]),
    ("partner_with", ["英特尔", "合作", "伙伴", "生态"]),
    ("has_attention", ["GitHub", "HuggingFace", "Hugging Face", "arXiv", "论文", "benchmark", "新闻", "媒体"]),
]

SOURCE_QUALITY_WEIGHTS = {
    "hkexnews.hk": 1.0,
    "hkex.com.hk": 0.95,
    "z.ai": 0.9,
    "bigmodel.cn": 0.9,
    "github.com": 0.75,
    "huggingface.co": 0.72,
    "openrouter.ai": 0.7,
    "arxiv.org": 0.78,
    "xueqiu.com": 0.45,
    "sina.com.cn": 0.45,
    "lixinger.com": 0.55,
}

ATTENTION_SOURCE_KEYWORDS = {
    "github": ["github.com", "github"],
    "huggingface": ["huggingface.co", "hugging face", "huggingface"],
    "arxiv": ["arxiv.org", "arxiv", "论文"],
    "model_benchmark": ["benchmark", "榜单", "openrouter", "leaderboard", "eval"],
    "news": ["新闻", "媒体", "报道", "财联社", "36kr", "澎湃", "sina", "雪球"],
    "wechat": ["公众号", "微信"],
    "social": ["twitter", "x.com", "雪球", "股吧", "reddit", "hacker news"],
}

GENERIC_EVIDENCE_DOMAINS = {
    "bing.com",
    "google.com",
    "support.google.com",
    "support.microsoft.com",
    "microsoft.com",
    "reddit.com",
    "github.com",
    "stackoverflow.com",
    "explainshell.com",
    "positioniseverything.net",
}

BOTTLENECK_KEYWORDS = ["瓶颈", "卡脖子", "供给", "短缺", "算力", "GPU", "HBM", "光模块", "IDC", "电力", "产能"]
VALUE_CHAIN_EDGE_TYPES = {"upstream", "downstream", "supplier", "customer", "partner_with", "supplies", "uses", "produces", "bottleneck"}

SUPPLY_CHAIN_RULES = [
    {
        "bottleneck": "推理算力",
        "keywords": ["推理算力", "算力", "inference compute", "GPU", "英伟达", "NVIDIA"],
        "upstream": ["GPU", "HBM", "服务器", "云计算"],
    },
    {
        "bottleneck": "高带宽内存",
        "keywords": ["HBM", "高带宽内存", "显存", "memory bandwidth"],
        "upstream": ["HBM", "DRAM", "先进封装"],
    },
    {
        "bottleneck": "数据中心互联",
        "keywords": ["光模块", "交换机", "高速互联", "数据中心互联", "CPO", "硅光"],
        "upstream": ["光模块", "交换机", "硅光"],
    },
    {
        "bottleneck": "IDC电力",
        "keywords": ["IDC", "数据中心", "电力", "液冷", "能耗", "PUE"],
        "upstream": ["IDC", "电力", "液冷"],
    },
    {
        "bottleneck": "模型商业化",
        "keywords": ["商业化", "API", "MaaS", "企业客户", "ARR", "付费客户"],
        "upstream": ["企业客户", "API平台", "应用生态"],
    },
]

NODE_TYPE_ALIASES = {
    "组织": "organization",
    "人造物": "artifact",
    "方法": "method",
    "概念": "concept",
    "内容": "content",
    "数据": "data",
    "地点": "location",
    "人物": "person",
    "产品": "product",
    "技术": "technology",
    "事件": "event",
    "风险": "risk",
}

DOMAIN_ALIAS_PATTERNS = [
    (r"\bGLM(?:[- ]?\d+(?:\.\d+)?)?(?:[- ]?(?:Plus|Air|Flash))?\b", "model"),
    (r"\bChatGLM(?:[- ]?\d+(?:\.\d+)?)?\b", "model"),
    (r"\bCodeGeeX(?:[- ]?\d+(?:\.\d+)?)?\b", "product"),
    (r"\bCogVLM(?:[- ]?\d+(?:\.\d+)?)?\b", "model"),
    (r"\bCogView(?:[- ]?\d+(?:\.\d+)?)?\b", "model"),
    (r"\bMaaS\b", "business"),
    (r"\bZhipu AI\b", "english_name"),
    (r"\bZ\.ai\b", "brand"),
    (r"\bBigModel\b", "product"),
    (r"\bHugging ?Face\b", "platform"),
    (r"\bAgentic Engineering\b", "technology"),
    (r"\bVibe Coding\b", "technology"),
    (r"\bMoE\b", "technology"),
    (r"\bDSA\b", "technology"),
    (r"\bARC\b", "technology"),
    (r"\b200K\b", "metric"),
    (r"智谱AI", "brand"),
    (r"智谱清言", "product"),
    (r"大模型", "technology"),
    (r"智能体", "technology"),
    (r"代码模型", "product"),
    (r"多模态", "technology"),
    (r"模型微调", "technology"),
    (r"模型部署", "technology"),
    (r"模型即服务", "business"),
]

ALIAS_CANONICAL_OVERRIDES = {
    "zhipu ai": "Zhipu AI",
    "zhipu ai ": "Zhipu AI",
    "z.ai": "Z.ai",
    "glm 5": "GLM-5",
    "glm-5": "GLM-5",
    "agentic engineering": "Agentic Engineering",
    "vibe coding": "Vibe Coding",
    "huggingface": "HuggingFace",
    "hugging face": "HuggingFace",
    "bigmodel": "BigModel",
    "智谱ai": "智谱AI",
}


def _clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-", "--"}:
        return ""
    return text


def _split_alias_text(value):
    text = _clean(value)
    if not text:
        return []
    chunks = re.split(r"[;；,，/、|]+", text)
    result = []
    seen = set()
    for chunk in chunks:
        alias = _clean(chunk)
        if alias and alias not in seen:
            result.append(alias)
            seen.add(alias)
    return result


def clean_evidence_url(url):
    """Drop signed query params that can contain temporary secrets."""
    text = _clean(url)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        # Search snippets can contain malformed URL-like fragments. Keep the
        # non-sensitive path portion instead of aborting the whole index batch.
        text = re.split(r"[?#]", text, maxsplit=1)[0]
        text = re.sub(r"[\x00-\x1f\x7f]+", "", text).strip()
        return text[:2000]


def sanitize_evidence_text(value):
    """Remove signed URL query strings and common access-key fragments before indexing."""
    text = _clean(value)
    if not text:
        return ""

    def _clean_url_match(match):
        try:
            return clean_evidence_url(match.group(0))
        except Exception:
            return "[URL_REDACTED]"

    text = re.sub(r"https?://[^\s\"'<>]+", _clean_url_match, text)
    secret_patterns = [
        r"(?i)(AccessKeySecret|AccessKeyId|OSSAccessKeyId|access_key_secret|access_key_id)=([^&\s;]+)",
        r"(?i)(AccessKeySecret|AccessKeyId|OSSAccessKeyId|access_key_secret|access_key_id)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=_.-]{8,})",
    ]
    for pattern in secret_patterns:
        text = re.sub(pattern, r"\1=[REDACTED]", text)
    return text.strip()


def evidence_id(row):
    stock_code = normalize_stock_code(row.get("stock_code"), market=row.get("market") or "HK")
    text = "\n".join(
        _clean(row.get(field))
        for field in ("source", "title", "url", "summary")
    )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"evidence:{stock_code}:{digest}"


class LightRAGClient:
    """Small REST client for the local LightRAG API."""

    def __init__(self, base_url="http://127.0.0.1:9621", api_key=None, timeout=60):
        self.base_url = str(base_url or "http://127.0.0.1:9621").rstrip("/")
        self.api_key = _clean(api_key)
        self.timeout = int(timeout or 60)

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    def _post(self, path, payload, *, ignore_conflict=False):
        response = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 409 and ignore_conflict:
            return {"status": "duplicated", "message": response.text, "track_id": ""}
        if response.status_code >= 400:
            raise RuntimeError(f"LightRAG {path} failed: {response.status_code} {response.text[:500]}")
        if not response.text:
            return {}
        return response.json()

    def insert_text(self, text, file_source, *, chunking=None, ignore_conflict=True):
        payload = {"text": text, "file_source": file_source}
        if chunking:
            payload["chunking"] = chunking
        return self._post("/documents/text", payload, ignore_conflict=ignore_conflict)

    def insert_texts(self, texts, file_sources, *, chunking=None, ignore_conflict=True):
        payload = {"texts": list(texts or []), "file_sources": list(file_sources or [])}
        if chunking:
            payload["chunking"] = chunking
        return self._post("/documents/texts", payload, ignore_conflict=ignore_conflict)

    def query_data(
        self,
        query,
        *,
        mode="mix",
        top_k=None,
        chunk_top_k=None,
        max_total_tokens=None,
        hl_keywords=None,
        ll_keywords=None,
    ):
        payload = {"query": query, "mode": mode}
        optional = {
            "top_k": top_k,
            "chunk_top_k": chunk_top_k,
            "max_total_tokens": max_total_tokens,
            "hl_keywords": hl_keywords,
            "ll_keywords": ll_keywords,
        }
        payload.update({key: value for key, value in optional.items() if value not in (None, [], "")})
        return self._post("/query/data", payload)

    def query(self, query, *, mode="mix", response_type=None, include_references=True):
        payload = {
            "query": query,
            "mode": mode,
            "include_references": include_references,
        }
        if response_type:
            payload["response_type"] = response_type
        return self._post("/query", payload)


def build_entity_aliases(stock_info_frame, manual_aliases=None):
    """Build a first-pass alias registry from stock info and optional manual aliases."""
    rows = []
    manual_aliases = manual_aliases or {}
    if stock_info_frame is None or stock_info_frame.empty:
        return pd.DataFrame(columns=ENTITY_ALIAS_FIELDS)
    for _, row in stock_info_frame.fillna("").iterrows():
        stock_code = normalize_stock_code(row.get("stock_code"), market=row.get("market") or "HK")
        market = (_clean(row.get("market")) or "HK").upper()
        name = _clean(row.get("name"))
        aliases = [(stock_code, "stock_code", 1.0)]
        if name:
            aliases.append((name, "company_name", 0.95))
        for alias in _split_alias_text(row.get("theme_tags")):
            aliases.append((alias, "theme_seed", 0.55))
        for alias in manual_aliases.get(stock_code, []):
            aliases.append((alias, "manual", 0.95))
        seen = set()
        for alias, alias_type, confidence in aliases:
            key = (alias_type, alias)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                normalize_entity_alias_entry(
                    {
                        "stock_code": stock_code,
                        "market": market,
                        "alias": alias,
                        "alias_type": alias_type,
                        "source": "stock_info_registry" if alias_type != "manual" else "manual",
                        "confidence": confidence,
                    }
                )
            )
    return pd.DataFrame(rows, columns=ENTITY_ALIAS_FIELDS)


def extract_aliases_from_evidence(evidence_frame, existing_alias_frame=None, min_occurrences=1):
    """Extract product/model/technology aliases from evidence text."""
    rows = []
    if evidence_frame is None or evidence_frame.empty:
        return pd.DataFrame(columns=ENTITY_ALIAS_FIELDS)
    existing = set()
    if existing_alias_frame is not None and not existing_alias_frame.empty:
        for _, row in existing_alias_frame.fillna("").iterrows():
            existing.add((
                normalize_stock_code(row.get("stock_code"), market=row.get("market") or "HK"),
                _clean(row.get("alias")),
            ))
    now = datetime.utcnow().isoformat()
    for code, group in evidence_frame.fillna("").groupby("stock_code"):
        stock_code = normalize_stock_code(code, market="HK")
        text = "\n".join(
            "\n".join(_clean(row.get(field)) for field in ("title", "summary", "raw_text", "url"))
            for _, row in group.iterrows()
        )
        counts = {}
        types = {}
        for pattern, alias_type in DOMAIN_ALIAS_PATTERNS:
            for match in re.finditer(pattern, text, flags=re.I):
                alias = _clean(match.group(0))
                if not alias or len(alias) < 2:
                    continue
                lower_alias = alias.lower()
                key = ALIAS_CANONICAL_OVERRIDES.get(lower_alias, alias.strip())
                counts[key] = counts.get(key, 0) + 1
                types.setdefault(key, alias_type)
        for alias, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())):
            if count < int(min_occurrences or 1):
                continue
            if (stock_code, alias) in existing:
                continue
            rows.append(
                normalize_entity_alias_entry(
                    {
                        "stock_code": stock_code,
                        "market": "HK",
                        "alias": alias,
                        "alias_type": types.get(alias) or "evidence_alias",
                        "source": "evidence_alias_extractor",
                        "confidence": min(0.9, 0.62 + count * 0.06),
                        "updated_at": now,
                    }
                )
            )
    return pd.DataFrame(rows, columns=ENTITY_ALIAS_FIELDS)


def merge_alias_frames(*frames):
    """Merge alias frames with stable dedupe and schema normalization."""
    rows = []
    for frame in frames:
        if frame is None or frame.empty:
            continue
        for _, row in frame.fillna("").iterrows():
            try:
                rows.append(normalize_entity_alias_entry(row.to_dict()))
            except Exception:
                continue
    result = pd.DataFrame(rows, columns=ENTITY_ALIAS_FIELDS)
    if result.empty:
        return pd.DataFrame(columns=ENTITY_ALIAS_FIELDS)
    result["_confidence_num"] = pd.to_numeric(result["confidence"], errors="coerce").fillna(0)
    result = result.sort_values(["market", "stock_code", "alias", "_confidence_num", "updated_at"])
    result = result.drop_duplicates(subset=["market", "stock_code", "alias"], keep="last")
    result = result.drop(columns=["_confidence_num"])
    return result[ENTITY_ALIAS_FIELDS].reset_index(drop=True)


def alias_map_from_frame(alias_frame):
    """Return stock_code -> unique aliases."""
    result = {}
    if alias_frame is None or alias_frame.empty:
        return result
    frame = alias_frame.fillna("")
    if "stock_code" not in frame.columns or "alias" not in frame.columns:
        return result
    for code, group in frame.groupby("stock_code"):
        stock_code = normalize_stock_code(code, market="HK")
        aliases = [stock_code]
        aliases.extend(_clean(alias) for alias in group["alias"].astype(str) if _clean(alias))
        result[stock_code] = list(dict.fromkeys(aliases))
    return result


def build_lightrag_evidence_document(row, aliases=None):
    """Convert one evidence row to a deterministic LightRAG text document."""
    stock_code = normalize_stock_code(row.get("stock_code"), market=row.get("market") or "HK")
    market = (_clean(row.get("market")) or "HK").upper()
    aliases = list(dict.fromkeys(_clean(alias) for alias in (aliases or [stock_code]) if _clean(alias)))
    url = clean_evidence_url(row.get("url"))
    title = sanitize_evidence_text(row.get("title"))
    summary = sanitize_evidence_text(row.get("summary"))
    raw_text = sanitize_evidence_text(row.get("raw_text"))
    source = _clean(row.get("source"))
    fetched_at = _clean(row.get("fetched_at"))
    doc_id = evidence_id({**dict(row), "url": url})
    file_source = f"stock_evidence/{market}/{stock_code}/{doc_id}.txt"
    parts = [
        f"STOCK_CODE: {stock_code}",
        f"MARKET: {market}",
        f"ALIASES: {'; '.join(aliases)}",
        f"SOURCE: {source}",
        f"TITLE: {title}",
        f"URL: {url}",
        f"FETCHED_AT: {fetched_at}",
        "",
        "SUMMARY:",
        summary,
    ]
    if raw_text:
        parts.extend(["", "RAW_TEXT:", raw_text])
    text = "\n".join(part for part in parts if part is not None).strip()
    return {
        "doc_id": doc_id,
        "stock_code": stock_code,
        "market": market,
        "file_source": file_source,
        "text": text,
        "url": url,
        "title": title,
    }


def build_lightrag_evidence_documents(evidence_frame, alias_frame=None, stock_codes=None, limit=None):
    """Build LightRAG documents from evidence CSV rows."""
    if evidence_frame is None or evidence_frame.empty:
        return []
    frame = evidence_frame.fillna("").copy()
    if stock_codes:
        allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
        frame = frame.loc[frame["stock_code"].astype(str).map(lambda code: normalize_stock_code(code, market="HK")).isin(allowed)]
    alias_map = alias_map_from_frame(alias_frame)
    documents = []
    for _, row in frame.iterrows():
        stock_code = normalize_stock_code(row.get("stock_code"), market=row.get("market") or "HK")
        try:
            doc = build_lightrag_evidence_document(row, aliases=alias_map.get(stock_code, [stock_code]))
        except Exception:
            continue
        if len(doc["text"]) < 80:
            continue
        documents.append(doc)
        if limit and len(documents) >= int(limit):
            break
    seen = set()
    unique = []
    for doc in documents:
        if doc["file_source"] in seen:
            continue
        seen.add(doc["file_source"])
        unique.append(doc)
    return unique


def build_lightrag_stock_query(stock_code_or_theme, alias_frame=None):
    """Build a retrieval query with aliases and deep-profile intent."""
    raw = _clean(stock_code_or_theme)
    if not raw:
        raise ValueError("stock_code_or_theme is required")
    code = normalize_stock_code(raw, market="HK") if re.fullmatch(r"\d{4,5}", raw) else raw
    aliases = []
    if alias_frame is not None and not alias_frame.empty and "stock_code" in alias_frame.columns:
        group = alias_frame.loc[alias_frame["stock_code"].astype(str) == code]
        aliases = list(group.get("alias", pd.Series(dtype=str)).astype(str)) if not group.empty else []
    alias_blob = " ".join(list(dict.fromkeys([code] + [_clean(alias) for alias in aliases if _clean(alias)]))[:12])
    return (
        f"{alias_blob} 股票画像 主营业务 产品 技术 大模型 AI 产业链 上游 下游 "
        "卡脖子 风险 催化 新闻 论文 GitHub benchmark"
    )


def build_lightrag_profile_queries(stock_code_or_theme, alias_frame=None, profile_mode="full"):
    """Build multiple retrieval intents for investable stock profiling."""
    raw = _clean(stock_code_or_theme)
    if not raw:
        raise ValueError("stock_code_or_theme is required")
    code = normalize_stock_code(raw, market="HK") if re.fullmatch(r"\d{4,5}", raw) else raw
    aliases = []
    if alias_frame is not None and not alias_frame.empty and "stock_code" in alias_frame.columns:
        group = alias_frame.loc[alias_frame["stock_code"].astype(str) == code]
        aliases = list(group.get("alias", pd.Series(dtype=str)).astype(str)) if not group.empty else []
    unique_aliases = list(dict.fromkeys([code] + [_clean(alias) for alias in aliases if _clean(alias)]))
    core = " ".join(unique_aliases[:10])
    product_aliases = " ".join(
        alias for alias in unique_aliases
        if re.search(r"GLM|ChatGLM|CodeGeeX|CogVLM|CogView|MaaS|智谱清言|智能体|大模型|代码模型", alias, flags=re.I)
    ) or core
    if str(profile_mode or "full").lower() in {"fast", "selection", "compact"}:
        return [
            (
                f"{core} 股票画像 选股特征 主营业务 产品 技术 产业链 上游 下游 "
                "客户 商业化 竞争优势 风险 催化 新闻 热度"
            )
        ]
    return [
        f"{core} 股票画像 主营业务 收入结构 客户 商业化 毛利 研发投入",
        f"{product_aliases} 产品矩阵 大模型 代码模型 智能体 MaaS 多模态",
        f"{product_aliases} 技术路线 MoE 推理 长上下文 Agentic Engineering benchmark 论文 arxiv GitHub HuggingFace",
        f"{core} 产业链 上游 算力 芯片 云服务 下游 客户 生态 合作伙伴",
        f"{core} 风险 亏损 竞争 监管 成本 商业化 估值 裁员",
        f"{core} 催化 发布 合作 开源 备案 融资 新闻 热度",
    ]


def build_source_aware_queries(stock_code, aliases):
    """Create high-quality queries from stock code, company aliases, and product aliases."""
    stock_code = normalize_stock_code(stock_code, market="HK")
    aliases = list(dict.fromkeys(_clean(alias) for alias in aliases if _clean(alias)))
    company_aliases = [a for a in aliases if a and a != stock_code]
    primary = company_aliases[0] if company_aliases else stock_code
    alias_blob = " ".join(company_aliases[:4]) or primary
    return [
        f"site:hkexnews.hk {stock_code} {primary} 年报 招股书 主营业务",
        f"{stock_code}.HK {primary} company profile business segments annual report",
        f"{primary} {alias_blob} 官方 产品 技术 业务",
        f"{primary} {alias_blob} 论文 arxiv GitHub HuggingFace benchmark",
        f"{primary} {alias_blob} 新闻 合作 客户 收入",
    ]


def fetch_source_aware_evidence(
    stock_code,
    aliases,
    fetcher_cls,
    *,
    searxng_url=None,
    max_results_per_query=5,
    max_queries_per_stock=8,
    engines=None,
    language="zh-CN",
    categories="general",
    query_workers=1,
):
    """Fetch profile-quality evidence using alias-aware, source-aware SearXNG queries."""
    stock_code = normalize_stock_code(stock_code, market="HK")
    aliases = list(dict.fromkeys([stock_code] + [_clean(alias) for alias in aliases if _clean(alias)]))
    queries = build_source_aware_queries(stock_code, aliases)
    rows = fetcher_cls(
        stock_code,
        company_name=aliases[1] if len(aliases) > 1 else stock_code,
        searxng_url=searxng_url,
        max_results_per_query=max_results_per_query,
        max_queries_per_stock=max_queries_per_stock,
        engines=engines,
        language=language,
        categories=categories,
        queries=queries,
        query_workers=query_workers,
    ).fetch()
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    return list(rows or [])


def evidence_relevance_score(row, aliases):
    """Score whether a search result is about the target company/product."""
    aliases = [_clean(alias).lower() for alias in aliases if _clean(alias)]
    title = _clean(row.get("title")).lower()
    summary = _clean(row.get("summary")).lower()
    raw_text = _clean(row.get("raw_text")).lower()
    url = _clean(row.get("url")).lower()
    text = "\n".join([title, summary, raw_text, url])
    score = 0.0
    for alias in aliases:
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", alias))
        if len(alias) <= 2 and not has_cjk:
            continue
        if alias.lower() in text:
            score += 0.35
    trusted_domains = [
        "hkexnews.hk",
        "z.ai",
        "zhipu",
        "arxiv.org",
        "github.com",
        "huggingface.co",
        "openrouter.ai",
    ]
    if any(domain in url for domain in trusted_domains):
        score += 0.25
    if "query=" in title.lower() and not any(alias in summary + raw_text for alias in aliases):
        score -= 0.25
    if "no_results" in title.lower() or "search_error" in title.lower():
        score -= 1.0
    return max(0.0, min(1.0, score))


def filter_relevant_evidence(evidence_frame, alias_frame, min_score=0.25):
    """Filter noisy search evidence and add cleaned URLs."""
    if evidence_frame is None or evidence_frame.empty:
        return pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
    rows = []
    alias_map = {}
    if alias_frame is not None and not alias_frame.empty:
        for code, group in alias_frame.groupby("stock_code"):
            alias_map[str(code)] = list(group["alias"].astype(str))
    for _, row in evidence_frame.fillna("").iterrows():
        stock_code = normalize_stock_code(row.get("stock_code"), market=row.get("market") or "HK")
        aliases = alias_map.get(stock_code, [stock_code])
        score = evidence_relevance_score(row, aliases)
        if score < float(min_score):
            continue
        payload = {field: row.get(field, "") for field in COMPANY_RESEARCH_EVIDENCE_FIELDS}
        payload["stock_code"] = stock_code
        payload["market"] = (_clean(payload.get("market")) or "HK").upper()
        payload["source"] = DEEP_EVIDENCE_SOURCE
        payload["url"] = clean_evidence_url(payload.get("url"))
        payload["title"] = _clean(payload.get("title"))
        payload["summary"] = _clean(payload.get("summary"))
        payload["raw_text"] = _clean(payload.get("raw_text"))
        rows.append(payload)
    result = pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
    if not result.empty:
        result["_evidence_id"] = result.apply(evidence_id, axis=1)
        result = result.drop_duplicates(subset=["market", "stock_code", "_evidence_id"], keep="last")
        result = result[COMPANY_RESEARCH_EVIDENCE_FIELDS]
    return result


def build_profile_prompt(stock_code, evidence_rows, alias_rows):
    evidence_columns = ["source", "title", "summary", "url", "raw_text"]
    evidence = evidence_rows.reindex(columns=evidence_columns).fillna("").head(20).to_dict("records")
    aliases = alias_rows.fillna("").to_dict("records") if alias_rows is not None else []
    return [
        {
            "role": "system",
            "content": (
                "你是股票画像和产业链图谱抽取器。只输出 JSON。"
                "所有产品、技术、产业链、风险、催化必须基于 evidence。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "stock_code": stock_code,
                    "aliases": aliases,
                    "evidence": evidence,
                    "output_schema": {
                        "stock_code": "string",
                        "summary": "string",
                        "strengths": ["string"],
                        "risks": ["string"],
                        "open_questions": ["string"],
                        "deep_tags": [
                            {
                                "tag": "string",
                                "tag_type": "product|technology|theme|bottleneck|catalyst|risk|business|value_chain",
                                "confidence": "number 0-1",
                                "is_primary": "boolean",
                                "evidence_refs": ["url or title"],
                            }
                        ],
                        "nodes": [
                            {
                                "node_type": "stock|company|product|technology|theme|supply_chain|event",
                                "node_id": "string",
                                "name": "string",
                            }
                        ],
                        "edges": [
                            {
                                "src_type": "string",
                                "src_id": "string",
                                "edge_type": "produces|belongs_to|capability|bottleneck|upstream|downstream|catalyst_for|risk_of",
                                "dst_type": "string",
                                "dst_id": "string",
                                "confidence": "number 0-1",
                                "evidence_refs": ["url or title"],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def parse_profile_response(text):
    raw = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.S)
    if match:
        raw = match.group(1).strip()
    payload = json.loads(raw)
    if "stock_code" not in payload:
        raise ValueError("profile response missing stock_code")
    payload["stock_code"] = normalize_stock_code(payload["stock_code"], market=payload.get("market") or "HK")
    payload.setdefault("deep_tags", [])
    payload.setdefault("nodes", [])
    payload.setdefault("edges", [])
    return payload


def profile_payload_to_frames(payload, market="HK", source=PROFILE_LLM_SOURCE):
    stock_code = normalize_stock_code(payload.get("stock_code"), market=market)
    now = datetime.utcnow().isoformat()
    evidence_count = len(payload.get("evidence_refs") or [])
    if not evidence_count:
        refs = []
        for collection_name in ("deep_tags", "edges"):
            for item in payload.get(collection_name, []) or []:
                refs.extend(item.get("evidence_refs") or [])
        evidence_count = len(set(refs))
    profile = normalize_stock_profile_entry(
        {
            "stock_code": stock_code,
            "market": market,
            "profile_json": json.dumps(payload, ensure_ascii=False),
            "summary": payload.get("summary"),
            "strengths": payload.get("strengths"),
            "risks": payload.get("risks"),
            "open_questions": payload.get("open_questions"),
            "evidence_count": evidence_count,
            "confidence": payload.get("confidence") or 0.75,
            "updated_at": now,
        }
    )
    deep_tags = []
    for tag in payload.get("deep_tags", []):
        refs = tag.get("evidence_refs") or []
        deep_tags.append(
            normalize_stock_deep_tag_entry(
                {
                    "stock_code": stock_code,
                    "market": market,
                    "tag": tag.get("tag"),
                    "tag_type": tag.get("tag_type"),
                    "confidence": tag.get("confidence"),
                    "evidence_count": len(refs),
                    "source_count": len(set(refs)),
                    "freshness_days": tag.get("freshness_days") or 0,
                    "attention_velocity_7d": tag.get("attention_velocity_7d") or 0,
                    "is_primary": tag.get("is_primary"),
                    "evidence_refs": refs,
                    "source": source,
                    "updated_at": now,
                }
            )
        )
    nodes = [
        normalize_stock_graph_node_entry(
            {
                "node_id": f"stock:{stock_code}",
                "node_type": "stock",
                "name": stock_code,
                "canonical_name": stock_code,
                "source": source,
                "confidence": 1.0,
                "updated_at": now,
            }
        )
    ]
    for node in payload.get("nodes", []):
        node_type = _clean(node.get("node_type")) or "entity"
        node_id = _clean(node.get("node_id")) or _clean(node.get("name"))
        if not node_id:
            continue
        if ":" not in node_id:
            node_id = f"{node_type}:{node_id}"
        nodes.append(
            normalize_stock_graph_node_entry(
                {
                    "node_id": node_id,
                    "node_type": node_type,
                    "name": node.get("name") or node_id,
                    "canonical_name": node.get("canonical_name") or node.get("name") or node_id,
                    "properties_json": json.dumps(node.get("properties") or {}, ensure_ascii=False),
                    "source": source,
                    "confidence": node.get("confidence") or 0.75,
                    "updated_at": now,
                }
            )
        )
    edges = []
    for edge in payload.get("edges", []):
        src_type = _clean(edge.get("src_type")) or "entity"
        dst_type = _clean(edge.get("dst_type")) or "entity"
        src_id = _clean(edge.get("src_id"))
        dst_id = _clean(edge.get("dst_id"))
        if src_type == "stock" and src_id == stock_code:
            src_id = f"stock:{stock_code}"
        elif src_id and ":" not in src_id:
            src_id = f"{src_type}:{src_id}"
        if dst_type == "stock" and dst_id == stock_code:
            dst_id = f"stock:{stock_code}"
        elif dst_id and ":" not in dst_id:
            dst_id = f"{dst_type}:{dst_id}"
        edges.append(
            normalize_stock_graph_edge_entry(
                {
                    "src_type": src_type,
                    "src_id": src_id,
                    "edge_type": edge.get("edge_type"),
                    "dst_type": dst_type,
                    "dst_id": dst_id,
                    "confidence": edge.get("confidence"),
                    "evidence_refs": edge.get("evidence_refs"),
                    "source": source,
                    "updated_at": now,
                }
            )
        )
    return (
        pd.DataFrame([profile], columns=STOCK_PROFILE_FIELDS),
        pd.DataFrame(deep_tags, columns=STOCK_DEEP_TAG_FIELDS),
        pd.DataFrame(nodes, columns=STOCK_GRAPH_NODE_FIELDS).drop_duplicates(subset=["node_id"], keep="last"),
        pd.DataFrame(edges, columns=STOCK_GRAPH_EDGE_FIELDS).drop_duplicates(
            subset=["src_type", "src_id", "edge_type", "dst_type", "dst_id"], keep="last"
        ),
    )


def _node_type_from_name(name, stock_code=None):
    text = _clean(name).lower()
    if stock_code and text in {stock_code.lower(), f"stock:{stock_code.lower()}"}:
        return "stock"
    if any(keyword in text for keyword in ("glm", "chatglm", "gpt", "模型", "model", "产品", "product")):
        return "product"
    if any(keyword in text for keyword in ("ai", "大模型", "agent", "coding", "芯片", "算力", "technology", "技术")):
        return "technology"
    if any(keyword in text for keyword in ("风险", "risk", "监管", "competition", "成本")):
        return "risk"
    if any(keyword in text for keyword in ("上游", "下游", "供应", "客户", "产业链", "supply")):
        return "supply_chain"
    if any(keyword in text for keyword in ("发布", "合作", "融资", "新闻", "event", "benchmark")):
        return "event"
    return "entity"


def _stock_node_id(stock_code):
    return f"stock:{normalize_stock_code(stock_code, market='HK')}"


def _node_id(node_type, name, stock_code=None):
    clean = _clean(name)
    if not clean:
        return ""
    if node_type == "stock" or (stock_code and clean == stock_code):
        return f"stock:{normalize_stock_code(stock_code or clean, market='HK')}"
    if ":" in clean:
        return clean
    return f"{node_type}:{clean}"


def _canonical_node_type(raw_type, name, stock_code=None):
    clean_type = _clean(raw_type)
    clean_name = _clean(name)
    if stock_code and clean_name == stock_code:
        return "stock"
    if clean_type in NODE_TYPE_ALIASES:
        return NODE_TYPE_ALIASES[clean_type]
    return clean_type or _node_type_from_name(clean_name, stock_code=stock_code)


def _edge_type_from_relationship(rel):
    text = " ".join(
        _clean(rel.get(field)).lower()
        for field in ("description", "keywords", "relation", "edge_type")
    )
    mapping = [
        ("produces", ("produce", "product", "产品", "发布", "develop", "开发")),
        ("belongs_to", ("属于", "part of", "subset", "领域", "industry", "theme")),
        ("capability", ("能力", "capability", "benchmark", "性能", "coding", "推理")),
        ("bottleneck", ("瓶颈", "卡脖子", "bottleneck", "constraint", "shortage")),
        ("upstream", ("上游", "upstream", "supplier", "供应")),
        ("downstream", ("下游", "downstream", "customer", "客户")),
        ("catalyst_for", ("催化", "catalyst", "合作", "发布", "增长")),
        ("risk_of", ("风险", "risk", "监管", "competition", "成本")),
    ]
    for edge_type, keywords in mapping:
        if any(keyword in text for keyword in keywords):
            return edge_type
    return "related_to"


def _normalize_lightrag_edge(src_type, src_id, edge_type, dst_type, dst_id, rel):
    text = " ".join(_clean(rel.get(field)) for field in ("description", "keywords")).lower()
    if "股票代码" in text and src_type == "stock":
        return src_type, src_id, "alias_of", dst_type, dst_id
    if src_type in {"method", "artifact", "product"} and dst_type == "organization" and edge_type == "produces":
        if any(keyword in text for keyword in ("提供", "产品", "能力", "拥有", "运营")):
            return dst_type, dst_id, "produces", src_type, src_id
    if src_type in {"content"} and dst_type == "organization" and edge_type == "produces":
        return dst_type, dst_id, "discloses", src_type, src_id
    if src_type == "artifact" and dst_type == "organization" and any(keyword in text for keyword in ("拥有", "运营", "平台")):
        return dst_type, dst_id, "produces", src_type, src_id
    return src_type, src_id, edge_type, dst_type, dst_id


def _semantic_edge_type(edge_type, src_id, dst_id, evidence_refs=""):
    if edge_type not in {"related_to", "belongs_to"}:
        return edge_type
    text = " ".join([_clean(src_id), _clean(dst_id), _clean(evidence_refs)])
    for semantic_type, keywords in SEMANTIC_EDGE_KEYWORDS:
        if any(keyword.lower() in text.lower() for keyword in keywords):
            return semantic_type
    return edge_type


def _dimension_hit_counts(text):
    lowered = _clean(text).lower()
    result = {}
    for dimension, keywords in INVESTABLE_PROFILE_DIMENSIONS.items():
        count = 0
        for keyword in keywords:
            if keyword.lower() in lowered:
                count += 1
        result[dimension] = count
    return result


def score_stock_profile_quality(
    evidence_frame=None,
    alias_frame=None,
    node_frame=None,
    edge_frame=None,
    stock_code=None,
):
    """Score whether a stock profile is deep enough for investment research."""
    code = normalize_stock_code(stock_code, market="HK") if stock_code else ""
    evidence = evidence_frame.fillna("") if evidence_frame is not None else pd.DataFrame()
    aliases = alias_frame.fillna("") if alias_frame is not None else pd.DataFrame()
    nodes = node_frame.fillna("") if node_frame is not None else pd.DataFrame()
    edges = edge_frame.fillna("") if edge_frame is not None else pd.DataFrame()
    if code and not evidence.empty and "stock_code" in evidence.columns:
        evidence = evidence.loc[evidence["stock_code"].astype(str) == code]
    if code and not aliases.empty and "stock_code" in aliases.columns:
        aliases = aliases.loc[aliases["stock_code"].astype(str) == code]
    if code:
        nodes, edges = filter_graph_for_stock(nodes, edges, code)
    text_parts = []
    for frame, fields in (
        (evidence, ("title", "summary", "raw_text", "url")),
        (aliases, ("alias", "alias_type")),
        (nodes, ("node_id", "node_type", "name", "canonical_name", "properties_json")),
        (edges, ("src_id", "edge_type", "dst_id", "evidence_refs")),
    ):
        if frame is None or frame.empty:
            continue
        for field in fields:
            if field in frame.columns:
                text_parts.extend(frame[field].astype(str).tolist())
    text = "\n".join(text_parts)
    dimension_hits = _dimension_hit_counts(text)
    covered = [key for key, value in dimension_hits.items() if value > 0]
    evidence_sources = int(evidence["url"].nunique()) if not evidence.empty and "url" in evidence.columns else 0
    alias_count = int(aliases["alias"].nunique()) if not aliases.empty and "alias" in aliases.columns else 0
    node_count = int(nodes["node_id"].nunique()) if not nodes.empty and "node_id" in nodes.columns else 0
    edge_count = int(len(edges)) if edges is not None else 0
    score = 0
    score += min(25, len(covered) * 4)
    score += min(20, evidence_sources * 2)
    score += min(15, alias_count)
    score += min(20, node_count)
    score += min(20, edge_count * 2)
    missing = [key for key in INVESTABLE_PROFILE_DIMENSIONS if key not in covered]
    if evidence_sources < 5:
        missing.append("evidence_sources>=5")
    if alias_count < 8:
        missing.append("alias_count>=8")
    if edge_count < 15:
        missing.append("edge_count>=15")
    return {
        "stock_code": code,
        "quality_score": int(min(100, score)),
        "decision_ready": score >= 75 and not {"business", "product", "technology", "risk"}.intersection(missing),
        "covered_dimensions": covered,
        "missing_dimensions": missing,
        "dimension_hits": dimension_hits,
        "evidence_sources": evidence_sources,
        "alias_count": alias_count,
        "node_count": node_count,
        "edge_count": edge_count,
    }


def normalize_graph_frames(node_frame, edge_frame, source=LIGHTRAG_PROFILE_SOURCE):
    """Normalize graph node types and materialize missing edge endpoint nodes."""
    now = datetime.utcnow().isoformat()
    nodes = node_frame.copy() if node_frame is not None else pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
    edges = edge_frame.copy() if edge_frame is not None else pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS)
    if nodes.empty and edges.empty:
        return (
            pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS),
            pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS),
        )
    for column in STOCK_GRAPH_NODE_FIELDS:
        if column not in nodes.columns:
            nodes[column] = ""
    for column in STOCK_GRAPH_EDGE_FIELDS:
        if column not in edges.columns:
            edges[column] = ""
    nodes = nodes[STOCK_GRAPH_NODE_FIELDS].fillna("")
    edges = edges[STOCK_GRAPH_EDGE_FIELDS].fillna("")
    if not nodes.empty:
        nodes["node_type"] = nodes["node_type"].map(lambda value: NODE_TYPE_ALIASES.get(_clean(value), _clean(value) or "entity"))
        normalized_node_ids = []
        for _, row in nodes.iterrows():
            node_type = _clean(row.get("node_type")) or "entity"
            name = _clean(row.get("name")) or _clean(row.get("node_id"))
            node_id = _clean(row.get("node_id"))
            if ":" in node_id:
                prefix, suffix = node_id.split(":", 1)
                prefix = NODE_TYPE_ALIASES.get(prefix, prefix)
                node_id = f"{prefix}:{suffix}"
            else:
                node_id = _node_id(node_type, name)
            normalized_node_ids.append(node_id)
        nodes["node_id"] = normalized_node_ids
    node_ids = set(nodes["node_id"].astype(str)) if not nodes.empty else set()
    extra_nodes = []
    for _, edge in edges.iterrows():
        for type_field, id_field in (("src_type", "src_id"), ("dst_type", "dst_id")):
            node_type = NODE_TYPE_ALIASES.get(_clean(edge.get(type_field)), _clean(edge.get(type_field)) or "entity")
            node_id = _clean(edge.get(id_field))
            if ":" in node_id:
                prefix, suffix = node_id.split(":", 1)
                prefix = NODE_TYPE_ALIASES.get(prefix, prefix)
                normalized_id = f"{prefix}:{suffix}"
            else:
                normalized_id = _node_id(node_type, node_id)
            edges.loc[edge.name, type_field] = node_type
            edges.loc[edge.name, id_field] = normalized_id
            if normalized_id and normalized_id not in node_ids:
                name = normalized_id.split(":", 1)[1] if ":" in normalized_id else normalized_id
                extra_nodes.append(
                    normalize_stock_graph_node_entry(
                        {
                            "node_id": normalized_id,
                            "node_type": node_type,
                            "name": name,
                            "canonical_name": name,
                            "properties_json": json.dumps({"materialized_from_edge": True}, ensure_ascii=False),
                            "source": source,
                            "confidence": 0.58,
                            "updated_at": now,
                        }
                    )
                )
                node_ids.add(normalized_id)
    if extra_nodes:
        nodes = pd.concat([nodes, pd.DataFrame(extra_nodes, columns=STOCK_GRAPH_NODE_FIELDS)], ignore_index=True)
    if not nodes.empty:
        nodes = nodes.drop_duplicates(subset=["node_id"], keep="last")
    if not edges.empty:
        edges["edge_type"] = edges.apply(
            lambda row: _semantic_edge_type(
                row.get("edge_type"),
                row.get("src_id"),
                row.get("dst_id"),
                row.get("evidence_refs"),
            ),
            axis=1,
        )
        edges = edges.drop_duplicates(subset=["src_type", "src_id", "edge_type", "dst_type", "dst_id"], keep="last")
    return nodes[STOCK_GRAPH_NODE_FIELDS].reset_index(drop=True), edges[STOCK_GRAPH_EDGE_FIELDS].reset_index(drop=True)


def _reference_for_item(item, reference_map=None):
    refs = []
    for field in ("file_path", "source_id", "chunk_id", "reference_id"):
        value = _clean(item.get(field))
        if value:
            refs.append(value)
    if reference_map:
        ref_id = _clean(item.get("reference_id"))
        if ref_id and ref_id in reference_map:
            refs.append(reference_map[ref_id])
    return list(dict.fromkeys(refs))


def lightrag_context_to_stock_graph(context, stock_code=None, market="HK", source=LIGHTRAG_PROFILE_SOURCE):
    """Map LightRAG /query/data response into local stock graph node/edge frames."""
    now = datetime.utcnow().isoformat()
    payload = context or {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data or {}
    stock_code = normalize_stock_code(stock_code, market=market) if stock_code else ""
    references = data.get("references") or []
    reference_map = {
        _clean(ref.get("reference_id")): _clean(ref.get("file_path"))
        for ref in references if _clean(ref.get("reference_id"))
    }
    nodes = []
    edges = []
    if stock_code:
        nodes.append(
            normalize_stock_graph_node_entry(
                {
                    "node_id": f"stock:{stock_code}",
                    "node_type": "stock",
                    "name": stock_code,
                    "canonical_name": stock_code,
                    "properties_json": json.dumps({"market": market}, ensure_ascii=False),
                    "source": source,
                    "confidence": 1.0,
                    "updated_at": now,
                }
            )
        )
    entity_id_map = {}
    entity_type_map = {}
    for entity in data.get("entities") or []:
        name = _clean(entity.get("entity_name") or entity.get("name") or entity.get("id") or entity.get("entity_id"))
        if not name:
            continue
        node_type = _canonical_node_type(entity.get("entity_type"), name, stock_code=stock_code)
        node_id = _node_id(node_type, name, stock_code=stock_code)
        entity_id_map[name] = node_id
        entity_type_map[name] = node_type
        if stock_code and node_id == f"stock:{stock_code}":
            continue
        props = {
            "description": sanitize_evidence_text(entity.get("description")),
            "keywords": sanitize_evidence_text(entity.get("keywords")),
            "source_id": _clean(entity.get("source_id")),
            "file_path": _clean(entity.get("file_path")),
        }
        nodes.append(
            normalize_stock_graph_node_entry(
                {
                    "node_id": node_id,
                    "node_type": node_type,
                    "name": name,
                    "canonical_name": name,
                    "properties_json": json.dumps({k: v for k, v in props.items() if v}, ensure_ascii=False),
                    "source": source,
                    "confidence": entity.get("weight") or entity.get("rank") or 0.72,
                    "updated_at": now,
                }
            )
        )
    for rel in data.get("relationships") or []:
        src_name = _clean(rel.get("src_id") or rel.get("source") or rel.get("src"))
        dst_name = _clean(rel.get("tgt_id") or rel.get("target") or rel.get("dst_id") or rel.get("tgt"))
        if not src_name or not dst_name:
            continue
        src_id = entity_id_map.get(src_name)
        dst_id = entity_id_map.get(dst_name)
        src_type = entity_type_map.get(src_name) or _node_type_from_name(src_name, stock_code=stock_code)
        dst_type = entity_type_map.get(dst_name) or _node_type_from_name(dst_name, stock_code=stock_code)
        if not src_id:
            src_id = _node_id(src_type, src_name, stock_code=stock_code)
        if not dst_id:
            dst_id = _node_id(dst_type, dst_name, stock_code=stock_code)
        edge_type = _edge_type_from_relationship(rel)
        src_type, src_id, edge_type, dst_type, dst_id = _normalize_lightrag_edge(
            src_type, src_id, edge_type, dst_type, dst_id, rel
        )
        edges.append(
            normalize_stock_graph_edge_entry(
                {
                    "src_type": src_type,
                    "src_id": src_id,
                    "edge_type": edge_type,
                    "dst_type": dst_type,
                    "dst_id": dst_id,
                    "confidence": rel.get("weight") or 0.7,
                    "evidence_refs": _reference_for_item(rel, reference_map),
                    "source": source,
                    "updated_at": now,
                }
            )
        )
    for chunk in data.get("chunks") or []:
        content = sanitize_evidence_text(chunk.get("content"))
        file_path = _clean(chunk.get("file_path"))
        if not stock_code or not content:
            continue
        digest = hashlib.sha256((file_path + content[:300]).encode("utf-8")).hexdigest()[:12]
        chunk_node_id = f"evidence_chunk:{digest}"
        nodes.append(
            normalize_stock_graph_node_entry(
                {
                    "node_id": chunk_node_id,
                    "node_type": "evidence_chunk",
                    "name": file_path or chunk_node_id,
                    "canonical_name": file_path or chunk_node_id,
                    "properties_json": json.dumps({"content": content[:1000], "file_path": file_path}, ensure_ascii=False),
                    "source": source,
                    "confidence": 0.65,
                    "updated_at": now,
                }
            )
        )
        edges.append(
            normalize_stock_graph_edge_entry(
                {
                    "src_type": "stock",
                    "src_id": f"stock:{stock_code}",
                    "edge_type": "evidence_of",
                    "dst_type": "evidence_chunk",
                    "dst_id": chunk_node_id,
                    "confidence": 0.65,
                    "evidence_refs": _reference_for_item(chunk, reference_map),
                    "source": source,
                    "updated_at": now,
                }
            )
        )
    node_frame = pd.DataFrame(nodes, columns=STOCK_GRAPH_NODE_FIELDS)
    edge_frame = pd.DataFrame(edges, columns=STOCK_GRAPH_EDGE_FIELDS)
    if not node_frame.empty:
        node_frame = node_frame.drop_duplicates(subset=["node_id"], keep="last")
    if not edge_frame.empty:
        edge_frame = edge_frame.drop_duplicates(
            subset=["src_type", "src_id", "edge_type", "dst_type", "dst_id"], keep="last"
        )
    return normalize_graph_frames(node_frame, edge_frame, source=source)


def retrieve_subgraph(nodes_frame, edges_frame, seed_node_ids, depth=2):
    """Return a small undirected expansion from seed nodes using DataFrames."""
    if edges_frame is None or edges_frame.empty:
        return (
            pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS),
            pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS),
        )
    frontier = set(seed_node_ids)
    visited = set(frontier)
    selected_edges = []
    for _ in range(max(1, int(depth or 1))):
        mask = edges_frame["src_id"].isin(frontier) | edges_frame["dst_id"].isin(frontier)
        hop = edges_frame.loc[mask]
        if hop.empty:
            break
        selected_edges.append(hop)
        next_nodes = set(hop["src_id"]).union(set(hop["dst_id"])) - visited
        visited.update(next_nodes)
        frontier = next_nodes
        if not frontier:
            break
    edge_result = (
        pd.concat(selected_edges, ignore_index=True).drop_duplicates()
        if selected_edges else pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS)
    )
    if nodes_frame is None or nodes_frame.empty:
        node_result = pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
    else:
        node_result = nodes_frame.loc[nodes_frame["node_id"].isin(visited)].copy()
    return node_result.reset_index(drop=True), edge_result.reset_index(drop=True)


def _frame_text(row, fields):
    return " ".join(_clean(row.get(field)) for field in fields if _clean(row.get(field)))


def _top_unique(values, limit=12):
    result = []
    seen = set()
    for value in values:
        text = _clean(value)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
        if len(result) >= int(limit):
            break
    return result


def _filter_records_by_keywords(frame, keywords, fields, limit=10):
    if frame is None or frame.empty:
        return []
    rows = []
    for _, row in frame.fillna("").iterrows():
        text = _frame_text(row, fields)
        if any(keyword.lower() in text.lower() for keyword in keywords):
            rows.append(row.to_dict())
    return rows[: int(limit)]


def _hostname(url):
    try:
        return urlsplit(str(url or "")).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def score_source_quality(url_or_source):
    """Return a deterministic source quality score in 0..1."""
    text = _clean(url_or_source).lower()
    host = _hostname(text)
    target = host or text
    for key, score in SOURCE_QUALITY_WEIGHTS.items():
        if key in target:
            return float(score)
    if any(token in target for token in ["official", "官网", "ir", "公告", "年报", "招股书"]):
        return 0.82
    if any(token in target for token in ["news", "媒体", "finance", "stock"]):
        return 0.5
    return 0.4 if target else 0.0


def _theme_terms(theme):
    terms = _top_unique(re.split(r"[\s,，;；/|]+", _clean(theme)), limit=20)
    theme_text = _clean(theme)
    if theme_text and theme_text not in terms:
        terms.insert(0, theme_text)
    return [term for term in terms if term]


def _theme_match_count(text, theme_terms):
    source = _clean(text).lower()
    if not source:
        return 0
    return sum(1 for term in theme_terms if term.lower() in source)


def _theme_relevance_gate(theme_relevance, technology, commercialization, value_chain, catalysts, bottleneck):
    """Prevent generic attention/evidence from creating a false theme hit."""
    core_score = float(technology or 0) + float(commercialization or 0) + float(value_chain or 0) + float(catalysts or 0) + float(bottleneck or 0)
    relevance = float(theme_relevance or 0)
    if relevance >= 4.0 or core_score >= 10.0:
        return {
            "multiplier": 1.0,
            "max_score": 100.0,
            "label": "theme_relevant",
            "core_score": round(core_score, 3),
        }
    if relevance >= 2.0 or core_score >= 5.0:
        return {
            "multiplier": 0.65,
            "max_score": 35.0,
            "label": "weak_theme_relevance",
            "core_score": round(core_score, 3),
        }
    return {
        "multiplier": 0.25,
        "max_score": 12.0,
        "label": "no_theme_relevance",
        "core_score": round(core_score, 3),
    }


def _generic_evidence_domain_rate(refs_text):
    refs = _split_alias_text(refs_text)
    if not refs:
        return 0.0
    generic = 0
    for ref in refs:
        host = _hostname(ref)
        if any(domain == host or host.endswith(f".{domain}") for domain in GENERIC_EVIDENCE_DOMAINS):
            generic += 1
    return round(generic / len(refs), 4)


def _row_join(row, fields):
    return " ".join(_clean(row.get(field)) for field in fields)


def filter_graph_for_stock(node_frame=None, edge_frame=None, stock_code=None):
    """Return a stock-local graph slice with one-hop expansion for scoring."""
    code = normalize_stock_code(stock_code, market="HK") if stock_code else ""
    nodes = node_frame.fillna("") if node_frame is not None else pd.DataFrame()
    edges = edge_frame.fillna("") if edge_frame is not None else pd.DataFrame()
    if not code or edges.empty:
        return (
            pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS) if nodes.empty else nodes.iloc[0:0].copy(),
            pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS) if edges.empty else edges.iloc[0:0].copy(),
        )
    stock_node = f"stock:{code}"
    seed_edges = edges.loc[
        edges.get("src_id", pd.Series(dtype=str)).astype(str).eq(stock_node)
        | edges.get("dst_id", pd.Series(dtype=str)).astype(str).eq(stock_node)
        | edges.get("evidence_refs", pd.Series(dtype=str)).astype(str).str.contains(code, na=False)
    ].copy()
    if seed_edges.empty:
        local_edges = seed_edges
    else:
        local_node_ids = {stock_node}
        for column in ("src_id", "dst_id"):
            if column in seed_edges.columns:
                local_node_ids.update(_clean(value) for value in seed_edges[column].astype(str).tolist() if _clean(value))
        expanded_edges = edges.loc[
            edges.get("src_id", pd.Series(dtype=str)).astype(str).isin(local_node_ids)
            | edges.get("dst_id", pd.Series(dtype=str)).astype(str).isin(local_node_ids)
            | edges.get("evidence_refs", pd.Series(dtype=str)).astype(str).str.contains(code, na=False)
        ].copy()
        local_edges = expanded_edges.drop_duplicates().reset_index(drop=True)
    if local_edges.empty:
        local_nodes = nodes.loc[
            nodes.get("node_id", pd.Series(dtype=str)).astype(str).eq(stock_node)
        ].copy() if not nodes.empty else pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
        return local_nodes, local_edges
    local_node_ids = set()
    for column in ("src_id", "dst_id"):
        if column in local_edges.columns:
            local_node_ids.update(_clean(value) for value in local_edges[column].astype(str).tolist() if _clean(value))
    if not nodes.empty and "node_id" in nodes.columns:
        local_nodes = nodes.loc[nodes["node_id"].astype(str).isin(local_node_ids)].copy()
    else:
        local_nodes = pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
    return local_nodes, local_edges


def derive_attention_signals(
    evidence_frame=None,
    node_frame=None,
    edge_frame=None,
    alias_frame=None,
    stock_codes=None,
    asof_date=None,
):
    """Derive weak attention signals from existing evidence/graph artifacts.

    This is a deterministic local extractor. External APIs such as Twitter/GitHub
    can append rows with the same schema later.
    """
    asof = _clean(asof_date) or datetime.utcnow().date().isoformat()
    evidence = evidence_frame.fillna("") if evidence_frame is not None else pd.DataFrame()
    nodes = node_frame.fillna("") if node_frame is not None else pd.DataFrame()
    edges = edge_frame.fillna("") if edge_frame is not None else pd.DataFrame()
    aliases = alias_frame.fillna("") if alias_frame is not None else pd.DataFrame()
    allowed = {normalize_stock_code(code, market="HK") for code in stock_codes} if stock_codes else None
    rows = []

    def add(entity_type, entity_id, source, metric, value, window="7d", quality_score=0.5):
        if not entity_id:
            return
        rows.append(
            normalize_attention_signal_entry(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "source": source,
                    "metric": metric,
                    "value": float(value),
                    "window": window,
                    "velocity": float(value),
                    "quality_score": quality_score,
                    "asof_date": asof,
                }
            )
        )

    if not evidence.empty and "stock_code" in evidence.columns:
        for code, group in evidence.groupby("stock_code"):
            norm_code = normalize_stock_code(code, market="HK")
            if allowed and norm_code not in allowed:
                continue
            full_text = " ".join(
                _row_join(row, ["source", "title", "summary", "raw_text", "url"])
                for _, row in group.iterrows()
            ).lower()
            for source, keywords in ATTENTION_SOURCE_KEYWORDS.items():
                count = sum(full_text.count(keyword.lower()) for keyword in keywords)
                if count:
                    add("stock", norm_code, source, "mentions", count, quality_score=min(1.0, 0.35 + count / 20.0))
            source_quality = sum(score_source_quality(row.get("url") or row.get("source")) for _, row in group.iterrows())
            if source_quality:
                add("stock", norm_code, "evidence", "source_quality", source_quality, quality_score=min(1.0, source_quality / max(1, len(group))))

    if not edges.empty:
        edge_text_fields = ["src_id", "edge_type", "dst_id", "evidence_refs", "source"]
        for _, row in edges.iterrows():
            text = _row_join(row, edge_text_fields).lower()
            stock_entities = []
            for value in [row.get("src_id"), row.get("dst_id")]:
                value_text = _clean(value)
                if value_text.startswith("stock:"):
                    stock_entities.append(normalize_stock_code(value_text.split(":", 1)[1], market="HK"))
            for stock_code in stock_entities:
                if allowed and stock_code not in allowed:
                    continue
                for source, keywords in ATTENTION_SOURCE_KEYWORDS.items():
                    if any(keyword.lower() in text for keyword in keywords):
                        add("stock", stock_code, source, "graph_mentions", 1, quality_score=0.55)

    if not nodes.empty:
        for _, row in nodes.iterrows():
            node_id = _clean(row.get("node_id"))
            node_type = _clean(row.get("node_type")) or "entity"
            text = _row_join(row, ["node_id", "name", "canonical_name", "properties_json"])
            for source, keywords in ATTENTION_SOURCE_KEYWORDS.items():
                if any(keyword.lower() in text.lower() for keyword in keywords):
                    add(node_type, node_id, source, "entity_mentions", 1, quality_score=0.45)

    if not aliases.empty and "stock_code" in aliases.columns:
        for code, group in aliases.groupby("stock_code"):
            norm_code = normalize_stock_code(code, market="HK")
            if allowed and norm_code not in allowed:
                continue
            product_alias_count = int(group.get("alias_type", pd.Series(dtype=str)).astype(str).isin(["product", "model", "technology", "brand"]).sum())
            if product_alias_count:
                add("stock", norm_code, "alias_registry", "product_aliases", product_alias_count, quality_score=0.65)

    result = pd.DataFrame(rows, columns=[
        "entity_type", "entity_id", "source", "metric", "value", "window",
        "velocity", "quality_score", "asof_date",
    ])
    if not result.empty:
        result = (
            result.groupby(["entity_type", "entity_id", "source", "metric", "window", "asof_date"], as_index=False)
            .agg({"value": "sum", "velocity": "sum", "quality_score": "max"})
        )
    return result


def enrich_supply_chain_graph(
    evidence_frame=None,
    alias_frame=None,
    node_frame=None,
    edge_frame=None,
    stock_codes=None,
):
    """Add deterministic supply-chain/bottleneck graph edges from evidence text."""
    evidence = evidence_frame.fillna("") if evidence_frame is not None else pd.DataFrame()
    aliases = alias_frame.fillna("") if alias_frame is not None else pd.DataFrame()
    nodes = node_frame.fillna("") if node_frame is not None else pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
    edges = edge_frame.fillna("") if edge_frame is not None else pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS)
    allowed = {normalize_stock_code(code, market="HK") for code in stock_codes} if stock_codes else None
    now = datetime.utcnow().isoformat()
    new_nodes = []
    new_edges = []

    def add_node(node_type, name, source="supply_chain_rules", confidence=0.72):
        node_id = f"{node_type}:{name}"
        new_nodes.append(
            normalize_stock_graph_node_entry(
                {
                    "node_id": node_id,
                    "node_type": node_type,
                    "name": name,
                    "canonical_name": name,
                    "properties_json": "{}",
                    "source": source,
                    "confidence": confidence,
                    "updated_at": now,
                }
            )
        )
        return node_id

    def add_edge(src_type, src_id, edge_type, dst_type, dst_id, refs, confidence=0.68):
        new_edges.append(
            normalize_stock_graph_edge_entry(
                {
                    "src_type": src_type,
                    "src_id": src_id,
                    "edge_type": edge_type,
                    "dst_type": dst_type,
                    "dst_id": dst_id,
                    "confidence": confidence,
                    "evidence_refs": refs,
                    "source": "supply_chain_rules",
                    "updated_at": now,
                }
            )
        )

    if evidence.empty or "stock_code" not in evidence.columns:
        return normalize_graph_frames(nodes, edges)
    for code, group in evidence.groupby("stock_code"):
        norm_code = normalize_stock_code(code, market="HK")
        if allowed and norm_code not in allowed:
            continue
        alias_terms = []
        if not aliases.empty and "stock_code" in aliases.columns and "alias" in aliases.columns:
            alias_terms = aliases.loc[aliases["stock_code"].astype(str) == norm_code, "alias"].astype(str).tolist()
        stock_node = add_node("stock", norm_code, confidence=0.9)
        refs = _top_unique(group.get("url", pd.Series(dtype=str)).astype(str).tolist(), limit=8)
        text = " ".join(
            _row_join(row, ["title", "summary", "raw_text", "url"])
            for _, row in group.iterrows()
        )
        product_candidates = _top_unique(
            [alias for alias in alias_terms if any(token.lower() in alias.lower() for token in ["glm", "chatglm", "codegeex", "maas", "智谱"])]
            or alias_terms,
            limit=5,
        )
        product_node_ids = []
        for product in product_candidates:
            product_node_ids.append(add_node("product", product, confidence=0.74))
            add_edge("stock", stock_node, "produces", "product", f"product:{product}", refs, confidence=0.7)
        for rule in SUPPLY_CHAIN_RULES:
            if not any(keyword.lower() in text.lower() for keyword in rule["keywords"]):
                continue
            bottleneck_node = add_node("bottleneck", rule["bottleneck"], confidence=0.72)
            add_edge("stock", stock_node, "exposed_to", "bottleneck", bottleneck_node, refs, confidence=0.68)
            for product_node in product_node_ids:
                add_edge("product", product_node, "bottleneck", "bottleneck", bottleneck_node, refs, confidence=0.66)
            for upstream in rule["upstream"]:
                upstream_node = add_node("supplier_class", upstream, confidence=0.65)
                add_edge("bottleneck", bottleneck_node, "upstream", "supplier_class", upstream_node, refs, confidence=0.64)

    node_result = pd.concat([nodes, pd.DataFrame(new_nodes)], ignore_index=True) if new_nodes else nodes
    edge_result = pd.concat([edges, pd.DataFrame(new_edges)], ignore_index=True) if new_edges else edges
    return normalize_graph_frames(node_result, edge_result)


def build_stock_profile_report_payload(
    stock_code,
    evidence_frame=None,
    alias_frame=None,
    node_frame=None,
    edge_frame=None,
):
    """Build a deterministic stock profile report payload from evidence and graph data."""
    code = normalize_stock_code(stock_code, market="HK")
    evidence = evidence_frame.fillna("") if evidence_frame is not None else pd.DataFrame()
    aliases = alias_frame.fillna("") if alias_frame is not None else pd.DataFrame()
    nodes = node_frame.fillna("") if node_frame is not None else pd.DataFrame()
    edges = edge_frame.fillna("") if edge_frame is not None else pd.DataFrame()
    if not evidence.empty and "stock_code" in evidence.columns:
        evidence = evidence.loc[evidence["stock_code"].astype(str) == code]
    if not aliases.empty and "stock_code" in aliases.columns:
        aliases = aliases.loc[aliases["stock_code"].astype(str) == code]
    nodes, edges = filter_graph_for_stock(nodes, edges, code)
    quality = score_stock_profile_quality(evidence, aliases, nodes, edges, stock_code=code)
    alias_values = _top_unique(aliases["alias"].tolist() if "alias" in aliases.columns else [], limit=30)
    evidence_refs = _top_unique(evidence["url"].tolist() if "url" in evidence.columns else [], limit=20)
    sections = {}
    for section, keywords in REPORT_SECTION_KEYWORDS.items():
        section_edges = _filter_records_by_keywords(
            edges,
            keywords,
            ["src_id", "edge_type", "dst_id", "evidence_refs"],
            limit=12,
        )
        section_nodes = _filter_records_by_keywords(
            nodes,
            keywords,
            ["node_id", "node_type", "name", "canonical_name", "properties_json"],
            limit=12,
        )
        section_evidence = _filter_records_by_keywords(
            evidence,
            keywords,
            ["title", "summary", "raw_text", "url"],
            limit=6,
        )
        sections[section] = {
            "nodes": [
                {
                    "node_id": item.get("node_id", ""),
                    "node_type": item.get("node_type", ""),
                    "name": item.get("name", ""),
                }
                for item in section_nodes
            ],
            "edges": [
                {
                    "src_id": item.get("src_id", ""),
                    "edge_type": item.get("edge_type", ""),
                    "dst_id": item.get("dst_id", ""),
                    "evidence_refs": item.get("evidence_refs", ""),
                }
                for item in section_edges
            ],
            "evidence": [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "summary": item.get("summary", "")[:260],
                }
                for item in section_evidence
            ],
        }
    risks = sections.get("risks", {})
    catalysts = sections.get("catalysts", {})
    verdict = "watchlist" if quality["decision_ready"] and len(catalysts.get("edges", [])) >= len(risks.get("edges", [])) / 2 else "needs_more_evidence"
    if quality["quality_score"] < 60:
        verdict = "reject"
    return {
        "stock_code": code,
        "generated_at": datetime.utcnow().isoformat(),
        "verdict": verdict,
        "quality": quality,
        "aliases": alias_values,
        "sections": sections,
        "evidence_refs": evidence_refs,
    }


def render_stock_profile_report_markdown(payload):
    """Render stock profile report payload as markdown."""
    code = payload.get("stock_code", "")
    quality = payload.get("quality", {})
    lines = [
        f"# Stock Profile Report: {code}",
        "",
        f"- Verdict: {payload.get('verdict')}",
        f"- Quality score: {quality.get('quality_score')} / 100",
        f"- Decision ready: {quality.get('decision_ready')}",
        f"- Covered dimensions: {', '.join(quality.get('covered_dimensions') or [])}",
        f"- Evidence sources: {quality.get('evidence_sources')}",
        f"- Aliases: {', '.join((payload.get('aliases') or [])[:20])}",
        "",
    ]
    section_titles = {
        "products": "Products",
        "technology": "Technology",
        "business": "Business",
        "value_chain": "Value Chain",
        "catalysts": "Catalysts",
        "risks": "Risks",
        "attention": "Attention",
    }
    for key, title in section_titles.items():
        section = (payload.get("sections") or {}).get(key) or {}
        lines.extend([f"## {title}", ""])
        edges = section.get("edges") or []
        nodes = section.get("nodes") or []
        evidence = section.get("evidence") or []
        if edges:
            for item in edges[:8]:
                lines.append(f"- {item.get('src_id')} -[{item.get('edge_type')}]-> {item.get('dst_id')}")
        elif nodes:
            for item in nodes[:8]:
                lines.append(f"- {item.get('node_type')}:{item.get('name')}")
        else:
            lines.append("- No strong graph evidence yet.")
        if evidence:
            lines.append("")
            lines.append("Evidence:")
            for item in evidence[:4]:
                lines.append(f"- {item.get('title')} ({item.get('url')})")
        lines.append("")
    lines.append("## Evidence References")
    lines.append("")
    for ref in payload.get("evidence_refs") or []:
        lines.append(f"- {ref}")
    lines.append("")
    return "\n".join(lines)


def _numeric_score_from_stock_info(stock_code, stock_info_frame=None):
    if stock_info_frame is None or stock_info_frame.empty or "stock_code" not in stock_info_frame.columns:
        return {"liquidity_score": 0.0, "crowding_penalty": 0.0}
    code = normalize_stock_code(stock_code, market="HK")
    rows = stock_info_frame.fillna("").loc[stock_info_frame["stock_code"].astype(str) == code]
    if rows.empty:
        return {"liquidity_score": 0.0, "crowding_penalty": 0.0}
    row = rows.iloc[-1]

    def as_float(key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    amount = as_float("amount")
    volume = as_float("volume")
    market_cap = as_float("market_cap")
    pe_ratio = as_float("pe_ratio")
    pb_ratio = as_float("pb_ratio")
    liquidity = 0.0
    if amount > 0:
        liquidity += min(8.0, amount / 50_000_000.0)
    if volume > 0:
        liquidity += min(4.0, volume / 20_000_000.0)
    if market_cap > 0:
        liquidity += min(3.0, market_cap / 20_000_000_000.0)
    crowding = 0.0
    if pe_ratio > 80:
        crowding += min(8.0, (pe_ratio - 80.0) / 20.0)
    if pb_ratio > 10:
        crowding += min(5.0, (pb_ratio - 10.0) / 5.0)
    return {"liquidity_score": round(min(15.0, liquidity), 3), "crowding_penalty": round(min(15.0, crowding), 3)}


def _technical_trend_score(stock_code, feature_frame=None):
    if feature_frame is None or feature_frame.empty or "stock_code" not in feature_frame.columns:
        return 0.0
    code = normalize_stock_code(stock_code, market="HK")
    rows = feature_frame.fillna("").loc[feature_frame["stock_code"].astype(str) == code]
    if rows.empty:
        return 0.0
    score = 0.0
    for _, row in rows.tail(200).iterrows():
        name = _clean(row.get("feature_name")).lower()
        try:
            value = float(row.get("feature_value") or 0)
        except (TypeError, ValueError):
            continue
        if any(key in name for key in ["momentum", "ret_20", "ret_60", "rank_ic", "trend"]):
            score += max(-2.0, min(2.0, value))
    return round(max(-10.0, min(10.0, score)), 3)


def _attention_score_for_stock(stock_code, attention_frame=None):
    if attention_frame is None or attention_frame.empty or "entity_id" not in attention_frame.columns:
        return 0.0
    code = normalize_stock_code(stock_code, market="HK")
    rows = attention_frame.fillna("").loc[
        (attention_frame["entity_type"].astype(str) == "stock")
        & (attention_frame["entity_id"].astype(str) == code)
    ]
    total = 0.0
    for _, row in rows.iterrows():
        try:
            velocity = float(row.get("velocity") or row.get("value") or 0)
            quality = float(row.get("quality_score") or 0.5)
        except (TypeError, ValueError):
            continue
        total += velocity * max(0.1, min(1.0, quality))
    return round(min(15.0, total), 3)


def score_theme_opportunity(
    stock_code,
    evidence_frame=None,
    alias_frame=None,
    node_frame=None,
    edge_frame=None,
    attention_frame=None,
    stock_info_frame=None,
    feature_frame=None,
    theme="AI大模型",
    asof_date=None,
):
    """Score a stock's theme opportunity from graph/evidence dimensions."""
    payload = build_stock_profile_report_payload(stock_code, evidence_frame, alias_frame, node_frame, edge_frame)
    sections = payload.get("sections") or {}
    quality = payload.get("quality") or {}
    code = normalize_stock_code(stock_code, market="HK")
    theme_terms = _theme_terms(theme)

    def count(section, edge_types=None):
        edges = sections.get(section, {}).get("edges") or []
        if edge_types:
            return sum(1 for edge in edges if edge.get("edge_type") in edge_types)
        return len(edges)

    all_text = json.dumps(payload, ensure_ascii=False)
    theme_relevance = min(12.0, _theme_match_count(all_text, theme_terms) * 2.0) if theme_terms else 6.0
    technology = min(18.0, count("technology", {"uses_technology", "capability", "belongs_to", "related_to"}) * 2.0 + theme_relevance)
    commercialization = min(16.0, count("business", {"has_metric", "related_to", "catalyst_for"}) * 2.0)
    value_chain = min(14.0, count("value_chain", {"partner_with", "downstream", "upstream", "related_to", "produces", "bottleneck"}) * 2.0)
    catalysts = min(12.0, count("catalysts", {"catalyst_for", "partner_with", "produces", "related_to"}) * 2.0)
    graph_edges = edge_frame.fillna("") if edge_frame is not None else pd.DataFrame()
    bottleneck_hits = 0
    if not graph_edges.empty:
        for _, row in graph_edges.iterrows():
            text = _row_join(row, ["src_id", "edge_type", "dst_id", "evidence_refs"])
            if (
                row.get("edge_type") == "bottleneck"
                or row.get("dst_type") == "bottleneck"
                or (
                    row.get("edge_type") in VALUE_CHAIN_EDGE_TYPES
                    and any(keyword.lower() in text.lower() for keyword in BOTTLENECK_KEYWORDS)
                )
            ):
                bottleneck_hits += 1
    bottleneck = min(12.0, bottleneck_hits * 3.0)
    derived_attention = _attention_score_for_stock(code, attention_frame)
    attention = min(15.0, count("attention", {"has_attention", "related_to", "produces"}) * 2.0 + derived_attention)
    evidence_quality = min(12.0, float(quality.get("evidence_sources") or 0) * 1.5)
    numeric_scores = _numeric_score_from_stock_info(code, stock_info_frame)
    liquidity = numeric_scores["liquidity_score"]
    crowding = numeric_scores["crowding_penalty"]
    technical = _technical_trend_score(code, feature_frame)
    risk_penalty = min(24.0, count("risks", {"has_risk", "risk_of", "related_to"}) * 2.5)
    relevance_gate = _theme_relevance_gate(
        theme_relevance,
        technology,
        commercialization,
        value_chain,
        catalysts,
        bottleneck,
    )
    raw_score = (
        technology
        + commercialization
        + value_chain
        + bottleneck
        + catalysts
        + attention
        + evidence_quality
        + liquidity
        + technical
        - risk_penalty
        - crowding
    )
    score = max(
        0.0,
        min(
            float(relevance_gate["max_score"]),
            raw_score * float(relevance_gate["multiplier"]),
        ),
    )
    evidence_refs = ";".join((payload.get("evidence_refs") or [])[:8])
    component_scores = {
        "theme_relevance": theme_relevance,
        "theme_relevance_gate": relevance_gate["label"],
        "theme_core_score": relevance_gate["core_score"],
        "raw_score_before_gate": round(raw_score, 3),
        "generic_evidence_domain_rate": _generic_evidence_domain_rate(evidence_refs),
        "technology": technology,
        "commercialization": commercialization,
        "value_chain": value_chain,
        "bottleneck": bottleneck,
        "catalysts": catalysts,
        "attention": attention,
        "evidence_quality": evidence_quality,
        "liquidity": liquidity,
        "technical_trend": technical,
        "risk_penalty": risk_penalty,
        "crowding_penalty": crowding,
    }
    rank_reason = "; ".join(
        f"{key}={round(value, 2)}"
        for key, value in sorted(
            {key: value for key, value in component_scores.items() if isinstance(value, (int, float))}.items(),
            key=lambda item: abs(item[1]),
            reverse=True,
        )[:6]
    )
    if relevance_gate["label"] != "theme_relevant":
        rank_reason = f"{relevance_gate['label']}; {rank_reason}"
    now = datetime.utcnow().isoformat()
    row = {
        "stock_code": code,
        "market": "HK",
        "theme": _clean(theme) or "ALL",
        "score": round(score, 3),
        "technology_score": round(technology, 3),
        "commercialization_score": round(commercialization, 3),
        "value_chain_score": round(value_chain, 3),
        "bottleneck_score": round(bottleneck, 3),
        "catalyst_score": round(catalysts, 3),
        "attention_score": round(attention, 3),
        "evidence_quality_score": round(evidence_quality, 3),
        "liquidity_score": round(liquidity, 3),
        "technical_trend_score": round(technical, 3),
        "risk_penalty": round(risk_penalty, 3),
        "crowding_penalty": round(crowding, 3),
        "verdict": payload.get("verdict"),
        "rank_reason": rank_reason,
        "bull_case": "; ".join(
            _top_unique(
                [edge.get("dst_id") for edge in (sections.get("technology", {}).get("edges") or [])[:3]]
                + [edge.get("dst_id") for edge in (sections.get("catalysts", {}).get("edges") or [])[:3]],
                limit=6,
            )
        ),
        "bear_case": "; ".join(
            _top_unique(
                [edge.get("dst_id") for edge in (sections.get("risks", {}).get("edges") or [])[:6]],
                limit=6,
            )
        ),
        "key_evidence_refs": evidence_refs,
        "component_scores_json": json.dumps(component_scores, ensure_ascii=False, sort_keys=True),
        "asof_date": _clean(asof_date) or now[:10],
        "updated_at": now,
    }
    return normalize_theme_opportunity_score_entry(row)


def rank_theme_opportunities(
    theme,
    stock_codes=None,
    evidence_frame=None,
    alias_frame=None,
    node_frame=None,
    edge_frame=None,
    attention_frame=None,
    stock_info_frame=None,
    feature_frame=None,
    top_n=None,
    asof_date=None,
    min_score=None,
    show_progress=False,
):
    """Recall and rank stocks for a theme using graph/evidence/profile signals."""
    evidence = evidence_frame.fillna("") if evidence_frame is not None else pd.DataFrame()
    aliases = alias_frame.fillna("") if alias_frame is not None else pd.DataFrame()
    edges = edge_frame.fillna("") if edge_frame is not None else pd.DataFrame()
    stock_info = stock_info_frame.fillna("") if stock_info_frame is not None else pd.DataFrame()
    codes = []
    if stock_codes:
        codes = [normalize_stock_code(code, market="HK") for code in stock_codes]
    else:
        for frame in [evidence, aliases, stock_info]:
            if not frame.empty and "stock_code" in frame.columns:
                codes.extend(normalize_stock_code(code, market="HK") for code in frame["stock_code"].astype(str).tolist())
        if not edges.empty:
            for column in ["src_id", "dst_id"]:
                for value in edges.get(column, pd.Series(dtype=str)).astype(str).tolist():
                    if value.startswith("stock:"):
                        codes.append(normalize_stock_code(value.split(":", 1)[1], market="HK"))
    codes = list(dict.fromkeys(code for code in codes if code))
    if not codes:
        return pd.DataFrame(columns=THEME_OPPORTUNITY_SCORE_FIELDS)

    def frame_by_stock(frame):
        if frame is None or frame.empty or "stock_code" not in frame.columns:
            return {}
        copy = frame.copy()
        copy["_norm_stock_code"] = copy["stock_code"].astype(str).map(lambda value: normalize_stock_code(value, market="HK"))
        return {
            code: group.drop(columns=["_norm_stock_code"], errors="ignore").reset_index(drop=True)
            for code, group in copy.groupby("_norm_stock_code", sort=False)
        }

    evidence_by_code = frame_by_stock(evidence)
    alias_by_code = frame_by_stock(aliases)
    stock_info_by_code = frame_by_stock(stock_info)
    feature_source = feature_frame.fillna("") if feature_frame is not None else pd.DataFrame()
    feature_by_code = frame_by_stock(feature_source)
    attention_source = attention_frame.fillna("") if attention_frame is not None else pd.DataFrame()
    attention_by_code = {}
    if not attention_source.empty and "entity_id" in attention_source.columns:
        stock_attention = attention_source.loc[
            attention_source.get("entity_type", pd.Series(dtype=str)).astype(str) == "stock"
        ].copy()
        if not stock_attention.empty:
            stock_attention["_norm_stock_code"] = stock_attention["entity_id"].astype(str).map(lambda value: normalize_stock_code(value, market="HK"))
            attention_by_code = {
                code: group.drop(columns=["_norm_stock_code"], errors="ignore").reset_index(drop=True)
                for code, group in stock_attention.groupby("_norm_stock_code", sort=False)
            }

    nodes_source = node_frame.fillna("") if node_frame is not None else pd.DataFrame()
    nodes_by_id = {}
    if not nodes_source.empty and "node_id" in nodes_source.columns:
        nodes_by_id = {
            _clean(row.get("node_id")): row.to_dict()
            for _, row in nodes_source.iterrows()
            if _clean(row.get("node_id"))
        }
    edge_rows_by_code = {}
    if not edges.empty:
        for _, row in edges.iterrows():
            row_dict = row.to_dict()
            related_codes = set()
            for column in ("src_id", "dst_id"):
                value = _clean(row_dict.get(column))
                if value.startswith("stock:"):
                    related_codes.add(normalize_stock_code(value.split(":", 1)[1], market="HK"))
            for code in related_codes.intersection(codes):
                edge_rows_by_code.setdefault(code, []).append(row_dict)
        seed_node_ids_by_code = {}
        for code, rows_for_code in edge_rows_by_code.items():
            seed_node_ids = {f"stock:{code}"}
            for row_dict in rows_for_code:
                for column in ("src_id", "dst_id"):
                    value = _clean(row_dict.get(column))
                    if value:
                        seed_node_ids.add(value)
            seed_node_ids_by_code[code] = seed_node_ids
        if seed_node_ids_by_code:
            for _, row in edges.iterrows():
                row_dict = row.to_dict()
                src_id = _clean(row_dict.get("src_id"))
                dst_id = _clean(row_dict.get("dst_id"))
                evidence_refs = _clean(row_dict.get("evidence_refs"))
                for code, seed_node_ids in seed_node_ids_by_code.items():
                    if src_id in seed_node_ids or dst_id in seed_node_ids or code in evidence_refs:
                        edge_rows_by_code.setdefault(code, []).append(row_dict)

    iterator = codes
    if show_progress:
        iterator = tqdm(codes, desc=f"rank stocks: {str(theme)[:20]}", unit="stock")
    rows = []
    for code in iterator:
        local_edges = pd.DataFrame(edge_rows_by_code.get(code, []), columns=STOCK_GRAPH_EDGE_FIELDS)
        local_node_ids = {f"stock:{code}"}
        if not local_edges.empty:
            local_edges = local_edges.drop_duplicates().reset_index(drop=True)
            for column in ("src_id", "dst_id"):
                if column in local_edges.columns:
                    local_node_ids.update(_clean(value) for value in local_edges[column].astype(str).tolist() if _clean(value))
        local_nodes = pd.DataFrame(
            [nodes_by_id[node_id] for node_id in local_node_ids if node_id in nodes_by_id],
            columns=STOCK_GRAPH_NODE_FIELDS,
        )
        rows.append(
            score_theme_opportunity(
            code,
            evidence_frame=evidence_by_code.get(code, pd.DataFrame(columns=evidence.columns)),
            alias_frame=alias_by_code.get(code, pd.DataFrame(columns=aliases.columns)),
            node_frame=local_nodes,
            edge_frame=local_edges,
            attention_frame=attention_by_code.get(code, pd.DataFrame(columns=attention_source.columns)),
            stock_info_frame=stock_info_by_code.get(code, pd.DataFrame(columns=stock_info.columns)),
            feature_frame=feature_by_code.get(code, pd.DataFrame(columns=feature_source.columns)),
            theme=theme,
            asof_date=asof_date,
        )
        )
    result = pd.DataFrame(rows, columns=THEME_OPPORTUNITY_SCORE_FIELDS)
    if min_score is not None and not result.empty:
        result = result.loc[result["score"].astype(float) >= float(min_score)]
    if not result.empty:
        result = result.sort_values(["score", "evidence_quality_score", "attention_score"], ascending=False)
    if top_n:
        result = result.head(int(top_n))
    return result.reset_index(drop=True)
