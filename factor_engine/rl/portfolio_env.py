"""Gym-like offline portfolio environment.

The environment is deliberately dependency-free so it can be used in tests,
notebooks, or later wrapped by Gymnasium/Stable-Baselines.  The action is a
vector of target weights for the assets present on the current date.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.rl.reward import portfolio_reward


class PortfolioEnv:
    """Offline portfolio construction environment for daily alpha panels."""

    def __init__(
        self,
        panel: pd.DataFrame,
        *,
        date_col: str = "trade_date",
        asset_col: str = "stock_code",
        score_col: str = "ranking_score",
        return_col: str = "forward_return_20",
        cost_bps_col: str = "expected_transaction_cost_bps",
        max_weight: float = 0.08,
    ):
        if panel is None or panel.empty:
            raise ValueError("panel is empty")
        self.panel = panel.copy()
        if date_col not in self.panel.columns:
            self.panel = self.panel.reset_index().rename(columns={self.panel.index.name or "index": date_col})
        self.panel[date_col] = pd.to_datetime(self.panel[date_col], errors="coerce")
        self.panel.sort_values([date_col, asset_col], inplace=True)
        self.date_col = date_col
        self.asset_col = asset_col
        self.score_col = score_col
        self.return_col = return_col
        self.cost_bps_col = cost_bps_col
        self.max_weight = float(max_weight)
        self.dates = list(self.panel[date_col].dropna().sort_values().unique())
        self.step_idx = 0
        self.current_weights: dict[str, float] = {}
        self.equity = 1.0
        self.peak_equity = 1.0

    def reset(self):
        self.step_idx = 0
        self.current_weights = {}
        self.equity = 1.0
        self.peak_equity = 1.0
        return self._observation()

    def _day_frame(self) -> pd.DataFrame:
        date = self.dates[self.step_idx]
        return self.panel[self.panel[self.date_col] == date].copy()

    def _observation(self) -> dict:
        day = self._day_frame()
        return {
            "date": pd.Timestamp(self.dates[self.step_idx]),
            "assets": day[self.asset_col].astype(str).tolist(),
            "scores": pd.to_numeric(day.get(self.score_col), errors="coerce").fillna(0.0).to_numpy(dtype=float),
            "current_weights": np.array(
                [self.current_weights.get(str(code), 0.0) for code in day[self.asset_col].astype(str)],
                dtype=float,
            ),
            "features": day.drop(columns=[self.date_col], errors="ignore").reset_index(drop=True),
        }

    @staticmethod
    def expert_policy(observation: dict, *, top_n: int = 10, max_weight: float = 0.08) -> np.ndarray:
        scores = np.asarray(observation["scores"], dtype=float)
        weights = np.zeros_like(scores, dtype=float)
        if scores.size == 0:
            return weights
        top_n = max(1, min(int(top_n), len(scores)))
        top_idx = np.argsort(scores)[-top_n:]
        positive = np.maximum(scores[top_idx], 0.0)
        if positive.sum() <= 0:
            weights[top_idx] = 1.0 / top_n
        else:
            weights[top_idx] = positive / positive.sum()
        return np.clip(weights, 0.0, max_weight)

    def step(self, action):
        day = self._day_frame()
        assets = day[self.asset_col].astype(str).tolist()
        target = np.asarray(action, dtype=float)
        if target.shape[0] != len(assets):
            raise ValueError("action length must match observation assets")
        target = np.clip(target, 0.0, self.max_weight)
        if target.sum() > 1.0:
            target = target / target.sum()
        prev = np.array([self.current_weights.get(code, 0.0) for code in assets], dtype=float)
        turnover = float(np.abs(target - prev).sum())
        returns = pd.to_numeric(day.get(self.return_col), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        costs_bps = pd.to_numeric(day.get(self.cost_bps_col), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        gross_return = float(np.dot(target, returns))
        transaction_cost = float(np.dot(np.abs(target - prev), costs_bps) / 10000.0)
        self.equity *= 1.0 + gross_return - transaction_cost
        self.peak_equity = max(self.peak_equity, self.equity)
        drawdown = max(0.0, 1.0 - self.equity / self.peak_equity)
        concentration = float(np.sum(target ** 2))
        reward = portfolio_reward(
            gross_return,
            transaction_cost=transaction_cost,
            turnover=turnover,
            drawdown=drawdown,
            concentration=concentration,
        )
        self.current_weights = {code: float(w) for code, w in zip(assets, target) if w > 0}
        self.step_idx += 1
        done = self.step_idx >= len(self.dates)
        info = {
            "gross_return": gross_return,
            "transaction_cost": transaction_cost,
            "turnover": turnover,
            "drawdown": drawdown,
            "concentration": concentration,
            "equity": self.equity,
        }
        return (None if done else self._observation()), reward, done, info


def evaluate_policy(panel: pd.DataFrame, policy_fn, *, max_weight: float = 0.08, **env_kwargs) -> dict:
    """Evaluate a policy function on the offline environment."""
    env = PortfolioEnv(panel, max_weight=max_weight, **env_kwargs)
    obs = env.reset()
    rewards = []
    infos = []
    done = False
    while not done:
        action = policy_fn(obs)
        obs, reward, done, info = env.step(action)
        rewards.append(float(reward))
        infos.append(info)
    return {
        "steps": len(rewards),
        "total_reward": float(np.sum(rewards)),
        "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
        "final_equity": float(infos[-1]["equity"]) if infos else 1.0,
        "avg_turnover": float(np.mean([item["turnover"] for item in infos])) if infos else 0.0,
        "max_drawdown": float(max([item["drawdown"] for item in infos] or [0.0])),
    }
