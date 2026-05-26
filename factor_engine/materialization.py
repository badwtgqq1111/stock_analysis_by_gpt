#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Helpers for stable factor materialization identity."""

from __future__ import annotations

import hashlib
import json


def _canonicalize_config(value):
    if isinstance(value, dict):
        return {str(key): _canonicalize_config(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize_config(item) for item in value]
    return value


def canonicalize_factor_config(config):
    """Return a JSON-stable copy of factor config."""
    return _canonicalize_config(config or {})


def build_feature_materialization_metadata(factor_set, metadata=None, config=None):
    """Build a stable materialization identity for feature persistence."""
    normalized_factor_set = str(factor_set or "default").strip() or "default"
    factor_metadata = dict(metadata or {})
    canonical_config = canonicalize_factor_config(config)
    payload = {
        "factor_set": normalized_factor_set,
        "version": str(factor_metadata.get("version") or "0.1.0"),
        "config": canonical_config,
    }
    feature_config_hash = hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "feature_set": normalized_factor_set,
        "feature_version": payload["version"],
        "feature_config": canonical_config,
        "feature_config_hash": feature_config_hash,
    }
