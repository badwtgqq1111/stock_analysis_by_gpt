"""Exact 71-formula Alpha101 panel baseline (FactorBench-compatible).

Namespace: ``alpha101_exact71``.  This is intentionally separate from the
legacy single-stock ``factor_engine.expressions.alpha101`` proxy library.

The formulas and operator conventions are ported from FactorBench-Release at
commit ``8dbc5b42e005a4038dd52e4790b30b467a5c1575``
(``factor_mining/methods/alpha101/alpha101.py``, MIT).  The 30 formulas that
need industry neutralisation are absent; the public benchmark's executable set
is also 71.  ``_safe_corr`` still converts undefined rolling correlations to
zero, exactly as the benchmark does, and that deviation is recorded in the
metadata rather than hidden.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

REFERENCE = 'https://arxiv.org/abs/1601.00991'
VENDOR_COMMIT = '8dbc5b42e005a4038dd52e4790b30b467a5c1575'
NAMESPACE = 'alpha101_exact71'


def rank(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True)


def delay(df: pd.DataFrame, period: int = 1) -> pd.DataFrame:
    return df.shift(period)


def delta(df: pd.DataFrame, period: int = 1) -> pd.DataFrame:
    return df.diff(period)


def ts_sum(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).sum()


def sma(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).mean()


def stddev(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=2).std()


def correlation(x: pd.DataFrame, y: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return x.rolling(window, min_periods=min(3, window)).corr(y)


def covariance(x: pd.DataFrame, y: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return x.rolling(window, min_periods=3).cov(y)


def ts_rank(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).rank(pct=True)


def ts_min(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).min()


def ts_max(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).max()


def ts_argmax(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).apply(np.argmax, raw=True) + 1


def ts_argmin(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).apply(np.argmin, raw=True) + 1


def product(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    return df.rolling(window, min_periods=1).apply(np.prod, raw=True)


def decay_linear(df: pd.DataFrame, period: int = 10) -> pd.DataFrame:
    weights = np.arange(1, period + 1, dtype=float)
    weights /= weights.sum()

    def _wma(x):
        n = len(x)
        if n < period:
            w = np.arange(1, n + 1, dtype=float)
            w /= w.sum()
            return np.dot(x, w)
        return np.dot(x[-period:], weights)

    return df.rolling(period, min_periods=1).apply(_wma, raw=True)


def scale(df: pd.DataFrame, k: float = 1.0) -> pd.DataFrame:
    abs_sum = df.abs().sum(axis=1)
    return df.mul(k).div(abs_sum.where(abs_sum > 1e-10, np.nan), axis=0)


def sign(df: pd.DataFrame) -> pd.DataFrame:
    return np.sign(df)


def log(df: pd.DataFrame) -> pd.DataFrame:
    return np.log(df.abs().clip(lower=1e-10))


class Alpha101Exact71:
    """WorldQuant Alpha101 formulas that do not require industry data."""

    def __init__(self, open_: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame,
                 close: pd.DataFrame, volume: pd.DataFrame, vwap: pd.DataFrame | None = None,
                 returns: pd.DataFrame | None = None):
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        # The benchmark default is OHLC3.  The local caller passes vwap_proxy
        # explicitly and records it in the manifest.
        self.vwap = vwap if vwap is not None else (high + low + close) / 3
        self.returns = returns if returns is not None else close.pct_change(fill_method=None)

    def _safe_corr(self, x, y, window):
        return correlation(x, y, window).replace([np.inf, -np.inf], 0).fillna(0)

    def alpha001(self):
        inner = self.close.copy()
        inner[self.returns < 0] = stddev(self.returns, 20)
        return rank(ts_argmax(inner ** 2, 5))

    def alpha002(self):
        return -1 * self._safe_corr(
            rank(delta(log(self.volume), 2)),
            rank((self.close - self.open) / self.open), 6)

    def alpha003(self):
        return -1 * self._safe_corr(rank(self.open), rank(self.volume), 10)

    def alpha004(self):
        return -1 * ts_rank(rank(self.low), 9)

    def alpha005(self):
        return rank(self.open - sma(self.vwap, 10)) * (-1 * rank(self.close - self.vwap).abs())

    def alpha006(self):
        return -1 * self._safe_corr(self.open, self.volume, 10)

    def alpha007(self):
        adv20 = sma(self.volume, 20)
        alpha = -1 * ts_rank(delta(self.close, 7).abs(), 60) * sign(delta(self.close, 7))
        alpha[adv20 >= self.volume] = -1
        return alpha

    def alpha008(self):
        return -1 * rank(
            ts_sum(self.open, 5) * ts_sum(self.returns, 5)
            - delay(ts_sum(self.open, 5) * ts_sum(self.returns, 5), 10))

    def alpha009(self):
        d = delta(self.close, 1)
        cond1 = ts_min(d, 5) > 0
        cond2 = ts_max(d, 5) < 0
        alpha = -1 * d
        alpha[cond1 | cond2] = d
        return alpha

    def alpha010(self):
        d = delta(self.close, 1)
        cond1 = ts_min(d, 4) > 0
        cond2 = ts_max(d, 4) < 0
        alpha = -1 * d
        alpha[cond1 | cond2] = d
        return rank(alpha)

    def alpha011(self):
        return ((rank(ts_max(self.vwap - self.close, 3))
                 + rank(ts_min(self.vwap - self.close, 3)))
                * rank(delta(self.volume, 3)))

    def alpha012(self):
        return sign(delta(self.volume, 1)) * (-1 * delta(self.close, 1))

    def alpha013(self):
        return -1 * rank(covariance(rank(self.close), rank(self.volume), 5))

    def alpha014(self):
        return -1 * rank(delta(self.returns, 3)) * self._safe_corr(self.open, self.volume, 10)

    def alpha015(self):
        return -1 * ts_sum(rank(self._safe_corr(rank(self.high), rank(self.volume), 3)), 3)

    def alpha016(self):
        return -1 * rank(covariance(rank(self.high), rank(self.volume), 5))

    def alpha017(self):
        adv20 = sma(self.volume, 20)
        return -1 * (rank(ts_rank(self.close, 10))
                     * rank(delta(delta(self.close, 1), 1))
                     * rank(ts_rank(self.volume / adv20, 5)))

    def alpha018(self):
        return -1 * rank(stddev((self.close - self.open).abs(), 5)
                         + (self.close - self.open)
                         + self._safe_corr(self.close, self.open, 10))

    def alpha019(self):
        return (-1 * sign((self.close - delay(self.close, 7)) + delta(self.close, 7))
                * (1 + rank(1 + ts_sum(self.returns, 250))))

    def alpha020(self):
        return -1 * (rank(self.open - delay(self.high, 1))
                     * rank(self.open - delay(self.close, 1))
                     * rank(self.open - delay(self.low, 1)))

    def alpha021(self):
        cond1 = sma(self.close, 8) + stddev(self.close, 8) < sma(self.close, 2)
        cond2 = sma(self.volume, 20) / self.volume < 1
        alpha = pd.DataFrame(np.ones_like(self.close.values), index=self.close.index,
                             columns=self.close.columns)
        alpha[cond1 | cond2] = -1
        return alpha

    def alpha022(self):
        corr = self._safe_corr(self.high, self.volume, 5)
        return -1 * delta(corr, 5) * rank(stddev(self.close, 20))

    def alpha023(self):
        cond = sma(self.high, 20) < self.high
        alpha = pd.DataFrame(np.zeros_like(self.close.values), index=self.close.index,
                             columns=self.close.columns)
        alpha[cond] = -1 * delta(self.high, 2)
        return alpha

    def alpha024(self):
        cond = delta(sma(self.close, 100), 100) / delay(self.close, 100) <= 0.05
        alpha = -1 * delta(self.close, 3)
        alpha[cond] = -1 * (self.close - ts_min(self.close, 100))
        return alpha

    def alpha025(self):
        adv20 = sma(self.volume, 20)
        return rank(-1 * self.returns * adv20 * self.vwap * (self.high - self.close))

    def alpha026(self):
        return -1 * ts_max(self._safe_corr(ts_rank(self.volume, 5), ts_rank(self.high, 5), 5), 3)

    def alpha028(self):
        adv20 = sma(self.volume, 20)
        return scale(self._safe_corr(adv20, self.low, 5) + (self.high + self.low) / 2 - self.close)

    def alpha029(self):
        inner = rank(rank(-1 * rank(delta(self.close - 1, 5))))
        return (ts_min(rank(rank(scale(log(ts_sum(inner, 2))))), 5)
                + ts_rank(delay(-1 * self.returns, 6), 5))

    def alpha030(self):
        d = delta(self.close, 1)
        inner = sign(d) + sign(delay(d, 1)) + sign(delay(d, 2))
        return (1.0 - rank(inner)) * ts_sum(self.volume, 5) / ts_sum(self.volume, 20)

    def alpha033(self):
        return rank(-1 + self.open / self.close)

    def alpha034(self):
        inner = stddev(self.returns, 2) / stddev(self.returns, 5)
        inner = inner.replace([np.inf, -np.inf], 1).fillna(1)
        return rank(2 - rank(inner) - rank(delta(self.close, 1)))

    def alpha035(self):
        return (ts_rank(self.volume, 32)
                * (1 - ts_rank(self.close + self.high - self.low, 16))
                * (1 - ts_rank(self.returns, 32)))

    def alpha037(self):
        return (rank(self._safe_corr(delay(self.open - self.close, 1), self.close, 200))
                + rank(self.open - self.close))

    def alpha038(self):
        inner = (self.close / self.open).replace([np.inf, -np.inf], 1).fillna(1)
        return -1 * rank(ts_rank(self.open, 10)) * rank(inner)

    def alpha039(self):
        adv20 = sma(self.volume, 20)
        return (-1 * rank(delta(self.close, 7) * (1 - rank(decay_linear(self.volume / adv20, 9))))
                * (1 + rank(sma(self.returns, 250))))

    def alpha040(self):
        return -1 * rank(stddev(self.high, 10)) * self._safe_corr(self.high, self.volume, 10)

    def alpha041(self):
        return (self.high * self.low).pow(0.5) - self.vwap

    def alpha042(self):
        return rank(self.vwap - self.close) / rank(self.vwap + self.close)

    def alpha043(self):
        adv20 = sma(self.volume, 20)
        return ts_rank(self.volume / adv20, 20) * ts_rank(-1 * delta(self.close, 7), 8)

    def alpha044(self):
        return -1 * self._safe_corr(self.high, rank(self.volume), 5)

    def alpha045(self):
        return -1 * (rank(sma(delay(self.close, 5), 20))
                     * self._safe_corr(self.close, self.volume, 2)
                     * rank(correlation(ts_sum(self.close, 5), ts_sum(self.close, 20), 2)))

    def alpha046(self):
        inner = ((delay(self.close, 20) - delay(self.close, 10)) / 10
                 - (delay(self.close, 10) - self.close) / 10)
        alpha = -1 * delta(self.close, 1)
        alpha[inner < 0] = 1
        alpha[inner > 0.25] = -1
        return alpha

    def alpha047(self):
        adv20 = sma(self.volume, 20)
        return (((rank(1 / self.close) * self.volume / adv20)
                 * (self.high * rank(self.high - self.close) / (sma(self.high, 5) / 5)))
                - rank(self.vwap - delay(self.vwap, 5)))

    def alpha049(self):
        inner = ((delay(self.close, 20) - delay(self.close, 10)) / 10
                 - (delay(self.close, 10) - self.close) / 10)
        alpha = -1 * delta(self.close, 1)
        alpha[inner < -0.1] = 1
        return alpha

    def alpha050(self):
        return -1 * ts_max(rank(self._safe_corr(rank(self.volume), rank(self.vwap), 5)), 5)

    def alpha051(self):
        inner = ((delay(self.close, 20) - delay(self.close, 10)) / 10
                 - (delay(self.close, 10) - self.close) / 10)
        alpha = -1 * delta(self.close, 1)
        alpha[inner < -0.05] = 1
        return alpha

    def alpha052(self):
        return (-1 * delta(ts_min(self.low, 5), 5)
                * rank((ts_sum(self.returns, 240) - ts_sum(self.returns, 20)) / 220)
                * ts_rank(self.volume, 5))

    def alpha053(self):
        inner = (self.close - self.low).replace(0, 0.0001)
        return -1 * delta(((self.close - self.low) - (self.high - self.close)) / inner, 9)

    def alpha054(self):
        inner = (self.low - self.high).replace(0, -0.0001)
        return -1 * (self.low - self.close) * self.open ** 5 / (inner * self.close ** 5)

    def alpha055(self):
        divisor = (ts_max(self.high, 12) - ts_min(self.low, 12)).replace(0, 0.0001)
        inner = (self.close - ts_min(self.low, 12)) / divisor
        return -1 * self._safe_corr(rank(inner), rank(self.volume), 6)

    def alpha060(self):
        divisor = (self.high - self.low).replace(0, 0.0001)
        inner = ((self.close - self.low) - (self.high - self.close)) * self.volume / divisor
        return -(2 * scale(rank(inner)) - scale(rank(ts_argmax(self.close, 10))))

    def alpha061(self):
        adv180 = sma(self.volume, 180)
        return (rank(self.vwap - ts_min(self.vwap, 16))
                < rank(self._safe_corr(self.vwap, adv180, 18))).astype(float)

    def alpha062(self):
        adv20 = sma(self.volume, 20)
        return ((rank(self._safe_corr(self.vwap, sma(adv20, 22), 10))
                 < rank((rank(self.open) + rank(self.open))
                        < (rank((self.high + self.low) / 2) + rank(self.high))))
                * -1).astype(float)

    def alpha064(self):
        adv120 = sma(self.volume, 120)
        return ((rank(self._safe_corr(
            sma(self.open * 0.178404 + self.low * 0.821596, 13),
            sma(adv120, 13), 17))
            < rank(delta((self.high + self.low) / 2 * 0.178404 + self.vwap * 0.821596, 4)))
            * -1).astype(float)

    def alpha065(self):
        adv60 = sma(self.volume, 60)
        return ((rank(self._safe_corr(self.open * 0.00817 + self.vwap * 0.99183,
                                      sma(adv60, 9), 6))
                 < rank(self.open - ts_min(self.open, 14)))
                * -1).astype(float)

    def alpha066(self):
        denom = (self.open - (self.high + self.low) / 2).replace(0, 0.0001)
        return -1 * (rank(decay_linear(delta(self.vwap, 4), 7))
                     + ts_rank(decay_linear((self.low - self.vwap) / denom, 11), 7))

    def alpha068(self):
        adv15 = sma(self.volume, 15)
        return ((ts_rank(self._safe_corr(rank(self.high), rank(adv15), 9), 14)
                 < rank(delta(self.close * 0.518371 + self.low * 0.481629, 1)))
                * -1).astype(float)

    def alpha072(self):
        adv40 = sma(self.volume, 40)
        num = rank(decay_linear(self._safe_corr((self.high + self.low) / 2, adv40, 9), 10))
        denom = rank(decay_linear(
            self._safe_corr(ts_rank(self.vwap, 4), ts_rank(self.volume, 19), 7), 3))
        return num / denom.replace(0, np.nan)

    def alpha074(self):
        adv30 = sma(self.volume, 30)
        return ((rank(self._safe_corr(self.close, sma(adv30, 37), 15))
                 < rank(self._safe_corr(
                     rank(self.high * 0.0262 + self.vwap * 0.9738), rank(self.volume), 11)))
                * -1).astype(float)

    def alpha075(self):
        adv50 = sma(self.volume, 50)
        return ((rank(self._safe_corr(self.vwap, self.volume, 4))
                 < rank(self._safe_corr(rank(self.low), rank(adv50), 12))).astype(float))

    def alpha077(self):
        adv40 = sma(self.volume, 40)
        p1 = rank(decay_linear((self.high + self.low) / 2 + self.high - self.vwap - self.high, 20))
        p2 = rank(decay_linear(self._safe_corr((self.high + self.low) / 2, adv40, 3), 6))
        return pd.DataFrame(np.minimum(p1.values, p2.values), index=p1.index, columns=p1.columns)

    def alpha078(self):
        adv40 = sma(self.volume, 40)
        return rank(self._safe_corr(
            ts_sum(self.low * 0.352233 + self.vwap * 0.647767, 20),
            ts_sum(adv40, 20), 7)) ** rank(self._safe_corr(
                rank(self.vwap), rank(self.volume), 6))

    def alpha081(self):
        adv10 = sma(self.volume, 10)
        corr = self._safe_corr(self.vwap, ts_sum(adv10, 50), 8)
        return ((rank(log(product(rank(rank(corr) ** 4), 15)))
                 < rank(self._safe_corr(rank(self.vwap), rank(self.volume), 5)))
                * -1).astype(float)

    def alpha083(self):
        mid = (self.high - self.low) / (ts_sum(self.close, 5) / 5)
        denom = mid / (self.vwap - self.close).replace(0, np.nan)
        return rank(delay(mid, 2)) * rank(rank(self.volume)) / denom

    def alpha084(self):
        return ts_rank(self.vwap - ts_max(self.vwap, 15), 21).pow(delta(self.close, 5))

    def alpha085(self):
        adv30 = sma(self.volume, 30)
        return rank(self._safe_corr(self.high * 0.876703 + self.close * 0.123297, adv30, 10)) ** rank(
            self._safe_corr(ts_rank((self.high + self.low) / 2, 4), ts_rank(self.volume, 10), 7))

    def alpha086(self):
        adv20 = sma(self.volume, 20)
        return ((ts_rank(self._safe_corr(self.close, sma(adv20, 15), 6), 20)
                 < rank(self.open + self.close - self.vwap - self.open))
                * -1).astype(float)

    def alpha094(self):
        adv60 = sma(self.volume, 60)
        return -1 * rank(self.vwap - ts_min(self.vwap, 12)).pow(
            ts_rank(self._safe_corr(ts_rank(self.vwap, 20), ts_rank(adv60, 4), 18), 3))

    def alpha095(self):
        adv40 = sma(self.volume, 40)
        return (rank(self.open - ts_min(self.open, 12))
                < ts_rank(rank(self._safe_corr(
                    sma((self.high + self.low) / 2, 19), sma(adv40, 19), 13)) ** 5, 12)).astype(float)

    def alpha099(self):
        adv60 = sma(self.volume, 60)
        return ((rank(self._safe_corr(ts_sum((self.high + self.low) / 2, 20),
                                      ts_sum(adv60, 20), 9))
                 < rank(self._safe_corr(self.low, self.volume, 6)))
                * -1).astype(float)

    def alpha101(self):
        return (self.close - self.open) / ((self.high - self.low) + 0.001)

    def alpha_names(self) -> list[str]:
        return sorted(n for n in dir(self) if n.startswith('alpha') and n[5:].isdigit())

    def compute(self, name: str) -> pd.DataFrame:
        if name not in self.alpha_names():
            raise KeyError(f'{name!r} 不在 alpha101_exact71 清单中')
        return getattr(self, name)()

    def compute_all(self, strict: bool = True) -> dict[str, pd.DataFrame]:
        results, failures = {}, {}
        for name in self.alpha_names():
            try:
                results[name] = self.compute(name)
            except Exception as exc:  # noqa: BLE001 - failures are reported
                if strict:
                    raise RuntimeError(f'{name} failed: {type(exc).__name__}: {exc}') from exc
                failures[name] = f'{type(exc).__name__}: {exc}'
        self.last_failures = failures
        return results


@dataclass(frozen=True)
class Alpha101Metadata:
    namespace: str = NAMESPACE
    formula_count: int = 71
    source_commit: str = VENDOR_COMMIT
    reference: str = REFERENCE
    vwap: str = 'OHLC3 proxy by default; native upstream vwap is raw-scale and excluded'
    rank: str = 'cross-sectional percentile rank on the same date'
    missing: str = 'undefined rolling correlations become 0 exactly as FactorBench; other NaNs remain'
    excluded: str = '30 IndNeutralize formulas; no industry classification in the local panel'

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def build_alpha101(panel: pd.DataFrame, *, vwap_field: str = 'vwap_proxy') -> Alpha101Exact71:
    required = {'trade_date', 'stock_code', 'open', 'high', 'low', 'close', 'volume', vwap_field}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f'Alpha101 面板缺列: {sorted(missing)}')
    wide = {field: panel.pivot(index='trade_date', columns='stock_code', values=field)
            for field in ('open', 'high', 'low', 'close', 'volume', vwap_field)}
    return Alpha101Exact71(
        open_=wide['open'], high=wide['high'], low=wide['low'], close=wide['close'],
        volume=wide['volume'], vwap=wide[vwap_field],
        returns=panel.pivot(index='trade_date', columns='stock_code', values='return_1d')
        if 'return_1d' in panel else None)


def factor_panel(signals: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Long ``(trade_date, stock_code, alpha, value)`` representation."""
    pieces = []
    for name, wide in signals.items():
        long = wide.stack(future_stack=True).rename('value').reset_index()
        long.columns = ['trade_date', 'stock_code', 'value']
        long['alpha'] = name
        pieces.append(long)
    if not pieces:
        return pd.DataFrame(columns=['trade_date', 'stock_code', 'alpha', 'value'])
    return pd.concat(pieces, ignore_index=True)
