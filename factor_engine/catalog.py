#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Factor catalog and manifest helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from factor_engine.registry import create_factor_set, list_factor_sets


@dataclass(frozen=True)
class FactorManifestEntry:
    """One factor entry in a machine-readable catalog."""

    factor_id: str
    factor_set: str
    family: str
    source: str
    formula: str = ""
    status: str = "implemented"
    exactness: str = "native"
    input_fields: tuple[str, ...] = ()
    lookback: int | None = None
    requires_panel: bool = False
    requires_pit: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["input_fields"] = list(self.input_fields)
        return payload


def _metadata_manifest_entries(factor_set_name: str) -> list[dict[str, Any]]:
    factor_set = create_factor_set(factor_set_name)
    metadata = factor_set.metadata().to_dict()
    extra = metadata.get("extra") or {}
    entries = extra.get("manifest")
    if entries:
        return [dict(entry) for entry in entries]

    feature_names = list(extra.get("feature_names") or [])
    return [
        FactorManifestEntry(
            factor_id=str(name),
            factor_set=factor_set_name,
            family=factor_set_name,
            source=metadata.get("source") or "factor_engine",
            status="implemented",
            exactness=str(extra.get("exactness") or "native"),
            notes="metadata-only entry",
        ).to_dict()
        for name in feature_names
    ]


def list_factor_catalog() -> list[dict[str, Any]]:
    """Return registered factor set summary rows."""

    rows = []
    for name in list_factor_sets():
        factor_set = create_factor_set(name)
        metadata = factor_set.metadata().to_dict()
        extra = metadata.get("extra") or {}
        rows.append(
            {
                "factor_set": name,
                "description": metadata.get("description") or "",
                "version": metadata.get("version") or "",
                "source": metadata.get("source") or "factor_engine",
                "feature_count": int(extra.get("feature_count") or len(extra.get("feature_names") or [])),
                "exactness": extra.get("exactness") or "native",
                "production_default": bool(extra.get("production_default", False)),
            }
        )
    return sorted(rows, key=lambda row: row["factor_set"])


def export_factor_manifest(factor_set: str | None = None) -> dict[str, Any]:
    """Export manifest for one factor set or all registered factor sets."""

    names = [factor_set] if factor_set else list_factor_sets()
    factor_sets = []
    for name in names:
        fs = create_factor_set(name)
        metadata = fs.metadata().to_dict()
        entries = _metadata_manifest_entries(name)
        factor_sets.append(
            {
                "factor_set": name,
                "metadata": metadata,
                "feature_count": len(entries),
                "factors": entries,
            }
        )
    return {"factor_sets": factor_sets}


def show_factor(factor_id: str, factor_set: str | None = None) -> dict[str, Any]:
    """Find a factor by id in the manifest."""

    manifest = export_factor_manifest(factor_set=factor_set)
    for factor_set_payload in manifest["factor_sets"]:
        for entry in factor_set_payload["factors"]:
            if entry.get("factor_id") == factor_id:
                return {
                    "factor_set": factor_set_payload["factor_set"],
                    "metadata": factor_set_payload["metadata"],
                    "factor": entry,
                }
    scope = factor_set or "all factor sets"
    raise KeyError(f"unknown factor_id {factor_id!r} in {scope}")
