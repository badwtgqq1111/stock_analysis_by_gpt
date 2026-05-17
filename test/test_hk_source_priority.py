#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""港股抓取源优先级回归测试。"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.providers.hk_common import build_source_priority


def test_hk_default_priority_prefers_eastmoney():
    assert build_source_priority() == ["akshare_eastmoney", "tencent", "akshare_sina"]
    assert build_source_priority("akshare") == ["akshare_eastmoney", "tencent", "akshare_sina"]


def test_hk_explicit_source_priority_keeps_requested_source_first():
    assert build_source_priority("sina")[0] == "akshare_sina"
    assert build_source_priority("tencent")[0] == "tencent"
    assert build_source_priority("eastmoney")[0] == "akshare_eastmoney"
