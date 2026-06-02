#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""组合层最小入口。"""

import numpy as np
import pandas as pd

from data.model import normalize_bool

from backtest_engine.models import EquityPoint, PortfolioBuildResult, PortfolioReplayResult, TradeRecord
from factor_engine.signals import summarize_low_price_setup


def _concentration_penalty(count_in_cluster: int) -> float:
    """Progressive concentration penalty for soft sector constraints.

    Reference: Ehsani, Harvey & Li (2023) — long-only investors should use
    soft constraints rather than hard sector caps.

    Penalty schedule:
      0-1 stocks in cluster: no penalty
      2 stocks (3rd entry): moderate penalty (8 points)
      3 stocks (4th entry): significant penalty (18 points)
      4+ stocks (5th+ entry): near-hard block (35 points)
    """
    if count_in_cluster <= 1:
        return 0.0
    elif count_in_cluster == 2:
        return 8.0
    elif count_in_cluster == 3:
        return 18.0
    else:
        return 35.0


def _apply_within_cluster_quality_zscore(extracted: list[dict]) -> None:
    """Normalize quality_score within each cluster (QMJ-style).

    Reference: Asness, Frazzini & Pedersen (2014) — z-score quality within
    Fama-French 12 industry groups to eliminate sector bias.

    For each cluster:
      z = (quality - cluster_mean) / cluster_std
      quality_z = 50 + z * 10  (map to 0-100 scale; z=-3→20, z=+3→80)

    Clusters with < 3 stocks or zero variance keep their original score.
    cluster_id == -1 falls back to global z-score.
    """
    if len(extracted) < 3:
        return

    # Group by cluster_id
    clusters: dict[int, list[int]] = {}
    for i, e in enumerate(extracted):
        cid = e.get("cluster_id", -1)
        clusters.setdefault(cid, []).append(i)

    # Compute global stats for fallback
    all_qualities = [e["components"]["quality_score"] for e in extracted]
    global_mean = np.mean(all_qualities)
    global_std = np.std(all_qualities, ddof=1)

    for cid, indices in clusters.items():
        if len(indices) < 3:
            # Small cluster: use global z-score
            for i in indices:
                q = extracted[i]["components"]["quality_score"]
                if global_std > 0.01:
                    z = (q - global_mean) / global_std
                else:
                    z = 0.0
                extracted[i]["components"]["quality_score"] = float(np.clip(50.0 + z * 10.0, 0.0, 100.0))
            continue

        cluster_qualities = [extracted[i]["components"]["quality_score"] for i in indices]
        c_mean = np.mean(cluster_qualities)
        c_std = np.std(cluster_qualities, ddof=1)

        if c_std < 0.01:
            # All same quality in this cluster → neutral 50
            for i in indices:
                extracted[i]["components"]["quality_score"] = 50.0
            continue

        for i in indices:
            q = extracted[i]["components"]["quality_score"]
            z = (q - c_mean) / c_std
            # Cap z at ±3 to prevent extreme outliers from dominating
            z = np.clip(z, -3.0, 3.0)
            extracted[i]["components"]["quality_score"] = float(np.clip(50.0 + z * 10.0, 0.0, 100.0))


class TopNPortfolioBuilder:
    """基于当前评分与横截面信号的最小组合构建器。"""

    def __init__(
        self,
        top_n=3,
        initial_capital=100000,
        weighting_mode="equal_weight",
        buy_commission_rate=0.0,
        sell_commission_rate=0.0,
        slippage_rate=0.0,
        min_commission=0.0,
        enable_portfolio_replay=True,
    ):
        self.top_n = int(top_n)
        self.initial_capital = float(initial_capital)
        self.weighting_mode = str(weighting_mode or "equal_weight").strip().lower()
        self.buy_commission_rate = float(buy_commission_rate)
        self.sell_commission_rate = float(sell_commission_rate)
        self.slippage_rate = float(slippage_rate)
        self.min_commission = float(min_commission)
        self.enable_portfolio_replay = bool(enable_portfolio_replay)
        if self.weighting_mode not in {"equal_weight", "score_weight"}:
            raise ValueError(f"unsupported weighting_mode: {self.weighting_mode}")

    def build(self, stock_codes, analysis_results):
        """从单股票分析结果构建组合选择结果。"""
        pool_results = list(analysis_results or [])
        if not pool_results:
            return None

        ranking = self._build_ranking_rows(pool_results)
        signal_rows = []
        for result in pool_results:
            signal_rows.extend(self._collect_signal_rows(result))

        ranking.sort(
            key=lambda item: (
                -item["ranking_score"],
                -np.nan_to_num(item["current_signal_score"], nan=-1),
                -np.nan_to_num(item.get("regime_score", np.nan), nan=-1),
                -np.nan_to_num(item.get("matrix_score", np.nan), nan=-1),
                -item["backtest_return"],
            )
        )
        for item in ranking:
            eligibility = self._compute_selection_eligibility(item)
            item.update(eligibility)

        industry_candidate_codes = set()
        industry_selected_codes = []
        industry_candidate_ranking = []

        # Industry-aware candidate pool: every final holding should come from
        # an eligible per-industry shortlist unless that shortlist is empty.
        try:
            from backtest_engine.industry_selector import IndustryCandidateSelector

            ind_selector = IndustryCandidateSelector(
                top_n=self.top_n,
                max_per_industry=max(1, int(np.ceil(self.top_n / 2))),
                require_actionable=True,
                require_liquidity=True,
                require_fresh_signal=False,
            )
            industry_map = {
                r.get("stock_code", ""): r.get("industry_l2") or r.get("industry_l1", "")
                for r in ranking
            }
            ranking = ind_selector.select(ranking, industry_map)
            industry_candidate_ranking = [
                item for item in ranking
                if item.get("eligibility_pass") and item.get("industry_rank", 0) <= item.get("industry_cap", 0)
            ]
            industry_candidate_codes = {item.get("stock_code") for item in industry_candidate_ranking}
            industry_selected_codes = [
                item.get("stock_code") for item in ranking
                if item.get("selected") and item.get("eligibility_pass")
            ]
        except Exception:
            # Non-breaking: industry selector is optional enrichment
            for item in ranking:
                item.setdefault("industry_rank", 0)
                item.setdefault("industry_score", 50.0)
                item.setdefault("eligibility_pass", item.get("selection_eligible", True))
                item.setdefault("eligibility_reasons", item.get("eligibility_reasons", []))
                item.setdefault("selected", False)

        cross_sectional_picks = []
        grouped_candidates = {}
        contributions = []
        portfolio_return_sum = 0.0
        portfolio_return_count = 0
        portfolio_win_count = 0

        if signal_rows:
            signal_df = pd.DataFrame(signal_rows)
            signal_df = signal_df[signal_df["actionable"]].copy()
            if "signal_freshness_score" in signal_df.columns:
                signal_df = signal_df[signal_df["signal_freshness_score"].fillna(0) >= 35].copy()
            if "low_price_candidate" in signal_df.columns:
                signal_df = signal_df[signal_df["low_price_candidate"].fillna(True)].copy()
            if "setup_type" in signal_df.columns:
                signal_df = signal_df[signal_df["setup_type"].fillna("neutral") != "sideways"].copy()

            setup_bonus = signal_df.get("setup_score", 0).fillna(0) * 0.18 if "setup_score" in signal_df.columns else 0.0
            freshness_bonus = signal_df.get("signal_freshness_score", 0).fillna(0) * 0.10 if "signal_freshness_score" in signal_df.columns else 0.0
            sideways_penalty = signal_df.get("sideways_penalty", 0).fillna(0) * 1.10 if "sideways_penalty" in signal_df.columns else 0.0
            signal_df["selection_score"] = (
                signal_df["expected_3m_score"] * 0.50
                + signal_df["matrix_score"] * 0.22
                + signal_df["regime_score"] * 0.12
                + signal_df["signal_strength"] * 3.0
                + setup_bonus
                + freshness_bonus
                - signal_df["risk_score"] * 4.0
                - sideways_penalty
                - signal_df["forward_max_drawdown_60"].abs() * 100 * 0.35
            )

            for signal_date, day_group in signal_df.groupby("date"):
                selected_group = day_group.sort_values(
                    by=["selection_score", "expected_3m_score", "matrix_score", "regime_score", "signal_strength"],
                    ascending=False,
                ).head(self.top_n)
                grouped_candidates[signal_date] = len(day_group)

                for _, item in selected_group.iterrows():
                    realized_return_pct = item["forward_return_60"] * 100
                    portfolio_return_sum += realized_return_pct
                    portfolio_return_count += 1
                    if realized_return_pct > 0:
                        portfolio_win_count += 1
                    cross_sectional_picks.append(self._build_pick_record(item, signal_date, selected_group))
                    contributions.append(
                        {
                            "stock_code": item["stock_code"],
                            "signal_date": signal_date,
                            "contribution_return": realized_return_pct,
                            "entry_type": item["entry_type"],
                            "selection_score": item["selection_score"],
                        }
                    )

        estimated_portfolio_return = portfolio_return_sum / portfolio_return_count if portfolio_return_count > 0 else 0
        estimated_portfolio_win_rate = portfolio_win_count / portfolio_return_count * 100 if portfolio_return_count > 0 else 0

        # Kelly dynamic position sizing
        if contributions:
            contrib_returns = [c["contribution_return"] for c in contributions]
            wins = [r for r in contrib_returns if r > 0]
            losses = [abs(r) for r in contrib_returns if r < 0]
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = sum(losses) / len(losses) if losses else 0.0
            b_ratio = (avg_win / avg_loss) if avg_loss > 0 else 2.0
            p = estimated_portfolio_win_rate / 100.0
            kelly_f = max(0.0, (p * b_ratio - (1.0 - p)) / b_ratio) if b_ratio > 0 else 0.0
            half_kelly = kelly_f * 0.5
            kelly_position_ratio = half_kelly
        else:
            kelly_position_ratio = 1.0 / self.top_n if self.top_n > 0 else 0.1

        # Build ranking filtered to eligible candidates (used by sector
        # concentration logic below)
        eligible_ranking = [item for item in ranking if item.get("selection_eligible")]
        lightgbm_candidates = [item for item in ranking if item.get("selection_source") == "lightgbm_ranker"]

        # When IndustryCandidateSelector produced selections, use them as the
        # primary selected list — the selector already applied all hard filters.
        # Fall back to tiered selection only when no industry selections exist.
        if industry_selected_codes:
            selected_by_code = {item.get("stock_code"): item for item in ranking}
            selected = [
                selected_by_code[code]
                for code in industry_selected_codes
                if code in selected_by_code
            ]
            selected.sort(
                key=lambda item: -float(
                    item.get("final_score", item.get("ranking_score_adjusted", item.get("ranking_score", 0)))
                )
            )
            selected = [dict(item) for item in selected[: self.top_n]]
        else:
            # Legacy tiered selection (when IndustryCandidateSelector is not active)
            selection_universe = {item.get("stock_code") for item in eligible_ranking}
            preferred = [
                item for item in ranking
                if item.get("stock_code") in selection_universe and item.get("selection_eligible")
                and item.get("current_signal_actionable") and item.get("low_price_candidate", True)
                and item.get("signal_freshness_score", 100) >= 40
                and item.get("setup_type") in {"pre_breakout", "bottom_rebound"}
            ]
            active = [
                item for item in ranking
                if item.get("stock_code") in selection_universe and item.get("selection_eligible")
                and item.get("current_signal_actionable")
                and item.get("signal_freshness_score", 100) >= 35
                and item.get("setup_type") != "sideways"
            ]
            fallback = [
                item for item in ranking
                if item.get("stock_code") in selection_universe and item.get("selection_eligible")
                and item.get("signal_tier") != "weak"
                and item.get("signal_freshness_score", 100) >= 30
                and item.get("setup_type") != "sideways"
            ]
            if lightgbm_candidates:
                min_wr = 35.0
                preferred = [i for i in preferred if i.get("win_rate", 0) >= min_wr]
                active = [i for i in active if i.get("win_rate", 0) >= min_wr]
                fallback = [i for i in fallback if i.get("win_rate", 0) >= min_wr]
            selected = preferred[:self.top_n] or active[:self.top_n] or fallback[:self.top_n] or eligible_ranking[:self.top_n]
            selected = [dict(item) for item in selected]

        # Watchlist: weak-tier stocks with good setups
        watchlist = [
            dict(item) for item in ranking
            if item.get("setup_type") in {"pre_breakout", "bottom_rebound"} and item.get("signal_tier") == "weak"
        ][: self.top_n]
        if not watchlist:
            watchlist = [dict(item) for item in ranking if item.get("signal_tier") == "weak"][: self.top_n]

        # Ranking filtered for sector concentration logic below
        selection_universe_codes = {item.get("stock_code") for item in selected}
        ranking_filtered = [
            item for item in eligible_ranking
            if item.get("stock_code") in selection_universe_codes or item.get("selection_eligible")
        ]

        # --- Data quality: demote extreme PE to watchlist ---
        kept = []
        for item in selected:
            pe = item.get("pe_ratio")
            if pe is not None and np.isfinite(pe) and (pe <= 0 or pe > 500):
                watchlist.insert(0, dict(item))
            else:
                kept.append(item)
        selected = kept

        if lightgbm_candidates:
            kept = []
            for item in selected:
                if item.get("selection_source") == "lightgbm_ranker" and item.get("win_rate", 0) < 35.0:
                    watchlist.insert(0, dict(item))
                else:
                    kept.append(item)
            selected = kept

        # --- Sector concentration: progressive penalty (soft constraint) ---
        # Reference: Ehsani, Harvey & Li (2023) — long-only investors should avoid hard
        # sector neutralization. Instead, apply a progressive concentration penalty that
        # allows exceptional stocks to overcome cluster crowding.
        if selected:
            # Greedy selection with concentration-adjusted scores
            cluster_counts: dict[int, int] = {}
            capped_selected = []
            sector_demoted = []

            # Build a working copy of the ranking with adjusted scores
            allowed_fill_codes = set(industry_candidate_codes) if industry_candidate_codes else None
            remaining = [
                dict(item) for item in ranking_filtered
                if item["stock_code"] not in {s["stock_code"] for s in selected}
                and (allowed_fill_codes is None or item["stock_code"] in allowed_fill_codes)
                and not (
                    lightgbm_candidates
                    and item.get("selection_source") == "lightgbm_ranker"
                    and item.get("win_rate", 0) < 35.0
                )
            ]

            for item in selected:
                cid = int(item.get("cluster_id", -1) or -1)
                cnt = cluster_counts.get(cid, 0)
                penalty = _concentration_penalty(cnt)
                cluster_counts[cid] = cnt + 1
                if penalty >= 15:  # Hard block at 4+ concentration
                    sector_demoted.append(item)
                else:
                    capped_selected.append(item)

            # Fill vacancies from demotions with best remaining (concentration-aware)
            existing_codes = {item["stock_code"] for item in capped_selected}
            while len(capped_selected) < self.top_n and remaining:
                best_idx = -1
                best_adj_score = -1e9
                for i, item in enumerate(remaining):
                    if item["stock_code"] in existing_codes:
                        continue
                    cid = int(item.get("cluster_id", -1) or -1)
                    cnt = cluster_counts.get(cid, 0)
                    penalty = _concentration_penalty(cnt)
                    adj_score = item.get("ranking_score", 0) - penalty
                    if adj_score > best_adj_score:
                        best_adj_score = adj_score
                        best_idx = i
                if best_idx < 0:
                    break
                chosen = remaining.pop(best_idx)
                cid = int(chosen.get("cluster_id", -1) or -1)
                cluster_counts[cid] = cluster_counts.get(cid, 0) + 1
                capped_selected.append(chosen)
                existing_codes.add(chosen["stock_code"])

            if sector_demoted:
                watchlist.extend(sector_demoted)
            selected = capped_selected

        self.kelly_position_ratio = kelly_position_ratio
        self._apply_weights(selected)
        self._sync_final_selection_flags(ranking, selected)

        synthetic_portfolio_equity_curve = self._build_synthetic_portfolio_equity_curve(cross_sectional_picks)
        portfolio_replay = (
            self._build_portfolio_replay(cross_sectional_picks, pool_results)
            if self.enable_portfolio_replay
            else None
        )
        portfolio_equity_curve = (
            portfolio_replay.equity_curve
            if portfolio_replay and portfolio_replay.equity_curve
            else synthetic_portfolio_equity_curve
        )
        final_portfolio_value = (
            portfolio_replay.final_value
            if portfolio_replay and portfolio_replay.equity_curve
            else (synthetic_portfolio_equity_curve[-1]["equity"] if synthetic_portfolio_equity_curve else self.initial_capital)
        )

        result = PortfolioBuildResult(
            stock_pool=stock_codes,
            top_n=self.top_n,
            weighting_mode=self.weighting_mode,
            buy_commission_rate=self.buy_commission_rate,
            sell_commission_rate=self.sell_commission_rate,
            slippage_rate=self.slippage_rate,
            min_commission=self.min_commission,
            ranking=ranking,
            selected=selected,
            watchlist=watchlist,
            estimated_portfolio_return=estimated_portfolio_return,
            estimated_portfolio_win_rate=estimated_portfolio_win_rate,
            estimated_trade_count=portfolio_return_count,
            kelly_position_ratio=kelly_position_ratio,
            synthetic_portfolio_equity_curve=synthetic_portfolio_equity_curve,
            portfolio_equity_curve=portfolio_equity_curve,
            portfolio_final_value=final_portfolio_value,
            portfolio_replay=portfolio_replay,
            cross_sectional_picks=cross_sectional_picks,
            daily_candidate_counts=grouped_candidates,
            contributions=contributions,
            analysis_results=pool_results,
        )
        return result.to_dict()

    @staticmethod
    def _sync_final_selection_flags(ranking, selected):
        """Align ranking selected flags with the final exported holdings."""
        selected_codes = {str(item.get("stock_code", "")).zfill(5) for item in selected}
        selected_by_code = {
            str(item.get("stock_code", "")).zfill(5): item
            for item in selected
        }
        for row in ranking:
            code = str(row.get("stock_code", "")).zfill(5)
            is_selected = code in selected_codes
            row["selected"] = is_selected
            if is_selected:
                final_item = selected_by_code[code]
                for field in (
                    "portfolio_weight",
                    "allocated_capital",
                    "kelly_scale",
                    "vol_scale",
                    "weight_reason",
                    "portfolio_industry_hhi",
                ):
                    if field in final_item:
                        row[field] = final_item[field]
            else:
                for field in (
                    "portfolio_weight",
                    "allocated_capital",
                    "kelly_scale",
                    "vol_scale",
                    "weight_reason",
                    "portfolio_industry_hhi",
                ):
                    row.pop(field, None)

    def _apply_weights(self, selected):
        """工业级权重分配：Kelly 仓位缩放 + 波动率缩放 + 流动性容量上限。

        Weight pipeline (顺序执行):
        1. Base weight: equal-weight 或 ranking_score 加权
        2. Volatility scaling: 低波加仓 / 高波减仓 (Barroso & Santa-Clara 2015)
        3. Kelly scaling: 全局半凯利仓位比例
        4. Liquidity cap: 单票不超过 20 日均成交额的 5%
        5. Floor / re-normalize: 保证总和不超过 kelly_scale

        Each stock receives a ``weight_reason`` dict explaining every adjustment.
        """
        if not selected:
            return

        n = len(selected)
        kelly_scale = getattr(self, "kelly_position_ratio", 1.0 / max(n, 1))
        max_single_weight = 0.08          # hard cap: no single stock > 8%
        min_single_weight = 0.01          # floor: stocks below 1% get trimmed
        liquidity_capacity_frac = 0.05    # max 5% of 20d median daily turnover

        # ---- Step 1: base weight ----
        if self.weighting_mode == "equal_weight":
            base_weights = [1.0 / n] * n
            weight_method = "equal"
        else:
            scores = [max(float(item.get("ranking_score", 0) or 0), 0.0) for item in selected]
            total_score = sum(scores)
            if total_score <= 0:
                base_weights = [1.0 / n] * n
                weight_method = "equal(fallback)"
            else:
                base_weights = [s / total_score for s in scores]
                weight_method = "score_weighted"

        # ---- Step 2: volatility scaling ----
        vols = []
        for item in selected:
            v = item.get("recent_volatility")
            if v is None or not np.isfinite(v) or v <= 0:
                v = np.nan
            vols.append(v)

        median_vol = float(np.nanmedian(vols)) if not all(np.isnan(v) for v in vols) else 0.25
        if median_vol <= 0:
            median_vol = 0.25

        vol_scales = []
        for v in vols:
            if np.isnan(v) or v <= 0:
                vol_scales.append(1.0)
            else:
                vol_scales.append(float(np.clip(median_vol / v, 0.5, 2.0)))

        # ---- Step 3: Kelly scaling ----
        # ---- Step 4: liquidity capacity cap ----
        weights = []
        for i, item in enumerate(selected):
            # Kelly-adjusted base
            w = base_weights[i] * kelly_scale
            # Volatility adjustment
            w_vol = w * vol_scales[i]
            # Liquidity cap: max position = 5% of 20d median daily turnover
            liq_cap = 1.0  # no cap by default
            turnover_20d = item.get("median_turnover_amount_20d")
            if turnover_20d is not None and np.isfinite(turnover_20d) and turnover_20d > 0:
                max_position_value = float(turnover_20d) * liquidity_capacity_frac
                if max_position_value > 0:
                    liq_cap = max_position_value / self.initial_capital
            w_liq = min(w_vol, liq_cap) if liq_cap < 1.0 else w_vol
            # Hard cap
            w_capped = min(w_liq, max_single_weight)
            weights.append(max(w_capped, 0.0))

        # ---- Step 5: re-normalize ----
        total_w = sum(weights)
        target_total = kelly_scale  # total portfolio exposure ≤ kelly_scale

        if total_w > 0:
            if total_w > target_total:
                scale = target_total / total_w
                weights = [w * scale for w in weights]
            # Remove stocks that fall below floor after scaling
            for i in range(len(weights)):
                if 0 < weights[i] < min_single_weight:
                    weights[i] = 0.0
            # Re-normalize again
            total_w = sum(weights)
            if total_w > target_total and total_w > 0:
                scale = target_total / total_w
                weights = [w * scale for w in weights]
        else:
            weights = [target_total / n] * n

        # ---- Step 6: assign weights + build weight_reason ----
        for i, item in enumerate(selected):
            w = weights[i]
            reasons = {
                "method": weight_method,
                "base_weight": round(base_weights[i], 6),
                "kelly_scale": round(kelly_scale, 4),
                "vol_scale": round(vol_scales[i], 4),
                "vol_input": round(float(vols[i]), 4) if vols[i] is not None and np.isfinite(vols[i]) else None,
                "median_vol": round(median_vol, 4),
            }
            # Record if liquidity cap was binding
            turnover_20d = item.get("median_turnover_amount_20d")
            if turnover_20d is not None and np.isfinite(turnover_20d) and turnover_20d > 0:
                max_pos = float(turnover_20d) * liquidity_capacity_frac
                reasons["liquidity_cap"] = round(min(max_pos / self.initial_capital, 1.0), 6)
                reasons["liquidity_cap_binding"] = reasons["liquidity_cap"] < base_weights[i]
                reasons["median_turnover_20d"] = round(float(turnover_20d), 0)
            else:
                reasons["liquidity_cap"] = None
                reasons["liquidity_cap_binding"] = False

            reasons["hard_cap_binding"] = base_weights[i] > max_single_weight
            reasons["floor_triggered"] = 0 < base_weights[i] * kelly_scale < min_single_weight
            reasons["final_weight"] = round(w, 6)

            item["portfolio_weight"] = w
            item["allocated_capital"] = self.initial_capital * w
            item["kelly_scale"] = kelly_scale
            item["vol_scale"] = vol_scales[i]
            item["weight_reason"] = reasons

        # ---- Step 7: compute industry HHI for the selected portfolio ----
        try:
            from backtest_engine.industry_selector import compute_industry_hhi

            codes = [item.get("stock_code", "") for item in selected]
            ind_map = {
                item.get("stock_code", ""): item.get("industry_l2") or item.get("industry_l1", "")
                for item in selected
            }
            portfolio_hhi = compute_industry_hhi(codes, ind_map, weights)
            for item in selected:
                item["portfolio_industry_hhi"] = round(portfolio_hhi, 1)
        except Exception:
            for item in selected:
                item["portfolio_industry_hhi"] = None

    def _build_pick_record(self, item, signal_date, selected_group):
        """构建单个横截面入选记录，并附带当日权重。"""
        positive_scores = selected_group["selection_score"].clip(lower=0)
        if self.weighting_mode == "score_weight" and positive_scores.sum() > 0:
            pick_weight = max(float(item["selection_score"]), 0.0) / float(positive_scores.sum())
        else:
            pick_weight = 1.0 / max(len(selected_group), 1)

        return {
            "date": signal_date,
            "stock_code": item["stock_code"],
            "selection_score": item["selection_score"],
            "pick_weight": pick_weight,
            "allocated_capital_hint": self.initial_capital * pick_weight,
            "expected_3m_score": item["expected_3m_score"],
            "matrix_score": item["matrix_score"],
            "entry_type": item["entry_type"],
            "forward_return_20": item["forward_return_20"] * 100,
            "forward_return_40": item["forward_return_40"] * 100,
            "forward_return_60": item["forward_return_60"] * 100,
            "forward_max_drawdown_60": item["forward_max_drawdown_60"] * 100,
            "holding_horizon": item["holding_horizon"],
        }

    def _build_synthetic_portfolio_equity_curve(self, cross_sectional_picks):
        """基于横截面选股结果构建研究型组合净值曲线。"""
        if not cross_sectional_picks:
            return []

        pick_frame = pd.DataFrame(cross_sectional_picks).copy()
        pick_frame["date"] = pd.to_datetime(pick_frame["date"], errors="coerce")
        pick_frame.dropna(subset=["date"], inplace=True)
        if pick_frame.empty:
            return []

        equity = self.initial_capital
        curve = []
        for signal_date, day_group in pick_frame.groupby("date"):
            if self.weighting_mode == "score_weight":
                score_sum = day_group["selection_score"].clip(lower=0).sum()
                if score_sum > 0:
                    weighted_return_pct = (
                        (day_group["forward_return_60"] * day_group["selection_score"].clip(lower=0)).sum() / score_sum
                    )
                else:
                    weighted_return_pct = day_group["forward_return_60"].mean()
            else:
                weighted_return_pct = day_group["forward_return_60"].mean()

            period_return = weighted_return_pct / 100.0
            equity *= 1.0 + period_return
            curve.append(
                {
                    "date": signal_date,
                    "period_return_pct": weighted_return_pct,
                    "equity": equity,
                    "pick_count": int(len(day_group)),
                }
            )

        curve.sort(key=lambda item: item["date"])
        return curve

    def _build_portfolio_replay(self, cross_sectional_picks, analysis_results):
        """基于真实价格路径回放组合净值。"""
        if not cross_sectional_picks or not analysis_results:
            return PortfolioReplayResult(
                equity_curve=[],
                trades=[],
                open_positions=[],
                final_value=self.initial_capital,
                total_return=0.0,
                total_commission=0.0,
            )

        market_data = {}
        market_calendar = set()
        for result in analysis_results:
            stock_code = result.get("stock_code")
            data = result.get("data")
            if stock_code is None or data is None or data.empty:
                continue
            working = data.copy().sort_index()
            working.index = pd.to_datetime(working.index)
            market_data[stock_code] = working
            market_calendar.update(pd.to_datetime(working.index))

        if not market_data or not market_calendar:
            return PortfolioReplayResult(
                equity_curve=[],
                trades=[],
                open_positions=[],
                final_value=self.initial_capital,
                total_return=0.0,
                total_commission=0.0,
            )

        scheduled_entries = {}
        for pick in cross_sectional_picks:
            stock_code = pick["stock_code"]
            data = market_data.get(stock_code)
            if data is None or data.empty:
                continue
            signal_date = pd.Timestamp(pick["date"])
            if signal_date not in data.index:
                continue
            signal_pos = data.index.get_loc(signal_date)
            if isinstance(signal_pos, slice):
                signal_pos = signal_pos.start
            if signal_pos is None or int(signal_pos) + 1 >= len(data):
                continue

            entry_idx = int(signal_pos) + 1
            entry_date = pd.Timestamp(data.index[entry_idx])
            holding_horizon = max(int(pick.get("holding_horizon", 60)), 1)
            exit_idx = min(entry_idx + holding_horizon - 1, len(data) - 1)
            exit_date = pd.Timestamp(data.index[exit_idx])
            scheduled_entries.setdefault(entry_date, []).append(
                {
                    **pick,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "entry_price": float(data.iloc[entry_idx]["Open"]),
                }
            )

        if not scheduled_entries:
            return PortfolioReplayResult(
                equity_curve=[],
                trades=[],
                open_positions=[],
                final_value=self.initial_capital,
                total_return=0.0,
                total_commission=0.0,
            )

        cash = self.initial_capital
        open_positions = []
        trades = []
        equity_curve = []
        ordered_dates = sorted(market_calendar)

        for current_date in ordered_dates:
            remaining_positions = []
            for position in open_positions:
                data = market_data[position["stock_code"]]
                close_price = self._price_asof(data, current_date, field="Close")
                if close_price is None:
                    remaining_positions.append(position)
                    continue
                if current_date >= position["exit_date"]:
                    execution_price = close_price * (1.0 - self.slippage_rate)
                    gross_amount = position["shares"] * execution_price
                    commission = self._commission(gross_amount, self.sell_commission_rate)
                    proceeds = gross_amount - commission
                    cash += proceeds
                    trades.append(
                        TradeRecord(
                            date=current_date,
                            stock_code=position["stock_code"],
                            type="sell",
                            price=float(execution_price),
                            shares=int(position["shares"]),
                            amount=proceeds,
                            gross_amount=gross_amount,
                            commission=commission,
                            entry_date=position["entry_date"],
                            signal_date=position["signal_date"],
                        )
                    )
                else:
                    remaining_positions.append(position)
            open_positions = remaining_positions

            entry_candidates = scheduled_entries.get(current_date, [])
            if entry_candidates:
                available_slots = max(self.top_n - len(open_positions), 0)
                if available_slots > 0 and cash > 0:
                    chosen_candidates = sorted(entry_candidates, key=lambda item: item["selection_score"], reverse=True)[:available_slots]
                    normalized_weights = self._normalize_pick_weights(chosen_candidates)
                    cash_snapshot = cash
                    spent_cash = 0.0
                    for candidate, weight in zip(chosen_candidates, normalized_weights):
                        raw_entry_price = float(candidate["entry_price"])
                        entry_price = raw_entry_price * (1.0 + self.slippage_rate)
                        budget = cash_snapshot * weight
                        denominator = entry_price * (1.0 + self.buy_commission_rate)
                        shares = int(budget / denominator) if denominator > 0 else 0
                        if shares <= 0:
                            continue
                        gross_amount = shares * entry_price
                        commission = self._commission(gross_amount, self.buy_commission_rate)
                        amount = gross_amount + commission
                        if spent_cash + amount > cash:
                            remaining_cash = cash - spent_cash
                            shares = int((remaining_cash - self.min_commission) / denominator) if denominator > 0 else 0
                            if shares <= 0:
                                continue
                            gross_amount = shares * entry_price
                            commission = self._commission(gross_amount, self.buy_commission_rate)
                            amount = gross_amount + commission
                            if spent_cash + amount > cash:
                                continue
                        spent_cash += amount
                        open_positions.append(
                            {
                                "stock_code": candidate["stock_code"],
                                "signal_date": pd.Timestamp(candidate["date"]),
                                "entry_date": current_date,
                                "exit_date": candidate["exit_date"],
                                "shares": shares,
                                "entry_price": entry_price,
                                "entry_commission": commission,
                                "pick_weight": weight,
                            }
                        )
                        trades.append(
                            TradeRecord(
                                date=current_date,
                                stock_code=candidate["stock_code"],
                                type="buy",
                                price=entry_price,
                                shares=shares,
                                amount=amount,
                                gross_amount=gross_amount,
                                commission=commission,
                                signal_date=pd.Timestamp(candidate["date"]),
                                planned_exit_date=candidate["exit_date"],
                            )
                        )
                    cash -= spent_cash

            equity = cash
            for position in open_positions:
                data = market_data[position["stock_code"]]
                close_price = self._price_asof(data, current_date, field="Close")
                if close_price is not None:
                    equity += position["shares"] * close_price
            equity_curve.append(
                EquityPoint(
                    date=current_date,
                    equity=equity,
                    cash=cash,
                    open_position_count=len(open_positions),
                )
            )

        final_value = equity_curve[-1].equity if equity_curve else self.initial_capital
        total_commission = sum(float(trade.commission or 0.0) for trade in trades)
        return PortfolioReplayResult(
            equity_curve=equity_curve,
            trades=trades,
            open_positions=open_positions,
            final_value=final_value,
            total_return=(final_value - self.initial_capital) / self.initial_capital * 100,
            total_commission=total_commission,
        )

    @staticmethod
    def _normalize_pick_weights(candidates):
        """对候选入场的 pick weight 重新归一化。"""
        if not candidates:
            return []
        weights = [max(float(item.get("pick_weight", 0.0) or 0.0), 0.0) for item in candidates]
        total = sum(weights)
        if total <= 0:
            equal_weight = 1.0 / len(candidates)
            return [equal_weight for _ in candidates]
        return [weight / total for weight in weights]

    @staticmethod
    def _price_asof(data, current_date, field="Close"):
        """获取某只证券在指定日期可用的最新价格。"""
        eligible = data.loc[data.index <= current_date]
        if eligible.empty or field not in eligible.columns:
            return None
        value = eligible.iloc[-1][field]
        return float(value) if pd.notna(value) else None

    def _commission(self, gross_amount, rate):
        """计算组合 replay 的成交费用。"""
        if gross_amount <= 0 or rate <= 0:
            return 0.0
        return max(gross_amount * rate, self.min_commission)

    # ---- Ranking 组件配置 (工业标准: 截面 Rank 标准化) ----
    # 每个组件在截面上转换为 0-100 百分位排名, 免疫厚尾和量纲差异.
    # direction: +1=越高越好, -1=越低越好
    # weight: 组件在复合得分中的相对权重
    _LIGHTGBM_COMPONENTS = [
        # (name,               direction, weight)
        ("win_rate_pct",         1,  0.15),
        ("latest_model_score",   1,  0.30),
        ("quality_score",        1,  0.15),
        ("risk_adjusted_score",  1,  0.06),
        ("signal_freshness",     1,  0.06),
        ("pb_value_score",       1,  0.06),
        ("overheat_penalty",    -1,  0.08),
        ("drawdown_penalty",    -1,  0.05),
        ("downtrend_penalty",   -1,  0.05),
        ("hot_sector_value",    -1,  0.02),
        ("trade_count",         -1,  0.02),
    ]

    _FACTOR_COMPONENTS = [
        # (name,                       direction, weight)
        ("current_signal_score",        1,  0.28),
        ("latest_expected_3m_score",    1,  0.24),
        ("latest_matrix_score",         1,  0.18),
        ("latest_regime_score",         1,  0.12),
        ("signal_freshness",            1,  0.08),
        ("sideways_penalty",           -1,  0.10),
    ]

    @staticmethod
    def _cross_sectional_rank(values):
        """截面百分位排名 (0-100).

        工业标准方法: 将任意分布的因子值映射到 [0,100] 均匀分布,
        天然免疫厚尾异常值和不同因子间的量纲差异.
        """
        n = len(values)
        if n <= 1:
            return [50.0] * n
        arr = np.asarray(values, dtype=float)
        nan_mask = np.isnan(arr)
        if nan_mask.any():
            arr = np.where(nan_mask, np.nanmedian(arr), arr)
        order = np.argsort(np.argsort(arr, kind="mergesort"), kind="mergesort")
        ranks = order.astype(float) / (n - 1) * 100.0
        return ranks.tolist()

    @staticmethod
    def _build_ranking_rows(pool_results):
        """工业级排名: 截面 Rank 标准化 + 加权复合.

        两阶段流程:
          1. 提取每只股票的原始组件值 (含 James-Stein 收缩等预处理)
          2. 每个组件做截面百分位排名 → 按 direction 翻转 → 加权求和
          3. 叠加 binary/categorical bonus (pre_breakout, low_price 等)

        相比线性加法:
          - 截面 Rank 免疫厚尾 —— 不再需要手动平方过热罚分
          - 所有组件贡献归一化到同等尺度 —— 量纲差异自动消除
          - 权重含义清晰: weight=0.25 ≡ 该组件最高贡献 25 分
        """
        lightgbm_results = []
        factor_results = []
        for r in pool_results:
            if r.get("selection_source") == "lightgbm_ranker":
                lightgbm_results.append(r)
            else:
                factor_results.append(r)

        rows = []
        if lightgbm_results:
            rows.extend(TopNPortfolioBuilder._build_lightgbm_rank_rows(lightgbm_results))
        if factor_results:
            rows.extend(TopNPortfolioBuilder._build_factor_rank_rows(factor_results))
        return rows

    @staticmethod
    def _build_lightgbm_rank_rows(results):
        """LightGBM 路径: 截面 Rank 排名."""
        # ---- Phase 1: 提取原始组件值 ----
        extracted = []
        for r in results:
            stock_code = r["stock_code"]
            setup_type = r.get("setup_type", "neutral")

            raw_win_rate = np.nan_to_num(r.get("win_rate", 50.0), nan=50.0)
            trade_count = int(r.get("trade_count", 60) or 60)
            prior_wr = 55.0
            cred = min(trade_count / 20.0, 1.0)
            win_rate_pct = prior_wr + cred * (raw_win_rate - prior_wr)

            latest_model_score = np.nan_to_num(r.get("latest_expected_3m_score", np.nan), nan=0.0)
            quality_score = np.nan_to_num(r.get("quality_score", np.nan), nan=50.0)
            risk_adj = np.nan_to_num(r.get("risk_adjusted_score", np.nan), nan=0.0)
            overheat = np.nan_to_num(r.get("overheat_penalty_score", np.nan), nan=0.0)
            drawdown = np.nan_to_num(r.get("drawdown_penalty_score", np.nan), nan=0.0)
            downtrend = np.nan_to_num(r.get("downtrend_penalty_score", np.nan), nan=0.0)
            hot_sector = np.nan_to_num(r.get("hot_sector_value_score", 50.0), nan=50.0)

            signal_age = int(r.get("signal_age_days",
                TopNPortfolioBuilder._compute_signal_age_days(r.get("latest_signal_date"))) or 0)
            explicit_freshness = r.get("signal_freshness_score")
            if explicit_freshness is not None and np.isfinite(explicit_freshness):
                signal_freshness = float(explicit_freshness)
            else:
                signal_freshness = max(0.0, 100.0 - max(signal_age, 0) * 8.0)

            pb_ratio = np.nan_to_num(r.get("pb_ratio", 0), nan=0.0)
            pb_value_score = float(np.clip(12.0 - pb_ratio, 0, 12)) / 12.0 * 100.0

            # setup-specific downtrend scaling (preserved as pre-rank adjustment)
            if setup_type == "bottom_rebound":
                dw_scale = 0.40
            elif setup_type == "pre_breakout":
                dw_scale = 0.60
            else:
                dw_scale = 1.00
            downtrend = downtrend * dw_scale

            pre_breakout_flag = 1.0 if setup_type == "pre_breakout" else 0.0

            extracted.append({
                "stock_code": stock_code,
                "selection_source": "lightgbm_ranker",
                "components": {
                    "win_rate_pct":        win_rate_pct,
                    "latest_model_score":  latest_model_score,
                    "quality_score":       quality_score,
                    "risk_adjusted_score": risk_adj,
                    "signal_freshness":    signal_freshness,
                    "pb_value_score":      pb_value_score,
                    "overheat_penalty":    overheat,
                    "drawdown_penalty":    drawdown,
                    "downtrend_penalty":   downtrend,
                    "hot_sector_value":    hot_sector,
                    "trade_count":         float(trade_count),
                },
                "pre_breakout_flag": pre_breakout_flag,
                "result": r,
                "win_rate_pct": win_rate_pct,
                "trade_count": trade_count,
                "signal_freshness_score": signal_freshness,
                "signal_age_days": signal_age,
                "setup_type": setup_type,
                "cluster_id": int(r.get("cluster_id", -1) or -1),
            })

        # ---- Phase 2: 截面 Rank 标准化 ----
        # P4b: Within-cluster quality normalization (QMJ-style)
        # Reference: Asness, Frazzini & Pedersen (2014) — quality z-scored
        # within industry groups to remove sector bias before portfolio ranking.
        _apply_within_cluster_quality_zscore(extracted)

        comp_cfg = TopNPortfolioBuilder._LIGHTGBM_COMPONENTS
        n = len(extracted)
        for name, direction, weight in comp_cfg:
            values = [e["components"][name] for e in extracted]
            ranks = TopNPortfolioBuilder._cross_sectional_rank(values)
            for i, e in enumerate(extracted):
                e["components"][name + "_rank"] = 100.0 - ranks[i] if direction < 0 else ranks[i]

        # ---- Phase 3: 加权复合 + 返回行 ----
        rows = []
        for e in extracted:
            r = e["result"]
            setup_type = e["setup_type"]

            score = sum(
                e["components"][name + "_rank"] * weight
                for name, _direction, weight in comp_cfg
            )
            score += e["pre_breakout_flag"] * 8.0  # binary bonus

            # Data quality penalty: PE ≤ 0 or PE > 500
            pe_val = r.get("pe_ratio")
            if pe_val is not None and np.isfinite(pe_val) and (pe_val <= 0 or pe_val > 500):
                score -= 30.0

            # P0: Liquidity hard filter — reject untradeable stocks (halted / zero turnover)
            if not r.get("liquidity_ok", True):
                score -= 100.0

            # P1: Nonlinear overheat penalty — extreme speculation signals crash risk
            # Reference: Barroso & Santa-Clara (2015) volatility-managed portfolios
            overheat_val = np.nan_to_num(r.get("overheat_penalty_score", np.nan), nan=0.0)
            if overheat_val > 80:
                score -= 25.0
            elif overheat_val > 60:
                score -= 10.0
            if bool(r.get("startup_candidate", False)) or r.get("trend_state") == "startup":
                score += 12.0
            if r.get("trend_state") == "downtrend":
                score -= 12.0

            backtest = r.get("backtest") or {}
            rows.append({
                "stock_code": e["stock_code"],
                "ranking_score": score,
                "expected_3m_score": r["latest_expected_3m_score"],
                "matrix_score": r["latest_matrix_score"],
                "regime_score": r.get("latest_regime_score"),
                "quality_score": r.get("quality_score"),
                "quality_data_coverage": r.get("quality_data_coverage"),
                "quality_missing_fields": r.get("quality_missing_fields"),
                "quality_peer_group": r.get("quality_peer_group"),
                "entry_type": r["latest_entry_type"],
                "signal_tier": r.get("latest_signal_tier"),
                "latest_signal_date": r.get("latest_signal_date"),
                "current_signal_active": r.get("current_signal_active", False),
                "current_signal_actionable": r.get("current_signal_actionable", False),
                "current_signal_score": r.get("current_signal_score"),
                "avg_forward_return_60_signal": np.nan_to_num(r.get("avg_forward_return_60_signal", 0), nan=0),
                "avg_forward_return_60_watch": np.nan_to_num(r.get("avg_forward_return_60_watch", 0), nan=0),
                "backtest_return": backtest.get("total_return", 0),
                "win_rate": backtest.get("win_rate", 0),
                "trade_count": backtest.get("total_trades", 0),
                "factor_set": r.get("factor_set"),
                "selection_source": "lightgbm_ranker",
                "setup_type": setup_type,
                "setup_score": float(r.get("setup_score", 0.0) or 0.0),
                "sideways_penalty": float(r.get("sideways_penalty", 0.0) or 0.0),
                "low_price_candidate": bool(r.get("low_price_candidate", setup_type != "sideways")),
                "liquidity_ok": bool(r.get("liquidity_ok", True)),
                "signal_freshness_score": e["signal_freshness_score"],
                "signal_age_days": e["signal_age_days"],
                "factor_explanation": r.get("factor_explanation", {}),
                "risk_adjusted_score": r.get("risk_adjusted_score"),
                "latest_risk_score": r.get("latest_risk_score"),
                "drawdown_penalty_score": r.get("drawdown_penalty_score"),
                "recent_drawdown": r.get("recent_drawdown"),
                "recent_volatility": r.get("recent_volatility"),
                "startup_score": r.get("startup_score"),
                "startup_candidate": bool(r.get("startup_candidate", False)),
                "startup_candidate_score": r.get("startup_candidate_score"),
                "overheat_penalty_score": r.get("overheat_penalty_score"),
                "downtrend_penalty_score": r.get("downtrend_penalty_score"),
                "trend_state": r.get("trend_state"),
                "hot_sector_value_score": r.get("hot_sector_value_score"),
                "pe_ratio": r.get("pe_ratio"),
                "pb_ratio": r.get("pb_ratio"),
                "cluster_rps": r.get("cluster_rps"),
                "cluster_id": r.get("cluster_id"),
                "market_cap": r.get("market_cap"),
                "industry_l1": r.get("industry_l1"),
                "industry_l2": r.get("industry_l2"),
                "industry_l3": r.get("industry_l3"),
                "industry_source": r.get("industry_source"),
                "industry_updated_at": r.get("industry_updated_at"),
                "instrument_type": r.get("instrument_type"),
                "is_fund_like": normalize_bool(r.get("is_fund_like"), default=False),
                "tradable_flag": normalize_bool(r.get("tradable_flag"), default=True),
                "value_score": r.get("value_score"),
                "valuation_score": r.get("valuation_score"),
                "valuation_metric_used": r.get("valuation_metric_used"),
                "valuation_data_coverage": r.get("valuation_data_coverage"),
                "valuation_peer_group": r.get("valuation_peer_group"),
                "data_coverage_score": r.get("data_coverage_score"),
                "data_missing_fields": r.get("data_missing_fields"),
                "require_complete_data_for_selection": bool(r.get("require_complete_data_for_selection", False)),
            })

        return rows

    @staticmethod
    def _compute_selection_eligibility(item):
        """Compute hard selection eligibility and data coverage for final holdings.

        Incorporates findings from:
        - Chekhlov, Uryasev & Zabarankin (2005): drawdown >30% → hard filter
        - Ai, Liu & Lin (2024): weak signals degrade model performance
        - Bryzgalova et al. (2022): missing financial data is non-random bias
        """
        reasons = []
        coverage_fields = {
            "current_signal_actionable": item.get("current_signal_actionable"),
            "liquidity_ok": item.get("liquidity_ok", True),
            "market_cap": item.get("market_cap"),
            "latest_risk_score": item.get("latest_risk_score"),
            "risk_adjusted_score": item.get("risk_adjusted_score"),
            "drawdown_penalty_score": item.get("drawdown_penalty_score"),
            "industry_l1": item.get("industry_l1"),
        }
        missing_fields = []
        for field, value in coverage_fields.items():
            if value is None:
                missing_fields.append(field)
            elif isinstance(value, float) and np.isnan(value):
                missing_fields.append(field)
            elif isinstance(value, str) and not value.strip():
                missing_fields.append(field)

        data_coverage_score = max(0.0, 100.0 * (1.0 - len(missing_fields) / max(len(coverage_fields), 1)))

        if not item.get("current_signal_active"):
            reasons.append("signal_not_active")
        if not item.get("current_signal_actionable"):
            reasons.append("signal_not_actionable")
        if not item.get("liquidity_ok", True):
            reasons.append("liquidity_not_ok")
        if item.get("is_fund_like", False):
            reasons.append("fund_like_instrument")
        if not item.get("tradable_flag", True):
            reasons.append("not_tradable")
        if item.get("setup_type") == "sideways":
            reasons.append("sideways_setup")
        explicit_coverage = item.get("data_coverage_score")
        if explicit_coverage is not None and np.isfinite(explicit_coverage):
            data_coverage_score = float(explicit_coverage)

        if data_coverage_score < 50:
            reasons.append(f"low_data_coverage({data_coverage_score / 100.0:.0%})")
        if item.get("require_complete_data_for_selection", False) and data_coverage_score < 70:
            reasons.append("insufficient_data_coverage")

        freshness = TopNPortfolioBuilder._float_or_default(item.get("signal_freshness_score"), 100.0)
        if freshness < 35:
            reasons.append(f"stale_signal(freshness={freshness:.0f})")

        # Ai, Liu & Lin (2024): weak signal tier → exclude from selected
        signal_tier = str(item.get("signal_tier", "")).strip().lower()
        if signal_tier == "weak":
            reasons.append("weak_signal_tier")

        overheat = TopNPortfolioBuilder._float_or_default(item.get("overheat_penalty_score"), 0.0)
        if overheat >= 85:
            reasons.append(f"overheated({overheat:.0f})")

        # Chekhlov et al. (2005): CDaR — extreme drawdown → near-certain underperformance
        drawdown = TopNPortfolioBuilder._float_or_default(item.get("drawdown_penalty_score"), 0.0)
        if drawdown >= 30:
            reasons.append(f"excessive_drawdown({drawdown:.0f})")

        downtrend = TopNPortfolioBuilder._float_or_default(item.get("downtrend_penalty_score"), 0.0)
        if downtrend >= 50:
            reasons.append(f"severe_downtrend({downtrend:.0f})")

        # Bryzgalova et al. (2022): missing financials → use with caution
        quality_cov = TopNPortfolioBuilder._float_or_default(item.get("quality_data_coverage"), 1.0)
        if quality_cov < 0.3:
            reasons.append(f"low_quality_coverage({quality_cov:.0%})")

        # PE sanity (PE > 300 = near-zero earnings → unreliable valuation)
        pe_val = item.get("pe_ratio")
        if pe_val is not None and np.isfinite(pe_val):
            pe_float = float(pe_val)
            if pe_float <= 0:
                reasons.append(f"negative_pe({pe_float:.1f})")
            elif pe_float > 300:
                reasons.append(f"extreme_pe({pe_float:.0f})")

        pb_val = item.get("pb_ratio")
        if pb_val is not None and np.isfinite(pb_val):
            pb_float = float(pb_val)
            if pb_float <= 0:
                reasons.append(f"negative_pb({pb_float:.1f})")
            elif pb_float > 50:
                reasons.append(f"extreme_pb({pb_float:.0f})")

        reasons = list(dict.fromkeys(reasons))

        return {
            "selection_eligible": not reasons,
            "eligibility_reasons": reasons,
            "data_coverage_score": float(data_coverage_score),
            "data_missing_fields": item.get("data_missing_fields") or missing_fields,
        }

    @staticmethod
    def _float_or_default(value, default):
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
    def _build_factor_rank_rows(results):
        """Factor 验证路径: 截面 Rank 排名."""
        extracted = []
        for r in results:
            stock_code = r["stock_code"]
            setup_type = r.get("setup_type", "neutral")

            signal_age = int(r.get("signal_age_days",
                TopNPortfolioBuilder._compute_signal_age_days(r.get("latest_signal_date"))) or 0)
            explicit_freshness = r.get("signal_freshness_score")
            if explicit_freshness is not None and np.isfinite(explicit_freshness):
                signal_freshness = float(explicit_freshness)
            else:
                signal_freshness = max(0.0, 100.0 - max(signal_age, 0) * 8.0)

            sideways_penalty = float(r.get("sideways_penalty", 0.0) or 0.0)
            active = r.get("current_signal_active") and r.get("current_signal_actionable")

            extracted.append({
                "stock_code": stock_code,
                "selection_source": r.get("selection_source"),
                "components": {
                    "current_signal_score":     np.nan_to_num(r.get("current_signal_score", np.nan), nan=0),
                    "latest_expected_3m_score": np.nan_to_num(r.get("latest_expected_3m_score", np.nan), nan=0),
                    "latest_matrix_score":      np.nan_to_num(r.get("latest_matrix_score", np.nan), nan=0),
                    "latest_regime_score":      np.nan_to_num(r.get("latest_regime_score", np.nan), nan=0),
                    "signal_freshness":         signal_freshness,
                    "sideways_penalty":         sideways_penalty,
                },
                "active": active,
                "setup_type": setup_type,
                "setup_score": float(r.get("setup_score", 0.0) or 0.0),
                "signal_freshness_score": signal_freshness,
                "signal_age_days": signal_age,
                "result": r,
            })

        comp_cfg = TopNPortfolioBuilder._FACTOR_COMPONENTS
        n = len(extracted)
        for name, direction, weight in comp_cfg:
            values = [e["components"][name] for e in extracted]
            ranks = TopNPortfolioBuilder._cross_sectional_rank(values)
            for i, e in enumerate(extracted):
                e["components"][name + "_rank"] = 100.0 - ranks[i] if direction < 0 else ranks[i]

        rows = []
        for e in extracted:
            r = e["result"]
            setup_type = e["setup_type"]

            score = sum(
                e["components"][name + "_rank"] * weight
                for name, _direction, weight in comp_cfg
            )
            # categorical bonuses
            setup_bonus = {"pre_breakout": 18.0, "bottom_rebound": 20.0, "neutral": 0.0, "sideways": -16.0}.get(setup_type, 0.0)
            low_price_bonus = 8.0 if bool(r.get("low_price_candidate", setup_type != "sideways")) else -22.0
            active_bonus = 100.0 if e["active"] else 0.0

            score += setup_bonus + low_price_bonus + active_bonus

            backtest = r.get("backtest") or {}
            rows.append({
                "stock_code": e["stock_code"],
                "ranking_score": score,
                "expected_3m_score": r["latest_expected_3m_score"],
                "matrix_score": r["latest_matrix_score"],
                "regime_score": r.get("latest_regime_score"),
                "entry_type": r["latest_entry_type"],
                "signal_tier": r.get("latest_signal_tier"),
                "latest_signal_date": r.get("latest_signal_date"),
                "current_signal_active": r.get("current_signal_active", False),
                "current_signal_actionable": r.get("current_signal_actionable", False),
                "current_signal_score": r.get("current_signal_score"),
                "avg_forward_return_60_signal": np.nan_to_num(r.get("avg_forward_return_60_signal", 0), nan=0),
                "avg_forward_return_60_watch": np.nan_to_num(r.get("avg_forward_return_60_watch", 0), nan=0),
                "backtest_return": backtest.get("total_return", 0),
                "win_rate": backtest.get("win_rate", 0),
                "trade_count": backtest.get("total_trades", 0),
                "factor_set": r.get("factor_set"),
                "selection_source": e["selection_source"],
                "setup_type": setup_type,
                "setup_score": e["setup_score"],
                "sideways_penalty": e["components"]["sideways_penalty"],
                "low_price_candidate": bool(r.get("low_price_candidate", setup_type != "sideways")),
                "signal_freshness_score": e["signal_freshness_score"],
                "signal_age_days": e["signal_age_days"],
                "factor_explanation": r.get("factor_explanation", {}),
                "industry_l1": r.get("industry_l1"),
                "industry_l2": r.get("industry_l2"),
                "industry_l3": r.get("industry_l3"),
                "industry_source": r.get("industry_source"),
                "industry_updated_at": r.get("industry_updated_at"),
                "instrument_type": r.get("instrument_type"),
                "is_fund_like": normalize_bool(r.get("is_fund_like"), default=False),
                "tradable_flag": normalize_bool(r.get("tradable_flag"), default=True),
                "liquidity_ok": bool(r.get("liquidity_ok", True)),
                "market_cap": r.get("market_cap"),
                "pe_ratio": r.get("pe_ratio"),
                "pb_ratio": r.get("pb_ratio"),
                "quality_score": r.get("quality_score"),
                "quality_data_coverage": r.get("quality_data_coverage"),
                "quality_missing_fields": r.get("quality_missing_fields"),
                "quality_peer_group": r.get("quality_peer_group"),
                "value_score": r.get("value_score"),
                "valuation_score": r.get("valuation_score"),
                "valuation_metric_used": r.get("valuation_metric_used"),
                "valuation_data_coverage": r.get("valuation_data_coverage"),
                "valuation_peer_group": r.get("valuation_peer_group"),
                "risk_adjusted_score": r.get("risk_adjusted_score"),
                "latest_risk_score": r.get("latest_risk_score"),
                "drawdown_penalty_score": r.get("drawdown_penalty_score"),
                "overheat_penalty_score": r.get("overheat_penalty_score"),
                "downtrend_penalty_score": r.get("downtrend_penalty_score"),
                "trend_state": r.get("trend_state"),
                "recent_drawdown": r.get("recent_drawdown"),
                "recent_volatility": r.get("recent_volatility"),
                "data_coverage_score": r.get("data_coverage_score"),
                "data_missing_fields": r.get("data_missing_fields"),
                "require_complete_data_for_selection": bool(r.get("require_complete_data_for_selection", False)),
            })

        return rows

    @staticmethod
    def _compute_signal_age_days(latest_signal_date):
        if latest_signal_date is None or pd.isna(latest_signal_date):
            return 999
        signal_date = pd.Timestamp(latest_signal_date)
        reference_date = pd.Timestamp.now("UTC").tz_localize(None).normalize()
        return max(int((reference_date - signal_date.normalize()).days), 0)

    @staticmethod
    def _double_sort_select(ranking: list[dict], top_n: int) -> list[dict] | None:
        """QMJ Double Sort: size x quality portfolio construction.

        Reference: Asness, Frazzini & Pedersen (2014) — double sort on size
        and quality produces the QMJ factor. We adapt it for long-only top-N
        selection.

        1. Split by market_cap median -> Big / Small
        2. Within each, split by quality_score tercile -> High / Mid / Low
        3. Select preferentially from: Big+High, Small+High, Big+Mid, ...

        Returns selected list or None if insufficient data for double sort.
        """
        valid = [
            r for r in ranking
            if (mc := r.get("market_cap")) is not None
            and np.isfinite(mc) and mc > 0
            and (qs := r.get("quality_score")) is not None
            and np.isfinite(qs)
        ]
        if len(valid) < max(top_n * 2, 20):
            return None

        caps = [r["market_cap"] for r in valid]
        cap_median = np.median(caps)
        big = [r for r in valid if r["market_cap"] >= cap_median]
        small = [r for r in valid if r["market_cap"] < cap_median]

        def _quality_terciles(group):
            if len(group) < 3:
                return {"High": group, "Mid": [], "Low": []}
            qualities = [r["quality_score"] for r in group]
            q33 = float(np.percentile(qualities, 33.33))
            q67 = float(np.percentile(qualities, 66.67))
            return {
                "High": [r for r in group if r["quality_score"] >= q67],
                "Mid": [r for r in group if q33 <= r["quality_score"] < q67],
                "Low": [r for r in group if r["quality_score"] < q33],
            }

        big_q = _quality_terciles(big)
        small_q = _quality_terciles(small)

        # Sort within each bucket by ranking_score
        for bucket in [big_q["High"], small_q["High"], big_q["Mid"],
                       small_q["Mid"], big_q["Low"], small_q["Low"]]:
            bucket.sort(key=lambda r: r.get("ranking_score", 0), reverse=True)

        selected = []
        for bucket in [big_q["High"], small_q["High"], big_q["Mid"],
                       small_q["Mid"], big_q["Low"], small_q["Low"]]:
            needed = top_n - len(selected)
            if needed <= 0:
                break
            selected.extend(bucket[:needed])

        return selected if len(selected) >= top_n else None

    @staticmethod
    def _summarize_low_price_setup(data):
        return summarize_low_price_setup(data)

    @staticmethod
    def _collect_signal_rows(result):
        stock_code = result["stock_code"]
        buy_signals = result.get("buy_signals")
        if buy_signals is None or buy_signals.empty:
            return []

        rows = []
        for _, signal in buy_signals.sort_values("date").iterrows():
            if pd.isna(signal.get("forward_return_60")):
                continue
            rows.append(
                {
                    "date": signal["date"],
                    "stock_code": stock_code,
                    "expected_3m_score": float(signal.get("expected_3m_score", 0)),
                    "matrix_score": float(signal.get("matrix_score", 0)),
                    "regime_score": float(signal.get("regime_score", 0) or 0),
                    "signal_strength": float(signal.get("signal_strength", 0)),
                    "risk_score": float(signal.get("risk_score", 0)),
                    "signal_tier": signal.get("signal_tier", "strong"),
                    "actionable": bool(signal.get("actionable", True)),
                    "setup_type": result.get("setup_type", "neutral"),
                    "setup_score": float(result.get("setup_score", 0.0) or 0.0),
                    "sideways_penalty": float(result.get("sideways_penalty", 0.0) or 0.0),
                    "low_price_candidate": bool(result.get("low_price_candidate", True)),
                    "signal_freshness_score": float(result.get("signal_freshness_score", 100.0) or 0.0),
                    "signal_age_days": int(result.get("signal_age_days", 0) or 0),
                    "forward_return_20": float(signal.get("forward_return_20", 0) or 0),
                    "forward_return_40": float(signal.get("forward_return_40", 0) or 0),
                    "forward_return_60": float(signal.get("forward_return_60", 0) or 0),
                    "forward_max_drawdown_60": float(signal.get("forward_max_drawdown_60", 0) or 0),
                    "entry_type": signal.get("entry_type"),
                    "holding_horizon": int(signal.get("holding_horizon", 60)),
                }
            )
        return rows
