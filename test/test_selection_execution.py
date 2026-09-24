#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""选股执行层约束测试：整手可买 + 每周再平衡节奏。"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService


class _StubWarehouse:
    def __init__(self, bars, info=None):
        self._bars = bars
        self._info = info if info is not None else pd.DataFrame(columns=["stock_code", "name", "market_cap"])

    def read_ohlcv(self, **kwargs):
        frame = self._bars
        codes = kwargs.get("stock_code")
        if codes is not None:
            wanted = {str(codes)} if isinstance(codes, str) else {str(code) for code in codes}
            frame = frame[frame["stock_code"].astype(str).isin(wanted)]
        columns = kwargs.get("columns")
        if columns:
            frame = frame[[column for column in columns if column in frame.columns]]
        return frame.copy()

    def read_stock_info(self, stock_codes=None, market=None):
        return self._info.copy()


def _write_scores(directory: Path, rows):
    directory.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows, columns=["trade_date", "stock_code", "model_score_raw", "model_score"])
    frame.to_csv(directory / "cn_lightgbm_scores.csv", index=False)
    frame.to_csv(directory / "cn_transformer_scores.csv", index=False)


def _bars(codes_with_price, sessions=40):
    dates = pd.bdate_range(end="2026-09-11", periods=sessions)
    frames = []
    for code, price in codes_with_price.items():
        close = pd.Series(np.linspace(price * 0.9, price, sessions), index=dates)
        frames.append(pd.DataFrame({
            "stock_code": code, "trade_date": dates, "high": close * 1.01,
            "low": close * 0.99, "close": close, "volume": 2e6, "amount": 2e8,
        }))
    return pd.concat(frames, ignore_index=True)


def test_affordability_filter_drops_unbuyable_names(tmp_path):
    scores = tmp_path / "scores"
    _write_scores(scores, [
        ("2026-09-11", "EXPENSIVE.SZ", 0.9, 99.0),
        ("2026-09-11", "MID.SZ", 0.8, 90.0),
        ("2026-09-11", "CHEAP.SH", 0.7, 80.0),
    ])
    service = MarketDataService.__new__(MarketDataService)
    service.warehouse = _StubWarehouse(_bars({"EXPENSIVE.SZ": 300.0, "MID.SZ": 60.0, "CHEAP.SH": 20.0}))

    result = service.select_persisted_model_scores(
        model_scores_dir=str(scores), output_dir=str(tmp_path / "out"), top_n=1,
        portfolio_mode="topn", initial_capital=43_137.82,
        affordability={"enabled": True, "equity": 43_137.82, "lot_size": 100, "budget_ratio": 0.15},
        show_progress=False,
    )

    assert result["affordability"]["max_price"] == 64.71
    assert result["affordability"]["universe"] == 3
    assert result["affordability"]["affordable"] == 2
    selected = pd.read_csv(tmp_path / "out" / "cn_ensemble_selected.csv")
    assert "EXPENSIVE.SZ" not in set(selected["stock_code"])
    assert "MID.SZ" in set(selected["stock_code"])


def test_rebalance_stride_carries_the_previous_book_forward(tmp_path):
    scores = tmp_path / "scores"
    _write_scores(scores, [
        ("2026-09-11", "MID.SZ", 0.9, 99.0),
        ("2026-09-11", "CHEAP.SH", 0.8, 90.0),
    ])
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"stock_code": ["OLD.SZ"], "target_weight": [0.5]}).to_csv(
        out / "cn_ensemble_selected.csv", index=False
    )
    (out / "cn_ensemble_rebalance_state.json").write_text(
        json.dumps({"trade_date": "2026-09-10", "rebalance_stride_days": 5}), encoding="utf-8"
    )
    service = MarketDataService.__new__(MarketDataService)
    service.warehouse = _StubWarehouse(_bars({"MID.SZ": 60.0, "CHEAP.SH": 20.0}))

    result = service.select_persisted_model_scores(
        model_scores_dir=str(scores), output_dir=str(out), top_n=2,
        portfolio_mode="topn", initial_capital=43_137.82,
        rebalance_stride_days=5, show_progress=False,
    )

    assert result["status"] == "carried_forward"
    assert result["business_days_since_rebalance"] == 1
    kept = pd.read_csv(out / "cn_ensemble_selected.csv")
    assert kept["stock_code"].tolist() == ["OLD.SZ"]


def test_force_rebalance_bypasses_the_stride(tmp_path):
    scores = tmp_path / "scores"
    _write_scores(scores, [("2026-09-11", "MID.SZ", 0.9, 99.0)])
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cn_ensemble_rebalance_state.json").write_text(
        json.dumps({"trade_date": "2026-09-10", "rebalance_stride_days": 5}), encoding="utf-8"
    )
    service = MarketDataService.__new__(MarketDataService)
    service.warehouse = _StubWarehouse(_bars({"MID.SZ": 60.0}))

    result = service.select_persisted_model_scores(
        model_scores_dir=str(scores), output_dir=str(out), top_n=1,
        portfolio_mode="topn", initial_capital=43_137.82,
        rebalance_stride_days=1, show_progress=False,
    )

    assert result["status"] == "completed"
    assert result["selected_count"] == 1


def test_pk_state_cannot_advance_preselection_candidate_clock(tmp_path):
    scores = tmp_path / "scores"
    _write_scores(scores, [("2026-09-11", "NEW.SZ", 0.9, 99.0)])
    out = tmp_path / "out"
    out.mkdir()
    pd.DataFrame({"stock_code": ["OLD.SZ"], "target_weight": [0.5]}).to_csv(
        out / "cn_ensemble_preselected.csv", index=False,
    )
    # PK may run every day, but its state must not make preselection think
    # the candidate pool was refreshed on that day.
    (out / "cn_ensemble_rebalance_state.json").write_text(json.dumps({
        "trade_date": "2026-09-10", "rebalance_stride_days": 1,
    }), encoding="utf-8")
    service = MarketDataService.__new__(MarketDataService)
    service.warehouse = _StubWarehouse(_bars({"NEW.SZ": 20.0}))
    result = service.select_persisted_model_scores(
        model_scores_dir=str(scores), output_dir=str(out), top_n=1,
        portfolio_mode="topn", rebalance_stride_days=5,
        preselection_only=True, show_progress=False,
    )
    assert result["status"] == "completed"
    assert pd.read_csv(out / "cn_ensemble_preselected.csv")["stock_code"].tolist() == ["NEW.SZ"]
    own_state = out / "cn_ensemble_preselection_rebalance_state.json"
    assert own_state.is_file()
    assert json.loads(own_state.read_text())["trade_date"] == "2026-09-11"
    assert json.loads((out / "cn_ensemble_rebalance_state.json").read_text())["trade_date"] == "2026-09-10"


def test_preselection_stride_uses_only_its_own_state(tmp_path):
    scores = tmp_path / "scores"
    _write_scores(scores, [("2026-09-11", "NEW.SZ", 0.9, 99.0)])
    out = tmp_path / "out"
    out.mkdir()
    pd.DataFrame({"stock_code": ["OLD.SZ"], "target_weight": [0.5]}).to_csv(
        out / "cn_ensemble_preselected.csv", index=False,
    )
    (out / "cn_ensemble_preselection_rebalance_state.json").write_text(json.dumps({
        "trade_date": "2026-09-10", "rebalance_stride_days": 5,
    }), encoding="utf-8")
    service = MarketDataService.__new__(MarketDataService)
    service.warehouse = _StubWarehouse(_bars({"NEW.SZ": 20.0}))
    result = service.select_persisted_model_scores(
        model_scores_dir=str(scores), output_dir=str(out), top_n=1,
        portfolio_mode="topn", rebalance_stride_days=5,
        preselection_only=True, show_progress=False,
    )
    assert result["status"] == "carried_forward"
    assert result["candidate_origin_date"] == "2026-09-10"
    assert result["score_date"] == "2026-09-11"
    assert pd.read_csv(out / "cn_ensemble_preselected.csv")["stock_code"].tolist() == ["OLD.SZ"]


def test_universe_audit_is_surfaced_for_the_pipeline_report(tmp_path):
    """The gate/filter audit must reach the report, not stay a local variable."""
    scores = tmp_path / "scores"
    _write_scores(scores, [("2026-09-11", "MID.SZ", 0.9, 99.0), ("2026-09-11", "BAD.SZ", 0.8, 90.0)])
    service = MarketDataService.__new__(MarketDataService)
    service.warehouse = _StubWarehouse(
        _bars({"MID.SZ": 60.0, "BAD.SZ": 20.0}),
        info=pd.DataFrame({
            "stock_code": ["MID.SZ", "BAD.SZ"],
            "name": ["正常股份", "*ST坏"],
            "market_cap": [1e9, 1e9],
        }),
    )

    result = service.select_persisted_model_scores(
        model_scores_dir=str(scores), output_dir=str(tmp_path / "out"), top_n=2,
        portfolio_mode="topn", initial_capital=43_137.82, show_progress=False,
        startup_gate={"enabled": False},
        universe_filter={"enabled": True, "exclude_st": True, "min_median_amount_20d": 1e7},
    )

    assert result["startup_gate"]["enabled"] is False
    audit = result["universe_filter"]
    assert audit["enabled"] is True
    assert audit["st_dropped"] == ["BAD.SZ"]
    assert audit["pool_before"] == 2 and audit["pool_after"] == 1
