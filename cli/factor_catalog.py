#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CLI helpers for factor catalog commands."""

from __future__ import annotations

import json

import pandas as pd

from factor_engine import export_factor_manifest, list_factor_catalog, show_factor


def main_factor_list(export_csv=None):
    rows = list_factor_catalog()
    frame = pd.DataFrame(rows)
    if export_csv:
        frame.to_csv(export_csv, index=False)
    if frame.empty:
        print("未注册因子集")
    else:
        print(frame.to_string(index=False))
    return rows


def main_factor_manifest(factor_set=None, export_json=None, export_csv=None):
    manifest = export_factor_manifest(factor_set=factor_set)
    if export_json:
        with open(export_json, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
    if export_csv:
        rows = []
        for factor_set_payload in manifest["factor_sets"]:
            rows.extend(factor_set_payload["factors"])
        pd.DataFrame(rows).to_csv(export_csv, index=False)
    if not export_json and not export_csv:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main_factor_show(factor_id, factor_set=None):
    payload = show_factor(factor_id=factor_id, factor_set=factor_set)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload
