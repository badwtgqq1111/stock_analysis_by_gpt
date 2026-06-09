"""Hot-sector + relative valuation scoring for HK stocks.

Fetches PE/PB from Tencent API and computes sector-relative valuation
percentiles using the existing correlation-based sector clusters.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fetch_valuation_batch(
    stock_codes: list[str],
    max_workers: int = 20,
    progress_callback=None,
) -> dict[str, tuple[float, float]]:
    """Fetch PE and PB ratios for HK stocks via Tencent API.

    Returns dict mapping stock_code -> (pe_ratio, pb_ratio).
    Missing data yields (nan, nan).
    """
    result: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()
    completed = 0
    total = len(stock_codes)

    def _fetch_one(code: str) -> tuple[str, float, float]:
        try:
            url = f"https://qt.gtimg.cn/q=r_hk{code}"
            r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            r.encoding = "gbk"
            m = re.search(r'"(.+)"', r.text)
            if not m:
                return code, np.nan, np.nan
            fields = m.group(1).split("~")
            pe = float(fields[39]) if len(fields) > 39 and fields[39] else np.nan
            pb = float(fields[43]) if len(fields) > 43 and fields[43] else np.nan
            return code, pe, pb
        except Exception:
            return code, np.nan, np.nan

    if max_workers <= 1:
        for stock_code in stock_codes:
            code, pe, pb = _fetch_one(stock_code)
            with lock:
                result[code] = (pe, pb)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in stock_codes}
        for future in as_completed(futures):
            code, pe, pb = future.result()
            with lock:
                result[code] = (pe, pb)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)

    return result


def compute_sector_valuation(
    batch_data_map: dict,
    valuation_data: dict[str, tuple[float, float]],
    sector_features_df: pd.DataFrame,
) -> pd.DataFrame:
    """Compute hot-sector + relative valuation scores.

    Args:
        batch_data_map: {stock_code: OHLCV DataFrame} (used for size, not required
            for computation but passed for consistency).
        valuation_data: {stock_code: (pe_ratio, pb_ratio)} from fetch_valuation_batch.
        sector_features_df: DataFrame from compute_sector_features() with columns
            stock_code, cluster_id, cluster_rps, cluster_breadth5, cluster_breadth20.

    Returns:
        DataFrame with columns: stock_code, pe_ratio, pb_ratio, pe_percentile,
        pb_percentile, value_score, hot_sector_score, hot_sector_value_score.
    """
    if sector_features_df is None or sector_features_df.empty:
        return _default_valuation_df(list(valuation_data.keys()))

    sf = sector_features_df.set_index("stock_code")
    rows = []

    # Group stocks by cluster for within-cluster percentile calculation
    cluster_groups: dict[int, list[str]] = {}
    for code in valuation_data:
        cid = int(sf.at[code, "cluster_id"]) if code in sf.index else -1
        cluster_groups.setdefault(cid, []).append(code)

    for code, (pe, pb) in valuation_data.items():
        cid = int(sf.at[code, "cluster_id"]) if code in sf.index else -1
        cluster_peers = cluster_groups.get(cid, [code])

        if pd.notna(pe) and pe > 0:
            peer_pes = []
            for p in cluster_peers:
                v = valuation_data.get(p)
                if v and pd.notna(v[0]) and v[0] > 0:
                    peer_pes.append(v[0])
            if len(peer_pes) > 1:
                pe_percentile = pd.Series(peer_pes).rank(pct=True).iloc[
                    peer_pes.index(pe)
                ] * 100.0
            else:
                pe_percentile = 50.0
        else:
            pe_percentile = 50.0

        if pd.notna(pb) and pb > 0:
            peer_pbs = []
            for p in cluster_peers:
                v = valuation_data.get(p)
                if v and pd.notna(v[1]) and v[1] > 0:
                    peer_pbs.append(v[1])
            if len(peer_pbs) > 1:
                pb_percentile = pd.Series(peer_pbs).rank(pct=True).iloc[
                    peer_pbs.index(pb)
                ] * 100.0
            else:
                pb_percentile = 50.0
        else:
            pb_percentile = 50.0

        # value_score: lower percentile = cheaper = higher score
        value_score = (100.0 - pe_percentile) * 0.5 + (100.0 - pb_percentile) * 0.5
        value_score = float(np.clip(value_score, 0.0, 100.0))

        # hot_sector_score from existing sector features
        cluster_rps = float(sf.at[code, "cluster_rps"]) if code in sf.index else 50.0
        cluster_breadth5 = float(sf.at[code, "cluster_breadth5"]) if code in sf.index else 0.5
        hot_sector_score = cluster_rps * 0.5 + cluster_breadth5 * 50.0
        hot_sector_score = float(np.clip(hot_sector_score, 0.0, 100.0))

        # composite
        hot_sector_value_score = hot_sector_score * 0.5 + value_score * 0.5
        hot_sector_value_score = float(np.clip(hot_sector_value_score, 0.0, 100.0))

        rows.append({
            "stock_code": code,
            "pe_ratio": pe,
            "pb_ratio": pb,
            "pe_percentile": round(float(pe_percentile), 2),
            "pb_percentile": round(float(pb_percentile), 2),
            "value_score": round(value_score, 2),
            "hot_sector_score": round(hot_sector_score, 2),
            "hot_sector_value_score": round(hot_sector_value_score, 2),
        })

    return pd.DataFrame(rows)


def _default_valuation_df(stock_codes: list[str]) -> pd.DataFrame:
    """Return a DataFrame with neutral (50) scores for all stocks."""
    return pd.DataFrame([
        {
            "stock_code": code,
            "pe_ratio": np.nan,
            "pb_ratio": np.nan,
            "pe_percentile": 50.0,
            "pb_percentile": 50.0,
            "value_score": 50.0,
            "hot_sector_score": 50.0,
            "hot_sector_value_score": 50.0,
        }
        for code in stock_codes
    ])
