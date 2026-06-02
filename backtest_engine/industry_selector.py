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
        max_drawdown_pct: float = 0.30,
        max_downtrend_penalty: float = 50.0,
        require_liquidity: bool = True,
        require_actionable: bool = True,
        require_non_sideways: bool = True,
        require_fresh_signal: bool = True,
        min_signal_freshness: float = 35.0,
        min_data_coverage: float = 0.5,
        min_quality_coverage: float = 0.3,
        min_signal_tier: str = "medium",
        max_pe_ratio: float = 300.0,
        industry_level: str = "l2",
        mode: str = "core_overlay",
        overlay_strength: float = 0.0,
        hot_industry_weight_multiplier: float = 1.3,
        max_industry_weight: float = 0.35,
        timing_oos_win_rate: float | None = None,
        timing_oos_ir: float | None = None,
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
        self.min_quality_coverage = float(min_quality_coverage)
        self.min_signal_tier = str(min_signal_tier).strip().lower()
        self.max_pe_ratio = float(max_pe_ratio)
        self.industry_level = str(industry_level)
        self.mode = str(mode or "core_overlay").strip().lower()
        if self.mode not in {"core", "core_overlay", "timing_only"}:
            self.mode = "core_overlay"
        self.overlay_strength = float(np.clip(overlay_strength, 0.0, 1.0))
        self.hot_industry_weight_multiplier = float(max(hot_industry_weight_multiplier, 1.0))
        self.max_industry_weight = float(np.clip(max_industry_weight, 0.05, 1.0))
        self.timing_oos_win_rate = timing_oos_win_rate
        self.timing_oos_ir = timing_oos_ir

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
            reasons = list(row.get("eligibility_reasons") or [])
            self._check_eligibility(row, reasons)
            reasons = self._normalize_reasons(reasons)
            row["eligibility_pass"] = len(reasons) == 0
            row["eligibility_reasons"] = reasons

        eligible = [r for r in ranking_rows if r.get("eligibility_pass")]

        # ---- Phase 2: Within-industry scoring ----
        industries = self._group_by_industry(eligible, industry_map)
        self._assign_industry_opportunity_ranks(eligible)
        self._assign_industry_scores(eligible)
        if self.timing_oos_win_rate is None or self.timing_oos_ir is None:
            gate = compute_industry_timing_oos_gate(eligible)
            if self.timing_oos_win_rate is None:
                self.timing_oos_win_rate = gate.get("industry_timing_oos_win_rate")
            if self.timing_oos_ir is None:
                self.timing_oos_ir = gate.get("industry_timing_oos_ir")
            for row in eligible:
                row["industry_timing_oos_win_rate"] = self.timing_oos_win_rate
                row["industry_timing_oos_ir"] = self.timing_oos_ir
                row["industry_timing_oos_observations"] = gate.get("industry_timing_oos_observations", 0)
                row["industry_timing_oos_method"] = gate.get("industry_timing_oos_method")
            if self.mode == "core_overlay":
                self._recompute_combined_scores(eligible)

        for ind, members in industries.items():
            members.sort(key=lambda r: -float(r.get("industry_alpha_score", r.get("industry_score", 0))))
            for i, row in enumerate(members):
                row["industry_rank"] = i + 1
                row["industry_candidate_count"] = len(members)

        # ---- Phase 3: Per-industry Top-N candidate selection ----
        for ind, members in industries.items():
            # Dynamic cap: min(max_per_industry, ceil(industry_size * 15%))
            base_cap = min(
                self.max_per_industry,
                max(self.min_industry_candidates, int(np.ceil(len(members) * 0.15))),
            )
            bucket = self._industry_timing_bucket(members[0]) if members else "Neutral"
            overlay_cap = self._candidate_cap_with_overlay(base_cap, bucket)
            for i, row in enumerate(members):
                row["candidate_cap_base"] = base_cap
                row["candidate_cap_overlay"] = overlay_cap
                row["industry_cap"] = overlay_cap
                row["industry_timing_bucket"] = bucket
                row["industry_weight_budget"] = self._industry_weight_budget(bucket)
                row["industry_budget_reason"] = self._industry_budget_reason(bucket)
                row["selection_layer"] = self._selection_layer(i, base_cap, overlay_cap)
                row["selected"] = i < overlay_cap and bucket != "Broken"

        # ---- Phase 4: Cross-industry final ranking ----
        selected = [r for r in eligible if r.get("selected")]
        selected.sort(key=lambda r: -float(r.get("combined_selection_score", r.get("industry_score", 0))))

        # Apply industry concentration penalty to final ranking
        industry_pick_counts: dict[str, int] = {}
        for row in selected:
            ind = self._industry_group_key(
                row["stock_code"],
                industry_map.get(row["stock_code"]) or row.get("industry_l2", "") or row.get("industry_l1", ""),
            )
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
            row["final_score"] = (
                float(row.get("industry_alpha_score", row.get("industry_score", 50.0))) * 0.70
                + float(row.get("industry_opportunity_score", 50.0)) * self._effective_overlay_strength()
                + float(row.get("ranking_score", 0)) * 0.10
                - penalty
            )
            industry_pick_counts[ind] = count + 1

        # Re-sort after penalty and take top_n
        selected.sort(key=lambda r: -float(r.get("final_score", r.get("ranking_score_adjusted", 0))))
        final_pool = selected[: self.top_n]

        # Mark final selection
        for row in ranking_rows:
            row["selected"] = row in final_pool
            row.setdefault("selection_layer", "fallback" if not row.get("eligibility_pass") else "core")
            row.setdefault("industry_alpha_score", row.get("industry_score", 50.0))
            row.setdefault("industry_opportunity_score", 50.0)
            row.setdefault("combined_selection_score", row.get("final_score", row.get("ranking_score", 0)))
            row.setdefault("industry_timing_bucket", "Neutral")
            row.setdefault("industry_timing_oos_win_rate", self.timing_oos_win_rate)
            row.setdefault("industry_timing_oos_ir", self.timing_oos_ir)
            row.setdefault("industry_timing_oos_observations", 0)
            row.setdefault("industry_timing_oos_method", None)
            row.setdefault("candidate_cap_base", row.get("industry_cap", 0))
            row.setdefault("candidate_cap_overlay", row.get("industry_cap", 0))
            row.setdefault("industry_weight_budget", self._industry_weight_budget("Neutral"))
            row.setdefault("industry_budget_reason", "neutral:base_budget")

        return ranking_rows

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_industry_map_from_rows(
        self, rows: list[dict[str, Any]]
    ) -> dict[str, str | None]:
        level = self.industry_level
        return {
            r["stock_code"]: self._industry_group_key(
                r["stock_code"],
                r.get(f"industry_{level}") or r.get("industry_l1", ""),
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
                reasons.append("liquidity_not_ok")

        if self.require_non_sideways:
            setup = row.get("setup_type", "")
            if str(setup).strip().lower() == "sideways":
                reasons.append("sideways_setup")

        if self.require_fresh_signal:
            freshness = self._float_or_default(row.get("signal_freshness_score"), 100.0)
            if freshness < self.min_signal_freshness:
                reasons.append(f"stale_signal(freshness={freshness:.0f})")

        # Ai, Liu & Lin (2024): weak signals degrade model performance
        signal_tier = str(row.get("signal_tier", "")).strip().lower()
        tier_order = {"strong": 0, "medium": 1, "weak": 2}
        min_tier_val = tier_order.get(self.min_signal_tier, 1)
        actual_tier_val = tier_order.get(signal_tier, 2)
        if actual_tier_val > min_tier_val:
            reasons.append(f"weak_signal_tier(tier={signal_tier})")

        # Bryzgalova et al. (2022): missing data is not random — hard floor
        quality_cov = self._float_or_default(row.get("quality_data_coverage"), 1.0)
        if quality_cov < self.min_quality_coverage:
            reasons.append(f"low_quality_coverage({quality_cov:.0%})")

        overheat = self._float_or_default(row.get("overheat_penalty_score"), 0.0)
        if overheat >= self.max_overheat:
            reasons.append(f"overheated(score={overheat:.0f})")

        # Chekhlov, Uryasev & Zabarankin (2005): drawdown >30% implies
        # near-certain long-run underperformance for long-only portfolios.
        drawdown = self._float_or_default(row.get("drawdown_penalty_score"), 0.0)
        if drawdown >= 100 * self.max_drawdown_pct:
            reasons.append(f"excessive_drawdown(score={drawdown:.0f})")

        downtrend = self._float_or_default(row.get("downtrend_penalty_score"), 0.0)
        if downtrend >= self.max_downtrend_penalty:
            reasons.append(f"severe_downtrend(score={downtrend:.0f})")

        data_cov = self._float_or_default(row.get("data_coverage_score"), 100.0) / 100.0
        if data_cov < self.min_data_coverage:
            reasons.append(f"low_data_coverage({data_cov:.0%})")

        # PE sanity check
        pe = row.get("pe_ratio")
        if pe is not None and not (isinstance(pe, float) and np.isnan(pe)):
            pe_val = float(pe)
            if pe_val <= 0:
                reasons.append(f"negative_pe({pe_val:.1f})")
            elif pe_val > self.max_pe_ratio:
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
    def _float_or_default(value: Any, default: float) -> float:
        if value is None:
            return float(default)
        try:
            if np.isnan(value):
                return float(default)
        except (TypeError, ValueError):
            pass
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _normalize_reasons(reasons: list[str]) -> list[str]:
        normalized: list[str] = []
        key_to_index: dict[str, int] = {}
        aliases = {
            "weak_signal_tier": "weak_signal_tier",
            "excessive_drawdown": "excessive_drawdown",
            "severe_downtrend": "severe_downtrend",
            "downtrend": "severe_downtrend",
            "low_quality_coverage": "low_quality_coverage",
            "low_data_coverage": "low_data_coverage",
            "stale_signal": "stale_signal",
            "overheated": "overheated",
            "negative_pe": "negative_pe",
            "extreme_pe": "extreme_pe",
            "negative_pb": "negative_pb",
            "extreme_pb": "extreme_pb",
        }
        for reason in reasons:
            reason_text = str(reason)
            base = reason_text.split("(", 1)[0]
            key = aliases.get(base, base)
            if key in key_to_index:
                index = key_to_index[key]
                if "(" in reason_text and "(" not in normalized[index]:
                    normalized[index] = reason_text
                continue
            normalized.append(reason_text)
            key_to_index[key] = len(normalized) - 1
        return normalized

    @staticmethod
    def _group_by_industry(
        rows: list[dict[str, Any]],
        industry_map: dict[str, str | None],
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            code = row["stock_code"]
            ind = IndustryCandidateSelector._industry_group_key(
                code,
                industry_map.get(code) or row.get("industry_l2") or row.get("industry_l1", ""),
            )
            groups.setdefault(ind, []).append(row)
        return groups

    @staticmethod
    def _industry_group_key(stock_code: str, industry_label: Any) -> str:
        label = str(industry_label or "").strip()
        if label:
            return label
        return f"__unclassified__:{stock_code}"

    @staticmethod
    def _safe_rank(values: list[float], *, higher_is_better: bool = True) -> list[float]:
        n = len(values)
        if n <= 1:
            return [50.0] * n
        arr = np.asarray(values, dtype=float)
        if np.isnan(arr).all():
            return [50.0] * n
        median = float(np.nanmedian(arr))
        arr = np.where(np.isnan(arr), median, arr)
        if float(np.nanstd(arr)) < 1e-12:
            return [50.0] * n
        order = np.argsort(np.argsort(arr, kind="mergesort"), kind="mergesort")
        ranks = order.astype(float) / max(n - 1, 1) * 100.0
        if not higher_is_better:
            ranks = 100.0 - ranks
        return ranks.tolist()

    def _assign_industry_opportunity_ranks(self, rows: list[dict[str, Any]]) -> None:
        """Rank industry return/volatility once per real industry."""
        by_industry: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = self._industry_group_key(
                str(row.get("stock_code", "")),
                row.get("industry_l2") or row.get("industry_l1") or "",
            )
            by_industry.setdefault(key, row)

        keys = list(by_industry.keys())
        ret_ranks = self._safe_rank([
            self._float_or_default(by_industry[k].get("industry_ret_20d"), 0.0)
            for k in keys
        ])
        vol_ranks = self._safe_rank([
            self._float_or_default(by_industry[k].get("industry_vol_60d"), 0.0)
            for k in keys
        ])
        rank_payload = {
            key: {
                "industry_ret_20d_rank": ret_ranks[i],
                "industry_vol_60d_rank": vol_ranks[i],
            }
            for i, key in enumerate(keys)
        }
        for row in rows:
            key = self._industry_group_key(
                str(row.get("stock_code", "")),
                row.get("industry_l2") or row.get("industry_l1") or "",
            )
            row.update(rank_payload.get(key, {}))

    def _assign_industry_scores(self, rows: list[dict[str, Any]]) -> None:
        """Assign Core/Overlay fields using peer-group percentile ranks."""
        peer_groups = self._build_peer_groups(rows)
        for peer_key, members in peer_groups.items():
            ranks = {
                "model_score_within_industry": self._safe_rank([
                    self._float_or_default(r.get("ranking_score"), 50.0) for r in members
                ]),
                "quality_score_within_industry": self._safe_rank([
                    self._float_or_default(r.get("quality_score"), 50.0) for r in members
                ]),
                "valuation_score_within_industry": self._safe_rank([
                    self._float_or_default(
                        r.get("valuation_score", r.get("value_score")),
                        50.0,
                    ) for r in members
                ]),
                "risk_adjusted_score_within_industry": self._safe_rank([
                    self._float_or_default(r.get("risk_adjusted_score"), r.get("ranking_score", 50.0))
                    for r in members
                ]),
                "liquidity_score_within_industry": self._safe_rank([
                    self._float_or_default(r.get("median_turnover_amount_20d"), 0.0)
                    for r in members
                ]),
                "stock_vs_industry_rank": self._safe_rank([
                    self._float_or_default(r.get("stock_vs_industry_rank"), 50.0)
                    for r in members
                ]),
            }
            for idx, row in enumerate(members):
                for name, values in ranks.items():
                    row[name] = float(values[idx])
                row["industry_peer_group_used"] = peer_key
                row["industry_peer_count"] = len(members)

                alpha = (
                    row["model_score_within_industry"] * 0.35
                    + row["quality_score_within_industry"] * 0.20
                    + row["valuation_score_within_industry"] * 0.15
                    + row["risk_adjusted_score_within_industry"] * 0.15
                    + row["liquidity_score_within_industry"] * 0.10
                    + row["stock_vs_industry_rank"] * 0.05
                )
                opportunity = self._compute_industry_opportunity_score(row)
                overlay_strength = self._effective_overlay_strength()
                combined = alpha
                if self.mode == "timing_only":
                    combined = opportunity
                elif self.mode == "core_overlay":
                    combined = alpha * (1.0 - overlay_strength) + opportunity * overlay_strength

                row["industry_alpha_score"] = float(np.clip(alpha, 0.0, 100.0))
                row["industry_score"] = row["industry_alpha_score"]
                row["industry_opportunity_score"] = float(np.clip(opportunity, 0.0, 100.0))
                row["combined_selection_score"] = float(np.clip(combined, 0.0, 100.0))
                row["industry_timing_oos_win_rate"] = self.timing_oos_win_rate
                row["industry_timing_oos_ir"] = self.timing_oos_ir

    def _recompute_combined_scores(self, rows: list[dict[str, Any]]) -> None:
        overlay_strength = self._effective_overlay_strength()
        for row in rows:
            alpha = self._float_or_default(row.get("industry_alpha_score"), 50.0)
            opportunity = self._float_or_default(row.get("industry_opportunity_score"), 50.0)
            if self.mode == "timing_only":
                combined = opportunity
            elif self.mode == "core_overlay":
                combined = alpha * (1.0 - overlay_strength) + opportunity * overlay_strength
            else:
                combined = alpha
            row["combined_selection_score"] = float(np.clip(combined, 0.0, 100.0))

    def _build_peer_groups(self, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        l2_groups: dict[str, list[dict[str, Any]]] = {}
        l1_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            l2 = str(row.get("industry_l2") or "").strip()
            l1 = str(row.get("industry_l1") or "").strip()
            if l2:
                l2_groups.setdefault(f"l2:{l2}", []).append(row)
            if l1:
                l1_groups.setdefault(f"l1:{l1}", []).append(row)

        peer_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            l2 = str(row.get("industry_l2") or "").strip()
            l1 = str(row.get("industry_l1") or "").strip()
            l2_key = f"l2:{l2}" if l2 else ""
            l1_key = f"l1:{l1}" if l1 else ""
            if l2_key and len(l2_groups.get(l2_key, [])) >= 8:
                key = l2_key
            elif l1_key and len(l1_groups.get(l1_key, [])) >= 15:
                key = l1_key
            else:
                key = "global_fallback"
            peer_groups.setdefault(key, []).append(row)
        return peer_groups

    def _compute_industry_opportunity_score(self, row: dict[str, Any]) -> float:
        rps20 = self._float_or_default(row.get("industry_rps_20d"), 50.0)
        rps60 = self._float_or_default(row.get("industry_rps_60d"), 50.0)
        breadth20 = self._float_or_default(row.get("industry_breadth_20d"), 0.5) * 100.0
        ret20_rank = self._float_or_default(row.get("industry_ret_20d_rank"), rps20)
        vol60_rank = self._float_or_default(row.get("industry_vol_60d_rank"), 50.0)
        score = (
            rps20 * 0.35
            + rps60 * 0.25
            + breadth20 * 0.20
            + ret20_rank * 0.10
            - vol60_rank * 0.10
        )
        return float(np.clip(score, 0.0, 100.0))

    def _effective_overlay_strength(self) -> float:
        if self.mode == "core":
            return 0.0
        if self.mode == "timing_only":
            return 1.0
        configured = self.overlay_strength
        if self.timing_oos_win_rate is None:
            return 0.0
        win_rate = float(self.timing_oos_win_rate)
        if win_rate < 0.60:
            return 0.0
        if win_rate < 0.65:
            return min(configured, 0.10)
        if win_rate < 0.70:
            return min(configured, 0.20)
        return min(configured, 0.30)

    def _industry_timing_bucket(self, row: dict[str, Any]) -> str:
        opp = self._float_or_default(row.get("industry_opportunity_score"), 50.0)
        rps20 = self._float_or_default(row.get("industry_rps_20d"), 50.0)
        rps60 = self._float_or_default(row.get("industry_rps_60d"), 50.0)
        breadth20 = self._float_or_default(row.get("industry_breadth_20d"), 0.5)
        vol60 = self._float_or_default(row.get("industry_vol_60d"), 0.0)
        ret20 = self._float_or_default(row.get("industry_ret_20d"), 0.0)
        if ret20 < -0.18 or (rps20 < 20 and rps60 < 30 and breadth20 < 0.35):
            return "Broken"
        if opp >= 70 and rps20 >= 65 and rps60 >= 55 and breadth20 >= 0.55 and vol60 <= 0.80:
            return "Hot"
        if opp <= 35 or (rps20 < 35 and breadth20 < 0.45):
            return "Cold"
        return "Neutral"

    def _candidate_cap_with_overlay(self, base_cap: int, bucket: str) -> int:
        if self.mode == "core":
            return int(base_cap)
        strength = self._effective_overlay_strength()
        if bucket == "Hot" and strength >= 0.20:
            return int(min(self.max_per_industry, base_cap + 1))
        if bucket == "Cold" and strength > 0:
            return int(max(self.min_industry_candidates, base_cap - 1))
        if bucket == "Broken":
            return int(max(0, min(base_cap, self.min_industry_candidates)))
        return int(base_cap)

    def _industry_weight_budget(self, bucket: str) -> float:
        base = min(0.30, self.max_industry_weight)
        strength = self._effective_overlay_strength()
        if bucket == "Hot" and strength > 0:
            return float(min(self.max_industry_weight, base * self.hot_industry_weight_multiplier))
        if bucket == "Cold" and strength > 0:
            return 0.15
        if bucket == "Broken":
            return 0.05
        return float(base)

    def _industry_budget_reason(self, bucket: str) -> str:
        strength = self._effective_overlay_strength()
        if self.mode == "core":
            return "core:overlay_disabled"
        if self.timing_oos_win_rate is None:
            return "overlay_report_only:no_oos_gate"
        if strength <= 0:
            return "overlay_disabled:oos_win_rate_below_60pct"
        return f"{bucket.lower()}:overlay_strength={strength:.2f}"

    @staticmethod
    def _selection_layer(index: int, base_cap: int, overlay_cap: int) -> str:
        if index < base_cap:
            return "core"
        if index < overlay_cap:
            return "overlay_boosted"
        return "fallback"


# ---------------------------------------------------------------------------
# Portfolio-level industry soft constraints
# ---------------------------------------------------------------------------

def compute_industry_timing_oos_gate(
    ranking_rows: list[dict[str, Any]],
    *,
    top_quantile: float = 0.30,
) -> dict[str, Any]:
    """Estimate an OOS-style gate for the industry timing overlay.

    The selector only has the current run's point-in-time ranking rows, not a
    full historical industry panel.  This gate therefore uses each stock's
    already out-of-sample backtest/forward-return summary as a proxy and tests
    whether high opportunity industries beat the industry median.

    It is intentionally conservative: if there are fewer than five industry
    observations, the gate returns ``None`` and Overlay remains report-only.
    """
    industry_rows: dict[str, list[dict[str, Any]]] = {}
    for row in ranking_rows or []:
        industry = str(row.get("industry_l2") or row.get("industry_l1") or "").strip()
        if not industry:
            continue
        industry_rows.setdefault(industry, []).append(row)

    observations = []
    for industry, members in industry_rows.items():
        returns = []
        opportunities = []
        for row in members:
            ret = row.get("avg_forward_return_60_signal")
            if ret is None or not np.isfinite(ret):
                ret = row.get("backtest_return")
            if ret is None or not np.isfinite(ret):
                continue
            returns.append(float(ret))
            opportunities.append(IndustryCandidateSelector._float_or_default(
                row.get("industry_opportunity_score"), 50.0,
            ))
        if not returns:
            continue
        observations.append(
            {
                "industry": industry,
                "opportunity": float(np.nanmean(opportunities)) if opportunities else 50.0,
                "return": float(np.nanmean(returns)),
            }
        )

    n = len(observations)
    if n < 5:
        return {
            "industry_timing_oos_win_rate": None,
            "industry_timing_oos_ir": None,
            "industry_timing_oos_observations": n,
            "industry_timing_oos_method": "insufficient_industry_observations",
        }

    opp_values = np.asarray([item["opportunity"] for item in observations], dtype=float)
    threshold = float(np.nanquantile(opp_values, max(0.0, min(1.0, 1.0 - top_quantile))))
    selected = [item for item in observations if item["opportunity"] >= threshold]
    if not selected:
        selected = sorted(observations, key=lambda item: item["opportunity"], reverse=True)[:1]

    median_return = float(np.nanmedian([item["return"] for item in observations]))
    excess = np.asarray([item["return"] - median_return for item in selected], dtype=float)
    win_rate = float(np.mean(excess > 0.0)) if len(excess) else None
    if len(excess) > 1 and float(np.nanstd(excess, ddof=1)) > 1e-12:
        ir = float(np.nanmean(excess) / np.nanstd(excess, ddof=1))
    else:
        ir = 0.0 if len(excess) else None

    return {
        "industry_timing_oos_win_rate": win_rate,
        "industry_timing_oos_ir": ir,
        "industry_timing_oos_observations": n,
        "industry_timing_oos_method": "industry_opportunity_vs_forward_return_proxy",
    }


def compute_industry_hhi(
    selected_codes: list[str],
    industry_map: dict[str, str | None],
    weights: list[float] | None = None,
    *,
    normalize_weights: bool = False,
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
    elif normalize_weights:
        total_weight = float(sum(float(w or 0.0) for w in weights))
        if total_weight > 0:
            weights = [float(w or 0.0) / total_weight for w in weights]

    ind_weights: dict[str, float] = {}
    for code, w in zip(selected_codes, weights):
        ind = IndustryCandidateSelector._industry_group_key(code, industry_map.get(code))
        ind_weights[ind] = ind_weights.get(ind, 0.0) + w

    hhi = sum(v ** 2 for v in ind_weights.values()) * 10000.0
    return float(hhi)


def compute_industry_weight_table(
    selected_codes: list[str],
    industry_map: dict[str, str | None],
    weights: list[float],
    selected_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build an industry weight breakdown table.

    Returns
    -------
    list[dict] with keys: industry, stock_count, weight_pct, stocks.
    """
    row_by_code = {
        str(row.get("stock_code", "")).zfill(5): row
        for row in (selected_rows or [])
    }
    ind_data: dict[str, dict[str, Any]] = {}
    for code, w in zip(selected_codes, weights):
        raw_label = str(industry_map.get(code) or "").strip()
        ind_key = IndustryCandidateSelector._industry_group_key(code, raw_label)
        if ind_key not in ind_data:
            ind_data[ind_key] = {
                "industry": raw_label or "unknown",
                "stock_count": 0,
                "weight_pct": 0.0,
                "stocks": [],
                "industry_timing_bucket": None,
                "industry_weight_budget_pct": None,
                "industry_budget_reason": None,
            }
        ind_data[ind_key]["stock_count"] += 1
        ind_data[ind_key]["weight_pct"] += w * 100.0
        ind_data[ind_key]["stocks"].append(code)
        row = row_by_code.get(str(code).zfill(5), {})
        if row:
            ind_data[ind_key]["industry_timing_bucket"] = (
                ind_data[ind_key]["industry_timing_bucket"]
                or row.get("industry_timing_bucket")
            )
            budget = row.get("industry_weight_budget")
            if budget is not None and ind_data[ind_key]["industry_weight_budget_pct"] is None:
                ind_data[ind_key]["industry_weight_budget_pct"] = float(budget) * 100.0
            ind_data[ind_key]["industry_budget_reason"] = (
                ind_data[ind_key]["industry_budget_reason"]
                or row.get("industry_budget_reason")
            )

    result = sorted(ind_data.values(), key=lambda x: -x["weight_pct"])
    for item in result:
        item["weight_pct"] = round(item["weight_pct"], 2)
        if item["industry_weight_budget_pct"] is not None:
            item["industry_weight_budget_pct"] = round(item["industry_weight_budget_pct"], 2)
    return result


def compute_industry_attribution_table(
    ranking_rows: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Summarize Core/Overlay diagnostics by industry.

    This is a point-in-time attribution scaffold for CSV/LLM reporting.  It
    does not claim realized alpha; it explains the current selection pressure
    by separating within-industry alpha from industry opportunity.
    """
    selected_codes = {
        str(row.get("stock_code", "")).zfill(5)
        for row in (selected_rows or [])
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in ranking_rows or []:
        if not row.get("eligibility_pass", row.get("selection_eligible", True)):
            continue
        industry = str(row.get("industry_l2") or row.get("industry_l1") or "unknown").strip() or "unknown"
        groups.setdefault(industry, []).append(row)

    result = []
    for industry, members in groups.items():
        selected_count = sum(
            1 for row in members
            if str(row.get("stock_code", "")).zfill(5) in selected_codes
        )
        alpha_scores = [
            IndustryCandidateSelector._float_or_default(row.get("industry_alpha_score"), 50.0)
            for row in members
        ]
        opp_scores = [
            IndustryCandidateSelector._float_or_default(row.get("industry_opportunity_score"), 50.0)
            for row in members
        ]
        final_scores = [
            IndustryCandidateSelector._float_or_default(row.get("final_score"), row.get("ranking_score", 50.0))
            for row in members
        ]
        first = members[0]
        result.append(
            {
                "industry": industry,
                "eligible_count": len(members),
                "selected_count": selected_count,
                "avg_industry_alpha_score": round(float(np.mean(alpha_scores)), 4),
                "avg_industry_opportunity_score": round(float(np.mean(opp_scores)), 4),
                "avg_final_score": round(float(np.mean(final_scores)), 4),
                "industry_timing_bucket": first.get("industry_timing_bucket", "Neutral"),
                "industry_weight_budget_pct": (
                    round(float(first.get("industry_weight_budget")) * 100.0, 4)
                    if first.get("industry_weight_budget") is not None else None
                ),
                "industry_budget_reason": first.get("industry_budget_reason"),
                "industry_rps_20d": first.get("industry_rps_20d"),
                "industry_rps_60d": first.get("industry_rps_60d"),
                "industry_breadth_20d": first.get("industry_breadth_20d"),
                "industry_vol_60d": first.get("industry_vol_60d"),
            }
        )

    result.sort(key=lambda item: (-item["selected_count"], -item["avg_final_score"]))
    return result
