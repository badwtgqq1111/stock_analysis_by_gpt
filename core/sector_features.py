#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""行业/赛道特征计算 —— 基于收益率相关性的统计行业分类。

不依赖外部行业数据，直接从已有 OHLCV 计算：
1. 用 60 日收益率相关性聚类股票 → 动态行业分组
2. 计算行业 RPS（行业内平均涨跌幅的横截面排名）
3. 计算个股相对行业超额收益
"""

import numpy as np
import pandas as pd


def _compute_returns_matrix(price_data):
    """从 {stock_code: DataFrame(close)} 构建收益率矩阵 (dates × stocks)。"""
    closes = {}
    for code, df in price_data.items():
        if df is None or (hasattr(df, 'empty') and df.empty) or len(df) < 20:
            continue
        col = df.get("Close")
        if col is None:
            col = df.get("close")
        if col is None or len(col) < 20:
            continue
        s = pd.to_numeric(col, errors="coerce").dropna()
        if len(s) < 20:
            continue
        closes[code] = s
    if not closes:
        return None, None, None
    price_matrix = pd.DataFrame(closes).sort_index()
    ret_matrix = price_matrix.pct_change().dropna(how="all")
    return price_matrix, ret_matrix, list(price_matrix.columns)


def _cluster_stocks_by_correlation(ret_matrix, n_clusters=30, min_corr=0.6):
    """用收益率相关性聚类股票。返回 {stock_code: cluster_id}。"""
    from sklearn.cluster import AgglomerativeClustering

    corr = ret_matrix.corr().fillna(0)
    if corr.empty or len(corr.columns) < 10:
        return {code: -1 for code in ret_matrix.columns}

    distance = 1 - corr.abs()
    n = min(n_clusters, len(ret_matrix.columns) // 5)
    n = max(3, n)
    clustering = AgglomerativeClustering(
        n_clusters=n, metric="precomputed", linkage="average"
    )
    labels = clustering.fit_predict(distance.values)
    return dict(zip(corr.columns, labels))


def compute_sector_features(price_data, date=None):
    """计算行业特征。

    Args:
        price_data: {stock_code: DataFrame(Close)}  —— 所有股票的 OHLCV 数据
    Returns:
        DataFrame with columns: stock_code, cluster_id, cluster_size,
        cluster_mean_ret5, cluster_mean_ret20, cluster_rps,
        stock_vs_cluster_ret5, stock_vs_cluster_ret20
    """
    price_matrix, ret_matrix, codes = _compute_returns_matrix(price_data)
    if price_matrix is None or ret_matrix is None:
        return pd.DataFrame()

    clusters = _cluster_stocks_by_correlation(ret_matrix)

    # 计算每只股票和每个 cluster 的 5/20 日收益
    ret5 = price_matrix.pct_change(5).iloc[-1] if len(price_matrix) > 5 else pd.Series(dtype=float)
    ret20 = price_matrix.pct_change(20).iloc[-1] if len(price_matrix) > 20 else pd.Series(dtype=float)

    rows = []
    cluster_stats = {}
    for cid in set(clusters.values()):
        members = [c for c, cl in clusters.items() if cl == cid]
        if len(members) < 2:
            continue
        mem_ret5 = ret5[ret5.index.isin(members)].dropna()
        mem_ret20 = ret20[ret20.index.isin(members)].dropna()
        cluster_stats[cid] = {
            "mean_ret5": float(mem_ret5.mean()) if len(mem_ret5) > 0 else 0.0,
            "mean_ret20": float(mem_ret20.mean()) if len(mem_ret20) > 0 else 0.0,
            "size": len(members),
            "breadth5": float((mem_ret5 > 0).mean()) if len(mem_ret5) > 0 else 0.0,
            "breadth20": float((mem_ret20 > 0).mean()) if len(mem_ret20) > 0 else 0.0,
        }

    # 行业 RPS: 按行业平均收益排名
    if len(cluster_stats) > 1:
        all_mean_ret20 = np.array([v["mean_ret20"] for v in cluster_stats.values()])
        ranked = np.argsort(np.argsort(all_mean_ret20)) + 1
        rps_map = dict(zip(cluster_stats.keys(), (ranked / len(ranked)) * 100))
    else:
        rps_map = {}

    for code in codes:
        cid = clusters.get(code, -1)
        stats = cluster_stats.get(cid, {})
        stock_ret5 = float(ret5.get(code, np.nan)) if code in ret5.index else np.nan
        stock_ret20 = float(ret20.get(code, np.nan)) if code in ret20.index else np.nan
        cluster_ret5 = stats.get("mean_ret5", np.nan)
        cluster_ret20 = stats.get("mean_ret20", np.nan)

        rows.append({
            "stock_code": code,
            "cluster_id": int(cid),
            "cluster_size": stats.get("size", 0),
            "cluster_mean_ret5": cluster_ret5,
            "cluster_mean_ret20": cluster_ret20,
            "cluster_rps": float(rps_map.get(cid, 50.0)),
            "cluster_breadth5": stats.get("breadth5", 0.0),
            "cluster_breadth20": stats.get("breadth20", 0.0),
            "stock_vs_cluster_ret5": stock_ret5 - cluster_ret5 if not np.isnan(stock_ret5) and not np.isnan(cluster_ret5) else np.nan,
            "stock_vs_cluster_ret20": stock_ret20 - cluster_ret20 if not np.isnan(stock_ret20) and not np.isnan(cluster_ret20) else np.nan,
        })

    result = pd.DataFrame(rows)

    for col in ["stock_vs_cluster_ret5", "stock_vs_cluster_ret20", "stock_vs_cluster_ret5", "stock_vs_cluster_ret20"]:
        if col in result.columns:
            result[col] = result[col].fillna(0.0)
    for col in ["cluster_rps", "cluster_breadth5", "cluster_breadth20"]:
        if col in result.columns:
            result[col] = result[col].fillna(50.0)

    # 强势回踩信号: 中长线超额 + 短线暴跌
    if all(c in result.columns for c in ["stock_vs_cluster_ret20", "stock_vs_cluster_ret5"]):
        result["dip_buy_signal"] = result["stock_vs_cluster_ret20"] - result["stock_vs_cluster_ret5"].abs() * 2
        result["dip_buy_signal"] = result["dip_buy_signal"].clip(-1.0, 1.0)
    if all(c in result.columns for c in ["cluster_rps", "stock_vs_cluster_ret20"]):
        result["hot_sector_leader"] = (result["cluster_rps"] / 100.0) * (result["stock_vs_cluster_ret20"] + 0.5)
        result["hot_sector_leader"] = result["hot_sector_leader"].clip(0.0, 1.0)

    return result
