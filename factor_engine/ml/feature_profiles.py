"""Auditable feature profiles so a training run can trade breadth for cost.

The CN clean panel stores one value plus one missing-mask per factor, so a run
over ~680 Alpha factors is ~1,366 model columns.  Extending the evaluation
window multiplies that cost by the number of folds, which is the practical
reason to keep a smaller, thesis-driven feature set available.

Profiles are *not* a signal claim: they are a cost/breadth knob that must be
validated with the same out-of-sample comparison as any other change.  Three
profiles exist:

``full``
    every factor in the panel (historical behaviour; default).
``compact``
    the price/volume/liquidity/money-flow families with the academic,
    financial-statement and valuation factors dropped.
``core``
    the explicit shortlist matching the P1.17 thesis: half-year-low position,
    short-horizon momentum/pullback, volume contraction/expansion, channel
    structure, and money flow.  Roughly 60 factors.

Every resolution reports the matched names, the dropped names and any pattern
that matched nothing, so a manifest can show exactly what a run consumed.
"""

from __future__ import annotations

import fnmatch
import re

# Families kept by ``compact``: observable price/volume microstructure plus the
# money-flow block.  These are the inputs the P1.17 thesis actually argues for.
COMPACT_PATTERNS = (
    "pv_*",
    "tr_*",
    "vr_*",
    "vol_*",
    "liquidity_*",
    "turnover*",
    "price_position_*",
    "momentum_*",
    "RPS_*",
    "sector_rps*",
    "moneyflow*",
    "flow_second_wave_*",
    "calendar_*",
    "valuation_market_cap_log",
)

# Dropped even when a kept pattern matches: statement/valuation blocks are
# sparse, slow-moving and orthogonal to the entry-timing question.
COMPACT_DROP_PATTERNS = ("financial_*", "gross_margin*", "*_ind_pct", "valuation_*")

# ``core`` is an explicit list because it is a documented research decision, not
# a glob: the half-year-low position family, the pullback/momentum family, the
# volume contraction/expansion family, the channel structure block, and money
# flow.  Membership is reviewed whenever the thesis changes.
CORE_FEATURES = (
    # --- half-year-low position / "not extended" ---------------------------------
    "price_position_52w_high",
    "pv_close_to_ma20",
    "RPS_20",
    "RPS_60",
    # --- short-horizon momentum and pullback -------------------------------------
    "pv_return_1d",
    "pv_return_5d",
    "pv_return_20d",
    "sector_rps_reversal_20d",
    "tr_rel_ma20",
    # --- volatility / contraction ------------------------------------------------
    "vol_atr_pct_14",
    "tr_volatility_20",
    "vol_downside_20",
    "vol_term_5_60",
    "vol_max_drawdown_60",
    # --- volume and liquidity ----------------------------------------------------
    "pv_volume_ratio_20d",
    "pv_intraday_range",
    "pv_log_volume",
    "pv_log_amount",
    "turnover_rate",
    "liquidity_amount_ma20",
    "liquidity_amount_ma60",
    "liquidity_turnover_rate",
    "liquidity_amihud_illiq_20d",
    # --- channel structure (Donchian breakout / pullback) ------------------------
    "pv_donchian_pos_20",
    "pv_donchian_width_20",
    "pv_donchian_dist_upper_20",
    "pv_donchian_break_20",
    "pv_donchian_break_count_20",
    "pv_donchian_since_break_20",
    "pv_donchian_break_volume_20",
    "pv_donchian_pierce_20",
    "pv_donchian_pierce_volume_20",
    # --- money flow (three sources, levels and z-scores) -------------------------
    "moneyflow_net_amount_pct",
    "moneyflow_net_z_5d",
    "moneyflow_net_z_10d",
    "moneyflow_net_z_20d",
    "moneyflow_positive_ratio_5d",
    "moneyflow_positive_ratio_10d",
    "moneyflow_dc_net_amount_pct",
    "moneyflow_dc_net_z_5d",
    "moneyflow_dc_net_z_10d",
    "moneyflow_dc_positive_ratio_10d",
    "moneyflow_ths_net_amount_pct",
    "moneyflow_ths_net_z_5d",
    "moneyflow_ths_net_z_10d",
    # --- second-wave confirmation block (P1.17 W2/W3) ----------------------------
    "flow_second_wave_flag",
    "flow_second_wave_source_count",
    "flow_second_wave_max_z5",
    "flow_second_wave_seed_age",
    "flow_second_wave_pullback_pct",
    "flow_second_wave_pullback_volume_ratio",
    "flow_second_wave_price_recovery_1d",
    # --- size and calendar -------------------------------------------------------
    "valuation_market_cap_log",
    "calendar_weekday_sin",
    "calendar_weekday_cos",
    "calendar_month_start",
    "calendar_month_end",
)

FEATURE_PROFILES = ("full", "compact", "core")


def resolve_feature_profile(
    available_columns,
    *,
    profile: str = "full",
    include_patterns=(),
    exclude_patterns=(),
) -> tuple[list[str], dict]:
    """Return the base feature names a run should consume, plus an audit record.

    ``available_columns`` may hold panel column names (``*_clean`` /
    ``*_is_missing``) or bare base names; both are accepted.
    """
    base_names = sorted(_base_names(available_columns))
    requested = str(profile or "full").strip().lower() or "full"
    if requested not in FEATURE_PROFILES:
        raise ValueError(f"unknown feature profile {requested!r}; expected one of {','.join(FEATURE_PROFILES)}")

    if requested == "full":
        selected = list(base_names)
    elif requested == "compact":
        selected = [name for name in base_names if any(fnmatch.fnmatch(name, pattern) for pattern in COMPACT_PATTERNS)]
        selected = [
            name for name in selected
            if not any(fnmatch.fnmatch(name, pattern) for pattern in COMPACT_DROP_PATTERNS)
        ]
    else:  # core
        available = set(base_names)
        selected = [name for name in CORE_FEATURES if name in available]

    for pattern in include_patterns or ():
        for name in base_names:
            if fnmatch.fnmatch(name, str(pattern)) and name not in selected:
                selected.append(name)
    for pattern in exclude_patterns or ():
        selected = [name for name in selected if not fnmatch.fnmatch(name, str(pattern))]

    selected = list(dict.fromkeys(selected))
    audit = {
        "profile": requested,
        "available_feature_count": len(base_names),
        "selected_feature_count": len(selected),
        "selected_features": selected,
        "dropped_feature_count": len(base_names) - len(selected),
        "include_patterns": [str(value) for value in (include_patterns or ())],
        "exclude_patterns": [str(value) for value in (exclude_patterns or ())],
    }
    if requested == "core":
        missing = [name for name in CORE_FEATURES if name not in set(base_names)]
        audit["core_features_missing"] = missing
    return selected, audit


def _base_names(columns) -> set[str]:
    names: set[str] = set()
    for column in columns or ():
        name = str(column)
        if name.endswith("_is_missing"):
            names.add(name[: -len("_is_missing")])
        elif name.endswith("_clean"):
            names.add(name[: -len("_clean")])
        elif not re.search(r"_(is_missing|clean)$", name):
            names.add(name)
    return names


__all__ = [
    "COMPACT_DROP_PATTERNS",
    "COMPACT_PATTERNS",
    "CORE_FEATURES",
    "FEATURE_PROFILES",
    "resolve_feature_profile",
]
