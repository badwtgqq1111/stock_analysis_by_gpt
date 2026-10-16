"""Shadow selection preserves names, timing and production isolation."""

import numpy as np
import pandas as pd
import pytest

from scripts.run_volatility_shadow import build_histories


def _fixture():
    days = pd.bdate_range("2025-01-01", periods=85)
    rng = np.random.default_rng(5)
    ret = pd.DataFrame(rng.normal(0, .02, (85, 3)), index=days, columns=["A", "B", "C"])
    ledger = pd.DataFrame({"trade_date": [days[70]] * 3 + [days[75]] * 2,
                           "source": ["preselected"] * 3 + ["arm"] * 2,
                           "stock_code": ["A", "B", "C", "A", "C"],
                           "rank": [2, 1, 3, 1, 2], "model_score": [8., 9., 7., 9., 7.],
                           "weight": [0, 0, 0, .5, .5], "name": ["A", "B", "C", "A", "C"]})
    return ledger, ret


def test_shadow_variants_keep_names_and_use_only_known_returns():
    ledger, returns = _fixture()
    histories, audit, risk = build_histories(ledger, returns, .10)
    assert set(histories) == {"cn_ensemble_equal", "cn_ensemble_volcap", "arm_select_equal",
                              "arm_select_histvol", "arm_select_volrisk"}
    for left, right in (("cn_ensemble_equal", "cn_ensemble_volcap"),
                        ("arm_select_equal", "arm_select_histvol"),
                        ("arm_select_equal", "arm_select_volrisk")):
        assert histories[left].stock_code.tolist() == histories[right].stock_code.tolist()
    assert histories["cn_ensemble_equal"].stock_code.tolist() == ["B", "A", "C"]
    assert not risk.empty
    assert (audit.status == "ready").all()
    altered = returns.copy()
    altered.loc[altered.index > ledger.trade_date.max()] = -.90
    replay, _, _ = build_histories(ledger, altered, .10)
    for key in histories:
        pd.testing.assert_frame_equal(histories[key], replay[key])


def test_risk_data_gap_fails_to_cash_without_name_drop():
    ledger, returns = _fixture()
    returns.loc[ledger.trade_date.min(), "A"] = np.nan
    histories, audit, _ = build_histories(ledger, returns, .10)
    cap = histories["cn_ensemble_volcap"]
    assert len(cap) == 3
    assert cap.target_weight.sum() == 0
    assert audit.set_index("variant").loc["cn_ensemble_volcap", "status"] == "cash_insufficient_risk"


def test_registered_shadow_curves_are_independent():
    from backend.services import strategy_registry as registry
    assert registry.validate() == []
    for ident in ("cn_ensemble_volcap", "arm_select_volrisk"):
        spec = registry.spec(ident)
        assert spec["stage"] == "research"
        assert spec["refresh"]["mode"] == "skip"
        assert "output/research/volatility_shadow" in spec["curve"]["shadow"]["nav_csv"]
