"""P2.71 predictive diagnostics with explicit daily cross-sectional samples."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _aligned(factor: pd.Series, label: pd.Series) -> pd.DataFrame:
    frame = pd.concat([factor.rename('factor'), label.rename('label')], axis=1, join='inner')
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if frame.index.nlevels != 2:
        raise ValueError('因子与标签须使用 (trade_date, stock_code) 两层索引')
    return frame


def daily_ic(factor: pd.Series, label: pd.Series, *, method: str = 'spearman',
             min_assets: int = 10) -> pd.Series:
    """One cross-sectional IC per date on the exact same valid assets."""
    if method not in ('pearson', 'spearman'):
        raise ValueError('method 必须是 pearson 或 spearman')
    frame = _aligned(factor, label)
    if frame.empty:
        return pd.Series(dtype=float)
    if method == 'spearman':
        frame = frame.groupby(level=0, group_keys=False).rank(pct=True)
    grouped = frame.groupby(level=0)
    counts = grouped.size()
    valid = counts[counts >= int(min_assets)].index
    if len(valid) == 0:
        return pd.Series(dtype=float)
    sub = frame[frame.index.get_level_values(0).isin(valid)]
    out = sub.groupby(level=0).apply(
        lambda g: g.factor.corr(g.label), include_groups=False)
    out.index = pd.DatetimeIndex(out.index)
    out.name = f'{method}_ic'
    return out.replace([np.inf, -np.inf], np.nan).dropna()


def daily_ic_wide(factor: pd.DataFrame, label: pd.DataFrame, *, method: str = 'spearman',
                  min_assets: int = 10) -> pd.Series:
    """Vectorised daily IC on wide ``date x asset`` frames.

    The same finite mask is applied to factor and label.  This is the fast path
    used by the 71-formula audit; the long-form helper above remains for tests
    and small panels.
    """
    if method not in ('pearson', 'spearman'):
        raise ValueError('method 必须是 pearson 或 spearman')
    common_dates = factor.index.intersection(label.index)
    common_assets = factor.columns.intersection(label.columns)
    x = factor.loc[common_dates, common_assets].replace([np.inf, -np.inf], np.nan)
    y = label.loc[common_dates, common_assets].replace([np.inf, -np.inf], np.nan)
    mask = x.notna() & y.notna()
    x = x.where(mask)
    y = y.where(mask)
    if method == 'spearman':
        x = x.rank(axis=1, pct=True)
        y = y.rank(axis=1, pct=True)
    n = mask.sum(axis=1)
    xm = x.sub(x.mean(axis=1), axis=0)
    ym = y.sub(y.mean(axis=1), axis=0)
    num = (xm * ym).sum(axis=1)
    den = np.sqrt((xm * xm).sum(axis=1) * (ym * ym).sum(axis=1))
    out = (num / den.replace(0, np.nan)).where(n >= int(min_assets))
    out.index = pd.DatetimeIndex(out.index)
    out.name = f'{method}_ic'
    return out.replace([np.inf, -np.inf], np.nan).dropna()


def newey_west_t(values: pd.Series, lags: int = 20) -> float | None:
    x = np.asarray(values.dropna(), dtype=float)
    n = len(x)
    if n < 3:
        return None
    lags = min(int(lags), n - 1)
    mu = float(x.mean())
    centered = x - mu
    gamma0 = float(np.dot(centered, centered) / n)
    var = gamma0
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = float(np.dot(centered[lag:], centered[:-lag]) / n)
        var += 2.0 * weight * gamma
    if var <= 0:
        return None
    return mu / np.sqrt(var / n)


def block_bootstrap_ci(values: pd.Series, *, block: int = 20, draws: int = 2000,
                       seed: int = 71, alpha: float = 0.05) -> tuple[float, float] | None:
    x = np.asarray(values.dropna(), dtype=float)
    n = len(x)
    if n < block:
        return None
    rng = np.random.default_rng(seed)
    starts = np.arange(0, n - block + 1)
    means = np.empty(draws)
    blocks_needed = int(np.ceil(n / block))
    for i in range(draws):
        chosen = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate([x[s:s + block] for s in chosen])[:n]
        means[i] = sample.mean()
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def summarize_ic(values: pd.Series, *, lags: int = 20, bootstrap_block: int = 20) -> dict:
    x = values.dropna().astype(float)
    n = len(x)
    if n == 0:
        return {'dates': 0, 'mean': None, 'std': None, 'icir': None,
                'annualized_icir': None, 'nw_t': None, 'ci95': None,
                'positive_ratio': None, 'first_date': None, 'last_date': None}
    mean = float(x.mean())
    std = float(x.std(ddof=1)) if n > 1 else None
    return {
        'dates': int(n),
        'mean': mean,
        'std': std,
        'icir': float(mean / std) if std and std > 0 else None,
        'annualized_icir': float(mean / std * np.sqrt(252)) if std and std > 0 else None,
        'nw_t': newey_west_t(x, lags=lags),
        'ci95': block_bootstrap_ci(x, block=min(bootstrap_block, max(2, n))),
        'positive_ratio': float((x > 0).mean()),
        'first_date': str(x.index.min().date()),
        'last_date': str(x.index.max().date()),
    }


def style_exposures(panel: pd.DataFrame, factors: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """Five descriptive styles used by FactorBench.

    beta252, vol60, 12-1 momentum, 21-day reversal and log20 dollar volume.
    The local volume/amount are not qfq-consistent, so dollar volume uses
    the qfq ``close * volume`` proxy and is labelled as such.
    """
    wide = {field: panel.pivot(index='trade_date', columns='stock_code', values=field)
            for field in ('close', 'volume')}
    close, volume = wide['close'], wide['volume']
    ret = close.pct_change(fill_method=None)
    market = ret.mean(axis=1)
    styles = {
        'beta252': ret.rolling(252, min_periods=60).cov(market).div(
            market.rolling(252, min_periods=60).var(), axis=0),
        'vol60': ret.rolling(60, min_periods=20).std(),
        'momentum_12_1': close.shift(21) / close.shift(252) - 1,
        'reversal_21': -(close / close.shift(21) - 1),
        'liquidity_log20': np.log((close * volume).rolling(20, min_periods=10).mean().clip(lower=1)),
    }
    out = {}
    for name, wide_factor in factors.items():
        residuals = []
        for date, row in wide_factor.iterrows():
            y = row.dropna()
            if len(y) < 20:
                continue
            x = pd.DataFrame({k: v.loc[date] for k, v in styles.items()}).reindex(y.index)
            x = x.replace([np.inf, -np.inf], np.nan)
            valid = y.notna() & x.notna().all(axis=1)
            if valid.sum() < 20:
                continue
            design = np.column_stack([np.ones(valid.sum()), x.loc[valid].to_numpy(dtype=float)])
            target = y.loc[valid].to_numpy(dtype=float)
            beta, *_ = np.linalg.lstsq(design, target, rcond=None)
            resid = target - design @ beta
            index = pd.MultiIndex.from_product(
                [[date], y.loc[valid].index], names=['trade_date', 'stock_code'])
            residuals.append(pd.Series(resid, index=index, name='residual'))
        out[name] = pd.concat(residuals) if residuals else pd.Series(dtype=float)
    return out


def rank_correlation(a: pd.Series, b: pd.Series) -> float | None:
    frame = _aligned(a, b)
    if len(frame) < 3:
        return None
    a_rank = frame.factor.rank(pct=True)
    b_rank = frame.label.rank(pct=True)
    if a_rank.nunique() < 2 or b_rank.nunique() < 2:
        return None
    return float(a_rank.corr(b_rank))
