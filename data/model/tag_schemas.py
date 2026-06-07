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

ENTITY_ALIAS_FIELDS = [
    "stock_code",
    "market",
    "alias",
    "alias_type",
    "source",
    "confidence",
    "updated_at",
]

STOCK_PROFILE_FIELDS = [
    "stock_code",
    "market",
    "profile_json",
    "summary",
    "strengths",
    "risks",
    "open_questions",
    "evidence_count",
    "confidence",
    "updated_at",
]

STOCK_DEEP_TAG_FIELDS = [
    "stock_code",
    "market",
    "tag",
    "tag_type",
    "confidence",
    "evidence_count",
    "source_count",
    "freshness_days",
    "attention_velocity_7d",
    "is_primary",
    "evidence_refs",
    "source",
    "updated_at",
]

STOCK_GRAPH_NODE_FIELDS = [
    "node_id",
    "node_type",
    "name",
    "canonical_name",
    "properties_json",
    "source",
    "confidence",
    "updated_at",
]

STOCK_GRAPH_EDGE_FIELDS = [
    "src_type",
    "src_id",
    "edge_type",
    "dst_type",
    "dst_id",
    "confidence",
    "evidence_refs",
    "source",
    "updated_at",
]

ATTENTION_SIGNAL_FIELDS = [
    "entity_type",
    "entity_id",
    "source",
    "metric",
    "value",
    "window",
    "velocity",
    "quality_score",
    "asof_date",
]

THEME_OPPORTUNITY_SCORE_FIELDS = [
    "stock_code",
    "market",
    "theme",
    "score",
    "technology_score",
    "commercialization_score",
    "value_chain_score",
    "bottleneck_score",
    "catalyst_score",
    "attention_score",
    "evidence_quality_score",
    "liquidity_score",
    "technical_trend_score",
    "risk_penalty",
    "crowding_penalty",
    "verdict",
    "rank_reason",
    "bull_case",
    "bear_case",
    "key_evidence_refs",
    "component_scores_json",
    "asof_date",
    "updated_at",
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
    "product",
    "technology",
    "bottleneck",
    "catalyst",
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


def normalize_entity_alias_entry(entry):
    payload = dict(entry or {})
    stock_code = normalize_stock_code(payload.get("stock_code"), market=payload.get("market") or "HK")
    alias = _clean_text(payload.get("alias"))
    if not alias:
        raise ValueError("alias is required")
    return {
        "stock_code": stock_code,
        "market": (_clean_text(payload.get("market")) or "HK").upper(),
        "alias": alias,
        "alias_type": _clean_text(payload.get("alias_type")) or "alias",
        "source": _clean_text(payload.get("source")) or "unknown",
        "confidence": normalize_confidence(payload.get("confidence"), default=0.7),
        "updated_at": _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat(),
    }


def normalize_stock_profile_entry(entry):
    payload = dict(entry or {})
    stock_code = normalize_stock_code(payload.get("stock_code"), market=payload.get("market") or "HK")
    return {
        "stock_code": stock_code,
        "market": (_clean_text(payload.get("market")) or "HK").upper(),
        "profile_json": _clean_text(payload.get("profile_json")),
        "summary": _clean_text(payload.get("summary")),
        "strengths": join_semicolon_tags(payload.get("strengths")),
        "risks": join_semicolon_tags(payload.get("risks")),
        "open_questions": join_semicolon_tags(payload.get("open_questions")),
        "evidence_count": int(float(payload.get("evidence_count") or 0)),
        "confidence": normalize_confidence(payload.get("confidence"), default=0.5),
        "updated_at": _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat(),
    }


def normalize_stock_deep_tag_entry(entry):
    payload = dict(entry or {})
    stock_code = normalize_stock_code(payload.get("stock_code"), market=payload.get("market") or "HK")
    tag = _clean_text(payload.get("tag"))
    if not tag:
        raise ValueError("tag is required")
    return {
        "stock_code": stock_code,
        "market": (_clean_text(payload.get("market")) or "HK").upper(),
        "tag": tag,
        "tag_type": _clean_text(payload.get("tag_type")) or "theme",
        "confidence": normalize_confidence(payload.get("confidence"), default=0.5),
        "evidence_count": int(float(payload.get("evidence_count") or 0)),
        "source_count": int(float(payload.get("source_count") or 0)),
        "freshness_days": float(payload.get("freshness_days") or 0),
        "attention_velocity_7d": float(payload.get("attention_velocity_7d") or 0),
        "is_primary": normalize_bool(payload.get("is_primary"), default=False),
        "evidence_refs": join_semicolon_tags(payload.get("evidence_refs")),
        "source": _clean_text(payload.get("source")) or "unknown",
        "updated_at": _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat(),
    }


def normalize_stock_graph_node_entry(entry):
    payload = dict(entry or {})
    node_id = _clean_text(payload.get("node_id"))
    if not node_id:
        raise ValueError("node_id is required")
    node_type = _clean_text(payload.get("node_type")) or "entity"
    name = _clean_text(payload.get("name")) or node_id
    return {
        "node_id": node_id,
        "node_type": node_type,
        "name": name,
        "canonical_name": _clean_text(payload.get("canonical_name")) or name,
        "properties_json": _clean_text(payload.get("properties_json")),
        "source": _clean_text(payload.get("source")) or "unknown",
        "confidence": normalize_confidence(payload.get("confidence"), default=0.5),
        "updated_at": _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat(),
    }


def normalize_stock_graph_edge_entry(entry):
    payload = dict(entry or {})
    src_type = _clean_text(payload.get("src_type"))
    src_id = _clean_text(payload.get("src_id"))
    edge_type = _clean_text(payload.get("edge_type"))
    dst_type = _clean_text(payload.get("dst_type"))
    dst_id = _clean_text(payload.get("dst_id"))
    if not all([src_type, src_id, edge_type, dst_type, dst_id]):
        raise ValueError("src_type/src_id/edge_type/dst_type/dst_id are required")
    return {
        "src_type": src_type,
        "src_id": src_id,
        "edge_type": edge_type,
        "dst_type": dst_type,
        "dst_id": dst_id,
        "confidence": normalize_confidence(payload.get("confidence"), default=0.5),
        "evidence_refs": join_semicolon_tags(payload.get("evidence_refs")),
        "source": _clean_text(payload.get("source")) or "unknown",
        "updated_at": _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat(),
    }


def normalize_attention_signal_entry(entry):
    payload = dict(entry or {})
    entity_type = _clean_text(payload.get("entity_type"))
    entity_id = _clean_text(payload.get("entity_id"))
    source = _clean_text(payload.get("source"))
    metric = _clean_text(payload.get("metric"))
    if not all([entity_type, entity_id, source, metric]):
        raise ValueError("entity_type/entity_id/source/metric are required")
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source": source,
        "metric": metric,
        "value": float(payload.get("value") or 0),
        "window": _clean_text(payload.get("window")) or "point",
        "velocity": float(payload.get("velocity") or 0),
        "quality_score": normalize_confidence(payload.get("quality_score"), default=0.5),
        "asof_date": _clean_text(payload.get("asof_date")) or datetime.utcnow().date().isoformat(),
    }


def normalize_theme_opportunity_score_entry(entry):
    payload = dict(entry or {})
    stock_code = normalize_stock_code(payload.get("stock_code"), market=payload.get("market") or "HK")
    updated_at = _clean_text(payload.get("updated_at")) or datetime.utcnow().isoformat()
    asof_date = _clean_text(payload.get("asof_date")) or updated_at[:10]

    def as_float(key, default=0.0):
        try:
            return float(payload.get(key) or default)
        except (TypeError, ValueError):
            return float(default)

    return {
        "stock_code": stock_code,
        "market": (_clean_text(payload.get("market")) or "HK").upper(),
        "theme": _clean_text(payload.get("theme")) or "ALL",
        "score": as_float("score"),
        "technology_score": as_float("technology_score"),
        "commercialization_score": as_float("commercialization_score"),
        "value_chain_score": as_float("value_chain_score"),
        "bottleneck_score": as_float("bottleneck_score"),
        "catalyst_score": as_float("catalyst_score"),
        "attention_score": as_float("attention_score"),
        "evidence_quality_score": as_float("evidence_quality_score"),
        "liquidity_score": as_float("liquidity_score"),
        "technical_trend_score": as_float("technical_trend_score"),
        "risk_penalty": as_float("risk_penalty"),
        "crowding_penalty": as_float("crowding_penalty"),
        "verdict": _clean_text(payload.get("verdict")) or "watchlist",
        "rank_reason": _clean_text(payload.get("rank_reason")),
        "bull_case": join_semicolon_tags(payload.get("bull_case")),
        "bear_case": join_semicolon_tags(payload.get("bear_case")),
        "key_evidence_refs": join_semicolon_tags(payload.get("key_evidence_refs")),
        "component_scores_json": _clean_text(payload.get("component_scores_json")),
        "asof_date": asof_date,
        "updated_at": updated_at,
    }
