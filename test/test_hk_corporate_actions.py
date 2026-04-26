#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""港股企业行为本地测试。"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.providers.hk_corporate_actions import HKCorporateActionsFetcher
from data.ingest.service import MarketDataService


def test_hk_corporate_actions_plan_parsing():
    source_frame = pd.DataFrame(
        [
            {
                "最新公告日期": "2026-04-01",
                "财政年度": "2025",
                "分红方案": "每10股派5元送2股",
                "分配类型": "末期股息",
                "除净日": "2026-04-20",
                "截至过户日": "2026-04-22",
                "发放日": "2026-05-10",
            }
        ]
    )

    parsed = HKCorporateActionsFetcher._normalize_dividend_frame(source_frame)
    assert len(parsed) == 2
    assert set(parsed["action_type"].tolist()) == {"cash_dividend", "bonus_issue"}
    cash_row = parsed.loc[parsed["action_type"] == "cash_dividend"].iloc[0]
    bonus_row = parsed.loc[parsed["action_type"] == "bonus_issue"].iloc[0]
    assert cash_row["cash_dividend"] == 0.5
    assert bonus_row["bonus_share_ratio"] == 0.2


def test_sync_hk_corporate_actions():
    fake_actions = pd.DataFrame(
        [
            {
                "event_date": "2026-04-20",
                "announcement_date": "2026-04-01",
                "ex_date": "2026-04-20",
                "record_date": "2026-04-22",
                "payment_date": "2026-05-10",
                "fiscal_year": "2025",
                "plan_explain": "每10股派5元",
                "distribution_type": "末期股息",
                "action_type": "cash_dividend",
                "cash_dividend": 0.5,
            }
        ]
    )

    def fake_fetch(self, start_date=None, end_date=None, num_records=None):
        self.last_successful_source = "unit_test"
        return fake_actions.copy()

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            with patch("data.ingest.service.HKCorporateActionsFetcher.fetch", new=fake_fetch):
                result = service.sync_hk_corporate_actions("00700", persist_raw=True)

            assert result["rows"] == 1
            assert result["source"] == "unit_test"
            assert result["raw_snapshot_path"] is not None

            loaded = service.get_hk_corporate_actions("00700")
            assert len(loaded) == 1
            assert loaded["stock_code"].iloc[0] == "00700"
            assert loaded["action_type"].iloc[0] == "cash_dividend"
            assert loaded["cash_dividend"].iloc[0] == 0.5
        finally:
            service.close()


if __name__ == "__main__":
    test_hk_corporate_actions_plan_parsing()
    test_sync_hk_corporate_actions()
    print("hk corporate actions tests passed")
