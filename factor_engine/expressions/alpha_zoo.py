#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Composite alpha zoo factor sets."""

from __future__ import annotations

import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.registry import create_factor_set, register_factor_set


ALPHA_ZOO_HK_COMPONENTS = (
    "alpha158_hk",
    "alpha101",
    "academic_hk",
    "valuation_hk",
    "financial_quality_hk",
    "financial_cross_section_hk",
)


@register_factor_set("alpha_zoo_hk")
class AlphaZooHKFactorSet(BaseFactorSet):
    """Production candidate bundle across technical, style and financial factors."""

    name = "alpha_zoo_hk"
    description = "HK alpha zoo bundle: alpha158_hk + alpha101 + academic + valuation + financial"
    version = "0.1.0"

    def transform(self, frame, context=None):
        frames = []
        for component in ALPHA_ZOO_HK_COMPONENTS:
            feature_frame = create_factor_set(component, config=self.config.get(component)).transform(frame, context=context)
            if feature_frame is not None and not feature_frame.empty:
                frames.append(feature_frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    def metadata(self):
        feature_names = []
        assumptions = []
        manifest = []
        component_payloads = []
        for component in ALPHA_ZOO_HK_COMPONENTS:
            metadata = create_factor_set(component, config=self.config.get(component)).metadata().to_dict()
            extra = metadata.get("extra") or {}
            component_names = list(extra.get("feature_names") or [])
            feature_names.extend(component_names)
            assumptions.extend(list(metadata.get("assumptions") or []))
            manifest.extend(list(extra.get("manifest") or []))
            component_payloads.append(
                {
                    "factor_set": component,
                    "feature_count": len(component_names),
                    "version": metadata.get("version"),
                    "exactness": extra.get("exactness"),
                }
            )
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=tuple(dict.fromkeys(assumptions)),
            extra={
                "feature_count": len(feature_names),
                "feature_names": feature_names,
                "components": component_payloads,
                "production_default": True,
                "exactness": "mixed",
                "manifest": manifest,
            },
        )
