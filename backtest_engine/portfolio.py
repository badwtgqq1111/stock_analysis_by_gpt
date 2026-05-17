#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""组合层最小入口。"""

import numpy as np
import pandas as pd

from backtest_engine.models import EquityPoint, PortfolioBuildResult, PortfolioReplayResult, TradeRecord
from factor_engine.signals import summarize_low_price_setup


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

        ranking = []
        signal_rows = []
        for result in pool_results:
            ranking.append(self._build_ranking_row(result))
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

        preferred_actionable = [
            item for item in ranking
            if item.get("current_signal_active")
            and item.get("current_signal_actionable")
            and item.get("low_price_candidate", True)
            and item.get("signal_freshness_score", 100) >= 40
            and item.get("setup_type") in {"pre_breakout", "bottom_rebound"}
        ]
        active_actionable = [
            item for item in ranking
            if item.get("current_signal_active")
            and item.get("current_signal_actionable")
            and item.get("signal_freshness_score", 100) >= 35
            and item.get("setup_type") != "sideways"
        ]
        fallback_candidates = [
            item for item in ranking
            if item.get("signal_tier") != "weak"
            and item.get("signal_freshness_score", 100) >= 30
            and item.get("setup_type") != "sideways"
        ]
        watchlist = [
            dict(item) for item in ranking
            if item.get("setup_type") in {"pre_breakout", "bottom_rebound"} and item.get("signal_tier") == "weak"
        ][: self.top_n]
        if not watchlist:
            watchlist = [dict(item) for item in ranking if item.get("signal_tier") == "weak"][: self.top_n]

        selected = (
            preferred_actionable[: self.top_n]
            if preferred_actionable
            else (
                active_actionable[: self.top_n]
                if active_actionable
                else (fallback_candidates[: self.top_n] if fallback_candidates else ranking[: self.top_n])
            )
        )
        selected = [dict(item) for item in selected]
        self._apply_weights(selected)

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

    def _apply_weights(self, selected):
        """对选中的标的分配权重与资金。"""
        if not selected:
            return

        if self.weighting_mode == "equal_weight":
            weight = 1.0 / len(selected)
            for item in selected:
                item["portfolio_weight"] = weight
                item["allocated_capital"] = self.initial_capital * weight
            return

        scores = []
        for item in selected:
            score = float(item.get("ranking_score", 0) or 0)
            scores.append(max(score, 0.0))

        total_score = sum(scores)
        if total_score <= 0:
            equal_weight = 1.0 / len(selected)
            for item in selected:
                item["portfolio_weight"] = equal_weight
                item["allocated_capital"] = self.initial_capital * equal_weight
            return

        for item, score in zip(selected, scores):
            weight = score / total_score
            item["portfolio_weight"] = weight
            item["allocated_capital"] = self.initial_capital * weight

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

    @staticmethod
    def _build_ranking_row(result):
        stock_code = result["stock_code"]
        backtest = result.get("backtest") or {}
        avg_forward_return_60_signal = np.nan_to_num(result.get("avg_forward_return_60_signal", 0), nan=0)
        avg_forward_return_60_watch = np.nan_to_num(result.get("avg_forward_return_60_watch", 0), nan=0)
        setup_type = result.get("setup_type", "neutral")
        setup_score = float(result.get("setup_score", 0.0) or 0.0)
        sideways_penalty = float(result.get("sideways_penalty", 0.0) or 0.0)
        low_price_candidate = bool(result.get("low_price_candidate", setup_type != "sideways"))
        signal_age_days = int(
            result.get("signal_age_days", TopNPortfolioBuilder._compute_signal_age_days(result.get("latest_signal_date"))) or 0
        )
        signal_freshness_score = float(
            result.get(
                "signal_freshness_score",
                max(0.0, 100.0 - max(signal_age_days, 0) * 8.0),
            )
            or 0.0
        )
        active_bonus = 100 if result.get("current_signal_active") and result.get("current_signal_actionable") else 0
        setup_type_bonus = {
            "pre_breakout": 18.0,
            "bottom_rebound": 20.0,
            "neutral": 0.0,
            "sideways": -16.0,
        }.get(setup_type, 0.0)
        low_price_bonus = 8.0 if low_price_candidate else -22.0
        ranking_score = (
            active_bonus
            + np.nan_to_num(result.get("current_signal_score", np.nan), nan=0) * 0.50
            + np.nan_to_num(result.get("latest_expected_3m_score", np.nan), nan=0) * 0.20
            + np.nan_to_num(result.get("latest_matrix_score", np.nan), nan=0) * 0.15
            + np.nan_to_num(result.get("latest_regime_score", np.nan), nan=0) * 0.15
            + setup_type_bonus
            + setup_score * 0.18
            + signal_freshness_score * 0.10
            + low_price_bonus
            - sideways_penalty * 0.90
        )
        return {
            "stock_code": stock_code,
            "ranking_score": ranking_score,
            "expected_3m_score": result["latest_expected_3m_score"],
            "matrix_score": result["latest_matrix_score"],
            "regime_score": result.get("latest_regime_score"),
            "entry_type": result["latest_entry_type"],
            "signal_tier": result.get("latest_signal_tier"),
            "latest_signal_date": result.get("latest_signal_date"),
            "current_signal_active": result.get("current_signal_active", False),
            "current_signal_actionable": result.get("current_signal_actionable", False),
            "current_signal_score": result.get("current_signal_score"),
            "avg_forward_return_60_signal": avg_forward_return_60_signal,
            "avg_forward_return_60_watch": avg_forward_return_60_watch,
            "backtest_return": backtest.get("total_return", 0),
            "win_rate": backtest.get("win_rate", 0),
            "trade_count": backtest.get("total_trades", 0),
            "factor_set": result.get("factor_set"),
            "selection_source": result.get("selection_source"),
            "setup_type": setup_type,
            "setup_score": setup_score,
            "sideways_penalty": sideways_penalty,
            "low_price_candidate": low_price_candidate,
            "signal_freshness_score": signal_freshness_score,
            "signal_age_days": signal_age_days,
            "factor_explanation": result.get("factor_explanation", {}),
        }

    @staticmethod
    def _compute_signal_age_days(latest_signal_date):
        if latest_signal_date is None or pd.isna(latest_signal_date):
            return 999
        signal_date = pd.Timestamp(latest_signal_date)
        reference_date = pd.Timestamp.now("UTC").tz_localize(None).normalize()
        return max(int((reference_date - signal_date.normalize()).days), 0)

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
