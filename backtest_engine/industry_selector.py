#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Industry-aware candidate selector.

Replaces the current "full-market ranking → concentration penalty" flow with:

1. **Hard filter** — eligibility checks that remove untradeable / broken stocks
   before they ever enter the candidate pool.
2. **Per-industry Top-N** — each industry contributes its strongest candidates
   (capped dynamically by industry size), preventing any single industry
   from dominating.
3. **Soft portfolio constraints** — industry concentration and crowding are
   penalised in the final ranking, not enforced as hard caps.

Usage
-----
::

    from backtest_engine.industry_selector import IndustryCandidateSelector

    selector = IndustryCandidateSelector(
        top_n=10,
        max_per_industry=3,
        min_industry_candidates=1,
        min_market_cap=1e9,
        min_daily_turnover=1e6,
        max_overheat=80,
    )

    candidates = selector.select(ranking_rows, industry_map)
    # candidates is a list of ranking dicts augmented with:
    #   eligibility_pass, eligibility_reasons, industry_rank, industry_score
"""

from __future__ import annotations

from typing import Any

import numpy as np


class IndustryCandidateSelector:
    """Industry-aware hard filter + Top-N candidate selector."""

    def __init__(
        self,
        *,
        top_n: int = 10,
        max_per_industry: int | None = None,
        min_industry_candidates: int = 1,
        min_market_cap: float = 0.0,
        min_daily_turnover: float = 0.0,
        max_overheat: float = 85.0,
        max_drawdown_pct: float = 0.35,
        max_downtrend_penalty: float = 80.0,
        require_liquidity: bool = True,
        require_actionable: bool = True,
        require_non_sideways: bool = True,
        require_fresh_signal: bool = True,
        min_signal_freshness: float = 35.0,
        min_data_coverage: float = 0.5,
        industry_level: str = "l2",
    ):
        self.top_n = int(top_n)
        # dynamic max_per_industry if not set: ceil(top_n / 2)
        self.max_per_industry = (
            int(max_per_industry)
            if max_per_industry is not None
            else max(1, int(np.ceil(top_n / 2)))
        )
        self.min_industry_candidates = int(min_industry_candidates)
        self.min_market_cap = float(min_market_cap)
        self.min_daily_turnover = float(min_daily_turnover)
        self.max_overheat = float(max_overheat)
        self.max_drawdown_pct = float(max_drawdown_pct)
        self.max_downtrend_penalty = float(max_downtrend_penalty)
        self.require_liquidity = bool(require_liquidity)
        self.require_actionable = bool(require_actionable)
        self.require_non_sideways = bool(require_non_sideways)
        self.require_fresh_signal = bool(require_fresh_signal)
        self.min_signal_freshness = float(min_signal_freshness)
        self.min_data_coverage = float(min_data_coverage)
        self.industry_level = str(industry_level)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        ranking_rows: list[dict[str, Any]],
        industry_map: dict[str, str | None] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply hard filters and per-industry Top-N selection.

        Parameters
        ----------
        ranking_rows : list[dict]
            As produced by ``TopNPortfolioBuilder._build_ranking_rows()``.
            Expected keys: ``stock_code``, ``ranking_score``,
            ``current_signal_actionable``, ``liquidity_ok``,
            ``setup_type``, ``signal_freshness_score``, ``data_coverage_score``,
            ``overheat_penalty_score``, ``drawdown_penalty_score``,
            ``downtrend_penalty_score``, ``industry_l1``, ``industry_l2``.
        industry_map : dict or None
            ``{stock_code: industry_label}``.  When None, ``industry_l2`` is
            read from each ranking row.

        Returns
        -------
        list[dict]
            The input rows augmented with:
            - ``eligibility_pass`` (bool)
            - ``eligibility_reasons`` (list[str])
            - ``industry_rank`` (int, within-industry rank, 1-based)
            - ``industry_score`` (float, within-industry composite)
            - ``industry_candidate_count`` (int, how many from this industry)
            - ``selected`` (bool, whether this stock enters the final pool)
        """
        if industry_map is None:
            industry_map = self._build_industry_map_from_rows(ranking_rows)

        # ---- Phase 1: Hard filter ----
        for row in ranking_rows:
            reasons = []
            self._check_eligibility(row, reasons)
            row["eligibility_pass"] = len(reasons) == 0
            row["eligibility_reasons"] = reasons

        eligible = [r for r in ranking_rows if r.get("eligibility_pass")]

        # ---- Phase 2: Within-industry scoring ----
        industries = self._group_by_industry(eligible, industry_map)

        for ind, members in industries.items():
            # Sort by ranking_score within industry
            members.sort(key=lambda r: -float(r.get("ranking_score", 0)))

            # Compute within-industry rank
            for i, row in enumerate(members):
                row["industry_rank"] = i + 1
                row["industry_score"] = self._compute_industry_score(row, i, len(members))

            row["industry_candidate_count"] = len(members)

        # ---- Phase 3: Per-industry Top-N candidate selection ----
        for ind, members in industries.items():
            # Dynamic cap: min(max_per_industry, ceil(industry_size * 15%))
            cap = min(
                self.max_per_industry,
                max(self.min_industry_candidates, int(np.ceil(len(members) * 0.15))),
            )
            for i, row in enumerate(members):
                row["industry_cap"] = cap
                row["selected"] = i < cap

        # ---- Phase 4: Cross-industry final ranking ----
        selected = [r for r in eligible if r.get("selected")]
        selected.sort(key=lambda r: -float(r.get("ranking_score", 0)))

        # Apply industry concentration penalty to final ranking
        industry_pick_counts: dict[str, int] = {}
        for row in selected:
            ind = industry_map.get(row["stock_code"]) or row.get("industry_l2", "")
            count = industry_pick_counts.get(ind, 0)
            # Progressive penalty: 0 for 1st, -8 for 2nd, -18 for 3rd, -35 for 4th+
            penalty = 0
            if count >= 4:
                penalty = 35
            elif count >= 3:
                penalty = 18
            elif count >= 2:
                penalty = 8
            row["industry_concentration_penalty"] = penalty
            row["ranking_score_adjusted"] = float(row.get("ranking_score", 0)) - penalty
            industry_pick_counts[ind] = count + 1

        # Re-sort after penalty and take top_n
        selected.sort(key=lambda r: -float(r.get("ranking_score_adjusted", 0)))
        final_pool = selected[: self.top_n]

        # Mark final selection
        for row in ranking_rows:
            row["selected"] = row in final_pool

        return ranking_rows

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_industry_map_from_rows(
        self, rows: list[dict[str, Any]]
    ) -> dict[str, str | None]:
        level = self.industry_level
        return {
            r["stock_code"]: (
                r.get(f"industry_{level}") or r.get("industry_l1", "")
            )
            for r in rows
        }

    def _check_eligibility(self, row: dict[str, Any], reasons: list[str]) -> None:
        code = row.get("stock_code", "?")

        if self.require_actionable:
            actionable = row.get("current_signal_actionable")
            if actionable is False:
                reasons.append("signal_not_actionable")

        if self.require_liquidity:
            liq = row.get("liquidity_ok")
            if liq is False:
                reasons.append("low_liquidity")

        if self.require_non_sideways:
            setup = row.get("setup_type", "")
            if str(setup).strip().lower() == "sideways":
                reasons.append("sideways_setup")

        if self.require_fresh_signal:
            freshness = float(row.get("signal_freshness_score", 100) or 100)
            if freshness < self.min_signal_freshness:
                reasons.append(f"stale_signal(freshness={freshness:.0f})")

        overheat = float(row.get("overheat_penalty_score", 0) or 0)
        if overheat >= self.max_overheat:
            reasons.append(f"overheated(score={overheat:.0f})")

        drawdown = float(row.get("drawdown_penalty_score", 0) or 0)
        if drawdown >= 100 * self.max_drawdown_pct:
            reasons.append(f"excessive_drawdown(score={drawdown:.0f})")

        downtrend = float(row.get("downtrend_penalty_score", 0) or 0)
        if downtrend >= self.max_downtrend_penalty:
            reasons.append(f"downtrend(score={downtrend:.0f})")

        data_cov = float(row.get("data_coverage_score", 100) or 100) / 100.0
        if data_cov < self.min_data_coverage:
            reasons.append(f"low_data_coverage({data_cov:.0%})")

        # PE sanity check
        pe = row.get("pe_ratio")
        if pe is not None and not (isinstance(pe, float) and np.isnan(pe)):
            pe_val = float(pe)
            if pe_val <= 0:
                reasons.append(f"negative_pe({pe_val:.1f})")
            elif pe_val > 500:
                reasons.append(f"extreme_pe({pe_val:.0f})")

        # PB sanity check
        pb = row.get("pb_ratio")
        if pb is not None and not (isinstance(pb, float) and np.isnan(pb)):
            pb_val = float(pb)
            if pb_val <= 0:
                reasons.append(f"negative_pb({pb_val:.1f})")
            elif pb_val > 50:
                reasons.append(f"extreme_pb({pb_val:.0f})")

    @staticmethod
    def _group_by_industry(
        rows: list[dict[str, Any]],
        industry_map: dict[str, str | None],
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            code = row["stock_code"]
            ind = industry_map.get(code) or row.get("industry_l2") or row.get("industry_l1", "")
            if not ind:
                ind = "__unclassified__"
            groups.setdefault(ind, []).append(row)
        return groups

    @staticmethod
    def _compute_industry_score(
        row: dict[str, Any],
        rank_index: int,
        industry_size: int,
    ) -> float:
        """Composite within-industry score (0–100).

        Components:
        - ranking_score (model): 35%
        - latest_risk_score: -20% (risk penalty)
        - win_rate: 15%
        - quality_score: 10%
        - value_score: 10%
        - recent_drawdown: -10%
        """
        ranking = float(row.get("ranking_score", 50) or 50) / 100.0
        risk = float(row.get("latest_risk_score", 100) or 100) / 100.0
        win_rate = float(row.get("win_rate", 50) or 50) / 100.0
        quality = float(row.get("quality_score", 50) or 50) / 100.0
        value = float(row.get("value_score", 50) or 50) / 100.0
        drawdown = abs(float(row.get("recent_drawdown", 0) or 0))

        score = (
            ranking * 35.0
            + risk * 20.0
            + win_rate * 15.0
            + quality * 10.0
            + value * 10.0
            - drawdown * 10.0
        )
        return float(np.clip(score, 0.0, 100.0))


# ---------------------------------------------------------------------------
# Portfolio-level industry soft constraints
# ---------------------------------------------------------------------------

def compute_industry_hhi(
    selected_codes: list[str],
    industry_map: dict[str, str | None],
    weights: list[float] | None = None,
) -> float:
    """Compute Herfindahl-Hirschman Index for industry concentration.

    HHI = Σ(w_i)² where w_i is the weight of each industry in the portfolio.
    0 = fully diversified, 10000 = single industry.

    Parameters
    ----------
    selected_codes : list[str]
    industry_map : dict
    weights : list[float] or None
        Portfolio weights.  When None, equal-weight is assumed.

    Returns
    -------
    float  HHI (0–10000).
    """
    n = len(selected_codes)
    if n == 0:
        return 0.0

    if weights is None:
        weights = [1.0 / n] * n

    ind_weights: dict[str, float] = {}
    for code, w in zip(selected_codes, weights):
        ind = industry_map.get(code) or "unknown"
        ind_weights[ind] = ind_weights.get(ind, 0.0) + w

    hhi = sum(v ** 2 for v in ind_weights.values()) * 10000.0
    return float(hhi)


def compute_industry_weight_table(
    selected_codes: list[str],
    industry_map: dict[str, str | None],
    weights: list[float],
) -> list[dict[str, Any]]:
    """Build an industry weight breakdown table.

    Returns
    -------
    list[dict] with keys: industry, stock_count, weight_pct, stocks.
    """
    ind_data: dict[str, dict[str, Any]] = {}
    for code, w in zip(selected_codes, weights):
        ind = industry_map.get(code) or "unknown"
        if ind not in ind_data:
            ind_data[ind] = {"industry": ind, "stock_count": 0, "weight_pct": 0.0, "stocks": []}
        ind_data[ind]["stock_count"] += 1
        ind_data[ind]["weight_pct"] += w * 100.0
        ind_data[ind]["stocks"].append(code)

    result = sorted(ind_data.values(), key=lambda x: -x["weight_pct"])
    for item in result:
        item["weight_pct"] = round(item["weight_pct"], 2)
    return result
