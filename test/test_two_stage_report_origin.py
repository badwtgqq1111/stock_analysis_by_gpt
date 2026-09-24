"""A carried candidate list must not look like a fresh same-day model Top-N."""

import json
import sys

import pandas as pd

from scripts import build_feishu_report, render_two_stage_report


def test_report_displays_candidate_origin_when_trade_date_is_later(tmp_path, monkeypatch):
    pd.DataFrame({
        "stock_code": ["OLD.SZ"], "trade_date": ["2026-09-18"],
        "selection_channel": ["model"], "model_score": [99.0], "rank": [1],
        "target_weight": [1.0],
    }).to_csv(tmp_path / "cn_ensemble_preselected.csv", index=False)
    pd.DataFrame({
        "stock_code": ["OLD.SZ"], "trade_date": ["2026-09-21"],
        "selection_channel": ["model"], "target_weight": [0.35],
        "last_close": [10.0], "one_lot_value": [1000.0],
        "lots_at_target": [1], "volatility_20d": [0.2],
    }).to_csv(tmp_path / "cn_ensemble_selected.csv", index=False)
    monkeypatch.setattr(sys, "argv", ["render_two_stage_report.py", "--trade-date", "2026-09-21",
                                  "--replay-dir", str(tmp_path), "--output-dir", str(tmp_path)])
    assert render_two_stage_report.main() == 0
    text = (tmp_path / "two_stage_report_2026-09-21.md").read_text()
    payload = json.loads((tmp_path / "two_stage_report_2026-09-21.json").read_text())
    assert "预选来源日：**2026-09-18**（沿用候选，非当日重选）" in text
    assert payload["candidate_origin_date"] == "2026-09-18"
    assert payload["preselection_carried_forward"] is True


def test_feishu_notice_displays_candidate_origin(tmp_path, monkeypatch):
    pd.DataFrame({
        "stock_code": ["OLD.SZ"], "trade_date": ["2026-09-18"],
        "selection_channel": ["model"], "model_score": [99.0], "rank": [1],
    }).to_csv(tmp_path / "cn_ensemble_preselected.csv", index=False)
    pd.DataFrame({
        "stock_code": ["OLD.SZ"], "target_weight": [0.35], "lots_at_target": [1],
    }).to_csv(tmp_path / "cn_ensemble_selected.csv", index=False)
    monkeypatch.setattr(build_feishu_report, "_market_data",
                        lambda codes, date: ({}, {}, pd.DataFrame()))
    monkeypatch.setattr(sys, "argv", ["build_feishu_report.py", "--trade-date", "2026-09-21",
                                  "--replay-dir", str(tmp_path), "--output-dir", str(tmp_path)])
    assert build_feishu_report.main() == 0
    text = (tmp_path / "feishu_2026-09-21.md").read_text()
    payload = json.loads((tmp_path / "feishu_2026-09-21.json").read_text())
    assert "预选来源日：2026-09-18（沿用候选，非 2026-09-21 当日重选）" in text
    assert payload["preselection_carried_forward"] is True
