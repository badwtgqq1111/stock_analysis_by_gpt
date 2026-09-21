#!/usr/bin/env python3
"""Tests for the cost/breadth feature profiles."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.ml.feature_profiles import (
    CORE_FEATURES,
    FEATURE_PROFILES,
    resolve_feature_profile,
)

AVAILABLE = [
    "pv_return_20d_clean", "pv_return_20d_is_missing", "pv_donchian_pos_20_clean",
    "flow_second_wave_flag_clean", "flow_second_wave_flag_is_missing",
    "moneyflow_net_z_5d_clean", "valuation_pb_clean", "valuation_market_cap_log_clean",
    "academic_smb_size_proxy_clean", "financial_net_margin_clean", "gross_margin_ind_pct_clean",
    "RPS_20_clean", "turnover_rate_clean", "vol_atr_pct_14_clean", "not_in_any_profile_clean",
]


def test_full_profile_keeps_every_available_feature():
    # The resolver works on base names: a value column and its mask collapse to
    # one entry, because the reader expands the base name into both columns.
    selected, audit = resolve_feature_profile(AVAILABLE, profile="full")
    assert audit["selected_feature_count"] == audit["available_feature_count"] == 13
    assert "not_in_any_profile" in selected
    assert audit["profile"] == "full"


def test_compact_profile_drops_statement_and_valuation_families():
    selected, audit = resolve_feature_profile(AVAILABLE, profile="compact")
    assert "pv_return_20d" in selected
    assert "flow_second_wave_flag" in selected
    assert "moneyflow_net_z_5d" in selected
    assert "financial_net_margin" not in selected
    assert "academic_smb_size_proxy" not in selected
    assert "valuation_pb" not in selected
    assert audit["profile"] == "compact"


def test_core_profile_is_an_explicit_shortlist_and_reports_gaps():
    selected, audit = resolve_feature_profile(AVAILABLE, profile="core")
    assert "pv_donchian_pos_20" in selected
    assert "RPS_20" in selected
    assert "valuation_market_cap_log" in selected
    assert "academic_smb_size_proxy" not in selected
    # Names listed in CORE_FEATURES but absent from the panel are reported, not hidden.
    assert set(audit["core_features_missing"]) == set(CORE_FEATURES) - {
        "pv_return_20d", "pv_donchian_pos_20", "flow_second_wave_flag", "moneyflow_net_z_5d",
        "valuation_market_cap_log", "RPS_20", "turnover_rate", "vol_atr_pct_14",
    }


def test_include_and_exclude_patterns_extend_a_profile():
    selected, _ = resolve_feature_profile(
        AVAILABLE, profile="core",
        include_patterns=["academic_*"], exclude_patterns=["pv_donchian_*"],
    )
    assert "academic_smb_size_proxy" in selected
    assert "pv_donchian_pos_20" not in selected


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="unknown feature profile"):
        resolve_feature_profile(AVAILABLE, profile="tiny")
    assert set(FEATURE_PROFILES) == {"full", "compact", "core"}


def test_named_families_can_be_switched_off_wholesale():
    from factor_engine.ml.feature_profiles import FEATURE_FAMILIES, family_patterns

    available = [
        "moneyflow_net_z_5d_clean", "moneyflow_dc_net_z_5d_clean", "flow_second_wave_flag_clean",
        "flow_second_wave_source_count_clean", "pv_volume_ratio_20d_clean", "tr_dryup_days_10_clean",
        "turnover_rate_clean", "valuation_market_cap_log_clean",
    ]
    assert set(FEATURE_FAMILIES) >= {"moneyflow", "valuation", "fundamental", "academic", "price_volume"}
    assert "moneyflow*" in family_patterns(["moneyflow"])

    with_moneyflow, _ = resolve_feature_profile(available, profile="full")
    without, audit = resolve_feature_profile(available, profile="full", exclude_families=["moneyflow"])
    assert "moneyflow_net_z_5d" in with_moneyflow and "flow_second_wave_flag" in with_moneyflow
    assert "moneyflow_net_z_5d" not in without
    assert "moneyflow_dc_net_z_5d" not in without
    assert "flow_second_wave_flag" not in without and "flow_second_wave_source_count" not in without
    assert "pv_volume_ratio_20d" in without and "turnover_rate" in without
    assert audit["exclude_families"] == ["moneyflow"]
    assert sorted(set(with_moneyflow) - set(without)) == [
        "flow_second_wave_flag", "flow_second_wave_source_count",
        "moneyflow_dc_net_z_5d", "moneyflow_net_z_5d",
    ]


def test_unknown_feature_family_is_rejected():
    with pytest.raises(ValueError, match="unknown feature families"):
        resolve_feature_profile(["pv_return_20d_clean"], profile="full", exclude_families=["funding"])
