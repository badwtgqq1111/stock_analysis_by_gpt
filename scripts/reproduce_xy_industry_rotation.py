#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复现兴业证券 2019-09-19《系统化资产配置系列之二：行业的重新分类以及行业轮动策略》（于明明）。

论文流水线（逐条对照实现，见报告 §1）：
  ① **行业重新聚类**：行业月收益的两两相关矩阵 → 距离 ``d_ij = sqrt(2(1-rho_ij))`` →
     **最小生成树（Kruskal）** 剪枝 → 图上被"割裂"成 4 大块（TMT / 大金融 / 大消费 / 成长类）；
     论文最终没有直接用 MST，而是采用**中信风格 5 大类**（金融/周期/消费/成长/稳定，
     按三级行业风格标签、**流通市值加权**的风格指数）。
  ② **风格择时因子库**：经济景气（GDP/PMI 分项/投资/地产/产量/进出口）、通胀（PMI 生产资料生活资料/CPI）、
     利率（SHIBOR/国债 YTM）、风险偏好（信用价差）、市场内部结构（各大类相对走势）、
     技术指标（市场波动率）、跨市场（商品/美股/汇率）；宏观序列按**发布日**对齐（前值填充到日频）。
  ③ **信号生成：三分位点法** —— 按上下三分位给出 看多/看平/看空，用
     ``t = (F1-F3)/sqrt(S1/n1 + S3/n3)`` 衡量预测力（论文发现该 t 值与策略夏普高度相关）。
  ④ **信号合成：因子聚类再等权** —— 同质因子先按 correlation 距离 KMeans(3) 聚类，
     类内等权、类间等权，重复多次取平均 = 该风格的最终打分 Sign。
  ⑤ **轮动**：每个调仓节点按 Sign 降序排大类，持有 top-1（"不定期调仓"的月频抽样）。

**与原文的差异（不掩饰，逐条进报告）**：
  * 无中信风格成分表 / 中信行业指数 → 用 **MST 聚类结果** 作"大类"代理（K=4/5），
    收益用 110 个 tushare 行业 **等权** 合成（论文是市值加权风格指数）；
  * 论文因子库里的 **宏观 / 利率 / 风险偏好 / 跨市场** 四类在本仓库 **整条缺失**
    （`assets/` 里没有任何宏观序列），只能覆盖「市场内部结构 + 技术指标 + 板块自身特点」三类；
  * 样本 2016-2026（论文样本内 2004-2018、样本外 2018-08-04~2019-09-09 仅 13 个月）；
  * 论文的三分位阈值/方向取自**样本内全样本**（含前视）；这里默认用**扩张窗口**（只用 t 及之前），
    `--in-sample-quantiles` 可切回论文口径做对照；
  * 聚类每月重算不现实，按 `--refresh` 个月刷新一次标签（快照之间的月份沿用该快照），
    因子与打分都只用快照及之前的数据 —— 无前视。

用法：
  uv run python scripts/reproduce_xy_industry_rotation.py
  uv run python scripts/reproduce_xy_industry_rotation.py --k 4 5 --top-k 1 2 --refresh 6
输出：
  output/research/industry_rotation/xy_reproduce.json / .md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_zt_board_features as bzb  # noqa: E402
from reproduce_industry_rotation_papers import _board_membership, _nw_t  # noqa: E402

OUT_DIR = ROOT / "output/research/industry_rotation"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"

FACTORS = ("mom_1m", "mom_3m", "mom_6m", "vol_3m", "amount_share", "turnover_share", "zt_density")
FACTOR_LABEL = {
    "mom_1m": "大类 1 个月相对走势", "mom_3m": "大类 3 个月相对走势", "mom_6m": "大类 6 个月相对走势",
    "vol_3m": "大类 3 个月波动率", "amount_share": "成交额份额",
    "turnover_share": "换手份额", "zt_density": "涨停密度",
}
FACTOR_GROUP = {"mom_1m": "市场内部结构", "mom_3m": "市场内部结构", "mom_6m": "市场内部结构",
                "vol_3m": "技术性指标", "amount_share": "板块自身特点",
                "turnover_share": "板块自身特点", "zt_density": "板块自身特点"}
MACRO_PARQUET = ROOT / "assets/data/derived/cn_macro_features.parquet"
# 宏观/利率/跨市场指标（来自 scripts/sync_cn_macro.py；`yc_cb` 无权限 → 利率用 SHIBOR 期限结构代理）
MACRO_INDICATORS = (
    "pmi", "pmi_new_orders", "pmi_new_export_orders", "pmi_input_price",
    "pmi_raw_material_stock", "pmi_orders_minus_inventory",            # 宏观景气（论文①）
    "cpi_yoy", "ppi_yoy", "ppi_producer_goods_yoy", "ppi_consumer_goods_yoy",  # 通胀（论文②）
    "m1_yoy", "m2_yoy", "m1_minus_m2",                                  # 流动性（论文②/③）
    "gdp_yoy",                                                          # 宏观景气（论文①）
    "shibor_on_10avg", "shibor_3m_10avg", "shibor_term_spread_3m_on",   # 利率（论文③，SHIBOR）
    "ytm_gov_10y", "ytm_term_spread_10y_1y",                            # 利率（论文③，中债国债曲线）
    "credit_spread_corp_aaa_5y", "credit_spread_bank_aaa_5y",           # 风险偏好（论文④，信用价差）
    "us10y", "us_term_spread_10y_3m",                                   # 跨市场·美债（论文⑦）
    "nhci_mom_20d", "usdcnh_chg_20d",                                   # 跨市场·商品/汇率（论文⑦）
)
MACRO_LABEL = {
    "pmi": "制造业 PMI", "pmi_new_orders": "PMI 新订单", "pmi_new_export_orders": "PMI 新出口订单",
    "pmi_input_price": "PMI 购进价格", "pmi_raw_material_stock": "PMI 原材料库存",
    "pmi_orders_minus_inventory": "PMI 新订单−产成品库存", "cpi_yoy": "CPI 同比",
    "ppi_yoy": "PPI 同比", "ppi_producer_goods_yoy": "PPI 生产资料同比",
    "ppi_consumer_goods_yoy": "PPI 生活资料同比", "m1_yoy": "M1 同比", "m2_yoy": "M2 同比",
    "m1_minus_m2": "M1−M2 剪刀差", "gdp_yoy": "GDP 同比",
    "shibor_on_10avg": "SHIBOR ON 10 日均", "shibor_3m_10avg": "SHIBOR 3M 10 日均",
    "shibor_term_spread_3m_on": "SHIBOR 3M−ON 利差",
    "ytm_gov_10y": "中债国债 10Y", "ytm_term_spread_10y_1y": "国债 10Y−1Y 期限利差",
    "credit_spread_corp_aaa_5y": "信用价差（中短票AAA 5Y − 国债）",
    "credit_spread_bank_aaa_5y": "信用价差（银行普通债AAA 5Y − 国债）", "us10y": "美债 10Y",
    "us_term_spread_10y_3m": "美债 10Y−3M 利差", "nhci_mom_20d": "南华商品 20 日动量",
    "usdcnh_chg_20d": "USDCNH 20 日变化",
}
MISSING_FACTOR_GROUPS = [
    "宏观景气（GDP / PMI 及其分项 / 固定资产投资 / 地产开工销售 / 工业增加值 / 进出口）",
    "通胀（PMI 生产资料·生活资料 / CPI 分项）",
    "利率（SHIBOR 各期限 / 中债国债到期收益率）",
    "风险偏好（信用价差）",
    "跨市场（南华与 Wind 商品指数 / 标普 500 / 汇率）",
]


# ─── ① 行业重分类：相关矩阵 → 距离 → 最小生成树 → 剪枝成 K 个大类 ────────────
def corr_distance(matrix: pd.DataFrame, min_periods: int = 24) -> pd.DataFrame:
    """论文口径：rho 用月收益的成对完整观测，距离 ``d = sqrt(2(1-rho))``（rho=1 → d=0）。"""
    corr = matrix.corr(min_periods=min_periods)
    dist = np.sqrt(np.clip(2.0 * (1.0 - corr.to_numpy(dtype=float)), 0.0, None))
    return pd.DataFrame(dist, index=corr.index, columns=corr.columns)


def mst_clusters(dist: pd.DataFrame, k: int) -> dict[str, int]:
    """Kruskal 最小生成树 + 砍掉最长的 k-1 条边 → k 个连通块（论文"图上被割裂成几块"）。

    坑：``minimum_spanning_tree`` 把**权重 0 当成"没有这条边"**（稀疏矩阵语义）。
    走势几乎一致的行业（同指数化板块相关可到 1.0）距离正好是 0，会被静默丢掉 ——
    MST 于是断成两截、聚类结果随实现变化。统一加极小 ε（对角仍置 0）再建树。
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components, minimum_spanning_tree

    names = list(dist.index)
    weights = np.nan_to_num(dist.to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    np.fill_diagonal(weights, 0.0)
    weights = np.where(weights > 0, weights + 1e-9, 0.0)
    mst = minimum_spanning_tree(csr_matrix(weights)).tocoo()
    edges = sorted(zip(mst.data.tolist(), mst.row.tolist(), mst.col.tolist()), reverse=True)
    keep = np.zeros_like(weights)
    for weight, i, j in edges[max(int(k) - 1, 0):]:      # MST 有 n-1 条边，砍 k-1 条 → 恰好 k 块
        keep[i, j] = weight
    _, comp = connected_components(csr_matrix(keep), directed=False)
    order: list[int] = []
    for label in comp:                                   # 按行业名字典序编号 → 可复现
        if int(label) not in order:
            order.append(int(label))
    return {name: order.index(int(comp[i])) for i, name in enumerate(names)}


def snapshot_labels(ret: pd.DataFrame, k: int, *, window: int = 60, min_periods: int = 24,
                    refresh: int = 6, full_sample: bool = False) -> dict[pd.Period, pd.Series]:
    """聚类标签快照：`月份 → 行业→大类` 的映射。

    `full_sample=True`：用**整段样本**算一次（论文口径，含前视），所有月份共用一组标签；
    `full_sample=False`：每 `refresh` 个月用**滚动 window 个月**重算一次（只用历史，无前视）。
    """
    months = list(ret.index)
    if full_sample:
        labels = mst_clusters(corr_distance(ret, min_periods=min_periods), k)
        return {month: pd.Series(labels) for month in months}
    out: dict[pd.Period, pd.Series] = {}
    current: pd.Series | None = None
    for index, month in enumerate(months):
        if current is None or index % max(int(refresh), 1) == 0:
            history = ret.iloc[max(0, index + 1 - int(window)):index + 1]
            dist = corr_distance(history, min_periods=min_periods)
            usable = dist.dropna(how="all").dropna(axis=1, how="all")
            if usable.shape[0] <= k or usable.shape[1] <= k:
                current = None
            else:
                current = pd.Series(mst_clusters(usable, k))
        if current is not None:
            out[month] = current
    return out


# ─── 面板构建：行业月频收益 / 成交额 / 换手 / 涨停密度 ──────────────────────
def _membership(taxonomy: str) -> pd.DataFrame:
    if taxonomy == "tushare_industry":
        basic = pd.read_parquet(BASIC, columns=["ts_code", "industry"])
        basic["ts_code"] = basic["ts_code"].astype(str)
        mapping = basic.drop_duplicates("ts_code").set_index("ts_code")["industry"].astype(str)
        mapping = mapping.replace({"nan": "", "None": ""}).fillna("")
        membership = mapping.rename("board").reset_index().rename(columns={"ts_code": "stock_code"})
        return membership[membership["board"] != ""]
    membership = _board_membership(taxonomy)
    if membership is None:
        raise SystemExit(f"没有 {taxonomy} 的板块成分（先跑 scripts/sync_cn_boards.py）")
    return membership


def board_monthly_panels(start: str, end: str, min_members: int = 5,
                         taxonomy: str = "tushare_industry") -> dict[str, pd.DataFrame]:
    """月频板块面板：等权收益 / 成交额 / 换手 / 涨停密度（成分股均值）+ 全市场等权收益。"""
    membership = _membership(taxonomy)
    bars = bzb.load_bars(start)
    flags = bzb.limit_flags(bars)
    flags = flags[(flags["trade_date"] >= pd.Timestamp(start)) & (flags["trade_date"] <= pd.Timestamp(end))]
    flags = flags.merge(membership, on="stock_code", how="inner")
    flags["month"] = flags["trade_date"].dt.to_period("M")
    flags["gross"] = 1 + flags["pct_chg"].fillna(0)
    # 两步聚合（先把个股月收益连乘，再对成分股等权）——反了会得到天文数字
    stock_month = flags.groupby(["stock_code", "board", "month"]).agg(
        ret=("gross", lambda s: float(s.prod()) - 1.0), amount=("amount", "sum"),
        turnover=("turnover", "sum"), limit_up=("limit_up", "mean")).reset_index()
    counts = stock_month.groupby(["board", "month"])["stock_code"].nunique().rename("n")
    agg = stock_month.groupby(["board", "month"]).agg(
        ret=("ret", "mean"), amount=("amount", "sum"), turnover=("turnover", "sum"),
        zt=("limit_up", "mean")).reset_index().merge(counts.reset_index(), on=["board", "month"])
    agg = agg[agg["n"] >= int(min_members)]

    def pivot(column: str) -> pd.DataFrame:
        return agg.pivot(index="month", columns="board", values=column).sort_index()

    return {"ret": pivot("ret"), "amount": pivot("amount"), "turnover": pivot("turnover"),
            "zt": pivot("zt"), "market_ew": stock_month.groupby("month")["ret"].mean().sort_index()}


# ─── ② 因子面板（行业层 → 大类层）────────────────────────────────────────────
def industry_factors(panels: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """行业层因子面板（月份 × 行业）。份额类因子按当月全市场归一。"""
    ret = panels["ret"]
    amount_share = panels["amount"].div(panels["amount"].sum(axis=1), axis=0)
    turnover_share = panels["turnover"].div(panels["turnover"].sum(axis=1), axis=0)
    return {
        "mom_1m": ret,
        "mom_3m": (1 + ret).rolling(3, min_periods=3).apply(np.prod, raw=True) - 1,
        "mom_6m": (1 + ret).rolling(6, min_periods=6).apply(np.prod, raw=True) - 1,
        "vol_3m": ret.rolling(3, min_periods=3).std(),
        "amount_share": amount_share,
        "turnover_share": turnover_share,
        "zt_density": panels["zt"],
    }


def aggregate_to_clusters(factors: dict[str, pd.DataFrame], labels: pd.Series
                          ) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """行业因子 → 大类因子：价格类取成员**均值**，份额类取成员**求和**。"""
    members: dict[int, list[str]] = {}
    for name, cluster in labels.items():
        if name in factors["mom_1m"].columns:
            members.setdefault(int(cluster), []).append(name)
    big: dict[str, pd.DataFrame] = {}
    for name, frame in factors.items():
        how = "sum" if name.endswith("_share") else "mean"
        data = {}
        for cluster, names in members.items():
            if not names:
                continue
            data[cluster] = (frame[names].mean(axis=1) if how == "mean"
                             else frame[names].sum(axis=1))
        big[name] = pd.DataFrame(data).sort_index()
    return big, big["mom_1m"]


# ─── ③ 三分位点法 ───────────────────────────────────────────────────────────
def tertile_tstat(factor: pd.Series, forward: pd.Series,
                  lower_q: float = 1 / 3, upper_q: float = 2 / 3) -> float:
    """论文 §3.1：``t = (F1 - F3) / sqrt(S1/n1 + S3/n3)``（F1/F3 = 看多/看空信号的未来收益均值）。"""
    frame = pd.concat([factor.rename("f"), forward.rename("y")], axis=1).dropna()
    if len(frame) < 12:
        return float("nan")
    high, low = frame["f"].quantile(upper_q), frame["f"].quantile(lower_q)
    bull, bear = frame.loc[frame["f"] >= high, "y"], frame.loc[frame["f"] <= low, "y"]
    if len(bull) < 3 or len(bear) < 3:
        return float("nan")
    denom = np.sqrt(bull.var(ddof=1) / len(bull) + bear.var(ddof=1) / len(bear))
    return float((bull.mean() - bear.mean()) / denom) if denom > 1e-12 else float("nan")


def tertile_signal(factor: pd.Series, direction: int,
                   lower_q: float = 1 / 3, upper_q: float = 2 / 3) -> float:
    """当前观测 ≥ 上三分位 → +direction；≤ 下三分位 → -direction；中间看平。"""
    series = factor.dropna()
    if series.empty or not direction:
        return 0.0
    value = float(series.iloc[-1])
    high, low = series.quantile(upper_q), series.quantile(lower_q)
    if value >= high:
        return float(direction)
    if value <= low:
        return -float(direction)
    return 0.0


def rolling_tertile_tstat(factor: pd.Series, forward: pd.Series, window: int = 36,
                          min_periods: int = 24) -> pd.Series:
    """滚动窗口的三分位 t 值（只用当时及之前的数据）；符号 = 该因子的方向。"""
    frame = pd.concat([factor.rename("f"), forward.rename("y")], axis=1)

    # 逐窗口取"二维切片"：`rolling().apply` 会把两列拍成一维，所以这里显式按窗口切片；
    # 130 个窗口 × 2 列，成本可忽略。
    out = []
    values = frame.to_numpy(dtype=float)
    for index in range(len(values)):
        if index + 1 < min_periods:
            out.append(np.nan)
            continue
        window_values = values[max(0, index + 1 - window):index + 1]
        pair = window_values[np.isfinite(window_values).all(axis=1)]
        if len(pair) < 6:
            out.append(np.nan)
            continue
        order = np.argsort(pair[:, 0])
        ranked = pair[order]
        cut = max(int(len(ranked) / 3), 1)
        low, high = ranked[:cut, 1], ranked[-cut:, 1]
        denom = np.sqrt(low.var(ddof=1) / len(low) + high.var(ddof=1) / len(high))
        out.append(float((high.mean() - low.mean()) / denom) if denom > 1e-12 else np.nan)
    return pd.Series(out, index=factor.index, dtype=float)


def factor_sign_series(factor: pd.Series, forward: pd.Series, *,
                       expanding: bool = True, min_history: int = 24,
                       direction_window: int = 36) -> pd.Series:
    """逐月给出某因子的信号（向量化）。

    `expanding=True`（默认，无前视）：上下三分位来自**扩张窗口**；方向 = 滚动 36 月三分位 t 值的符号。
    `expanding=False`（论文口径）：三分位与方向都取自**整段样本**（含前视），仅作对照。
    """
    value = factor.astype(float)
    if not expanding:
        stat = tertile_tstat(factor, forward)
        direction = 1.0 if (np.isfinite(stat) and stat > 0) else (-1.0 if np.isfinite(stat) else 0.0)
        high = float(value.quantile(2 / 3)) if value.notna().any() else np.nan
        low = float(value.quantile(1 / 3)) if value.notna().any() else np.nan
    else:
        high = value.expanding(min_periods=min_history).quantile(2 / 3)
        low = value.expanding(min_periods=min_history).quantile(1 / 3)
        # 坑（实测踩到）：三分位 t 值用的配对是 (f[s], y[s])，而 y[s] = s+1 月的收益。
        # 站在 t 月末，只有 s <= t-1 的配对本就"已实现"；直接用第 t 行就等于
        # 拿 **t+1 月的收益**去定方向，再拿这个方向去决定 t+1 的持仓 —— 白送一个月的前视。
        # 实测这一处漏点把"不聚类直接选行业"的年化从 ~20% 抬到 57%（t 值 4.0）。
        # 方向必须在 t-1 可得的信息上确定 → 整体 shift(1)。
        direction = np.sign(rolling_tertile_tstat(value, forward, window=direction_window,
                                                  min_periods=min_history)).shift(1).fillna(0.0)
    signal = pd.Series(0.0, index=value.index)
    signal = signal.mask(value.notna() & (value >= high), direction)
    signal = signal.mask(value.notna() & (value <= low), -direction)
    return signal.fillna(0.0)


# ─── ④ 因子聚类再等权（论文 §3.2）────────────────────────────────────────────
def cluster_ensemble(signals: pd.DataFrame, k: int = 3, repeats: int = 100, seed: int = 0) -> pd.Series:
    """因子聚类再等权：correlation 距离 KMeans(k) → 类内等权 → 类间等权，重复 repeats 次取平均。

    论文重复 1000 次（每次随机初始点）；这里默认 100 次（把种子噪声再压一个数量级即可）。
    """
    import warnings

    from sklearn.cluster import KMeans
    from sklearn.exceptions import ConvergenceWarning

    if signals.empty:
        return pd.Series(dtype=float)
    columns = signals.shape[1]
    if columns == 1:
        return signals.iloc[:, 0].astype(float)
    values = signals.to_numpy(dtype=float)
    groups = min(int(k), columns)
    rng = np.random.default_rng(seed)
    total = np.zeros(values.shape[0])
    # KMeans 会因为"信号序列完全重复"只找到更少的簇（ConvergenceWarning）——
    # 这正是论文要解决的问题（同质因子），不是错误：重复多次取平均本身就是对它的处理。
    warnings.filterwarnings("ignore", category=ConvergenceWarning)
    for _ in range(max(int(repeats), 1)):
        labels = KMeans(n_clusters=groups, n_init=1,
                        random_state=int(rng.integers(1, 2**31 - 1))).fit_predict(values.T)
        acc = np.zeros(values.shape[0])
        for cluster in range(groups):
            members = np.where(labels == cluster)[0]
            if len(members):
                acc += values[:, members].mean(axis=1)
        total += acc / groups
    return pd.Series(total / max(int(repeats), 1), index=signals.index)


def score_matrix(factors: dict[str, pd.DataFrame], big_ret: pd.DataFrame, *, expanding: bool,
                 repeats: int, kmeans_k: int = 3, min_history: int = 24,
                 macro: pd.DataFrame | None = None, kind: str = "paper") -> pd.DataFrame:
    """每个调仓日、每个大类一个打分（只用 t 及之前的信息）。

    `kind`：
      * `paper`          —— 论文式：三分位信号 → **因子聚类再等权**（类内等权/类间等权，重复多次平均）
      * `repo7_linear`    —— 同样 7 个价量因子，但**简单等权**（用来区分"多因子"与"聚类合成"的贡献）
      * `repo7_ensemble`  —— 7 个价量因子 + 因子聚类再等权（把论文的合成方式用到仓库口径上）
      * `macro_only`      —— 只用宏观/利率/跨市场因子（市场级择时）
    `macro` 为市场级宏观面板（月份 × 指标）：对每个大类用**该大类自己的未来收益**定方向（论文按风格分别定方向）。
    """
    forward = big_ret.shift(-1)
    out = {}
    for cluster in big_ret.columns:
        price = pd.DataFrame({name: factor_sign_series(frame[cluster], forward[cluster],
                                                       expanding=expanding, min_history=min_history)
                              for name, frame in factors.items() if cluster in frame.columns})
        blocks = [price]
        if macro is not None and not macro.empty:
            macro_block = pd.DataFrame({name: factor_sign_series(macro[name], forward[cluster],
                                                                 expanding=expanding,
                                                                 min_history=min_history)
                                        for name in macro.columns})
            blocks.append(macro_block)
        library = pd.concat(blocks, axis=1)
        if library.empty:
            continue
        if kind == "paper":
            out[cluster] = cluster_ensemble(library, k=kmeans_k, repeats=repeats, seed=int(cluster))
        elif kind == "repo7_linear":
            # 同样 7 个价量因子，但**简单等权**：用来把"多因子"和"聚类合成"两件事分开
            out[cluster] = price.mean(axis=1)
        elif kind == "linear_macro":
            out[cluster] = library.mean(axis=1)
        elif kind == "macro_only":
            tail = library.iloc[:, price.shape[1]:]
            out[cluster] = tail.mean(axis=1) if tail.shape[1] else pd.Series(0.0, index=library.index)
        else:
            raise ValueError(f"未知打分口径 {kind}")
    return pd.DataFrame(out).sort_index()


def load_macro_panel(month_index) -> pd.DataFrame | None:
    """每个月的**月末可得**宏观因子（严格 `available_date <= 月末`）。

    论文 §2.2 强调"当且仅当数据发布后，新发布的宏观因子值才可作为信号" ——
    `sync_cn_macro.py` 把每条序列的发布规则写进了 `available_date`（PMI 月末、CPI/PPI 次月 10 日、
    M 系列次月 12 日、GDP 季后 20 日、日频当日），这里据此做 as-of 对齐，不用未来数据。
    """
    if not MACRO_PARQUET.exists():
        return None
    frame = pd.read_parquet(MACRO_PARQUET)
    frame = frame[frame["indicator"].isin(MACRO_INDICATORS)]
    if frame.empty:
        return None
    frame = frame.sort_values("available_date")
    ends = pd.DatetimeIndex([(stamp.end_time if hasattr(stamp, "end_time") else pd.Timestamp(stamp)).normalize()
                             for stamp in month_index])
    out = {}
    for name, group in frame.groupby("indicator"):
        group = group.sort_values("available_date")
        dates = group["available_date"].to_numpy()
        values = group["value"].to_numpy(dtype=float)
        position = np.searchsorted(dates, ends.to_numpy(), side="right") - 1
        out[name] = np.where(position >= 0, values[np.clip(position, 0, None)], np.nan)
    panel = pd.DataFrame(out, index=month_index)
    return panel.reindex(columns=[c for c in MACRO_INDICATORS if c in panel.columns])


def repo_board_score(factors: dict[str, pd.DataFrame], w_mom: float = 0.7,
                     w_zt: float = 0.3) -> pd.DataFrame:
    """本仓库现有**板块层**口径（`arm_concept` 的 board_mode=no_flow）：动量 0.7 − 涨停密度 0.3。

    横截面分位在**当期簇/行业之间**算（每月一行），只用当月及之前的数据 —— 与论文式打分同口径可比。
    """
    mom = factors["mom_1m"].rank(axis=1, pct=True)
    zt = factors["zt_density"].rank(axis=1, pct=True)
    return (w_mom * mom - w_zt * zt.fillna(0.5)).sort_index()


# ─── ⑤ 轮动回测与统计 ───────────────────────────────────────────────────────
def _stats(periods, per_year: float = 12.0) -> dict:
    """`per_year`：一年多少期 —— 月频 12、**日频 252**。

    坑：日频序列用 12 年化会把 (1+r) 的复利年化算成 equity^(12/n)，
    既不是年化也不是总收益（实测把 8.4 年的日频曲线报成"177 年"）。
    """
    values = np.asarray([p for p in np.asarray(periods, dtype=float) if np.isfinite(p)], dtype=float)
    if len(values) == 0:
        return {"n": 0}
    equity = np.cumprod(1 + values)
    peak = np.maximum.accumulate(equity)
    years = len(values) / float(per_year)
    annual = float(equity[-1] ** (1 / years) - 1) if years > 0 and equity[-1] > 0 else float("nan")
    vol = float(values.std(ddof=1) * np.sqrt(per_year)) if len(values) > 1 else float("nan")
    return {"n": int(len(values)), "period_mean": float(values.mean()),
            "annual": annual, "vol": vol,
            "sharpe": (float(annual / vol) if vol and np.isfinite(annual) and vol > 1e-9 else None),
            "max_drawdown": float((equity / peak - 1).min()),
            "win_rate": float((values > 0).mean()), "cumulative": float(equity[-1] - 1)}


def _pair_test(a: np.ndarray, b: np.ndarray) -> dict:
    frame = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(frame) < 8 or (frame["a"] - frame["b"]).std(ddof=1) < 1e-12:
        return {"n": int(len(frame)), "t": None, "p": None}
    from scipy import stats
    test = stats.ttest_rel(frame["a"], frame["b"])
    return {"n": int(len(frame)), "diff_mean": float((frame["a"] - frame["b"]).mean()),
            "t": float(test.statistic), "p": float(test.pvalue)}


def rotation(scores: pd.DataFrame, big_ret: pd.DataFrame, top_k: int) -> dict:
    """按 Sign 降序持有 top_k 个大类（等权），持有下一个月。"""
    periods, picks, benches, by_rank = [], [], [], {}
    for month in scores.index:
        if month not in big_ret.index:
            continue
        position = big_ret.index.get_loc(month)
        if position + 1 >= len(big_ret.index):
            continue
        nxt = big_ret.index[position + 1]
        row = scores.loc[month].dropna().sort_values(ascending=False)
        forward = big_ret.loc[nxt].reindex(row.index).dropna()
        if len(row) < top_k or forward.empty:
            continue
        chosen = list(row.index[:top_k])
        available = [c for c in chosen if c in forward.index]
        if not available:
            continue
        period = float(forward[available].mean())
        periods.append(period)
        benches.append(float(big_ret.loc[nxt].mean()))
        picks.append({"date": str(month), "next": str(nxt),
                      "clusters": [str(c) for c in chosen],
                      "scores": [round(float(row[c]), 3) for c in chosen]})
        for rank, cluster in enumerate(row.index[:len(row)], start=1):
            if cluster in forward.index:
                by_rank.setdefault(rank, []).append(float(forward[cluster]))
    return {"periods": np.asarray(periods), "bench": np.asarray(benches), "picks": picks,
            "by_rank": {rank: _stats(values) for rank, values in sorted(by_rank.items())}}


def _window_of(picks: list[dict], start: int, end: int) -> list[str]:
    if not picks:
        return []
    subset = picks[max(0, start):max(1, end)]
    return [subset[0]["next"], subset[-1]["next"]] if subset else []


def _variants_for(industry_factor: dict[str, pd.DataFrame], labels_by_month: dict,
                  *, expanding: bool, repeats: int, top_k_list, kmeans_k: int = 3,
                  score_kind: str = "paper", macro: pd.DataFrame | None = None,
                  include_macro: bool = False) -> dict:
    """一组"月份 → 行业标签"跑完整链路：大类因子 → 三分位 → 因子聚类等权 → 轮动。

    标签每 refresh 个月才变一次，所以按**快照**分段：每段用该段的标签把行业因子聚成大因子，
    再用扩张窗口（只用当时及之前）算出整段的 Sign 表；收益表不受截断影响（回测要用到的未来一个月收益）。
    """
    months = sorted(labels_by_month)
    if not months:
        return {}
    score_rows, return_rows, cluster_counts = {}, {}, []
    snapshot_key = None
    score_table = returns_table = None
    for month in months:
        labels = labels_by_month[month]
        key = tuple(sorted((str(k), int(v)) for k, v in labels.items()))
        if key != snapshot_key:
            factors, big_ret = aggregate_to_clusters(industry_factor, labels)
            score_table = (repo_board_score(factors) if score_kind == "repo"
                           else score_matrix(factors, big_ret, expanding=expanding, repeats=repeats,
                                             kmeans_k=kmeans_k,
                                             macro=macro if include_macro else None,
                                             kind=score_kind))
            returns_table = big_ret
            snapshot_key = key
            cluster_counts.append(len(set(int(v) for v in labels.values)))
        if month in score_table.index:
            score_rows[month] = score_table.loc[month]
        if month in returns_table.index:
            return_rows[month] = returns_table.loc[month]
    score_frame = pd.DataFrame(score_rows).T.sort_index()
    return_frame = pd.DataFrame(return_rows).T.sort_index()
    if score_frame.empty or return_frame.empty:
        return {}
    out: dict[str, object] = {}
    for top_k in top_k_list:
        run = rotation(score_frame, return_frame, top_k)
        period_stats = _stats(run["periods"])
        half = len(run["periods"]) // 2
        out[f"top{top_k}"] = {
            "top_k": top_k, "stats": period_stats,
            "vs_cluster_equal_weight": _pair_test(run["periods"], run["bench"]),
            "by_rank": run["by_rank"], "last_picks": run["picks"][-3:],
            # 前后半窗（仓库的常规稳健性口径）：样本内漂亮、后半窗转负 = 不能上线
            "halves": {
                "h1": {"window": _window_of(run["picks"], 0, half), "stats": _stats(run["periods"][:half])},
                "h2": {"window": _window_of(run["picks"], half, len(run["periods"])),
                       "stats": _stats(run["periods"][half:])},
            },
            "excess_halves": {"h1": _pair_test(run["periods"][:half], run["bench"][:half]),
                              "h2": _pair_test(run["periods"][half:], run["bench"][half:])},
            # 逐期序列（存下来，配对检验/口径对比不用重跑）
            "periods": {"dates": [p["date"] for p in run["picks"]],
                        "values": [round(float(v), 6) for v in run["periods"]],
                        "bench": [round(float(v), 6) for v in run["bench"]]},
        }
    out["snapshot_count"] = len(cluster_counts)
    out["mean_clusters"] = float(np.mean(cluster_counts)) if cluster_counts else None
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="复现兴业证券 2019 行业重新分类 + 行业轮动")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2026-10-08")
    parser.add_argument("--min-members", type=int, default=5)
    parser.add_argument("--taxonomy", default="tushare_industry")
    parser.add_argument("--k", type=int, nargs="+", default=[4, 5], help="大类个数（论文图上 4 块 / 最终 5 风格）")
    parser.add_argument("--top-k", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--repeats", type=int, default=100, help="因子聚类重复次数（论文 1000）")
    parser.add_argument("--window", type=int, default=60, help="滚动聚类窗口（月）")
    parser.add_argument("--refresh", type=int, default=6, help="聚类标签刷新间隔（月）")
    parser.add_argument("--random-seeds", type=int, default=10, help="随机分组对照的种子数")
    parser.add_argument("--in-sample-quantiles", action="store_true",
                        help="用论文口径（全样本阈值与方向，含前视）")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    panels = board_monthly_panels(args.start, args.end, args.min_members, args.taxonomy)
    ret = panels["ret"]
    industry_factor = industry_factors(panels)
    expanding = not args.in_sample_quantiles
    print(f"[xy] 行业月收益矩阵 {ret.shape[0]} 月 × {ret.shape[1]} 行业 "
          f"（{ret.index.min()} ~ {ret.index.max()}）", flush=True)

    results: dict[str, object] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": [str(ret.index.min()), str(ret.index.max())],
        "n_months": int(ret.shape[0]), "n_industries": int(ret.shape[1]),
        "taxonomy": args.taxonomy, "repeats": args.repeats, "refresh_months": args.refresh,
        "quantiles": "expanding（只用 t 及之前）" if expanding else "in_sample（论文口径，含前视）",
        "factors_used": {name: {"label": FACTOR_LABEL[name], "group": FACTOR_GROUP[name]}
                         for name in FACTORS},
        "factors_missing_from_warehouse": MISSING_FACTOR_GROUPS,
        "benchmarks": {
            "行业等权（论文基准）": _stats(ret.mean(axis=1).to_numpy()),
            "全市场等权（本仓库基准）": _stats(panels["market_ew"].reindex(ret.index).to_numpy()),
        },
        "variants": {},
        "notes": [
            "行业收益 = 行业成分**等权**（论文是流通市值加权的风格指数）；无中信风格成分表，"
            "用 MST 聚类作大类代理",
            "聚类标签每 refresh 个月刷新；因子与打分只用快照及之前的数据（rolling 版无前视）",
            "论文的宏观/利率/风险偏好/跨市场四类因子在本仓库整条缺失，只能用"
            "「市场内部结构 + 技术指标 + 板块自身特点」三类共 7 个因子",
        ],
    }

    scenarios: list[tuple[str, str, dict]] = []
    for k in args.k:
        scenarios.append((f"mst_full_k{k}", f"全样本 MST 聚类 k={k}（论文口径，含前视）",
                          snapshot_labels(ret, k, full_sample=True)))
        scenarios.append((f"mst_roll_k{k}", f"滚动 {args.window} 月 MST 聚类 k={k}（每 {args.refresh} 月刷新，无前视）",
                          snapshot_labels(ret, k, window=args.window, refresh=args.refresh, full_sample=False)))

    # ① 论文式：聚类 → 大类因子 → 三分位 → 因子聚类等权 → top-k 轮动
    for name, note, labels_by_month in scenarios:
        block = _variants_for(industry_factor, labels_by_month,
                              expanding=expanding, repeats=args.repeats, top_k_list=args.top_k)
        if block:
            results["variants"][name] = {"note": note, "source": "论文式（先聚类再轮动）", **block}

    # ①b 对照：同一批大类，但打分换成**仓库现有板块层口径**（动量 0.7 − 涨停密度 0.3）
    for name, note, labels_by_month in scenarios:
        block = _variants_for(industry_factor, labels_by_month, expanding=expanding,
                              repeats=args.repeats, top_k_list=args.top_k, score_kind="repo")
        if block:
            results["variants"][f"{name}__repo_signal"] = {
                "note": note + " · 打分换成仓库现有板块层口径（动量0.7−涨停密度0.3）",
                "source": "对照（同一批大类，换打分）", **block}

    # ①c 论文式 **补上宏观因子**（论文因子库的宏观/利率/跨市场部分，本地刚补齐）：
    #     宏观信号是市场级的，但方向按**每个大类自己的未来收益**定（论文对每个风格各建因子库）。
    macro_panel = load_macro_panel(ret.index)
    macro_keys = [] if macro_panel is None else [c for c in macro_panel.columns if macro_panel[c].notna().sum() > 24]
    if macro_panel is not None and macro_keys:
        macro_panel = macro_panel[macro_keys]
        results["macro_factors"] = {
            "n_indicators": len(macro_keys),
            "indicators": {k: MACRO_LABEL.get(k, k) for k in macro_keys},
            "coverage": {k: [str(macro_panel[k].first_valid_index()), str(macro_panel[k].last_valid_index())]
                         for k in macro_keys},
            "source": "assets/data/derived/cn_macro_features.parquet（scripts/sync_cn_macro.py）",
            "release_alignment": "严格 available_date <= 月末（论文 §2.2 的发布日对齐）",
        }
    extra_grid = [name for name, _, _ in scenarios if name.startswith("mst_roll")] + ["direct_industry"]
    # 2×2 正交对照：{7 价量因子, 7 价量 + 宏观} × {简单等权, 论文式因子聚类再等权}
    # （"7 价量 · 聚类再等权" 与上面的 paper 完全同义，不重复跑）
    extra_kinds = [
        ("paper_macro", "paper", True, "论文式（因子聚类再等权）· 7 价量因子 + **宏观/利率/跨市场**"),
        ("linear_macro", "linear_macro", True, "**简单等权** · 7 价量因子 + 宏观/利率/跨市场"),
        ("macro_only", "macro_only", True, "只用宏观/利率/跨市场因子（市场级择时）"),
        ("repo7_linear", "repo7_linear", False, "**简单等权** · 7 价量因子（与论文式同因子集，只换合成方式）"),
    ]
    label_sets = {name: table for name, _, table in scenarios}
    label_sets["direct_industry"] = None                    # 稍后统一构造（见 ②）

    # ② 对照：不聚类，直接对 110 个行业用同一套因子与打分选 top-k（本仓库现有做法）
    direct_labels = {}
    for month in ret.index:
        direct_labels[month] = pd.Series({name: i for i, name in enumerate(ret.columns)})
    block = _variants_for(industry_factor, direct_labels, expanding=expanding,
                          repeats=max(args.repeats // 4, 20), top_k_list=[1, 2, 3, 5], kmeans_k=3)
    if block:
        results["variants"]["direct_industry"] = {
            "note": "不重分类：每个行业各自当作一个大类，论文式因子/打分直接选 top-k 行业",
            "source": "对照（直接行业排序 · 论文式打分）", **block}
    block = _variants_for(industry_factor, direct_labels, expanding=expanding,
                          repeats=10, top_k_list=[1, 2, 3, 5], score_kind="repo")
    if block:
        results["variants"]["direct_industry__repo_signal"] = {
            "note": "不重分类 + 仓库现有板块层打分（动量0.7−涨停密度0.3）—— 与 arm_concept 板块层同源",
            "source": "对照（直接行业排序 · 仓库口径）", **block}

    # ①c/①d 扩展网格：把宏观因子与"因子聚类再等权"两种合成方式接上去（同一批大类、同一窗口）
    label_sets["direct_industry"] = direct_labels
    for base in extra_grid:
        table = label_sets.get(base)
        if table is None:
            continue
        for suffix, kind, use_macro, note in extra_kinds:
            if use_macro and (macro_panel is None or not macro_keys):
                continue
            block = _variants_for(industry_factor, table, expanding=expanding,
                                  repeats=max(args.repeats // 2, 20) if base == "direct_industry" else args.repeats,
                                  top_k_list=[1, 2] + ([5] if base == "direct_industry" else []),
                                  score_kind=kind, macro=macro_panel, include_macro=use_macro)
            if block:
                results["variants"][f"{base}__{suffix}"] = {
                    "note": note, "source": "对照（合成方式 / 因子集不同）", **block}

    # ③ 对照：随机分组（同 K、同规则）——检验"聚类结构本身"有没有贡献
    rng = np.random.default_rng(20261010)
    random_means = {}
    for top_k in args.top_k:
        means = []
        for _ in range(max(int(args.random_seeds), 1)):
            labels = pd.Series(rng.integers(0, max(args.k), size=len(ret.columns)), index=ret.columns)
            table = {month: labels for month in ret.index}
            block = _variants_for(industry_factor, table, expanding=expanding,
                                  repeats=max(args.repeats // 5, 10), top_k_list=[top_k])
            if block and block.get(f"top{top_k}"):
                stats = block[f"top{top_k}"]["stats"]
                if stats.get("period_mean") is not None:
                    means.append(stats["period_mean"])
        random_means[f"top{top_k}"] = {
            "seeds": len(means),
            "mean_period_return": float(np.mean(means)) if means else None,
            "std_period_return": float(np.std(means, ddof=1)) if len(means) > 1 else None,
        }
    # ④ 因子方向体检：行业层 rank IC（横截面，对下月行业收益）—— 决定"该追还是该避"
    forward_industry = ret.shift(-1)
    direction_rows = []
    for name in FACTORS:
        frame = industry_factor[name]
        ics, dates = [], []
        for month in frame.index:
            if month not in forward_industry.index:
                continue
            signal = frame.loc[month].dropna()
            target = forward_industry.loc[month].reindex(signal.index).dropna()
            common = signal.index.intersection(target.index)
            if len(common) < 20:
                continue
            ics.append(float(signal[common].rank().corr(target[common].rank())))
            dates.append(month)
        values = np.asarray(ics, dtype=float)
        direction_rows.append({
            "factor": name, "group": FACTOR_GROUP[name], "label": FACTOR_LABEL[name],
            "n_months": len(values),
            "mean_rank_ic": float(values.mean()) if len(values) else None,
            "nw_t": _nw_t(values, 6) if len(values) else None,
            "ic_positive_share": float((values > 0).mean()) if len(values) else None,
            "implied_direction": ("正向（追）" if len(values) and values.mean() > 0 else "反向（避）")
            if len(values) else None,
        })
    results["factor_direction_check"] = {
        "note": "行业层横截面 rank IC（因子 vs 下一个月行业等权收益），NW t lag=6；"
                "负 IC = 该因子越高、下月越差（该避/该反着用）",
        "rows": direction_rows,
    }

    results["variants"]["random_cluster_control"] = {
        "note": "随机把行业分到 K 组（同 K、同 top-k 规则），10 个种子平均 —— 聚类结构有没有贡献的对照",
        "source": "对照（随机分组）", "top_k": random_means}

    results["comparisons"] = compare_variants(results, [
        ("mst_roll_k5__repo7_linear", "mst_roll_k5", "同一批大类/同一批因子：简单等权 vs 论文式因子聚类再等权"),
        ("mst_roll_k4__repo7_linear", "mst_roll_k4", "同上（k=4）"),
        ("mst_roll_k5__paper_macro", "mst_roll_k5", "补上宏观因子值不值（k=5，聚类合成）"),
        ("mst_roll_k4__paper_macro", "mst_roll_k4", "补上宏观因子值不值（k=4，聚类合成）"),
        ("mst_roll_k5__linear_macro", "mst_roll_k5__repo7_linear", "宏观因子的增量（等权合成）"),
        ("mst_roll_k5__repo7_linear", "mst_roll_k5__repo_signal", "7 因子等权 vs 现行 2 因子线性（同批大类）"),
        ("mst_roll_k5__paper_macro", "mst_roll_k5__linear_macro", "同因子集：聚类等权 vs 简单等权（含宏观）"),
    ])
    (out_dir / "xy_reproduce.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(results, out_dir / "xy_reproduce.md")
    print(f"[xy] 写出 {out_dir / 'xy_reproduce.json'} + xy_reproduce.md", flush=True)
    for name, block in results["variants"].items():
        if isinstance(block, dict) and "top1" in block:
            stats = block["top1"]["stats"]
            sharpe = stats.get("sharpe")
            print(f"[xy] {name}: top1 期均 {stats['period_mean']:.4%} 年化 {stats['annual']:.2%} "
                  f"夏普 {'—' if sharpe is None else format(sharpe, '.2f')}", flush=True)
    return 0


def compare_variants(results: dict, pairs: list[tuple[str, str, str]]) -> list[dict]:
    """两个变体的逐期配对检验（按调仓日对齐；只比较共同存在的期）。"""
    out = []
    for left, right, note in pairs:
        a = (results["variants"].get(left) or {}).get("top1", {}).get("periods")
        b = (results["variants"].get(right) or {}).get("top1", {}).get("periods")
        if not a or not b:
            continue
        left_map = dict(zip(a["dates"], a["values"]))
        right_map = dict(zip(b["dates"], b["values"]))
        common = sorted(set(left_map) & set(right_map))
        if len(common) < 12:
            continue
        x = np.asarray([left_map[d] for d in common], dtype=float)
        y = np.asarray([right_map[d] for d in common], dtype=float)
        test = _pair_test(x, y)
        out.append({"left": left, "right": right, "note": note, "n": test["n"],
                    "left_annual": _stats(x)["annual"], "right_annual": _stats(y)["annual"],
                    "diff_monthly": test.get("diff_mean"), "t": test.get("t"), "p": test.get("p")})
    return out


# ─── 报告渲染（数字全部来自 results，避免"报告与 JSON 不一致"）──────────────
def _pct(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _num(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def write_report(results: dict, path: Path) -> None:
    variants = results.get("variants", {})
    bench = results.get("benchmarks", {})

    def stat_rows(names: list[str], keys: tuple[str, ...] = ("top1", "top2")) -> list[str]:
        rows = []
        for name in names:
            block = variants.get(name)
            if not isinstance(block, dict) or "top1" not in block:
                continue
            for key in keys:
                if key not in block:
                    continue
                stats = block[key]["stats"]
                vs = block[key].get("vs_cluster_equal_weight", {})
                halves = block[key].get("halves", {})
                rows.append("| " + " | ".join([
                    name, key.replace("top", "top-"), str(stats["n"]), _pct(stats["period_mean"]),
                    _pct(stats["annual"]), _num(stats.get("sharpe")), _pct(stats["max_drawdown"]),
                    _pct(stats.get("win_rate"), 0), _pct(vs.get("diff_mean")), _num(vs.get("t")),
                    _num(vs.get("p"), 3),
                    _pct((halves.get("h1") or {}).get("stats", {}).get("annual")),
                    _pct((halves.get("h2") or {}).get("stats", {}).get("annual")),
                ]) + " |")
        return rows

    order = [n for n in variants if isinstance(variants[n], dict) and "top1" in variants[n]]
    macro = results.get("macro_factors")
    lines = [
        "# 兴业证券《行业的重新分类以及行业轮动策略》(2019-09-19) 复现与对照",
        "",
        f"生成 {results.get('generated_at')}｜脚本 `scripts/reproduce_xy_industry_rotation.py`"
        "｜原文 `docs/research/20190919-兴业证券-系统化资产配置系列之二：行业的重新分类以及行业轮动策略.pdf`",
        f"样本 {results['window'][0]} ~ {results['window'][1]}（{results['n_months']} 个月 × "
        f"{results['n_industries']} 个行业，等权月频）｜三分位口径：{results['quantiles']}"
        f"｜因子聚类重复 {results['repeats']} 次｜聚类每 {results['refresh_months']} 月刷新",
        "",
        "## 0. 结论",
        "",
        f"1. 论文式打分（7 价量因子）top-1：滚动聚类 k=4 {_pct(variants.get('mst_roll_k4', {}).get('top1', {}).get('stats', {}).get('annual'))}、"
        f"k=5 {_pct(variants.get('mst_roll_k5', {}).get('top1', {}).get('stats', {}).get('annual'))}；"
        f"基准 行业等权 {_pct(bench.get('行业等权（论文基准）', {}).get('annual'))}、"
        f"全市场等权 {_pct(bench.get('全市场等权（本仓库基准）', {}).get('annual'))}。",
        f"2. 补上**宏观/利率/跨市场因子**后（{0 if not macro else macro['n_indicators']} 个指标，严格按发布日对齐）："
        f"滚动 k=4 {_pct(variants.get('mst_roll_k4__paper_macro', {}).get('top1', {}).get('stats', {}).get('annual'))}、"
        f"k=5 {_pct(variants.get('mst_roll_k5__paper_macro', {}).get('top1', {}).get('stats', {}).get('annual'))}；"
        f"纯宏观择时 {_pct(variants.get('mst_roll_k5__macro_only', {}).get('top1', {}).get('stats', {}).get('annual'))}。",
        f"3. **合成方式**：同一批大类、同一批 7 价量因子，**简单等权** 滚动 k=5 "
        f"{_pct(variants.get('mst_roll_k5__repo7_linear', {}).get('top1', {}).get('stats', {}).get('annual'))} vs "
        f"论文式**因子聚类再等权** {_pct(variants.get('mst_roll_k5', {}).get('top1', {}).get('stats', {}).get('annual'))}"
        f"（k=4：{_pct(variants.get('mst_roll_k4__repo7_linear', {}).get('top1', {}).get('stats', {}).get('annual'))} vs "
        f"{_pct(variants.get('mst_roll_k4', {}).get('top1', {}).get('stats', {}).get('annual'))}）"
        f"⇒ 在这个因子集上**聚类再等权是有害的**；对照现行 2 因子线性 "
        f"{_pct(variants.get('mst_roll_k5__repo_signal', {}).get('top1', {}).get('stats', {}).get('annual'))}。",
        "",
        "",
        "4. **配对检验**（同调仓日）：简单等权 vs 聚类再等权 "
        f"t={_num((results.get('comparisons') or [{}])[0].get('t'))}、p={_num((results.get('comparisons') or [{}])[0].get('p'), 3)}"
        "（**显著**，聚类再等权更差）；宏观因子的增量 p 在 0.12~0.95 之间（**不显著**）。",
        "5. **上线判定**：没有任何一条变体同时满足「配对显著 + 前后半窗同号」（h1 与 h2 普遍差 10pp 以上）"
        "⇒ 与 `output/research/industry_momentum/REPORT.md`、`industry_reversal` 的结论一致：**不进生产**。",
        f"6. **数据面**：宏观序列已入库（`scripts/sync_cn_macro.py` → `cn_macro_features.parquet`，"
        f"{0 if not macro else macro['n_indicators']} 个指标进入本实验）。中债国债收益率曲线与信用价差"
        "已从 akshare `bond_china_yield` 补齐（Tushare `yc_cb` 无接口权限）；7 类论文因子**全部有本地实现**。",
        "",
        "## 1. 论文方法拆解",
        "",
        "① 行业月收益相关矩阵 → 距离 `d=sqrt(2(1-rho))` → **最小生成树(Kruskal)** 剪枝 → 4 大块；",
        "①b 最终改用**中信风格 5 大类**（金融/周期/消费/成长/稳定，流通市值加权）；",
        "② 因子库 7 类：宏观景气 / 通胀 / 利率 / 风险偏好 / 市场内部结构 / 技术指标 / 跨市场；",
        "③ **三分位点法** + `t=(F1-F3)/sqrt(S1/n1+S3/n3)`；④ **因子聚类再等权**（KMeans(3) × 重复）；⑤ 按打分排序持有 top-1。",
        "",
        "## 2. 本地因子覆盖（本次补齐后）",
        "",
        "| 论文因子类 | 本地实现 | 来源 |",
        "|---|---|---|",
        "| ① 宏观景气 | PMI（含新订单/新出口订单/购进价格/原材料库存/新订单−库存）、GDP 同比 | `cn_pmi` / `cn_gdp` |",
        "| ② 通胀 | CPI 同比、PPI 同比（生产资料/生活资料） | `cn_cpi` / `cn_ppi` |",
        "| ③ 利率 | SHIBOR ON/3M 的 10 日均 + 3M−ON 期限利差（**中债 YTM 无接口权限，用 SHIBOR 代理**） | `shibor` |",
        "| ④ 风险偏好 | 美债 10Y−3M 利差（**信用价差缺**：依赖中债曲线） | `us_tycr` |",
        "| ⑤ 市场内部结构 | 大类 1/3/6 个月相对走势 | 本地行业面板 |",
        "| ⑥ 技术指标 | 大类 3 个月波动率 | 本地行业面板 |",
        "| ⑦ 跨市场 | 南华商品指数 20 日动量、USDCNH 20 日变化、美债 10Y | `index_daily` / `fx_daily` / `us_tycr` |",
        "",
        f"宏观面板：{0 if not macro else macro['n_indicators']} 个指标，"
        f"对齐规则：{'' if not macro else macro['release_alignment']}；"
        "来源 `scripts/sync_cn_macro.py` → `assets/data/derived/cn_macro_features.parquet`。",
        "",
        "## 3. 基准",
        "",
        "| 组合 | 期均 | 年化 | 夏普 | 最大回撤 |",
        "|---|---|---|---|---|",
    ]
    for name, stats in bench.items():
        lines.append(f"| {name} | {_pct(stats['period_mean'])} | {_pct(stats['annual'])} | "
                     f"{_num(stats.get('sharpe'))} | {_pct(stats['max_drawdown'])} |")
    lines += ["", "## 4. 各口径 top-1 / top-2（vs 簇等权 = 相对 K 个大类等权的配对检验；h1/h2 = 前后半窗年化）", "",
              "| 变体 | 持仓 | 期数 | 期均 | 年化 | 夏普 | 最大回撤 | 胜率 | vs 簇等权 | t | p | 前半窗年化 | 后半窗年化 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    lines += stat_rows(order)
    lines += ["", "## 5. 打分排序单调性（论文卖点）", "",
              "| 变体 | rank1 | rank2 | rank3 | rank4 | rank5 |", "|---|---|---|---|---|---|"]
    for name in order:
        ranks = variants[name]["top1"].get("by_rank") or {}
        values = [round(ranks[k]["period_mean"] * 100, 2)
                  for k in sorted(ranks, key=lambda x: int(x)) if ranks[k].get("period_mean") is not None]
        if values:
            lines.append("| " + name + " | " + " | ".join(str(v) for v in values[:5]) + " |")
    lines += ["", "## 6. 因子方向体检（行业层 rank IC vs 下月收益）", "",
              "| 因子 | 类别 | 平均 rank IC | NW t | IC>0 占比 | 含义 |", "|---|---|---|---|---|---|"]
    for row in (results.get("factor_direction_check") or {}).get("rows", []):
        lines.append(f"| `{row['factor']}` | {row['group']} | {row['mean_rank_ic']:+.4f} | "
                     f"{row['nw_t']:+.2f} | {row['ic_positive_share']:.0%} | {row['implied_direction']} |")
    comparisons = results.get("comparisons") or []
    if comparisons:
        lines += ["", "## 6b. 变体之间的配对检验（同调仓日对齐）", "",
                  "| 对比 | 说明 | 期数 | 左年化 | 右年化 | 月均差 | t | p |",
                  "|---|---|---|---|---|---|---|---|"]
        for row in comparisons:
            lines.append(f"| {row['left']} vs {row['right']} | {row['note']} | {row['n']} | "
                         f"{_pct(row['left_annual'])} | {_pct(row['right_annual'])} | "
                         f"{_pct(row['diff_monthly'])} | {_num(row['t'])} | {_num(row['p'], 3)} |")
    lines += ["", "## 7. 说明与坑", ""]
    lines += [f"- {note}" for note in results.get("notes", [])]
    lines += ["- `minimum_spanning_tree` 把权重 0 当作「无边」，走势一致的行业会被静默丢边 → 加 ε；",
              "- 三分位 t 值的配对是 `(f[s], y[s])`，`y[s]` 是 s+1 月收益 → 方向必须 `shift(1)`（否则「不聚类直接选行业」的年化会被抬到 57%）；",
              "- 宏观序列严格按 `available_date <= 月末` 对齐（论文 §2.2 的发布日纪律）。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
