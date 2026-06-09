"""Execution baseline simulator for TWAP/VWAP/POV/IS/AC schedules."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExecutionOrder:
    stock_code: str
    side: str
    quantity: float
    arrival_price: float


class ExecutionSimulator:
    """Simulate baseline order schedules on minute/daily volume bars."""

    def __init__(self, bars: pd.DataFrame):
        if bars is None or bars.empty:
            raise ValueError("bars is empty")
        self.bars = bars.copy().reset_index(drop=True)
        if "volume" not in self.bars.columns:
            self.bars["volume"] = 1.0
        if "price" not in self.bars.columns:
            if "Close" in self.bars.columns:
                self.bars["price"] = self.bars["Close"]
            else:
                self.bars["price"] = 1.0

    def schedule(self, order: ExecutionOrder, *, algo: str = "twap", max_pov: float = 0.10, risk_aversion: float = 1.0) -> pd.DataFrame:
        algo = str(algo or "twap").lower()
        n = len(self.bars)
        qty = max(float(order.quantity), 0.0)
        if qty <= 0:
            return pd.DataFrame()
        volume = pd.to_numeric(self.bars["volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
        if algo == "twap":
            child = np.repeat(qty / n, n)
        elif algo == "vwap":
            weights = volume / max(float(volume.sum()), 1.0)
            child = weights.to_numpy(dtype=float) * qty
        elif algo == "pov":
            child = np.minimum(volume.to_numpy(dtype=float) * float(max_pov), qty)
            total = child.sum()
            if total > 0:
                child *= qty / total
        elif algo in {"is", "implementation_shortfall"}:
            decay = np.exp(-np.linspace(0.0, 2.0 * float(risk_aversion), n))
            child = decay / decay.sum() * qty
        elif algo in {"ac", "almgren_chriss"}:
            curve = np.sinh(np.linspace(float(risk_aversion), 0.05, n))
            child = curve / curve.sum() * qty
        else:
            raise ValueError(f"unsupported execution algo: {algo}")
        return self._fills(order, child, algo)

    def _fills(self, order: ExecutionOrder, child_qty: np.ndarray, algo: str) -> pd.DataFrame:
        prices = pd.to_numeric(self.bars["price"], errors="coerce").fillna(float(order.arrival_price))
        volume = pd.to_numeric(self.bars["volume"], errors="coerce").fillna(1.0).clip(lower=1.0)
        side = str(order.side).lower()
        direction = 1.0 if side == "buy" else -1.0
        participation = child_qty / volume.to_numpy(dtype=float)
        impact_bps = 2.0 + 60.0 * np.sqrt(np.clip(participation, 0.0, None))
        fill_price = prices.to_numpy(dtype=float) * (1.0 + direction * impact_bps / 10000.0)
        frame = pd.DataFrame({
            "stock_code": order.stock_code,
            "algo": algo,
            "side": side,
            "slice": np.arange(1, len(child_qty) + 1),
            "target_qty": child_qty,
            "market_volume": volume,
            "participation_rate": participation,
            "arrival_price": float(order.arrival_price),
            "fill_price": fill_price,
            "impact_bps": impact_bps,
        })
        frame["notional"] = frame["target_qty"] * frame["fill_price"]
        frame["implementation_shortfall_bps"] = (
            (frame["fill_price"] / float(order.arrival_price) - 1.0) * 10000.0 * direction
        )
        return frame
