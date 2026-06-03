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
