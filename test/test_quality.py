#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""quality scoring tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.quality import enrich_with_quality


def test_enrich_with_quality_reports_component_coverage(capsys):
    scores = {"00001": 61.0, "00002": 50.0, "00003": 42.0}
    details = {
        "00001": {"quality_data_coverage": 1.0},
        "00002": {"quality_data_coverage": 0.5},
        "00003": {"quality_data_coverage": 0.0},
    }

    enriched = enrich_with_quality(
        ["00001", "00002", "00003", "00004"],
        scores,
        quality_details=details,
        show_progress=True,
    )

    captured = capsys.readouterr()

    assert enriched["00004"] == 50.0
    assert "3 只有效数据, 1 只用默认值(50)" in captured.out
    assert "1 只完整" in captured.out
    assert "1 只部分" in captured.out
    assert "1 只低覆盖(<30%)" in captured.out
    assert "1 只未知" in captured.out
