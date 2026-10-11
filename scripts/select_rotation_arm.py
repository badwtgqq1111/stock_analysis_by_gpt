#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行业轮动手臂（rot_arm）：把**复现里效果最好的板块层口径**落成每日名单。

背景（`output/research/industry_rotation/xy_reproduce.md` / `xy_daily.md`）：
  把兴业证券 2019《行业的重新分类以及行业轮动策略》的五步机件（相关距离 → 最小生成树聚类 →
  三分位点法 → 因子合成 → 按打分排序持有）在本地 105 个行业上复现并做了 A/B：
    * 板块层用 **7 个价量因子简单等权**：滚动聚类 k=5 top-1 毛年化 **12.38%**
      （同一批大类、同一批因子换成论文式"因子聚类再等权"只有 3.18%，配对 t=2.03、p=0.045
      → 论文的合成方式在本地**显著更差**，所以这里用简单等权）；
    * 对照：现行 `arm_concept` 的板块层（动量0.7−涨停密度0.3，2 因子）同框架下 6.89%；
    * 宏观因子（25 个，按发布日对齐）只在论文式合成里有用，简单等权下增量不显著
      → **默认不掺宏观**，`--board-kind paper_macro` 可切换。
  ⚠️ 显著性：k=5 top-1 相对大类等权 t=1.51（p=0.135），**未过上线闸门** —— 所以登记为
  `stage=candidate`（研究/备选），每日出名单、进选股日历与策略池，但**不参与生产订单与权重**。

口径：
  * 板块层（本手臂新增）：MST 滚动 60 个月聚类（每 6 个月刷新）→ 4~5 个大类；
    7 因子（1/3/6 个月动量、3 个月波动、成交额份额、换手份额、涨停密度）横截面分位 → 等权 → 取 top-k 大类；
  * 个股层（**复用 `arm_concept`**）：大类成分 = 成员个股，板块内买滞涨 + 资金流 + 流动性；
    每大类 ≤4（候选）/ ≤2（持仓）、每行业 ≤2；排除当日涨停、ST、20 日均成交额 < 5000 万；
  * 历史：每次写 `output/results_cn/rot_arm_history/<决策日>.csv` → `build_selection_ledger.py`
    读它进**选股日历**（与 arm/concept/jq 各源并列）。

用法：
  uv run python scripts/select_rotation_arm.py --as-of 2026-10-09
  uv run python scripts/select_rotation_arm.py --board-kind paper_macro --k 4 --top-clusters 2
输出：
  output/results_cn/rot_arm_selection.csv / .json（最新）+ rot_arm_history/<决策日>.csv
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
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import backend.services.market_board_service as mbs  # noqa: E402
import reproduce_xy_industry_rotation as rot  # noqa: E402
import select_concept_arm as arm  # noqa: E402
import xy_daily_rotation as daily  # noqa: E402

OUT = ROOT / "output/results_cn"
HISTORY = OUT / "rot_arm_history"
BOARD_KINDS = {
    "repo7_linear": ("repo7_linear", False, "7 个价量因子简单等权（复现里最优：12.38%/年，t=1.51）"),
    "paper_macro": ("paper", True, "论文式：7 价量 + 25 个宏观因子，因子聚类再等权"),
}


def mst_clusters_min_size(dist: pd.DataFrame, k: int, min_size: int = 8) -> dict[str, int]:
    """Kruskal 最小生成树 + 剪枝成 k 个大类，**但只剪"两边都 ≥ min_size"的边**。

    为什么不能照搬"剪最长的 k-1 条边"（论文/研究脚本的口径）：MST 的叶子常常是**单个行业**
    —— 实测 k=5 时 `电信运营` 被单独剪成一块，于是"选中的大类"只有 1 个行业、
    个股候选池只剩 2 只票。论文里的 5 个风格大类（金融/周期/消费/成长/稳定）都是几十个
    三级行业的大集合，加最小规模约束更贴近原文，也让个股层的池子够用。
    研究脚本 `reproduce_xy_industry_rotation.py` 仍保留论文原口径（便于对照）。
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import minimum_spanning_tree

    names = list(dist.index)
    count = len(names)
    weights = np.nan_to_num(dist.to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    np.fill_diagonal(weights, 0.0)
    weights = np.where(weights > 0, weights + 1e-9, 0.0)      # 权重 0 会被当成"没有边"
    mst = minimum_spanning_tree(csr_matrix(weights)).tocoo()
    edges = sorted(zip(mst.data.tolist(), mst.row.tolist(), mst.col.tolist()), reverse=True)

    def component_sizes(kept: list[tuple[float, int, int]]) -> np.ndarray:
        parent = list(range(count))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for _, i, j in kept:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
        sizes: dict[int, int] = {}
        for index in range(count):
            root = find(index)
            sizes[root] = sizes.get(root, 0) + 1
        return np.asarray([sizes[find(i)] for i in range(count)], dtype=int)

    kept = list(edges)
    cuts = 0
    for edge in edges:
        if cuts >= max(int(k) - 1, 0):
            break
        trial = [e for e in kept if e is not edge]
        after = component_sizes(trial)
        # 树里删掉一条边必然分成两半：两半都够大才允许剪
        if after[edge[1]] >= int(min_size) and after[edge[2]] >= int(min_size):
            kept = trial
            cuts += 1
    roots = component_sizes(kept)
    labels = list({})
    unique: list[int] = []
    for value in roots.tolist():
        if value not in unique:
            unique.append(int(value))
    mapping = {name: unique.index(int(roots[i])) for i, name in enumerate(names)}
    _ = labels
    return mapping


def monthly_from_daily(daily_ret: pd.DataFrame) -> pd.DataFrame:
    """日频行业收益 → 月频（复利）。聚类只用到月频相关矩阵，不必再读一遍日线。"""
    periods = daily_ret.index.to_period("M")
    return (1 + daily_ret).groupby(periods).prod() - 1


def cluster_labels(daily_ret: pd.DataFrame, k: int, window: int, refresh: int,
                   min_size: int = 8) -> tuple[pd.Series, str]:
    """最新一期的聚类标签（滚动窗口，无前视）+ 标签所属月份。

    与研究脚本的区别只有一处：**加了最小大类规模约束**（见 `mst_clusters_min_size`），
    否则 k=5 时可能出现只有一个行业的"大类"（实测 `电信运营`），个股池只剩 2 只票。
    """
    monthly = monthly_from_daily(daily_ret)
    months = list(monthly.index)
    labels_by_month: dict = {}
    current, last_refresh = None, None
    for index, month in enumerate(months):
        if current is None or (last_refresh is not None and index - last_refresh >= int(refresh)):
            history = monthly.iloc[max(0, index + 1 - int(window)):index + 1]
            dist = rot.corr_distance(history, min_periods=24)
            usable = dist.dropna(how="all").dropna(axis=1, how="all")
            if usable.shape[0] > k and usable.shape[1] > k:
                current = pd.Series(mst_clusters_min_size(usable, k, min_size=min_size))
                last_refresh = index
        if current is not None:
            labels_by_month[month] = current
    months_labeled = sorted(labels_by_month)
    if not months_labeled:
        raise SystemExit("聚类标签为空：历史样本不足（需要 ≥24 个月）")
    month = months_labeled[-1]
    return labels_by_month[month], str(month)


def _assemble(snap: pd.Timestamp, stocks: pd.DataFrame, membership: pd.DataFrame, choice: dict,
              basics: pd.DataFrame, *, stock_mode: str, board_kind: str, candidates: int,
              holdings: int, per_cluster: int, max_per_cluster_hold: int,
              max_per_industry: int, capital: float) -> pd.DataFrame:
    """个股层 + 约束 + 输出表（`run()` 与口径对比共用）。"""
    labels = choice["labels"]
    chosen = choice["chosen"]
    pool = membership.rename(columns={"board": "industry"}).merge(
        stocks.reset_index(), on="stock_code", how="inner")
    pool = pool[pool["industry"].isin(choice["industries"])].copy()
    pool["cluster"] = pool["industry"].map(lambda name: int(labels.get(name, -1)))
    pool["cluster_score"] = pool["cluster"].map(chosen).astype(float)
    pool = pool[pool["tradable_ok"]].sort_values(["cluster_score", "stock_score"], ascending=False)
    pool = pool.drop_duplicates(subset=["stock_code"], keep="first")

    def _name(code: str) -> str:
        return str(basics.loc[code, "name"]) if (not basics.empty and code in basics.index) else ""

    # 候选池：每大类 ≤per_cluster、每行业 ≤max_per_industry
    rows, cluster_counts, industry_counts = [], {}, {}
    for _, row in pool.iterrows():
        cluster, industry = int(row["cluster"]), str(row["industry"])
        if cluster_counts.get(cluster, 0) >= int(per_cluster):
            continue
        if industry_counts.get(industry, 0) >= int(max_per_industry):
            continue
        rows.append(row)
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(rows) >= int(candidates):
            break
    pool = pd.DataFrame(rows)
    if pool.empty:
        raise SystemExit("候选池为空：选定大类里没有可交易标的")

    held_rows, hold_counts = [], {}
    for _, row in pool.iterrows():
        cluster = int(row["cluster"])
        if hold_counts.get(cluster, 0) >= int(max_per_cluster_hold):
            continue
        held_rows.append(row)
        hold_counts[cluster] = hold_counts.get(cluster, 0) + 1
        if len(held_rows) >= int(holdings):
            break
    held_codes = {str(r["stock_code"]) for r in held_rows}
    budget = capital / max(len(held_codes), 1)

    table_rows = []
    for rank, (_, row) in enumerate(pool.iterrows(), 1):
        code = str(row["stock_code"])
        px = None if pd.isna(row.get("close")) else float(row["close"])
        in_portfolio = code in held_codes
        lots = int(budget / (px * 100)) if (px and in_portfolio) else 0
        table_rows.append({
            "trade_date": snap.strftime("%Y-%m-%d"), "stock_code": code,
            "name": _name(code), "board": f"大类{int(row['cluster'])}",
            "industry": str(row["industry"]),
            "score": round(float(row["stock_score"]), 6), "rank": rank,
            "in_portfolio": bool(in_portfolio),
            "target_weight": round(1.0 / max(len(held_codes), 1), 4) if in_portfolio else 0.0,
            "last_close": px, "tradable": True, "limit_up": False,
            "lots_at_capital": lots, "planned_amount": round(lots * (px or 0) * 100, 0),
            "pit_name": row.get("pit_name") if "pit_name" in row else _name(code),
            "mktcap_rank": None,
            "concept_score": round(float(row["cluster_score"]), 6),
            "rev_5d": None if pd.isna(row.get("rev_5d")) else round(float(row["rev_5d"]), 5),
            "flow_5d_yuan": None if pd.isna(row.get("flow_5d")) else round(float(row["flow_5d"]), 0),
            "stock_mode": stock_mode, "board_mode": board_kind,
        })
    return pd.DataFrame(table_rows)


def board_choice(panels: dict, *, kind: str, use_macro: bool, k: int, top_clusters: int,
                 repeats: int = 20, min_cluster_size: int = 8, as_of=None,
                 macro_panel=None) -> dict:
    """板块层：日频行业面板 → MST 大类 → 打分（论文式 / 7 因子等权）→ top-k 大类 → 成员行业。

    抽出来是为了让 `run()`（出货口径）与口径对比（`rot_arm_variant_sweep.py`）**共用同一段逻辑** ——
    否则"对比脚本"和"出货脚本"各写一份，迟早漂移成两个不同的策略。
    `panels` 允许被调用方预先加载并按 as_of 切片（对比脚本一次加载、多口径复用）。
    """
    factors = daily.daily_factors(panels)
    labels, label_month = cluster_labels(panels["ret"], k, window=60, refresh=6,
                                         min_size=min_cluster_size)
    cluster_factors, _ = rot.aggregate_to_clusters(factors, labels)
    cluster_ret_1d = daily._cluster_returns_daily(panels["ret"], labels)
    macro = macro_panel if macro_panel is not None else (
        rot.load_macro_panel(panels["ret"].index) if use_macro else None)
    if macro is not None:
        # 防御：调用方（口径对比）可能传进来**整段**宏观面板，而 `panels` 已按 as_of 切片 ——
        # 两边索引不一致会在 `pd.concat` 里给价量信号补出 NaN，KMeans 直接报
        # "Input X contains NaN"（实测踩到）。统一对齐到行业面板的索引。
        macro = macro.reindex(panels["ret"].index)
    scores = rot.score_matrix(cluster_factors, cluster_ret_1d, expanding=True, repeats=repeats,
                              macro=macro, kind=kind)
    history = scores if as_of is None else scores.loc[:as_of]
    history = history.dropna(how="all")
    if history.empty:
        raise SystemExit("打分表为空：检查行业面板与因子")
    latest = history.iloc[-1].sort_values(ascending=False)
    top = latest.head(int(top_clusters))
    chosen = {int(c): float(v) for c, v in top.items()}
    industries = [name for name, cluster in labels.items() if int(cluster) in chosen]
    return {"labels": labels, "chosen": chosen, "industries": industries,
            "label_month": label_month,
            "all_scores": {int(c): float(v) for c, v in latest.items()},
            "n_indicators_macro": 0 if macro is None else int(macro.shape[1])}


def run(as_of: str = "", *, k: int = 5, top_clusters: int = 1, candidates: int = 10,
        holdings: int = 10, capital: float = 200_000.0, per_cluster: int = 10,
        max_per_cluster_hold: int = 10, max_per_industry: int = 2,
        stock_mode: str = "reversal", board_kind: str = "repo7_linear",
        repeats: int = 20, start: str = "2016-01-01", min_cluster_size: int = 8) -> dict:
    if board_kind not in BOARD_KINDS:
        raise SystemExit(f"未知 board-kind={board_kind}；可选 {sorted(BOARD_KINDS)}")
    kind, use_macro, kind_label = BOARD_KINDS[board_kind]

    # 坑（实测踩到）：`enriched_daily(120)` 只给**最近 120 天**。回填历史决策日时 as_of 不在窗口里，
    # 第一版用 `if snap not in dates: snap = dates[-1]` **静默**退回最新交易日 —— 于是 2025-09-08 与
    # 2026-09-25 出的是**同一份名单**（用未来数据写了历史，正是最危险的那种前视）。
    # 现在：窗口按 as_of 反推（`window_days_for`），并**拒绝**静默退回。
    snapshot = pd.Timestamp(as_of).normalize() if as_of else pd.Timestamp(
        sorted(mbs.enriched_daily(5)["trade_date"].unique())[-1])
    lookback = max(120, int(mbs.window_days_for(snapshot)) + 35)
    bars = mbs.enriched_daily(lookback)
    dates = sorted(bars["trade_date"].unique())
    snap = snapshot
    if snap not in dates:
        raise SystemExit(f"行情窗口（{lookback} 天）覆盖不到 {snap.date()}；"
                         f"可用区间 {pd.Timestamp(dates[0]).date()} ~ {pd.Timestamp(dates[-1]).date()}")

    # ── 板块层：日频行业面板 → MST 大类 → 打分 → top-k 大类 ────────────────────
    panels = daily.board_daily_panels(start, snap.strftime("%Y-%m-%d"))
    choice = board_choice(panels, kind=kind, use_macro=use_macro, k=k,
                          top_clusters=top_clusters, repeats=repeats,
                          min_cluster_size=min_cluster_size, as_of=snap)
    labels, chosen = choice["labels"], choice["chosen"]
    label_month = choice["label_month"]

    # ── 个股层：复用 arm_concept 的个股打分 + 约束 ─────────────────────────────
    stocks = arm.stock_scores(bars, arm._moneyflow_window(snap), snap, mode=stock_mode)
    table = _assemble(snap, stocks, rot._membership("tushare_industry"), choice, mbs.basic_frame(),
                      stock_mode=stock_mode, board_kind=board_kind, candidates=candidates,
                      holdings=holdings, per_cluster=per_cluster,
                      max_per_cluster_hold=max_per_cluster_hold,
                      max_per_industry=max_per_industry, capital=capital)

    return {
        "snapshot_date": snap.strftime("%Y-%m-%d"), "table": table,
        "label_month": label_month, "board_kind": board_kind, "kind": kind, "use_macro": use_macro,
        "kind_label": kind_label, "k": int(k), "top_clusters": int(top_clusters),
        "scores": {str(int(c)): round(float(v), 4) for c, v in choice["all_scores"].items()},
        "chosen_clusters": {str(int(c)): round(float(v), 4) for c, v in chosen.items()},
        "cluster_industries": {str(int(c)): sorted(n for n, lab in labels.items() if int(lab) == int(c))
                               for c in sorted(set(labels.astype(int)))},
        "chosen_industries": sorted(choice["industries"]),
        "n_industries": int(labels.shape[0]),
        "n_indicators_macro": int(choice.get("n_indicators_macro", 0)),
        "stock_mode": stock_mode, "max_per_industry": max_per_industry,
        "per_cluster": per_cluster, "max_per_cluster_hold": max_per_cluster_hold,
    }


def run_variants(as_of: str, configs: list[dict], *, start: str = "2016-01-01",
                 panels_all: dict | None = None, macro_panel=None) -> dict[str, dict]:
    """同一天、多套口径各出一份名单（口径对比用；面板/行情只加载一次，避免 N 倍重复 IO）。

    `configs` 每项：`{tag, board_kind, k, top_clusters, stock_mode, holdings?, candidates?, ...}`
    —— 除 `tag` 外的键与 `run()` 同名，缺省取 `run()` 的默认值。
    """
    snapshot = pd.Timestamp(as_of).normalize()
    lookback = max(120, int(mbs.window_days_for(snapshot)) + 35)
    bars = mbs.enriched_daily(lookback)
    dates = sorted(bars["trade_date"].unique())
    if snapshot not in dates:
        raise SystemExit(f"行情窗口（{lookback} 天）覆盖不到 {snapshot.date()}")
    panels = panels_all if panels_all is not None else \
        daily.board_daily_panels(start, snapshot.strftime("%Y-%m-%d"))
    panels = {key: (value.loc[:snapshot] if hasattr(value, "loc") else value)
              for key, value in panels.items()}
    flow = arm._moneyflow_window(snapshot)
    membership = rot._membership("tushare_industry")
    basics = mbs.basic_frame()
    stocks_cache: dict[str, pd.DataFrame] = {}
    out: dict[str, dict] = {}
    for config in configs:
        tag = str(config["tag"])
        board_kind = str(config.get("board_kind", "repo7_linear"))
        if board_kind not in BOARD_KINDS:
            raise SystemExit(f"未知 board-kind={board_kind}")
        kind, use_macro, kind_label = BOARD_KINDS[board_kind]
        stock_mode = str(config.get("stock_mode", "reversal"))
        choice = board_choice(panels, kind=kind, use_macro=use_macro,
                              k=int(config.get("k", 5)),
                              top_clusters=int(config.get("top_clusters", 1)),
                              repeats=int(config.get("repeats", 20)),
                              min_cluster_size=int(config.get("min_cluster_size", 8)),
                              as_of=snapshot, macro_panel=macro_panel)
        if stock_mode not in stocks_cache:
            stocks_cache[stock_mode] = arm.stock_scores(bars, flow, snapshot, mode=stock_mode)
        table = _assemble(snapshot, stocks_cache[stock_mode], membership, choice, basics,
                          stock_mode=stock_mode, board_kind=board_kind,
                          candidates=int(config.get("candidates", 10)),
                          holdings=int(config.get("holdings", 10)),
                          per_cluster=int(config.get("per_cluster", 10)),
                          max_per_cluster_hold=int(config.get("max_per_cluster_hold", 10)),
                          max_per_industry=int(config.get("max_per_industry", 2)),
                          capital=float(config.get("capital", 200_000.0)))
        out[tag] = {"table": table, "choice": choice, "kind_label": kind_label,
                    "board_kind": board_kind, "stock_mode": stock_mode,
                    "k": int(config.get("k", 5)),
                    "top_clusters": int(config.get("top_clusters", 1))}
    return out


def backfill(dates: list[str], **kwargs) -> list[str]:
    """按历史决策日**逐日**出名单并写进 rot_arm_history（供影子组合/闸门立刻拿到多期样本）。

    PIT 安全：每期只用"该决策日及之前"的行情/资金流/宏观；正是为了不让人工补历史
    时把未来数据混进去，这里一次只跑一个 as_of，而不是回填一个区间后统一打分。
    """
    written = []
    for date in dates:
        try:
            result = run(date, **kwargs)
        except SystemExit as exc:                                   # 数据不足的早期日期跳过
            print(f"[rot-arm] {date} 跳过：{exc}", flush=True)
            continue
        HISTORY.mkdir(parents=True, exist_ok=True)
        result["table"].to_csv(HISTORY / f"{result['snapshot_date']}.csv", index=False)
        written.append(result["snapshot_date"])
        held = result["table"][result["table"]["in_portfolio"]]
        print(f"[rot-arm] {date} → {len(result['table'])} 候选 / {len(held)} 持仓 "
              f"{', '.join(held['stock_code'].astype(str))}", flush=True)
    return written


def write_outputs(result: dict, args) -> None:
    table = result["table"]
    OUT.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT / "rot_arm_selection.csv", index=False)
    table.to_csv(HISTORY / f"{result['snapshot_date']}.csv", index=False)
    (OUT / "rot_arm_selection.json").write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_date": result["snapshot_date"],
        "arm": "rotation_arm",
        "board_kind": result["board_kind"], "board_kind_label": result["kind_label"],
        "cluster_k": result["k"], "top_clusters": result["top_clusters"],
        "label_month": result["label_month"], "n_industries": result["n_industries"],
        "n_indicators_macro": result["n_indicators_macro"],
        "stock_mode": result["stock_mode"],
        "source": (f"板块层：MST 滚动聚类 k={result['k']}（每 6 个月刷新，窗口 60 个月）"
                   f" → {result['kind_label']} → top-{result['top_clusters']} 大类；"
                   f"个股层：{arm.STOCK_MODE_LABELS.get(result['stock_mode'], result['stock_mode'])}"
                   f"｜候选 每大类≤{result['per_cluster']} / 每行业≤{result['max_per_industry']}"),
        "clusters": result["cluster_industries"],
        "cluster_scores": result["scores"], "chosen_clusters": result["chosen_clusters"],
        "chosen_industries": result["chosen_industries"],
        "evidence": ("复现报告 output/research/industry_rotation/xy_reproduce.md（月频）与 xy_daily.md（日频）："
                     "7 因子简单等权 k=5 top-1 毛年化 12.38%（现行 2 因子板块层 6.89%），"
                     "相对大类等权 t=1.51（p=0.135）——**未过上线闸门**，故为候选策略，"
                     "不参与生产订单与权重。"),
        "note": "研究手臂（rotation_arm），**不替代** preselection→pk 的生产名单；"
                "板块层证据来自本地复现，个股层复用 arm_concept 的滞涨腿。",
        "candidates": args.candidates, "holdings": args.holdings, "capital": args.capital,
        "order_precheck": "已排除当日涨停（不追高）、20 日均成交额 < 5000 万、ST",
        "diversify": (f"候选：每大类 ≤{args.per_cluster}、每行业 ≤{args.max_per_industry}；"
                      f"持仓：每大类 ≤{args.max_per_cluster_hold}"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="行业轮动手臂（复现里最优的板块层口径 → 每日名单）")
    ap.add_argument("--as-of", default="")
    ap.add_argument("--k", type=int, default=5, help="MST 大类个数")
    ap.add_argument("--top-clusters", type=int, default=1)
    ap.add_argument("--board-kind", default="repo7_linear", choices=sorted(BOARD_KINDS))
    ap.add_argument("--stock-mode", default="reversal", choices=sorted(arm.STOCK_MODES))
    ap.add_argument("--candidates", type=int, default=10)
    # 默认持仓 = 10（= 候选数，全部等权）。依据上线闸门 2026-10-10 的判决：
    #   rot_arm（持仓 5 只）vs pool_top5_rank → **REJECT**（同窗口 9/20 期更好、中位差 -0.57%）
    #   rot_arm_all10（候选 10 只全买）vs rot_arm → **KEEP**（14/22 期更好、中位差 +1.08%/期、p=0.286）
    # 两个口径都留在影子组合里继续累积样本，谁先过闸门谁当默认。
    ap.add_argument("--holdings", type=int, default=10)
    ap.add_argument("--capital", type=float, default=200_000.0)
    # 与 arm_concept 的"每板块≤4/持仓≤2"不同：这里的"板块"是含几十个行业的**大类**，
    # 只取 top-1 大类时要允许在大类内选满名单（靠"每行业≤2"保证分散）。
    ap.add_argument("--per-cluster", type=int, default=10)
    ap.add_argument("--max-per-cluster-hold", type=int, default=10)
    ap.add_argument("--max-per-industry", type=int, default=2)
    ap.add_argument("--repeats", type=int, default=20)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--backfill-dates", default="",
                    help="逗号分隔的历史决策日（如 2025-09-08,2025-10-14）：逐日出名单写进 rot_arm_history，"
                         "写入后影子组合/上线闸门立刻有多期样本")
    ap.add_argument("--write-latest", action="store_true",
                    help="配合 --backfill-dates：把最后一期也写成 rot_arm_selection.csv（默认不动最新名单）")
    ap.add_argument("--min-cluster-size", type=int, default=8,
                    help="每个大类至少包含多少个行业（避免剪出单行业大类、个股池只剩 2 只）")
    args = ap.parse_args()

    if args.backfill_dates:
        dates = [d.strip() for d in args.backfill_dates.split(",") if d.strip()]
        written = backfill(dates, k=args.k, top_clusters=args.top_clusters,
                           candidates=args.candidates, holdings=args.holdings,
                           capital=args.capital, per_cluster=args.per_cluster,
                           max_per_cluster_hold=args.max_per_cluster_hold,
                           max_per_industry=args.max_per_industry, stock_mode=args.stock_mode,
                           board_kind=args.board_kind, repeats=args.repeats, start=args.start,
                           min_cluster_size=args.min_cluster_size)
        print(f"[rot-arm] 回填完成 {len(written)} 个决策日 → {HISTORY}", flush=True)
        if args.write_latest and written:
            result = run(written[-1], k=args.k, top_clusters=args.top_clusters,
                         candidates=args.candidates, holdings=args.holdings, capital=args.capital,
                         per_cluster=args.per_cluster,
                         max_per_cluster_hold=args.max_per_cluster_hold,
                         max_per_industry=args.max_per_industry, stock_mode=args.stock_mode,
                         board_kind=args.board_kind, repeats=args.repeats, start=args.start,
                         min_cluster_size=args.min_cluster_size)
            write_outputs(result, args)
        return 0

    result = run(args.as_of, k=args.k, top_clusters=args.top_clusters, candidates=args.candidates,
                 holdings=args.holdings, capital=args.capital, per_cluster=args.per_cluster,
                 max_per_cluster_hold=args.max_per_cluster_hold,
                 max_per_industry=args.max_per_industry, stock_mode=args.stock_mode,
                 board_kind=args.board_kind, repeats=args.repeats, start=args.start,
                 min_cluster_size=args.min_cluster_size)
    write_outputs(result, args)
    table = result["table"]
    held = table[table["in_portfolio"]]
    print(f"[rot-arm] {result['snapshot_date']} 大类标签={result['label_month']} "
          f"口径={result['board_kind']} k={result['k']} top{result['top_clusters']}", flush=True)
    print(f"[rot-arm] 选定大类={result['chosen_clusters']}；成员行业 "
          f"{len(result['chosen_industries'])} 个：{result['chosen_industries'][:8]}…", flush=True)
    print(f"[rot-arm] 候选 {len(table)} 只 / 持仓 {len(held)} 只："
          f"{', '.join(f'{r.stock_code}({r.industry})' for r in held.itertuples())}", flush=True)
    print(f"[rot-arm] 写出 rot_arm_selection.csv/json + rot_arm_history/{result['snapshot_date']}.csv",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
