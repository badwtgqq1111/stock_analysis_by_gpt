#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Financial metrics refresh and coverage commands."""

from __future__ import annotations

import pandas as pd

from data.ingest.service import MarketDataService


def main_refresh_financial_metrics(
    stock_codes=None,
    limit=None,
    max_workers=1,
    show_progress=False,
    base_dir="./assets/data",
    data_source="akshare",
):
    service = MarketDataService(base_dir=base_dir, data_source=data_source)
    try:
        summary = service.refresh_hk_financial_metrics(
            stock_codes=stock_codes,
            limit=limit,
            max_workers=max_workers,
            show_progress=show_progress,
        )
        print(f"财务指标/估值快照刷新完成: {summary}")
        return summary
    finally:
        service.close()


def main_financial_coverage(
    stock_codes=None,
    base_dir="./assets/data",
    export_csv=None,
):
    service = MarketDataService(base_dir=base_dir)
    try:
        report = service.financial_coverage_report(stock_codes=stock_codes)
        frame = pd.DataFrame(report["field_coverage"])
        if export_csv:
            frame.to_csv(export_csv, index=False)
        print(frame.to_string(index=False) if not frame.empty else "暂无财务覆盖数据")
        return report
    finally:
        service.close()
